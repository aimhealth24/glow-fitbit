# Deploying to Railway

## 1. Files to bring over
Copy `app.py`, `requirements.txt`, `Procfile`, `.gitignore` into your repo,
alongside your existing `static/multi_user.html` (not included here — bring
your existing one, unchanged). Do **not** carry over `users.json`,
`tokens/`, or `aim_health_credentials.json` — those are now replaced by
MongoDB and an environment variable.

## 2. MongoDB
Reuse the same MongoDB instance your health data already writes to. No
manual collection setup needed — `users`, `tokens`, and `oauth_state`
collections are created automatically with the right indexes on first run.

## 3. Environment variables (Railway → Variables tab)
Set everything listed in `.env.example`. In particular:
- `MONGODB_URI` — your existing connection string
- `API_KEY` — generate one, e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. Give this value to the external system that will call `/api/health`.
- `GOOGLE_OAUTH_CLIENT_JSON` — paste the full contents of your existing `aim_health_credentials.json` as a single-line JSON string.

## 4. Google Cloud Console
Once Railway assigns your domain (e.g. `https://your-app.up.railway.app`),
add `https://your-app.up.railway.app/callback` as an authorized redirect URI
on the OAuth client in Google Cloud Console. The app builds the redirect URI
from the incoming request automatically, so no code change or extra env var
is needed if the domain changes later — just update it in Google Console.

## 5. Re-authenticate each user once
Because tokens previously lived on local disk, they weren't carried over.
After deploying, each existing user needs to hit "Sign in" once more from
the dashboard so their token gets stored in Mongo. After that, tokens
persist across all future deploys.

## 6. Verify
- `GET /health` → `{"status": "ok"}` — this is what Railway's health check should point at.
- `GET /api/health?user=<id>&date=YYYY-MM-DD` with header `X-API-Key: <your API_KEY>` → returns data (401 without the header).

## Note on scope
Only `/api/health` (the endpoint the external system calls) is protected
with the API key. The dashboard's user-management and sign-in endpoints
(`/api/users`, `/api/auth/*`) are still open, same as before — they're meant
for you as the admin via the dashboard UI. If this app becomes reachable by
people other than you, it's worth adding a login step in front of the
dashboard itself; that wasn't in scope here since only one machine-to-machine
integration was requested.
