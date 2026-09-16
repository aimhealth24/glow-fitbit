import os
import uuid
import json
from functools import wraps
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, render_template, session, redirect, url_for
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import requests
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

# Loads variables from a local .env file if one exists (for local dev only).
# In Railway, real environment variables are already set on the platform, so
# this is a no-op there — .env is git-ignored and never deployed.
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__, static_folder="static")

# Railway (and most PaaS) terminate TLS at an edge proxy, so requests reach
# this app over plain HTTP internally with X-Forwarded-Proto: https set.
# Without ProxyFix, request.host_url reports "http://" even in production,
# which breaks the OAuth redirect_uri built in _redirect_uri() below.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# ── Environment / config ───────────────────────────────────────────────────
# Secrets must come from real environment variables — no hardcoded fallbacks.
# The app fails fast at startup if something required is missing, rather
# than silently running with a bad default.

ENVIRONMENT = os.environ.get("ENVIRONMENT", "production")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required")

API_KEY = os.environ.get("API_KEY")  # required for machine-to-machine calls to /api/health

# Dashboard login — a single shared password gates the UI itself, separate
# from API_KEY which is only for the /api/health machine-to-machine call.
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY environment variable is required (used to sign login sessions)")
app.secret_key = SECRET_KEY
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=14)

SITE_PASSWORD = os.environ.get("SITE_PASSWORD")  # checked at login time, not required at startup

# Comma-separated list of origins allowed to call this API cross-origin from
# a browser, e.g. "https://dashboard.example.com,https://admin.example.com".
# Empty by default -> no cross-origin browser access (same-origin dashboard
# still works fine since Flask serves it directly).
_allowed_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
CORS(app, origins=_allowed_origins if _allowed_origins else [])

# OAuthlib requires HTTPS for redirect URIs unless explicitly relaxed. Only
# relax it outside production (e.g. local dev over http://localhost).
if ENVIRONMENT != "production":
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

# ── Postgres setup ──────────────────────────────────────────────────────────
# One pool per worker process (gunicorn spawns several); psycopg_pool handles
# checking connections out/in and reconnecting if the server drops one.
# Schema (tables, indexes, partitions) lives in schema.sql — run once against
# the database before first deploy. This module only ever reads/writes rows.
_db_pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5, kwargs={"row_factory": dict_row})


def db():
    """Context manager yielding a pooled connection. Use as:
    with db() as conn, conn.cursor() as cur: ..."""
    return _db_pool.connection()


def save_health_data(user_id, date, data, timezone="America/Chicago"):
    """Save health data for the requested date.

    Period:
        Start: YYYY-MM-DD 00:00:00
        End:   YYYY-MM-DD 11:59:59
    """

    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(timezone)

        period_start = datetime.strptime(
            date, "%Y-%m-%d"
        ).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
            tzinfo=tz,
        )

        period_end = period_start.replace(
            hour=11,
            minute=59,
            second=59,
            microsecond=0,
        )

        with db() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO health_snapshots (
                    user_id,
                    period_start,
                    period_end,
                    data,
                    fetched_at
                )
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (user_id, period_start, period_end)
                DO UPDATE SET
                    data = EXCLUDED.data,
                    fetched_at = now()
                """,
                (
                    user_id,
                    period_start,
                    period_end,
                    Jsonb(data),
                ),
            )

        return True

    except psycopg.Error as e:
        print(
            f"[db] Failed to save health data "
            f"for {user_id} on {date}: {e}"
        )
        return False


SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]
BASE_URL = "https://health.googleapis.com/v4/users/me"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# ── User registry (Postgres-backed) ─────────────────────────────────────────
# Users, tokens, and in-flight OAuth state all live in Postgres now — see
# schema.sql. Nothing here depends on local disk, so it survives redeploys.

def make_initials(label):
    parts = [p for p in label.strip().split() if p]
    if not parts:
        return "U"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def load_users():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, label, initials FROM users ORDER BY created_at")
        return cur.fetchall()


def find_user(users, user_id):
    return next((u for u in users if u["id"] == user_id), None)


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_stored_token(user_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT token FROM tokens WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return row["token"] if row else None


def save_stored_token(user_id, creds):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tokens (user_id, token, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (user_id) DO UPDATE SET token = EXCLUDED.token, updated_at = now()
            """,
            (user_id, Jsonb(json.loads(creds.to_json()))),
        )


def delete_stored_token(user_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM tokens WHERE user_id = %s", (user_id,))


def get_credentials(user="user1"):
    token_info = get_stored_token(user)
    if not token_info:
        return None
    creds = Credentials.from_authorized_user_info(token_info, SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_stored_token(user, creds)
            return creds
        except Exception:
            delete_stored_token(user)
    return None


def get_headers(creds):
    return {"Authorization": f"Bearer {creds.token}", "Accept": "application/json"}


def login_required(f):
    """Gates the dashboard UI and admin endpoints behind the shared site
    password. Separate from require_api_key, which protects the
    machine-to-machine /api/health endpoint instead."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def require_api_key(f):
    """Protects machine-to-machine endpoints with a shared secret header."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not API_KEY:
            return jsonify({"error": "Server misconfigured: API_KEY is not set"}), 500
        provided = request.headers.get("X-API-Key")
        if not provided or provided != API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ── Data helpers ──────────────────────────────────────────────────────────────

def fetch_interval_data(creds, data_type, date, timezone="America/Chicago"):
    from zoneinfo import ZoneInfo

    tz   = ZoneInfo(timezone)
    d    = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=tz)
    next_day = d + timedelta(days=1)

    field = data_type.replace("-", "_")

    # civil_start_time accepts ISO 8601 with UTC offset — the API interprets
    # it in the user's local time, so boundaries align with midnight local time
    filter_expr = (
        f'{field}.interval.civil_start_time >= "{d.strftime("%Y-%m-%dT%H:%M:%S")}" '
        f'AND {field}.interval.civil_start_time < "{next_day.strftime("%Y-%m-%dT%H:%M:%S")}"'
    )

    url = f"{BASE_URL}/dataTypes/{data_type}/dataPoints"
    points, page_token = [], None

    while True:
        params = {"filter": filter_expr, "pageSize": "10000"}
        if page_token:
            params["pageToken"] = page_token

        resp = requests.get(url, headers=get_headers(creds), params=params)
        if resp.status_code != 200:
            print(f"  [{data_type}] Error {resp.status_code}: {resp.text}")
            return []

        body = resp.json()
        points.extend(body.get("dataPoints", []))
        page_token = body.get("nextPageToken")
        if not page_token:
            break

    return points


def daily_rollup(creds, data_type, date):
    d        = datetime.strptime(date, "%Y-%m-%d")
    next_day = d + timedelta(days=1)
    def civil(dt):
        return {"date": {"year": dt.year, "month": dt.month, "day": dt.day}}
    url  = f"{BASE_URL}/dataTypes/{data_type}/dataPoints:dailyRollUp"
    body = {
        "range": {
            "start": civil(d),
            "end":   civil(next_day)   # exclusive boundary
        },
        "windowSizeDays": 1
    }
    resp = requests.post(url, headers=get_headers(creds), json=body)
    if resp.status_code != 200:
        return {}
    points = resp.json().get("rollupDataPoints", [])
    return points[0] if points else {}


def list_points(creds, data_type, filter_str, page_size=25):
    url    = f"{BASE_URL}/dataTypes/{data_type}/dataPoints"
    all_points, page_token = [], None
    while True:
        params = {"filter": filter_str, "pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(url, headers=get_headers(creds), params=params)
        if resp.status_code != 200:
            return []
        data = resp.json()
        all_points.extend(data.get("dataPoints", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return all_points


def list_by_date(creds, data_type, filter_name, date):
    next_day   = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    filter_str = f'{filter_name}.date >= "{date}" AND {filter_name}.date < "{next_day}"'
    return list_points(creds, data_type, filter_str)


def list_by_civil_time(creds, data_type, filter_name, date):
    next_day   = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    filter_str = f'{filter_name}.sample_time.civil_time >= "{date}" AND {filter_name}.sample_time.civil_time < "{next_day}"'
    return list_points(creds, data_type, filter_str, page_size=1000)

# ── Fetchers ──────────────────────────────────────────────────────────────────

def fetch_all(creds, date):
    result = {}

    steps = fetch_interval_data(creds, "steps", date)
    total_steps = sum(int(p["steps"].get("count", 0)) for p in steps)
    result["steps"] = int(float(total_steps)) if total_steps else 0

    dist_p = daily_rollup(creds, "distance", date)
    dist_raw = dist_p.get("distance", {}).get("millimetersSum", 0)
    result["distance_miles"] = round(float(dist_raw) / 1_609_344, 2) if dist_raw else 0.0

    vo2_pts = list_by_date(creds, "daily-vo2-max", "daily_vo2_max", date)
    result["vo2max"] = vo2_pts[0].get("dailyVo2Max", {}).get("vo2MaxMlPerMinPerKg") if vo2_pts else None

    swim_p = daily_rollup(creds, "swim-lengths-data", date)
    strokes_raw = swim_p.get("swimLengthsData", {}).get("strokeCountSum", 0)
    strokes = int(float(strokes_raw)) if strokes_raw else 0
    result["swim_strokes"] = strokes if strokes > 0 else None

    hr_pts = list_by_civil_time(creds, "heart-rate", "heart_rate", date)
    parsed = []
    for p in hr_pts:
        hr = p.get("heartRate", {})
        civil = hr.get("sampleTime", {}).get("civilTime", {})
        d = civil.get("date", {})
        t = civil.get("time", {})
        parsed.append({
            "datetime": f"{d['year']}-{d['month']:02d}-{d['day']:02d} {t.get('hours', 0):02d}:{t.get('minutes', 0):02d}:{t.get('seconds', 0):02d}",
            "bpm": int(hr.get("beatsPerMinute", 0)),
        })
    parsed.sort(key=lambda x: x["datetime"])

    bpms = [int(p.get("heartRate", {}).get("beatsPerMinute")) for p in hr_pts if p.get("heartRate", {}).get("beatsPerMinute")]
    result["hr_avg"] = round(sum(bpms) / len(bpms)) if bpms else None
    result["hr_min"] = min(bpms) if bpms else None
    result["hr_max"] = max(bpms) if bpms else None

    rhr_pts = list_by_date(creds, "daily-resting-heart-rate", "daily_resting_heart_rate", date)
    result["resting_hr"] = rhr_pts[0].get("dailyRestingHeartRate", {}).get("beatsPerMinute") if rhr_pts else None

    hrv_pts = list_by_date(creds, "daily-heart-rate-variability", "daily_heart_rate_variability", date)
    result["hrv"] = hrv_pts[0].get("dailyHeartRateVariability", {}).get("averageHeartRateVariabilityMilliseconds") if hrv_pts else None

    br_pts = list_by_date(creds, "daily-respiratory-rate", "daily_respiratory_rate", date)
    result["breathing_rate"] = br_pts[0].get("dailyRespiratoryRate", {}).get("breathsPerMinute") if br_pts else None

    spo2_pts = list_by_date(creds, "daily-oxygen-saturation", "daily_oxygen_saturation", date)
    result["spo2"] = spo2_pts[0].get("dailyOxygenSaturation", {}).get("averagePercentage") if spo2_pts else None

    temp_pts = list_by_date(creds, "daily-sleep-temperature-derivations", "daily_sleep_temperature_derivations", date)
    result["skin_temp"] = temp_pts[0].get("dailySleepTemperatureDerivations", {}).get("nightlyTemperatureCelsius") if temp_pts else None

    next_day   = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    filter_str = f'sleep.interval.civil_end_time >= "{date}" AND sleep.interval.civil_end_time < "{next_day}"'
    sleep_pts  = list_points(creds, "sleep", filter_str, page_size=25)
    sleep_data = {}
    if sleep_pts:
        for p in sleep_pts:
            if p.get("sleep", {}).get("type") == "STAGES":
                sleep_data = p.get("sleep", {})
                break
        if not sleep_data:
            sleep_data = sleep_pts[0].get("sleep", {})

    summary = sleep_data.get("summary", {})
    sleep_data = {
        "minutes_asleep": summary.get("minutesAsleep", 0),
        "minutes_awake": summary.get("minutesAwake", 0),
        "minutes_in_bed": summary.get("minutesInSleepPeriod", 0),
        "stages": {s["type"]: s.get("minutes", 0) for s in summary.get("stagesSummary", [])},
    }
    sleep_data["sleep_score"] = calculate_sleep_score(summary)
    result["sleep"] = sleep_data

    return result


def calculate_sleep_score(summary: dict) -> dict:
    """
    Estimates the Google Health sleep score from a `summary` object.

    Model: score ≈ 0.165 * minutes_asleep - 0.108 * minutes_awake + 18.17
    Fit via linear regression against 4 real (summary, actual_score) examples.

    NOTE: This is a statistical approximation, not Google's real (proprietary,
    personalized) algorithm.
    """

    def to_int(v, default=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    stages = {e.get("type"): to_int(e.get("minutes")) for e in summary.get("stagesSummary", [])}

    minutes_asleep = to_int(
        summary.get("minutesAsleep"),
        default=stages.get("LIGHT", 0) + stages.get("DEEP", 0) + stages.get("REM", 0),
    )
    minutes_awake = to_int(summary.get("minutesAwake"), default=stages.get("AWAKE", 0))

    raw_score = 0.1651146198152838 * minutes_asleep - 0.10835224008588279 * minutes_awake + 18.174930359947673
    score = int(round(min(100.0, max(0.0, raw_score))))

    if score >= 90:
        category = "Excellent"
    elif score >= 80:
        category = "Good"
    elif score >= 60:
        category = "Fair"
    else:
        category = "Poor"

    return {"score": score, "category": category}

# ── User management endpoints ─────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if not SITE_PASSWORD:
        return "Server misconfigured: SITE_PASSWORD is not set", 500

    error = None
    if request.method == "POST":
        if request.form.get("password", "") == SITE_PASSWORD:
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            next_path = request.args.get("next")
            # Only follow same-site relative paths, never an external URL.
            if next_path and next_path.startswith("/"):
                return redirect(next_path)
            return redirect(url_for("index"))
        error = "Incorrect password"

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/users", methods=["GET"])
@login_required
def list_users():
    users = load_users()
    out = []
    for u in users:
        creds = get_credentials(u["id"])
        out.append({
            "id": u["id"],
            "label": u["label"],
            "initials": u["initials"],
            "authenticated": creds is not None,
        })
    return jsonify(out)


@app.route("/api/users", methods=["POST"])
@login_required
def add_user():
    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "A name is required"}), 400
    if len(label) > 60:
        return jsonify({"error": "Name is too long"}), 400

    new_user = {
        "id": str(uuid.uuid4()),
        "label": label,
        "initials": make_initials(label),
    }
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, label, initials) VALUES (%s, %s, %s)",
                (new_user["id"], new_user["label"], new_user["initials"]),
            )
    except psycopg.Error as e:
        return jsonify({"error": f"Could not save user: {e}"}), 500

    return jsonify({**new_user, "authenticated": False}), 201


@app.route("/api/users/<user_id>", methods=["DELETE"])
@login_required
def delete_user(user_id):
    users = load_users()
    u = find_user(users, uuid.UUID(user_id))
    if not u:
        return jsonify({"error": "User not found"}), 404

    with db() as conn, conn.cursor() as cur:
        # ON DELETE CASCADE on tokens/oauth_state/health_daily/health_readings
        # (see schema.sql) means this alone cleans up everything for the user.
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))

    return jsonify({"status": "deleted"})

# ── Auth endpoints ────────────────────────────────────────────────────────────

def _redirect_uri():
    # Derived from the incoming request rather than hardcoded, so it
    # automatically matches whatever domain the app is actually deployed on
    # (Railway's assigned domain, a custom domain, or localhost in dev).
    return request.host_url.rstrip("/") + "/callback"


def _load_google_oauth_config():
    raw = os.environ.get("GOOGLE_OAUTH_CLIENT_JSON")
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return cfg.get("web", cfg.get("installed", {}))


@app.route("/api/auth/start")
@login_required
def auth_start():
    user = request.args.get("user", "user1")
    user = uuid.UUID(user)
    users = load_users()
    if not find_user(users, user):
        return jsonify({"error": "Unknown user. Add them first."}), 404

    cfg = _load_google_oauth_config()
    if not cfg:
        return jsonify({"error": "GOOGLE_OAUTH_CLIENT_JSON environment variable is not set or invalid."}), 400

    creds = get_credentials(user)
    if creds:
        return jsonify({"status": "already_authenticated"})

    from requests_oauthlib import OAuth2Session

    client_id      = cfg["client_id"]
    client_secret  = cfg["client_secret"]
    token_endpoint = cfg.get("token_uri", "https://oauth2.googleapis.com/token")
    redirect_uri   = _redirect_uri()

    oauth = OAuth2Session(client_id, scope=SCOPES, redirect_uri=redirect_uri)
    auth_url, state = oauth.authorization_url(
        "https://accounts.google.com/o/oauth2/auth",
        access_type="offline", prompt="consent",
    )

    with db() as conn, conn.cursor() as cur:
        # Opportunistic cleanup of abandoned sign-ins — cheap since this
        # table stays tiny. Postgres has no TTL index like Mongo did, so
        # expiry is enforced here and at read time in /callback below.
        cur.execute("DELETE FROM oauth_state WHERE created_at < now() - interval '10 minutes'")
        cur.execute(
            """
            INSERT INTO oauth_state (state, client_id, client_secret, token_uri, redirect_uri, user_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            """,
            (state, client_id, client_secret, token_endpoint, redirect_uri, user),
        )

    return jsonify({"auth_url": auth_url})


@app.route("/callback")
def callback():
    from requests_oauthlib import OAuth2Session

    state = request.args.get("state")
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM oauth_state WHERE state = %s AND created_at > now() - interval '10 minutes'",
            (state,),
        )
        state_row = cur.fetchone() if state else None
    if not state_row:
        return "State expired or invalid. Please restart sign-in from the dashboard.", 400

    oauth = OAuth2Session(
        state_row["client_id"], scope=SCOPES,
        redirect_uri=state_row["redirect_uri"], state=state,
    )
    token = oauth.fetch_token(
        state_row["token_uri"],
        authorization_response=request.url,
        client_secret=state_row["client_secret"],
        include_client_id=True,
    )
    creds = Credentials(
        token         = token.get("access_token"),
        refresh_token = token.get("refresh_token"),
        token_uri     = state_row["token_uri"],
        client_id     = state_row["client_id"],
        client_secret = state_row["client_secret"],
        scopes        = SCOPES,
    )
    user = state_row["user_id"]
    save_stored_token(user, creds)
    try:
        userinfo = requests.get(USERINFO_URL, headers=get_headers(creds), timeout=5)
        if userinfo.status_code == 200:
            email = userinfo.json().get("email")
            if email:
                with db() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE users SET email = %s WHERE id = %s", (email, user))
    except (requests.RequestException, psycopg.Error) as e:
        # Most likely cause of a psycopg error here: this same Google account
        # is already linked to a different dashboard user (email is UNIQUE).
        # Not fatal — the token was already saved; just log and move on.
        print(f"[callback] Could not save email for {user}: {e}")

    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM oauth_state WHERE state = %s", (state,))

    return """
    <html><body style="font-family:sans-serif;max-width:500px;margin:80px auto;text-align:center">
      <h2 style="color:#2e7d32">&#10003; Authenticated successfully!</h2>
      <p>You may close this tab and return to the dashboard.</p>
    </body></html>"""


@app.route("/api/auth/status")
@login_required
def auth_status():
    user = request.args.get("user", "user1")
    user = uuid.UUID(user)
    creds = get_credentials(user)
    return jsonify({"authenticated": creds is not None})

# ── Health data endpoint ──────────────────────────────────────────────────────

@app.route("/api/health")
@require_api_key
def health_data():
    user = request.args.get("user", "user1")
    user = uuid.UUID(user)
    date = request.args.get("date")

    users = load_users()
    if not find_user(users, user):
        return jsonify({"error": "Unknown user. Add them first."}), 404

    if not date:
        return jsonify({"error": "Missing 'date' query param (YYYY-MM-DD)"}), 400
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    creds = get_credentials(user)
    if not creds:
        return jsonify({"error": "Not authenticated. Please sign in first."}), 401

    try:
        result = fetch_all(creds, date)
    except Exception as e:
        print(f"[health_data] error fetching for {user}/{date}: {e}")
        return jsonify({"error": "Failed to fetch health data"}), 500

    saved = save_health_data(user, date, result)
    result["_saved_to_db"] = saved
    return jsonify(result)

# ── Ops ─────────────────────────────────────────────────────────────────────

@app.route("/health")
def health_check():
    """Liveness check for the platform's deploy health check — not the same
    as /api/health (Fitbit data). Verifies Postgres is reachable too."""
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        return jsonify({"status": "ok"}), 200
    except psycopg.Error:
        return jsonify({"status": "degraded", "database": "unreachable"}), 503

# ── Serve frontend ────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    # api_key is injected into the page's JS so the dashboard's own fetch
    # calls to /api/health can authenticate. It's still visible to anyone
    # who can load this page (view-source, dev tools) — acceptable for a
    # private/internal dashboard, but don't expose this URL publicly.
    return render_template("index.html", api_key=API_KEY or "")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
