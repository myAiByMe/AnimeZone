"""
routes.py - Frontend (templates + logique utilisateur)
À importer dans app.py
"""

import re
import logging
import m3u8
import requests
from urllib.parse import urljoin
from flask import render_template, request, redirect, url_for, flash, jsonify, Response
from flask_login import login_user, login_required, logout_user, current_user

from app import (
    db, User, UserProgress, UserFavorite,
    load_anime_data, get_anime_by_id, load_discover_data,
    get_all_genres, get_user_progress_optimized,
    get_user_favorites_optimized, video_session
)

logger = logging.getLogger(__name__)

# ==================
# SYSTÈME VIDÉO HYBRIDE
# ==================

def parse_video_url(url):
    """Parse URL vidéo - Support multi-domaines"""
    if not url:
        return None, None
    
    url_clean = url.strip().lower()
    
    # SENDVID (sendvid.com, sendvid.co, sendvid.net, etc.)
    if 'sendvid' in url_clean:
        # Patterns: sendvid.xxx/embed/ID ou sendvid.xxx/ID
        match = re.search(r'sendvid\.[a-z]+/embed/([a-zA-Z0-9]+)', url, re.IGNORECASE)
        if match:
            return ('sendvid', match.group(1))
        
        match = re.search(r'sendvid\.[a-z]+/([a-zA-Z0-9]+)', url, re.IGNORECASE)
        if match:
            return ('sendvid', match.group(1))
    
    # VIDMOLY (vidmoly.com, vidmoly.ru, vidmoly.me, etc.)
    if 'vidmoly' in url_clean:
        # Pattern: vidmoly.xxx/embed-ID.html ou vidmoly.xxx/ID
        match = re.search(r'vidmoly\.[a-z]+/embed-([a-zA-Z0-9]+)\.html', url, re.IGNORECASE)
        if match:
            return ('vidmoly', match.group(1))
        
        match = re.search(r'vidmoly\.[a-z]+/([a-zA-Z0-9]+)', url, re.IGNORECASE)
        if match:
            return ('vidmoly', match.group(1))
    
    # SIBNET (sibnet.ru, video.sibnet.ru, etc.)
    if 'sibnet' in url_clean:
        # Pattern: sibnet.xxx/video/ID ou video.sibnet.xxx/shell.php?videoid=ID
        match = re.search(r'sibnet\.[a-z]+/video/(\d+)', url, re.IGNORECASE)
        if match:
            return ('sibnet', match.group(1))
        
        match = re.search(r'videoid=(\d+)', url, re.IGNORECASE)
        if match:
            return ('sibnet', match.group(1))
    
    # GENERIC (autres lecteurs)
    return ('generic', url)


# ==================
# EXTRACTEURS SPÉCIFIQUES
# ==================

def extract_vidmoly_m3u8(embed_url):
    """Extrait M3U8 Vidmoly"""
    try:
        response = video_session.get(embed_url, timeout=10)
        html = response.text
        
        pattern = r'sources\s*:\s*\[\s*{\s*file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']'
        match = re.search(pattern, html, re.IGNORECASE)
        
        if match:
            return match.group(1)
        
        pattern2 = r'file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']'
        match = re.search(pattern2, html, re.IGNORECASE)
        
        return match.group(1) if match else None
    except Exception as e:
        logger.error(f"❌ Erreur Vidmoly M3U8: {e}")
        return None


def extract_sendvid_video(embed_url):
    """Extrait URL MP4 SendVid"""
    try:
        response = video_session.get(embed_url, timeout=10)
        html = response.text
        
        # Pattern 1: <source>
        pattern1 = r'<source[^>]*src=["\']([^"\']+\.mp4[^"\']*)["\']'
        match = re.search(pattern1, html, re.IGNORECASE)
        if match:
            url = match.group(1)
            return url if url.startswith('http') else urljoin('https://sendvid.com', url)
        
        # Pattern 2: file variable
        pattern2 = r'file\s*:\s*["\']([^"\']+\.(mp4|webm)[^"\']*)["\']'
        match = re.search(pattern2, html, re.IGNORECASE)
        if match:
            url = match.group(1)
            return url if url.startswith('http') else urljoin('https://sendvid.com', url)
        
        return None
    except Exception as e:
        logger.error(f"❌ Erreur SendVid: {e}")
        return None


def extract_sibnet_video(video_id):
    """Extrait vidéo Sibnet"""
    try:
        embed_url = f"https://video.sibnet.ru/shell.php?videoid={video_id}"
        response = video_session.get(embed_url, timeout=10)
        html = response.text
        
        # Chercher M3U8
        m3u8_match = re.search(r'["\']([^"\']+\.m3u8[^"\']*)["\']', html, re.IGNORECASE)
        if m3u8_match:
            return ('m3u8', m3u8_match.group(1))
        
        # Chercher MP4
        mp4_match = re.search(r'["\']([^"\']+\.mp4[^"\']*)["\']', html, re.IGNORECASE)
        if mp4_match:
            return ('mp4', mp4_match.group(1))
        
        return None, None
    except Exception as e:
        logger.error(f"❌ Erreur Sibnet: {e}")
        return None, None


def get_hls_segments(master_url):
    """Récupère segments HLS"""
    try:
        response = video_session.get(master_url, timeout=10)
        master = m3u8.loads(response.text)
        
        if master.segments:
            return master_url, master
        
        if master.playlists:
            base_url = master_url.rsplit('/', 1)[0] + '/'
            playlist_url = urljoin(base_url, master.playlists[-1].uri)
            response = video_session.get(playlist_url, timeout=10)
            playlist = m3u8.loads(response.text)
            return playlist_url, playlist
        
        return None, None
    except Exception as e:
        logger.error(f"❌ Erreur HLS: {e}")
        return None, None


# ==================
# EXTRACTEUR GÉNÉRIQUE (Fallback)
# ==================

def try_extract_all_methods(embed_url):
    """Teste TOUTES les méthodes pour extraire une vidéo"""
    try:
        response = video_session.get(embed_url, timeout=10)
        html = response.text
        base_url = embed_url.rsplit('/', 1)[0] + '/'
        
        # 1️⃣ Chercher M3U8
        m3u8_patterns = [
            r'sources\s*:\s*\[\s*{\s*file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
            r'file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
            r'<source[^>]*src=["\']([^"\']+\.m3u8[^"\']*)["\']',
            r'src=["\']([^"\']+\.m3u8[^"\']*)["\']',
            r'url\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
            r'hls\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        ]
        
        for pattern in m3u8_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                m3u8_url = match.group(1)
                if not m3u8_url.startswith('http'):
                    m3u8_url = urljoin(base_url, m3u8_url)
                logger.info(f"✅ M3U8 trouvé (générique): {m3u8_url[:60]}...")
                return ('hls', m3u8_url)
        
        # 2️⃣ Chercher MP4
        mp4_patterns = [
            r'<source[^>]*src=["\']([^"\']+\.mp4[^"\']*)["\']',
            r'src=["\']([^"\']+\.mp4[^"\']*)["\']',
            r'file\s*:\s*["\']([^"\']+\.mp4[^"\']*)["\']',
            r'url\s*:\s*["\']([^"\']+\.mp4[^"\']*)["\']',
            r'video["\']?\s*:\s*["\']([^"\']+\.mp4[^"\']*)["\']',
            r'src\s*=\s*["\']([^"\']+\.mp4[^"\']*)["\']',
        ]
        
        for pattern in mp4_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                mp4_url = match.group(1)
                if not mp4_url.startswith('http'):
                    mp4_url = urljoin(base_url, mp4_url)
                logger.info(f"✅ MP4 trouvé (générique): {mp4_url[:60]}...")
                return ('mp4', mp4_url)
        
        # 3️⃣ Chercher WEBM
        webm_patterns = [
            r'<source[^>]*src=["\']([^"\']+\.webm[^"\']*)["\']',
            r'src=["\']([^"\']+\.webm[^"\']*)["\']',
            r'file\s*:\s*["\']([^"\']+\.webm[^"\']*)["\']',
        ]
        
        for pattern in webm_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                webm_url = match.group(1)
                if not webm_url.startswith('http'):
                    webm_url = urljoin(base_url, webm_url)
                logger.info(f"✅ WEBM trouvé (générique): {webm_url[:60]}...")
                return ('webm', webm_url)
        
        logger.warning(f"⚠️ Aucune vidéo trouvée dans: {embed_url}")
        return None, None
        
    except Exception as e:
        logger.error(f"❌ Erreur extraction générique: {e}")
        return None, None


# ==================
# ROUTES FRONTEND
# ==================

def register_frontend_routes(app):
    """Enregistre toutes les routes frontend"""
    
    @app.route('/')
    def index():
        """Page d'accueil OPTIMISÉE"""
        if not current_user.is_authenticated:
            return redirect(url_for('login'))
        
        anime_data = load_anime_data()
        
        continue_watching = []
        latest_progress = get_user_progress_optimized(current_user.id, limit=20)
        
        processed = set()
        for progress in latest_progress:
            if progress.anime_id not in processed:
                anime = get_anime_by_id(progress.anime_id)
                if anime:
                    season = next((s for s in anime.get('seasons', []) 
                                 if s.get('season_number') == progress.season_number), None)
                    if season:
                        episode = next((e for e in season.get('episodes', []) 
                                      if e.get('episode_number') == progress.episode_number), None)
                        if episode:
                            continue_watching.append({
                                'anime': anime,
                                'progress': progress,
                                'season': season,
                                'episode': episode
                            })
                            processed.add(progress.anime_id)
        
        favorite_anime = []
        favorites = get_user_favorites_optimized(current_user.id, limit=15)
        for fav in favorites:
            anime = get_anime_by_id(fav.anime_id)
            if anime:
                favorite_anime.append(anime)
        
        featured = load_discover_data()
        featured = [a for a in featured if a.get('has_episodes', False)][:12]
        
        return render_template('index_new.html',
                              anime_list=featured,
                              continue_watching=continue_watching,
                              favorite_anime=favorite_anime)
    
    
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        """Login"""
        if current_user.is_authenticated:
            return redirect(url_for('index'))
        
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')
            
            user = User.query.filter_by(username=username).first()
            
            if user and user.check_password(password):
                import datetime
                user.last_login = datetime.datetime.utcnow()
                db.session.commit()
                login_user(user)
                
                next_page = request.args.get('next')
                return redirect(next_page if next_page else url_for('index'))
            
            flash('Nom d\'utilisateur ou mot de passe incorrect', 'danger')
        
        return render_template('login_new.html')
    
    
    @app.route('/register', methods=['GET', 'POST'])
    def register():
        """Register"""
        if current_user.is_authenticated:
            return redirect(url_for('index'))
        
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')
            confirm = request.form.get('confirm_password')
            
            if password != confirm:
                flash('Les mots de passe ne correspondent pas', 'danger')
            elif User.query.filter_by(username=username).first():
                flash('Nom d\'utilisateur déjà pris', 'danger')
            else:
                user = User(username=username)
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                flash('Compte créé avec succès!', 'success')
                return redirect(url_for('login'))
        
        return render_template('register_new.html')
    
    
    @app.route('/logout')
    @login_required
    def logout():
        """Déconnexion"""
        logout_user()
        flash('Vous avez été déconnecté', 'info')
        return redirect(url_for('login'))
    
    
    @app.route('/search')
    @login_required
    def search():
        """Recherche OPTIMISÉE"""
        query = request.args.get('query', '').lower()
        genre = request.args.get('genre', '').lower()
        
        anime_data = load_anime_data()
        
        filtered = []
        for anime in anime_data:
            title_match = query in anime.get('title', '').lower() if query else True
            genre_match = not genre or genre in [g.lower() for g in anime.get('genres', [])]
            has_episodes = anime.get('has_episodes', False)
            
            if title_match and genre_match and has_episodes:
                filtered.append(anime)
        
        filtered = filtered[:100]
        recent = [a for a in anime_data if a.get('has_episodes', False)][-20:]
        
        return render_template('search.html',
                              anime_list=filtered,
                              query=query,
                              selected_genre=genre,
                              genres=get_all_genres(),
                              other_anime_list=recent if not filtered else [])
    
    
    @app.route('/anime/<int:anime_id>')
    @login_required
    def anime_detail(anime_id):
        """Détails anime"""
        anime = get_anime_by_id(anime_id)
        
        if not anime:
            return render_template('404.html', message="Anime non trouvé"), 404
        
        if anime.get('seasons'):
            regular, kai, films = [], [], []
            for season in anime['seasons']:
                name = season.get('name', '')
                if season.get('season_number') == 99:
                    films.append(season)
                elif 'Kai' in name:
                    kai.append(season)
                else:
                    regular.append(season)
            
            regular.sort(key=lambda s: s.get('season_number', 0))
            kai.sort(key=lambda s: s.get('season_number', 0))
            anime['seasons'] = regular + films + kai
        
        is_favorite = UserFavorite.query.filter_by(
            user_id=current_user.id,
            anime_id=anime_id
        ).first() is not None
        
        episode_progress = {}
        for progress in UserProgress.query.filter_by(user_id=current_user.id, anime_id=anime_id).all():
            key = f"{progress.season_number}_{progress.episode_number}"
            episode_progress[key] = {
                'time_position': progress.time_position,
                'completed': progress.completed,
                'last_watched': progress.last_watched
            }
        
        latest_progress = UserProgress.query.filter_by(
            user_id=current_user.id,
            anime_id=anime_id,
            completed=False
        ).order_by(UserProgress.last_watched.desc()).first()
        
        return render_template('anime_new.html',
                              anime=anime,
                              is_favorite=is_favorite,
                              episode_progress=episode_progress,
                              latest_progress=latest_progress)
    
    
    @app.route('/player/<int:anime_id>/<int:season_num>/<int:episode_num>')
    @login_required
    def player(anime_id, season_num, episode_num):
        """Lecteur"""
        anime = get_anime_by_id(anime_id)
        
        if not anime:
            return render_template('404.html', message="Anime non trouvé"), 404
        
        season = next((s for s in anime.get('seasons', []) 
                      if s.get('season_number') == season_num), None)
        if not season:
            return render_template('404.html', message="Saison non trouvée"), 404
        
        episode = next((e for e in season.get('episodes', []) 
                       if e.get('episode_number') == episode_num), None)
        if not episode:
            return render_template('404.html', message="Épisode non trouvé"), 404
        
        def select_best_url(urls_dict):
            if not urls_dict:
                return None, None
            
            def prioritize(url_list):
                if not url_list:
                    return None
                if isinstance(url_list, str):
                    url_list = [url_list]
                
                vidmoly = [u for u in url_list if 'vidmoly' in u.lower()]
                sendvid = [u for u in url_list if 'sendvid' in u.lower()]
                sibnet = [u for u in url_list if 'sibnet' in u.lower()]
                
                return (vidmoly or sendvid or sibnet or url_list)[0] if (vidmoly or sendvid or sibnet or url_list) else None
            
            for lang in ['VF', 'VOSTFR']:
                if lang in urls_dict:
                    url = prioritize(urls_dict[lang])
                    if url:
                        return url, lang
            
            for lang, urls in urls_dict.items():
                url = prioritize(urls)
                if url:
                    return url, lang
            
            return None, None
        
        video_url, episode_lang = select_best_url(episode.get('urls', {}))
        
        if not video_url:
            return render_template('404.html', message="Source vidéo non disponible"), 404
        
        download_url = video_url
        if "sendvid.com" in video_url and "/embed/" not in video_url:
            video_id = video_url.split("/")[-1].split(".")[0]
            download_url = f"https://sendvid.com/embed/{video_id}"
        
        time_position = 0
        progress = UserProgress.query.filter_by(
            user_id=current_user.id,
            anime_id=anime_id,
            season_number=season_num,
            episode_number=episode_num
        ).first()
        
        if progress:
            time_position = progress.time_position
        
        is_favorite = UserFavorite.query.filter_by(
            user_id=current_user.id,
            anime_id=anime_id
        ).first() is not None
        
        return render_template('player.html',
                              anime=anime,
                              season=season,
                              episode=episode,
                              download_url=download_url,
                              time_position=time_position,
                              is_favorite=is_favorite,
                              episode_lang=episode_lang)
    
    
    @app.route('/profile')
    @login_required
    def profile():
        """Profil"""
        anime_data = load_anime_data()
        
        watching_anime = []
        for progress in get_user_progress_optimized(current_user.id, limit=50):
            anime = get_anime_by_id(progress.anime_id)
            if anime:
                season = next((s for s in anime.get('seasons', []) 
                             if s.get('season_number') == progress.season_number), None)
                episode = next((e for e in season.get('episodes', []) 
                              if e.get('episode_number') == progress.episode_number), None) if season else None
                
                watching_anime.append({
                    'progress': progress,
                    'anime': anime,
                    'season': season,
                    'episode': episode
                })
        
        favorite_anime = []
        for fav in get_user_favorites_optimized(current_user.id, limit=50):
            anime = get_anime_by_id(fav.anime_id)
            if anime:
                favorite_anime.append(anime)
        
        return render_template('profile_new.html',
                              watching_anime=watching_anime,
                              favorite_anime=favorite_anime)
    
    @app.route('/settings', methods=['GET', 'POST'])
    @login_required
    def settings():
        """Page de paramètres"""
        if request.method == 'POST':
            current_password = request.form.get('current_password')
            new_username = request.form.get('new_username')
            new_password = request.form.get('new_password')
            confirm = request.form.get('confirm_password')
            
            if not current_user.check_password(current_password):
                flash('Mot de passe actuel incorrect', 'danger')
                return redirect(url_for('settings'))
            
            if new_username and new_username != current_user.username:
                if User.query.filter_by(username=new_username).first():
                    flash('Nom d\'utilisateur déjà pris', 'danger')
                    return redirect(url_for('settings'))
                current_user.username = new_username
            
            if new_password:
                if new_password != confirm:
                    flash('Les nouveaux mots de passe ne correspondent pas', 'danger')
                    return redirect(url_for('settings'))
                current_user.set_password(new_password)
            
            db.session.commit()
            flash('Paramètres mis à jour', 'success')
            return redirect(url_for('settings'))
        
        return render_template('settings.html')

    @app.route('/categories')
    @login_required
    def categories():
        """Catégories"""
        anime_data = load_anime_data()
        genres = get_all_genres()
        
        genres_dict = {genre: [] for genre in genres}
        for anime in anime_data:
            for genre in anime.get('genres', []):
                if genre.lower() in genres_dict:
                    genres_dict[genre.lower()].append(anime)
        
        return render_template('categories.html',
                              all_anime=anime_data,
                              genres=genres,
                              genres_dict=genres_dict)
    
    
    # ==================
    # API VIDÉO HYBRIDE
    # ==================
    
    @app.route('/api/video/info', methods=['POST'])
    @login_required
    def video_info():
        """Info vidéo - Gère Vidmoly/SendVid/Sibnet + Fallback générique"""
        try:
            data = request.get_json()
            url = data.get('url', '').strip()
            
            if not url:
                return jsonify({'success': False, 'error': 'URL manquante'}), 400
            
            player_type, video_id = parse_video_url(url)
            video_key = f"{player_type}_{video_id}"
            
            logger.info(f"🎬 Traitement vidéo: {player_type} - {video_id}")
            
            # ========== SENDVID ==========
            if player_type == 'sendvid':
                embed_url = f"https://sendvid.com/embed/{video_id}"
                logger.info(f"📍 SendVid embed_url: {embed_url}")
                
                video_url = extract_sendvid_video(embed_url)
                logger.info(f"📍 SendVid video_url: {video_url}")
                
                if not video_url:
                    logger.warning(f"⚠️ SendVid MP4 non trouvé, fallback générique")
                    video_type, video_url = try_extract_all_methods(embed_url)
                    if not video_url:
                        return jsonify({'success': False, 'error': 'Vidéo non trouvée'}), 404
                    logger.info(f"✅ Fallback trouvé: {video_type} - {video_url[:60]}...")
                else:
                    video_type = 'mp4'
                
                # SENDVID ORIGINAL - garder le même format que ancien script
                app.config[f'video_{video_key}'] = {
                    'player_type': 'sendvid',
                    'url': video_url,
                    'video_type': video_type
                }
                
                return jsonify({
                    'success': True,
                    'player_type': 'sendvid',
                    'video_key': video_key,
                    'direct_mp4': True
                })
            
            # ========== VIDMOLY ==========
            elif player_type == 'vidmoly':
                embed_url = f"https://vidmoly.net/embed-{video_id}.html"
                logger.info(f"📍 Vidmoly embed_url: {embed_url}")
                
                m3u8_url = extract_vidmoly_m3u8(embed_url)
                logger.info(f"📍 Vidmoly m3u8_url: {m3u8_url}")
                
                if not m3u8_url:
                    logger.warning(f"⚠️ Vidmoly M3U8 non trouvé, fallback générique")
                    video_type, video_url = try_extract_all_methods(embed_url)
                    if not video_url:
                        return jsonify({'success': False, 'error': 'Vidéo non trouvée'}), 404
                    logger.info(f"✅ Fallback trouvé: {video_type} - {video_url[:60]}...")
                else:
                    video_type = 'hls'
                    video_url = m3u8_url
                
                # Si HLS, récupérer les segments
                if video_type == 'hls':
                    playlist_url, playlist = get_hls_segments(video_url)
                    if not playlist or not playlist.segments:
                        return jsonify({'success': False, 'error': 'Segments HLS non trouvés'}), 500
                    
                    app.config[f'video_{video_key}'] = {
                        'player_type': 'hls',
                        'url': playlist_url,
                        'playlist': playlist
                    }
                    
                    logger.info(f"✅ Vidmoly HLS trouvé: {len(playlist.segments)} segments")
                    return jsonify({
                        'success': True,
                        'player_type': 'hls',
                        'video_key': video_key,
                        'segments': len(playlist.segments)
                    })
                
                # Si MP4, streaming direct
                else:
                    app.config[f'video_{video_key}'] = {
                        'player_type': 'mp4',
                        'url': video_url
                    }
                    logger.info(f"✅ Vidmoly MP4 fallback")
                    return jsonify({
                        'success': True,
                        'player_type': 'mp4',
                        'video_key': video_key,
                        'direct_mp4': True
                    })
            
            # ========== SIBNET ==========
            elif player_type == 'sibnet':
                video_type, video_url = extract_sibnet_video(video_id)
                
                if not video_url:
                    logger.warning(f"⚠️ Sibnet non trouvé, fallback générique")
                    embed_url = f"https://video.sibnet.ru/shell.php?videoid={video_id}"
                    video_type, video_url = try_extract_all_methods(embed_url)
                    if not video_url:
                        return jsonify({'success': False, 'error': 'Vidéo non trouvée'}), 404
                    logger.info(f"✅ Fallback trouvé: {video_type} - {video_url[:60]}...")
                
                # Si HLS
                if video_type == 'hls' or 'm3u8' in str(video_type).lower():
                    playlist_url, playlist = get_hls_segments(video_url)
                    if not playlist or not playlist.segments:
                        return jsonify({'success': False, 'error': 'Segments HLS non trouvés'}), 500
                    
                    app.config[f'video_{video_key}'] = {
                        'player_type': 'hls',
                        'url': playlist_url,
                        'playlist': playlist
                    }
                    
                    logger.info(f"✅ Sibnet HLS trouvé: {len(playlist.segments)} segments")
                    return jsonify({
                        'success': True,
                        'player_type': 'hls',
                        'video_key': video_key,
                        'segments': len(playlist.segments)
                    })
                
                # Si MP4
                else:
                    app.config[f'video_{video_key}'] = {
                        'player_type': 'mp4',
                        'url': video_url
                    }
                    logger.info(f"✅ Sibnet MP4")
                    return jsonify({
                        'success': True,
                        'player_type': 'mp4',
                        'video_key': video_key,
                        'direct_mp4': True
                    })
            
            # ========== LECTEUR GÉNÉRIQUE ==========
            else:
                logger.info(f"📍 Lecteur générique: {url}")
                video_type, video_url = try_extract_all_methods(url)
                
                if not video_url:
                    return jsonify({'success': False, 'error': 'Source non trouvée', 'use_iframe': True}), 404
                
                logger.info(f"✅ Générique trouvé: {video_type} - {video_url[:60]}...")
                
                # Si HLS
                if video_type == 'hls' or 'm3u8' in str(video_type).lower():
                    playlist_url, playlist = get_hls_segments(video_url)
                    if not playlist or not playlist.segments:
                        return jsonify({'success': False, 'error': 'Segments HLS non trouvés'}), 500
                    
                    app.config[f'video_{video_key}'] = {
                        'player_type': 'hls',
                        'url': playlist_url,
                        'playlist': playlist
                    }
                    
                    return jsonify({
                        'success': True,
                        'player_type': 'hls',
                        'video_key': video_key,
                        'segments': len(playlist.segments)
                    })
                
                # Si MP4 ou WEBM
                else:
                    app.config[f'video_{video_key}'] = {
                        'player_type': 'mp4',
                        'url': video_url,
                        'mime_type': 'video/mp4' if video_type == 'mp4' else 'video/webm'
                    }
                    return jsonify({
                        'success': True,
                        'player_type': 'mp4',
                        'video_key': video_key,
                        'direct_mp4': True
                    })
        
        except Exception as e:
            logger.error(f"❌ Erreur API info: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500
    
    
    @app.route('/api/video/stream/<video_key>')
    @login_required
    def video_stream(video_key):
        """Stream vidéo"""
        video_data = app.config.get(f'video_{video_key}')
        if not video_data:
            logger.error(f"❌ Video key non trouvé: {video_key}")
            return "Non trouvé", 404
        
        player_type = video_data['player_type']
        logger.info(f"🎬 Stream type: {player_type}")
        
        # ========== HLS ==========
        if player_type == 'hls':
            playlist = video_data['playlist']
            base_url = video_data['url'].rsplit('/', 1)[0] + '/'
            
            manifest = "#EXTM3U\n#EXT-X-VERSION:3\n"
            manifest += f"#EXT-X-TARGETDURATION:{int(max(s.duration for s in playlist.segments if s.duration) + 1)}\n"
            manifest += "#EXT-X-MEDIA-SEQUENCE:0\n\n"
            
            for i, seg in enumerate(playlist.segments):
                seg_url = seg.uri if seg.uri.startswith('http') else urljoin(base_url, seg.uri)
                app.config[f'segment_{video_key}_{i}'] = seg_url
                manifest += f"#EXTINF:{seg.duration},\n/api/video/segment/{video_key}/{i}\n"
            
            manifest += "#EXT-X-ENDLIST\n"
            
            logger.info(f"✅ HLS manifest retourné ({len(playlist.segments)} segments)")
            return Response(manifest, mimetype='application/vnd.apple.mpegurl')
        
        # ========== SENDVID (Format ancien script) ==========
        elif player_type == 'sendvid':
            video_url = video_data['url']
            logger.info(f"📍 SendVid stream: {video_url[:60]}...")
            
            try:
                response = video_session.get(video_url, stream=True, timeout=30, allow_redirects=True)
                logger.info(f"✅ SendVid response status: {response.status_code}")
                
                def generate():
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            yield chunk
                
                return Response(
                    generate(),
                    mimetype='video/mp4',
                    headers={'Accept-Ranges': 'bytes'}
                )
            except Exception as e:
                logger.error(f"❌ Erreur SendVid stream: {e}")
                return "Erreur streaming", 500
        
        # ========== MP4 / WEBM (Direct) ==========
        elif player_type == 'mp4':
            video_url = video_data['url']
            mime_type = video_data.get('mime_type', 'video/mp4')
            logger.info(f"📍 MP4 stream: {video_url[:60]}...")
            
            try:
                response = video_session.get(video_url, stream=True, timeout=30, allow_redirects=True)
                logger.info(f"✅ MP4 response status: {response.status_code}")
                
                def generate():
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            yield chunk
                
                return Response(
                    generate(),
                    mimetype=mime_type,
                    headers={'Accept-Ranges': 'bytes'}
                )
            except Exception as e:
                logger.error(f"❌ Erreur MP4 stream: {e}")
                return "Erreur streaming", 500
        
        logger.error(f"❌ Type non supporté: {player_type}")
        return "Type non supporté", 400
    
    
    @app.route('/api/video/segment/<video_key>/<int:segment_num>')
    @login_required
    def video_segment(video_key, segment_num):
        """Proxy segment HLS"""
        video_data = app.config.get(f'video_{video_key}')
        if not video_data or video_data['player_type'] != 'hls':
            return "Non trouvé", 404
        
        segment_url = app.config.get(f'segment_{video_key}_{segment_num}')
        if not segment_url:
            return "Segment non trouvé", 404
        
        try:
            response = video_session.get(segment_url, timeout=20, stream=True)
            
            def generate():
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk
            
            return Response(generate(), mimetype='video/mp2t')
        except Exception as e:
            logger.error(f"❌ Erreur segment {segment_num}: {e}")
            return f"Erreur: {str(e)}", 500
    
    
    # ==================
    # ROUTES SIMPLES
    # ==================
    
    @app.route('/save-progress', methods=['POST'])
    @login_required
    def save_progress():
        """Sauvegarde progression"""
        import datetime
        anime_id = request.form.get('anime_id', type=int)
        season_number = request.form.get('season_number', type=int)
        episode_number = request.form.get('episode_number', type=int)
        time_position = request.form.get('time_position', type=float)
        completed = request.form.get('completed') == 'true'
        
        progress = UserProgress.query.filter_by(
            user_id=current_user.id,
            anime_id=anime_id,
            season_number=season_number,
            episode_number=episode_number
        ).first()
        
        if progress:
            progress.time_position = time_position
            progress.completed = completed
            progress.last_watched = datetime.datetime.utcnow()
        else:
            progress = UserProgress(
                user_id=current_user.id,
                anime_id=anime_id,
                season_number=season_number,
                episode_number=episode_number,
                time_position=time_position,
                completed=completed
            )
            db.session.add(progress)
        
        db.session.commit()
        return jsonify({'success': True})
    
    
    @app.route('/toggle-favorite', methods=['POST'])
    @login_required
    def toggle_favorite():
        """Toggle favori"""
        anime_id = request.form.get('anime_id', type=int)
        favorite = UserFavorite.query.filter_by(user_id=current_user.id, anime_id=anime_id).first()
        
        if favorite:
            db.session.delete(favorite)
            db.session.commit()
            return jsonify({'success': True, 'action': 'removed'})
        else:
            favorite = UserFavorite(user_id=current_user.id, anime_id=anime_id)
            db.session.add(favorite)
            db.session.commit()
            return jsonify({'success': True, 'action': 'added'})
    
    
    @app.route('/remove-from-watching', methods=['POST'])
    @login_required
    def remove_from_watching():
        """Retire un anime de la liste de visionnage"""
        anime_id = request.form.get('anime_id', type=int)
        
        if not anime_id:
            return jsonify({'success': False, 'error': 'ID manquant'}), 400
        
        try:
            deleted_count = UserProgress.query.filter_by(
                user_id=current_user.id,
                anime_id=anime_id
            ).delete()
            
            db.session.commit()
            
            logger.info(f"✅ Supprimé {deleted_count} progressions pour anime {anime_id}")
            return jsonify({'success': True, 'deleted': deleted_count})
        
        except Exception as e:
            logger.error(f"❌ Erreur suppression progression: {e}")
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
    
    
    # ==================
    # ERROR HANDLERS
    # ==================
    
    @app.errorhandler(404)
    def page_not_found(e):
        return render_template('404.html'), 404
    
    @app.errorhandler(500)
    def server_error(e):
        logger.error(f"Erreur 500: {e}")
        return render_template('404.html'), 500
