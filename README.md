# ReelGrab.App

Open source video downloader for YouTube and Instagram. Built with Flask and vanilla JS.

## Features

- Download YouTube videos, Shorts, and playlists
- Download Instagram Reels and posts
- Browse YouTube channels and Instagram profiles
- Quality selection (720p, 1080p, Best)
- Audio toggle (with/without audio)

## Project Structure

```
reelsDownloads/
  app.py              # Flask app (API + page routes)
  requirements.txt    # Python dependencies
  vercel.json         # Vercel deployment config
  templates/
    base.html         # Shared layout
    home.html         # Home page
    browse.html       # Browse creators page
  static/             # Static assets
```

## Run Locally

```bash
pip install -r requirements.txt
python app.py
```

App runs at `http://localhost:8000`

## Deploy on Vercel

1. Push to GitHub
2. Import repo on [vercel.com](https://vercel.com)
3. Deploy — `vercel.json` handles routing automatically

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/youtube/info` | Get YouTube video info |
| POST | `/api/youtube/download` | Download YouTube video |
| POST | `/api/youtube/channel` | Get channel videos |
| POST | `/api/youtube/playlist` | Get playlist videos |
| POST | `/api/instagram/info` | Get Instagram reel info |
| POST | `/api/instagram/download` | Download Instagram reel |
| POST | `/api/instagram/profile` | Get profile posts |
| GET | `/api/health` | Health check |

## Tech Stack

- **Backend:** Flask, yt-dlp, Instaloader
- **Frontend:** Jinja2, Tailwind CSS (CDN), Vanilla JS
- **Deployment:** Vercel (Python runtime)

## License

MIT
# reelsDownload
