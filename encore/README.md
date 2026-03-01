# Encore Backend API

Production-focused Django/DRF backend for importing public YouTube playlists, matching to Spotify tracks, and syncing to Spotify playlists.

## Features
- JWT auth (`/api/token/`, `/api/token/refresh/`)
- YouTube playlist import with metadata + duration ingestion
- Spotify OAuth linking (`/api/spotify/login/`, `/api/spotify/callback/`)
- Background sync with Celery + Redis
- Sync modes:
  - `append_only`
  - `smart_diff`
  - `full_replace`
- Retries + rate-limit handling on YouTube/Spotify transient failures
- Per-operation sync tracking (`SyncOperation`) and polling endpoint
- DRF throttling defaults for abuse protection

## Quick Start (Local)
```bash
cd encore
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py runserver
```

Run Celery worker in another shell:
```bash
cd encore
source .venv/bin/activate
celery -A config worker -l info
```

## Environment Variables
See `.env.example` for the full set.

Required for external integrations:
- `YOUTUBE_API_KEY`
- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`
- `SPOTIFY_REDIRECT_URI`
- `FIELD_ENCRYPTION_KEY`

Security/runtime:
- `SECRET_KEY`
- `DEBUG`
- `ALLOWED_HOSTS`
- `DATABASE_URL`
- `CELERY_BROKER_URL`
- `CELERY_RESULT_BACKEND`
- `CORS_ALLOWED_ORIGINS`
- `CSRF_TRUSTED_ORIGINS`

## API Overview
- `POST /api/register/`
- `POST /api/token/`
- `POST /api/token/refresh/`
- `GET /api/spotify/login/`
- `GET /api/spotify/callback/`
- `GET /api/spotify/status/`
- `POST /api/playlists/` (import YouTube playlist)
- `GET /api/playlists/`
- `GET /api/playlists/<id>/`
- `GET /api/playlists/<id>/items/`
- `POST /api/playlists/<id>/sync/`
- `GET /api/sync-operations/<id>/`
- `GET /api/schema/`

Browsable DRF auth helper: `GET /api-auth/login/`

## Tests
```bash
cd encore
python manage.py test
```

## Docker
```bash
cd encore
docker compose up --build
```

Services started:
- Django API (`web`) on `:8000`
- Celery worker (`celery`)
- Redis (`redis`) on `:6379`
- Postgres (`db`) on `:5432`

## Production Settings
Use:
- `DJANGO_SETTINGS_MODULE=config.settings.production`
- `DEBUG=False`
- strong `SECRET_KEY`
- non-wildcard `ALLOWED_HOSTS`
- TLS/HTTPS and trusted proxy headers

## Deployment Guide (Heroku)
Heroku discontinued the free dyno tier on **November 28, 2022**. Use Eco/Basic dynos instead.

1. Create app + managed services
```bash
heroku create <your-app-name>
heroku addons:create heroku-postgresql:mini
heroku addons:create heroku-redis:mini
```
2. Set config vars
```bash
heroku config:set DJANGO_SETTINGS_MODULE=config.settings.production
heroku config:set SECRET_KEY=<strong-secret>
heroku config:set DEBUG=False
heroku config:set ALLOWED_HOSTS=<your-app-name>.herokuapp.com
heroku config:set SPOTIFY_CLIENT_ID=<id>
heroku config:set SPOTIFY_CLIENT_SECRET=<secret>
heroku config:set SPOTIFY_REDIRECT_URI=https://<your-app-name>.herokuapp.com/api/spotify/callback/
heroku config:set YOUTUBE_API_KEY=<key>
heroku config:set FIELD_ENCRYPTION_KEY=<fernet-key>
```
3. Deploy and migrate
```bash
git push heroku main
heroku run python manage.py migrate
```
4. Run worker dyno
```bash
heroku ps:scale web=1 worker=1
```

## Operational Notes
- Sync is idempotent at playlist level and protected against concurrent active operations.
- `smart_diff` removes only tracks previously managed by Encore and no longer matched.
- Spotify 401 responses mark linked accounts inactive and require user re-linking.
