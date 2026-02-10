# Encore

Backend API for converting & syncing YouTube playlists → Spotify audio playlists.

Production-grade Django + DRF system with:
- Email/JWT auth
- YouTube Data API integration
- Spotify OAuth2 + Web API
- Stateful sync jobs
- Fuzzy matching (rapidfuzz)
- Retryable operations & partial failures

## Tech Stack
- Python 3.12+
- Django 5.1+
- Django REST Framework
- PostgreSQL (prod) / SQLite (dev)
- Celery + Redis (for background sync)
- Spotipy, google-api-python-client, rapidfuzz, yt-dlp

## Setup
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver