# Health Dashboard

A small Flask app that connects to Google Health (Fitbit) accounts, fetches
per-day activity, heart, and sleep data, and displays it on a dashboard.
Data is stored in MongoDB and also exposed via an API for other systems to
consume.

## Features

- Multi-user dashboard with per-user Google account connection
- Fetches steps, distance, VO2 max, swim strokes, heart rate, resting HR,
  HRV, breathing rate, SpO2, skin temperature, and sleep (including an
  estimated sleep score)
- Persists fetched data, users, and OAuth tokens in MongoDB
- `/api/health` endpoint for external systems to pull a user's data for a
  given day, protected by an API key

## Architecture

- **Backend**: Flask, serving both the dashboard UI and the JSON API
- **Storage**: MongoDB — three collections beyond your health data:
  - `users` — the registered dashboard users
  - `tokens` — each user's Google OAuth credentials
  - `oauth_state` — short-lived state for in-progress sign-ins (self-cleaning)
- **Frontend**: a single server-rendered template (`templates/multi_user.html`).
  It's rendered rather than served statically so the API key can be
  injected into its JS at request time instead of living in the file.
- **Production server**: gunicorn, via the included `Procfile`

## Environment variables

Copy `.env.example` to `.env` for local development (loaded automatically),
or set these in your hosting platform's environment/variables panel for
production.

| Variable | Required | Description |
|---|---|---|
| `MONGODB_URI` | Yes | MongoDB connection string. App won't start without it. |
| `API_KEY` | Yes | Shared secret for `/api/health`. Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `GOOGLE_OAUTH_CLIENT_JSON` | Yes | Full contents of your Google OAuth client JSON as a single-line string, e.g. `{"web":{"client_id":"...","client_secret":"...","token_uri":"https://oauth2.googleapis.com/token"}}`. |
| `MONGODB_DB` | No | Database name. Defaults to `aim_health`. |
| `MONGODB_COLLECTION` | No | Collection for fetched health data. Defaults to `health_data`. |
| `ENVIRONMENT` | No | Defaults to `production`. Set to `development` locally to allow OAuth over plain `http://localhost`. |
| `ALLOWED_ORIGINS` | No | Comma-separated origins allowed to call the API cross-origin from a browser. |
| `PORT` | No | Set automatically by most hosting platforms. Defaults to `8080` locally. |

## Getting started locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in real values, including ENVIRONMENT=development
python3 app.py         # runs on http://localhost:8080
```

In Google Cloud Console, add `http://localhost:8080/callback` as an
authorized redirect URI on your OAuth client before signing in locally.

## Deploying

1. Provision a MongoDB instance and set `MONGODB_URI`. Collections and
   indexes are created automatically on first run.
2. Set the remaining required environment variables on your platform.
3. Once your platform assigns a domain, add
   `https://<your-domain>/callback` as an authorized redirect URI in Google
   Cloud Console. The app derives the redirect URI from the incoming
   request, so this is the only place that needs updating if the domain
   ever changes.
4. Point your platform's health check at `GET /health`.
5. From the dashboard, connect each user's Google account once — this
   stores their token in MongoDB, where it persists across restarts and
   deploys.

## How sign-in works

1. Clicking **Connect Account** for a user calls `GET /api/auth/start?user=<id>`, which builds a Google authorization URL and stores a short-lived record in `oauth_state` (state token, client credentials, which user it's for).
2. The user signs in with Google in a popup and grants the requested scopes.
3. Google redirects to `/callback`, which looks up the matching `oauth_state` record, exchanges the authorization code for an access + refresh token, and saves them to `tokens`.
4. The `oauth_state` record is deleted immediately after use — it's single-use by design. Any abandoned attempt also expires automatically after 10 minutes. An empty `oauth_state` collection is the normal steady state, not a sign anything is broken.
5. The dashboard polls `/api/users` until it sees the user marked as authenticated.

Once connected, a user's refresh token is used to silently renew their
access token on every `/api/health` call — no repeated sign-ins needed
unless access is revoked, the OAuth client's credentials change, or (if
your Google OAuth consent screen is still in **Testing** mode) Google
auto-expires the refresh token after 7 days.

## API reference

**`GET /api/health?user=<id>&date=YYYY-MM-DD`**
Header: `X-API-Key: <API_KEY>`
Returns that user's health metrics for the given day and stores the result
in MongoDB.

**`GET /api/users`**
Returns `[{id, label, initials, authenticated}, ...]`.

**`POST /api/users`**
Body: `{"label": "Name"}`. Creates a new user.

**`DELETE /api/users/<id>`**
Removes a user and their stored token.

**`GET /api/auth/start?user=<id>`**
Begins the Google sign-in flow for a user.

**`GET /api/auth/status?user=<id>`**
Returns `{"authenticated": bool}`.

**`GET /health`**
Liveness check; also verifies MongoDB is reachable.

## Notes

- Only `/api/health` requires the API key. The dashboard's own endpoints
  (`/api/users`, `/api/auth/*`) are unauthenticated and intended for admin
  use through the dashboard UI itself — don't expose this app on a public
  URL without adding a real login in front of it.
- The API key is injected into the dashboard's rendered page so its own
  JavaScript can call `/api/health`, which means it's visible to anyone
  who can load the page (view-source, dev tools). Fine for a private,
  internal dashboard; not a secret from its own users.
- If you see `No time zone found with key America/Chicago`, install the
  `tzdata` package (already listed in `requirements.txt`) — some minimal
  Linux images don't ship IANA timezone data, which `zoneinfo` needs.
