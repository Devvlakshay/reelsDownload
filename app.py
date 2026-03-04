import os
import re
import time
import yt_dlp
import instaloader
from flask import Flask, render_template, request, jsonify, send_from_directory

# ─── Config ───
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")

YT_DLP_DEFAULT_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
    "socket_timeout": 30,
}

# Instagram needs cookies (login required), YouTube works better without them
# Detect browser for Instagram-only cookie usage
IG_COOKIE_OPTS = {}
if os.path.exists(COOKIES_FILE):
    IG_COOKIE_OPTS["cookiefile"] = COOKIES_FILE
    print(f"[ReelGrab] Using cookies file for Instagram: {COOKIES_FILE}")
else:
    for browser in ["chrome", "firefox", "edge", "brave", "chromium"]:
        try:
            test_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "cookiesfrombrowser": (browser,)}
            with yt_dlp.YoutubeDL(test_opts) as ydl:
                _ = ydl.cookiejar
            IG_COOKIE_OPTS["cookiesfrombrowser"] = (browser,)
            print(f"[ReelGrab] Using {browser} cookies for Instagram")
            break
        except Exception:
            continue

app = Flask(__name__)


# ─── Helpers ───
def sanitize_filename(filename):
    filename = re.sub(r'[<>:"/\\|?*#%&\x00-\x1f]', '', filename)
    filename = re.sub(r'\s+', '_', filename.strip())
    return filename[:200] if len(filename) > 200 else (filename or "untitled")


def generate_unique_filename(base_name, extension):
    sanitized = sanitize_filename(base_name)
    timestamp = int(time.time() * 1000)
    if not extension.startswith('.'):
        extension = f'.{extension}'
    return f"{sanitized}_{timestamp}{extension}"


def format_duration(seconds):
    if seconds is None or seconds < 0:
        return "0:00"
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_filesize(bytes_size):
    if bytes_size is None or bytes_size <= 0:
        return "Unknown"
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"


def format_views(count):
    if not count:
        return "0"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


def detect_platform(url):
    url_lower = url.lower()
    if any(d in url_lower for d in ["youtube.com", "youtu.be"]):
        return "youtube"
    if any(d in url_lower for d in ["instagram.com", "instagr.am"]):
        return "instagram"
    return None


# ─── YouTube Crawler ───
def yt_get_video_info(url):
    opts = {**YT_DLP_DEFAULT_OPTS, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = []
    seen = set()
    for f in info.get("formats", []):
        height = f.get("height")
        if height and height >= 360:
            label = f"{height}p"
            has_audio = f.get("acodec", "none") != "none"
            has_video = f.get("vcodec", "none") != "none"
            key = (label, has_audio, has_video)
            if key not in seen and has_video:
                seen.add(key)
                formats.append({
                    "format_id": f.get("format_id"),
                    "quality": label,
                    "resolution": f.get("resolution", f"{f.get('width', '?')}x{height}"),
                    "fps": f.get("fps"),
                    "filesize": format_filesize(f.get("filesize") or f.get("filesize_approx") or 0),
                    "has_audio": has_audio,
                    "ext": f.get("ext", "mp4"),
                })
    formats.sort(key=lambda x: int(x["quality"].replace("p", "")))

    return {
        "title": info.get("title", "Untitled"),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration", 0),
        "duration_formatted": format_duration(info.get("duration", 0)),
        "description": (info.get("description") or "")[:500],
        "view_count": info.get("view_count", 0),
        "uploader": info.get("uploader", "Unknown"),
        "upload_date": info.get("upload_date", ""),
        "webpage_url": info.get("webpage_url", url),
        "formats": formats,
    }


def yt_get_channel_info(channel_name):
    url = f"https://www.youtube.com/@{channel_name}/videos"
    if channel_name.startswith("http"):
        url = channel_name
    opts = {**YT_DLP_DEFAULT_OPTS, "extract_flat": True, "playlistend": 30}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    videos = []
    for entry in info.get("entries", []) or []:
        if entry:
            videos.append({
                "id": entry.get("id", ""),
                "title": entry.get("title", "Untitled"),
                "url": entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id', '')}",
                "thumbnail": (entry.get("thumbnails", [{}])[-1].get("url", "") if entry.get("thumbnails")
                              else f"https://i.ytimg.com/vi/{entry.get('id', '')}/hqdefault.jpg"),
                "duration": entry.get("duration", 0),
                "duration_formatted": format_duration(entry.get("duration") or 0),
                "view_count": entry.get("view_count", 0),
            })
    return {
        "channel_name": info.get("channel", info.get("uploader", channel_name)),
        "channel_url": info.get("channel_url", url),
        "subscriber_count": info.get("channel_follower_count", 0),
        "videos": videos,
    }


def yt_get_playlist_info(playlist_url):
    opts = {**YT_DLP_DEFAULT_OPTS, "extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(playlist_url, download=False)

    videos = []
    for entry in info.get("entries", []) or []:
        if entry:
            videos.append({
                "id": entry.get("id", ""),
                "title": entry.get("title", "Untitled"),
                "url": entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id', '')}",
                "thumbnail": (entry.get("thumbnails", [{}])[-1].get("url", "") if entry.get("thumbnails")
                              else f"https://i.ytimg.com/vi/{entry.get('id', '')}/hqdefault.jpg"),
                "duration": entry.get("duration", 0),
                "duration_formatted": format_duration(entry.get("duration") or 0),
            })
    return {
        "title": info.get("title", "Untitled Playlist"),
        "video_count": len(videos),
        "uploader": info.get("uploader", "Unknown"),
        "videos": videos,
    }


def yt_download_video(url, quality="720p", with_audio=True):
    height = quality.replace("p", "") if quality != "best" else ""
    if quality == "best":
        format_str = "bestvideo+bestaudio/best" if with_audio else "bestvideo/best"
    else:
        if with_audio:
            format_str = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
        else:
            format_str = f"bestvideo[height<={height}]/best[height<={height}]"

    info_opts = {**YT_DLP_DEFAULT_OPTS, "skip_download": True}
    with yt_dlp.YoutubeDL(info_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        title = info.get("title", "video")

    filename = generate_unique_filename(title, "mp4")
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    outtmpl = os.path.join(DOWNLOAD_DIR, sanitize_filename(title) + f"_{int(time.time() * 1000)}.%(ext)s")
    opts = {
        **YT_DLP_DEFAULT_OPTS,
        "format": format_str,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
    }
    if with_audio:
        opts["postprocessors"] = [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}]

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        final_ext = info.get("ext", "mp4")
        # yt-dlp sets requested_downloads with the final filepath
        downloads = info.get("requested_downloads", [])
        if downloads:
            final_path = downloads[0].get("filepath", "")
        else:
            final_path = outtmpl.replace("%(ext)s", final_ext)

    if not os.path.exists(final_path):
        # Fallback: scan download dir for matching files
        base = sanitize_filename(title)
        for f in sorted(os.listdir(DOWNLOAD_DIR), reverse=True):
            if f.startswith(base) and not f.endswith(".part"):
                final_path = os.path.join(DOWNLOAD_DIR, f)
                break

    file_size = os.path.getsize(final_path) if os.path.exists(final_path) else 0
    return {
        "filename": os.path.basename(final_path),
        "filesize": format_filesize(file_size),
        "title": title,
        "download_url": f"/api/youtube/download-file/{os.path.basename(final_path)}",
    }


# ─── Instagram Crawler ───
def ig_get_reel_info(url):
    opts = {**YT_DLP_DEFAULT_OPTS, **IG_COOKIE_OPTS, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = []
    seen = set()
    for f in info.get("formats", []):
        height = f.get("height")
        if height and height >= 360:
            label = f"{height}p"
            if label not in seen:
                seen.add(label)
                formats.append({
                    "format_id": f.get("format_id"),
                    "quality": label,
                    "resolution": f.get("resolution", f"{f.get('width', '?')}x{height}"),
                    "filesize": format_filesize(f.get("filesize") or f.get("filesize_approx") or 0),
                    "ext": f.get("ext", "mp4"),
                })
    formats.sort(key=lambda x: int(x["quality"].replace("p", "")))

    return {
        "title": info.get("title", info.get("description", "Instagram Reel")[:100]),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration", 0),
        "duration_formatted": format_duration(info.get("duration", 0)),
        "description": (info.get("description") or "")[:500],
        "uploader": info.get("uploader", info.get("uploader_id", "Unknown")),
        "webpage_url": info.get("webpage_url", url),
        "formats": formats,
    }


def ig_get_profile_content(username):
    loader = instaloader.Instaloader(
        download_pictures=False, download_videos=False,
        download_video_thumbnails=False, download_geotags=False,
        download_comments=False, save_metadata=False, compress_json=False,
    )
    try:
        profile = instaloader.Profile.from_username(loader.context, username)
    except instaloader.exceptions.ProfileNotExistsException:
        return {"error": f"Profile '{username}' not found", "posts": []}

    posts = []
    count = 0
    for post in profile.get_posts():
        if count >= 12:
            break
        post_data = {
            "shortcode": post.shortcode,
            "url": f"https://www.instagram.com/p/{post.shortcode}/",
            "thumbnail": post.url,
            "is_video": post.is_video,
            "type": "reel" if post.is_video else "post",
            "caption": (post.caption or "")[:200],
            "likes": post.likes,
            "date": post.date_utc.isoformat(),
        }
        if post.is_video:
            post_data["video_url"] = post.video_url
            post_data["video_duration"] = post.video_duration
        posts.append(post_data)
        count += 1

    return {
        "username": profile.username,
        "full_name": profile.full_name,
        "biography": profile.biography,
        "followers": profile.followers,
        "following": profile.followees,
        "post_count": profile.mediacount,
        "profile_pic": profile.profile_pic_url,
        "is_private": profile.is_private,
        "posts": posts,
    }


def ig_download_reel(url, quality="720p", with_audio=True):
    # Instagram reels have audio+video in single streams, so prefer "best" (combined)
    # Using bestvideo+bestaudio picks video-only streams and results in muted output
    if with_audio:
        format_str = "best"
    else:
        format_str = "bestvideo"

    timestamp = int(time.time() * 1000)
    outtmpl = os.path.join(DOWNLOAD_DIR, f"reel_{timestamp}.%(ext)s")
    opts = {
        **YT_DLP_DEFAULT_OPTS,
        **IG_COOKIE_OPTS,
        "format": format_str,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title", info.get("description", "reel")[:50])
        downloads = info.get("requested_downloads", [])
        if downloads:
            final_path = downloads[0].get("filepath", "")
        else:
            final_path = outtmpl.replace("%(ext)s", info.get("ext", "mp4"))

    if not os.path.exists(final_path):
        base = sanitize_filename(title)
        for f in sorted(os.listdir(DOWNLOAD_DIR), reverse=True):
            if f.startswith(base) and not f.endswith(".part"):
                final_path = os.path.join(DOWNLOAD_DIR, f)
                break

    file_size = os.path.getsize(final_path) if os.path.exists(final_path) else 0
    return {
        "filename": os.path.basename(final_path),
        "filesize": format_filesize(file_size),
        "title": title,
        "download_url": f"/api/instagram/download-file/{os.path.basename(final_path)}",
    }


# ─── Page Routes ───
@app.route("/")
def home():
    return render_template("home.html")


@app.route("/browse")
def browse():
    return render_template("browse.html")


# ─── API Routes: YouTube ───
@app.route("/api/youtube/info", methods=["POST"])
def api_youtube_info():
    try:
        data = request.get_json()
        info = yt_get_video_info(data["url"])
        return jsonify({"success": True, "data": info})
    except Exception as e:
        return jsonify({"success": False, "detail": str(e)}), 400


@app.route("/api/youtube/download", methods=["POST"])
def api_youtube_download():
    try:
        data = request.get_json()
        result = yt_download_video(data["url"], data.get("quality", "720p"), data.get("with_audio", True))
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "detail": str(e)}), 400


@app.route("/api/youtube/channel", methods=["POST"])
def api_youtube_channel():
    try:
        data = request.get_json()
        info = yt_get_channel_info(data["channel_name"])
        return jsonify({"success": True, "data": info})
    except Exception as e:
        return jsonify({"success": False, "detail": str(e)}), 400


@app.route("/api/youtube/playlist", methods=["POST"])
def api_youtube_playlist():
    try:
        data = request.get_json()
        info = yt_get_playlist_info(data["playlist_url"])
        return jsonify({"success": True, "data": info})
    except Exception as e:
        return jsonify({"success": False, "detail": str(e)}), 400


@app.route("/api/youtube/download-file/<filename>")
def api_youtube_download_file(filename):
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True, mimetype="video/mp4")


# ─── API Routes: Instagram ───
@app.route("/api/instagram/info", methods=["POST"])
def api_instagram_info():
    try:
        data = request.get_json()
        info = ig_get_reel_info(data["url"])
        return jsonify({"success": True, "data": info})
    except Exception as e:
        return jsonify({"success": False, "detail": str(e)}), 400


@app.route("/api/instagram/download", methods=["POST"])
def api_instagram_download():
    try:
        data = request.get_json()
        result = ig_download_reel(data["url"], data.get("quality", "720p"), data.get("with_audio", True))
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "detail": str(e)}), 400


@app.route("/api/instagram/profile", methods=["POST"])
def api_instagram_profile():
    try:
        data = request.get_json()
        info = ig_get_profile_content(data["username"])
        if "error" in info and info.get("posts") == []:
            return jsonify({"success": False, "detail": info["error"]}), 404
        return jsonify({"success": True, "data": info})
    except Exception as e:
        return jsonify({"success": False, "detail": str(e)}), 400


@app.route("/api/instagram/download-file/<filename>")
def api_instagram_download_file(filename):
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True, mimetype="video/mp4")


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=8000)
