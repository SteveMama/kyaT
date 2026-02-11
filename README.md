# MBTA Nearby — Personal Transit Helper

A personal, mobile-friendly web app that shows you the nearest MBTA stops, real-time departure predictions, and **walk-to-stop ETAs** so you know exactly when to leave.

## What it does

1. Gets your current GPS location (via browser)
2. Finds the nearest MBTA stops (using MBTA V3 API)
3. Calculates walking time to each stop (OpenRouteService or fallback estimate)
4. Fetches real-time predictions for each stop
5. Shows **"Leave in X minutes"** — the actionable number that tells you when to walk out the door

## Quick Start (Local)

```bash
# 1. Get your free API keys:
#    - MBTA: https://api-v3.mbta.com (optional but recommended for higher rate limits)
#    - OpenRouteService: https://openrouteservice.org/dev/#/signup (free, 2000 req/day)

# 2. Set environment variables
export MBTA_API_KEY="your-mbta-key"
export ORS_API_KEY="your-ors-key"

# 3. Install and run
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5000` on your phone (same Wi-Fi) or desktop browser.

## Deploy to Render (Free)

1. Push this folder to a GitHub repo
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your repo
4. Render auto-detects `render.yaml` — or manually set:
   - **Build:** `pip install -r requirements.txt`
   - **Start:** `gunicorn app:app --bind 0.0.0.0:$PORT`
5. Add environment variables: `MBTA_API_KEY`, `ORS_API_KEY`
6. Deploy!

Your app will be at `https://mbta-nearby-xxxx.onrender.com`. Add it to your iPhone home screen for an app-like experience.

## Add to iPhone Home Screen

1. Open your deployed URL in Safari
2. Tap the Share button → "Add to Home Screen"
3. Now you have a one-tap transit helper

## Architecture

```
Browser (GPS) → Flask API → MBTA V3 API (stops + predictions)
                          → OpenRouteService (walk routing)
                          → Returns merged JSON
                          → Frontend renders "leave-by" times
```

## API Keys

| Service | Free tier | Get it at |
|---------|-----------|-----------|
| MBTA V3 | Unlimited (rate-limited by IP without key; ~20 req/min) | https://api-v3.mbta.com |
| OpenRouteService | 2,000 directions/day, 40/min | https://openrouteservice.org/dev/#/signup |

## Files

- `app.py` — Flask backend (MBTA + ORS integration, walk ETA, leave-by calculation)
- `templates/index.html` — Mobile-first frontend (dark transit theme)
- `requirements.txt` — Python dependencies
- `render.yaml` — Render deployment config
- `Procfile` — Generic PaaS start command
