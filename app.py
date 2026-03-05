import os
import re
import json
import requests as http_requests
import yt_dlp
import instaloader
from flask import Flask, render_template, request, jsonify

# ─── Config ───
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

YT_DLP_DEFAULT_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
    "socket_timeout": 30,
}

# ─── Instagram GraphQL Scraper (no login required) ───
_IG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "X-IG-App-ID": "936619743392459",
    "X-ASBD-ID": "198387",
    "X-IG-WWW-Claim": "0",
    "Origin": "https://www.instagram.com",
    "Accept": "*/*",
}

_ENCODING_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _shortcode_to_pk(shortcode):
    """Convert Instagram shortcode to numeric media PK."""
    result = 0
    for char in shortcode[:28]:
        result = result * 64 + _ENCODING_CHARS.index(char)
    return result


def _extract_shortcode(url):
    """Extract shortcode from an Instagram URL."""
    m = re.search(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1)
    return None


def _ig_graphql_fetch(shortcode):
    """Fetch Instagram media info via GraphQL (no login needed)."""
    pk = _shortcode_to_pk(shortcode)
    session = http_requests.Session()

    # Step 1: Hit ruling endpoint to get CSRF cookie
    session.get(
        f"https://i.instagram.com/api/v1/web/get_ruling_for_content/?content_type=MEDIA&target_id={pk}",
        headers=_IG_HEADERS,
        timeout=15,
    )
    csrf = session.cookies.get("csrftoken", default="")

    # Step 2: Query GraphQL
    variables = json.dumps({
        "shortcode": shortcode,
        "child_comment_count": 3,
        "fetch_comment_count": 40,
        "parent_comment_count": 24,
        "has_threaded_comments": True,
    }, separators=(",", ":"))

    resp = session.post(
        "https://www.instagram.com/graphql/query/",
        headers={
            **_IG_HEADERS,
            "X-CSRFToken": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://www.instagram.com/reel/{shortcode}/",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "PolarisPostActionLoadPostQueryQuery",
            "variables": variables,
            "server_timestamps": "true",
            "doc_id": "8845758582119845",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    media = data.get("data", {}).get("xdt_shortcode_media")
    if not media:
        raise Exception("Could not fetch reel info from Instagram")
    return media


def _ig_embed_fallback(shortcode):
    """Fallback: scrape the embed page for video URL."""
    resp = http_requests.get(
        f"https://www.instagram.com/reel/{shortcode}/embed/",
        headers={"User-Agent": _IG_HEADERS["User-Agent"]},
        timeout=15,
    )
    # Try __additionalDataLoaded
    m = re.search(r"window\.__additionalDataLoaded\s*\(\s*[^,]+,\s*({.+?})\s*\)\s*;", resp.text)
    if m:
        additional = json.loads(m.group(1))
        items = additional.get("items", [])
        if items and items[0].get("video_versions"):
            best = max(items[0]["video_versions"], key=lambda v: v.get("width", 0))
            return {
                "video_url": best["url"],
                "thumbnail": items[0].get("image_versions2", {}).get("candidates", [{}])[0].get("url", ""),
                "owner": items[0].get("user", {}).get("username", "Unknown"),
                "caption": (items[0].get("caption", {}) or {}).get("text", ""),
                "duration": items[0].get("video_duration", 0),
            }
        media = additional.get("graphql", {}).get("shortcode_media") or additional.get("shortcode_media")
        if media and media.get("video_url"):
            return {
                "video_url": media["video_url"],
                "thumbnail": media.get("display_url", ""),
                "owner": media.get("owner", {}).get("username", "Unknown"),
                "caption": (media.get("edge_media_to_caption", {}).get("edges", [{}])[0].get("node", {}).get("text", "")),
                "duration": media.get("video_duration", 0),
            }
    return None

app = Flask(__name__)


# ─── Helpers ───
def sanitize_filename(filename):
    filename = re.sub(r'[<>:"/\\|?*#%&\x00-\x1f]', '', filename)
    filename = re.sub(r'\s+', '_', filename.strip())
    return filename[:200] if len(filename) > 200 else (filename or "untitled")


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


def _extract_info(url, extra_opts=None):
    opts = {**YT_DLP_DEFAULT_OPTS, "skip_download": True, "ignore_no_formats_error": True}
    if extra_opts:
        opts.update(extra_opts)
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _pick_format_url(info, quality="720p", with_audio=True):
    """Pick the best matching direct URL from extracted format info."""
    target_height = int(quality.replace("p", "")) if quality != "best" else 99999
    # Filter out non-video formats (storyboards, thumbnails, etc.)
    formats = [f for f in info.get("formats", [])
               if f.get("url") and f.get("ext") != "mhtml"
               and (f.get("vcodec", "none") != "none" or f.get("acodec", "none") != "none")]

    # Combined (audio+video) streams
    combined = [f for f in formats
                if f.get("vcodec", "none") != "none"
                and f.get("acodec", "none") != "none"]
    # Video-only streams
    video_only = [f for f in formats
                  if f.get("vcodec", "none") != "none"
                  and f.get("acodec", "none") == "none"]

    if with_audio and combined:
        candidates = [f for f in combined if (f.get("height") or 0) <= target_height]
        if not candidates:
            candidates = combined
        best = max(candidates, key=lambda f: f.get("height") or 0)
        return best.get("url"), best.get("ext", "mp4")

    # Fallback: video-only (no audio available as combined) or any stream
    pool = video_only if video_only else combined if combined else formats
    candidates = [f for f in pool if (f.get("height") or 0) <= target_height]
    if not candidates:
        candidates = pool
    if not candidates:
        return None, None
    best = max(candidates, key=lambda f: f.get("height") or 0)
    return best.get("url"), best.get("ext", "mp4")


# ─── YouTube Crawler ───
def yt_get_video_info(url):
    info = _extract_info(url)

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
    info = _extract_info(url, {"extract_flat": True, "playlistend": 30})

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
    info = _extract_info(playlist_url, {"extract_flat": True})

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


def yt_get_download_url(url, quality="720p", with_audio=True):
    info = _extract_info(url)
    title = info.get("title", "video")
    direct_url, ext = _pick_format_url(info, quality, with_audio)
    if not direct_url:
        raise Exception("No downloadable format found for this video")
    return {"url": direct_url, "title": title, "ext": ext}


# ─── Instagram Crawler (GraphQL first, no login needed) ───
def ig_get_reel_info(url):
    shortcode = _extract_shortcode(url)
    if not shortcode:
        raise Exception("Invalid Instagram URL")

    try:
        media = _ig_graphql_fetch(shortcode)
        title = ""
        caption_edges = media.get("edge_media_to_caption", {}).get("edges", [])
        if caption_edges:
            title = caption_edges[0].get("node", {}).get("text", "")
        title = title[:100] if title else "Instagram Reel"

        formats = []
        if media.get("video_url"):
            formats.append({
                "format_id": "graphql_best",
                "quality": "720p",
                "resolution": f"{media.get('dimensions', {}).get('width', '?')}x{media.get('dimensions', {}).get('height', '?')}",
                "filesize": "Unknown",
                "ext": "mp4",
            })

        return {
            "title": title,
            "thumbnail": media.get("display_url", ""),
            "duration": media.get("video_duration", 0),
            "duration_formatted": format_duration(media.get("video_duration", 0)),
            "description": title,
            "uploader": media.get("owner", {}).get("username", "Unknown"),
            "webpage_url": url,
            "formats": formats,
        }
    except Exception:
        # Fallback: try embed page
        fallback = _ig_embed_fallback(shortcode)
        if fallback:
            return {
                "title": (fallback["caption"][:100]) or "Instagram Reel",
                "thumbnail": fallback["thumbnail"],
                "duration": fallback.get("duration", 0),
                "duration_formatted": format_duration(fallback.get("duration", 0)),
                "description": fallback["caption"][:500],
                "uploader": fallback["owner"],
                "webpage_url": url,
                "formats": [{"format_id": "embed", "quality": "720p", "resolution": "?", "filesize": "Unknown", "ext": "mp4"}],
            }
        raise Exception("Could not fetch reel info. Instagram may be rate-limiting this server.")


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


def ig_get_download_url(url, quality="720p", with_audio=True):
    shortcode = _extract_shortcode(url)
    if not shortcode:
        raise Exception("Invalid Instagram URL")

    try:
        media = _ig_graphql_fetch(shortcode)
        video_url = media.get("video_url")
        if not video_url:
            raise Exception("No video found (this may be a photo post)")

        caption_edges = media.get("edge_media_to_caption", {}).get("edges", [])
        title = caption_edges[0]["node"]["text"][:50] if caption_edges else "reel"

        return {"url": video_url, "title": title, "ext": "mp4"}
    except Exception as graphql_err:
        # Fallback: try embed page
        fallback = _ig_embed_fallback(shortcode)
        if fallback and fallback.get("video_url"):
            return {
                "url": fallback["video_url"],
                "title": (fallback["caption"][:50]) or "reel",
                "ext": "mp4",
            }
        raise Exception(f"Could not download reel: {graphql_err}")


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
        result = yt_get_download_url(data["url"], data.get("quality", "720p"), data.get("with_audio", True))
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
        result = ig_get_download_url(data["url"], data.get("quality", "720p"), data.get("with_audio", True))
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


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=8000)
