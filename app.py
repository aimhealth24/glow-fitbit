import os
import re
import json
from functools import wraps
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory, render_template
from flask_cors import CORS
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import requests
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__, static_folder="templates")

# ── Environment / config ───────────────────────────────────────────────────
# Secrets must come from real environment variables — no hardcoded fallbacks.
# The app fails fast at startup if something required is missing, rather
# than silently running with a bad default.

ENVIRONMENT = os.environ.get("ENVIRONMENT", "production")

MONGODB_URI = os.environ.get("MONGODB_URI")
if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI environment variable is required")

MONGODB_DB_NAME     = os.environ.get("MONGODB_DB", "aim_health")
MONGODB_COLLECTION  = os.environ.get("MONGODB_COLLECTION", "health_data")

API_KEY = os.environ.get("API_KEY")  # required for machine-to-machine calls to /api/health

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

# ── Mongo setup ───────────────────────────────────────────────────────────

_mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=3000)
_mongo_db = _mongo_client[MONGODB_DB_NAME]

health_collection      = _mongo_db[MONGODB_COLLECTION]
users_collection       = _mongo_db["users"]
tokens_collection      = _mongo_db["tokens"]
oauth_state_collection = _mongo_db["oauth_state"]


def _ensure_mongo_indexes():
    try:
        # One document per (user, date) — re-fetching a date updates it
        # instead of creating a duplicate.
        health_collection.create_index([("user_id", 1), ("date", 1)], unique=True)
        users_collection.create_index("id", unique=True)
        tokens_collection.create_index("user_id", unique=True)
        oauth_state_collection.create_index("state", unique=True)
        # Abandoned OAuth flows expire automatically after 10 minutes.
        oauth_state_collection.create_index("created_at", expireAfterSeconds=600)
    except PyMongoError as e:
        print(f"[mongo] Could not create index (will still try to write later): {e}")


_ensure_mongo_indexes()


def save_health_data(user_id, date, data):
    """Upsert one day's fetched health data for a user. Returns True on success."""
    doc = {
        "user_id": user_id,
        "date": date,
        "data": data,
        "fetched_at": datetime.utcnow(),
    }
    try:
        health_collection.update_one(
            {"user_id": user_id, "date": date},
            {"$set": doc},
            upsert=True,
        )
        return True
    except PyMongoError as e:
        print(f"[mongo] Failed to save health data for {user_id} on {date}: {e}")
        return False


SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
]
BASE_URL = "https://health.googleapis.com/v4/users/me"

# ── User registry (Mongo-backed) ────────────────────────────────────────────
# Users used to live in users.json on local disk. On Railway the filesystem
# is ephemeral, so every redeploy would wipe the registry and every stored
# OAuth token with it. Both now live in Mongo, which already backs the rest
# of this app.

def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return s or "user"


def make_initials(label):
    parts = [p for p in label.strip().split() if p]
    if not parts:
        return "U"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def load_users():
    return list(users_collection.find({}, {"_id": 0}))


def find_user(users, user_id):
    return next((u for u in users if u["id"] == user_id), None)


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_stored_token(user_id):
    doc = tokens_collection.find_one({"user_id": user_id})
    return doc["token"] if doc else None


def save_stored_token(user_id, creds):
    tokens_collection.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "token": json.loads(creds.to_json()), "updated_at": datetime.utcnow()}},
        upsert=True,
    )


def delete_stored_token(user_id):
    tokens_collection.delete_one({"user_id": user_id})


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

@app.route("/api/users", methods=["GET"])
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
def add_user():
    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "A name is required"}), 400
    if len(label) > 60:
        return jsonify({"error": "Name is too long"}), 400

    existing_ids = {u["id"] for u in load_users()}
    base_id = slugify(label)
    user_id = base_id
    i = 2
    while user_id in existing_ids:
        user_id = f"{base_id}_{i}"
        i += 1

    new_user = {
        "id": user_id,
        "label": label,
        "initials": make_initials(label),
    }
    try:
        users_collection.insert_one(dict(new_user))
    except PyMongoError as e:
        return jsonify({"error": f"Could not save user: {e}"}), 500

    return jsonify({**new_user, "authenticated": False}), 201


@app.route("/api/users/<user_id>", methods=["DELETE"])
def delete_user(user_id):
    users = load_users()
    u = find_user(users, user_id)
    if not u:
        return jsonify({"error": "User not found"}), 404

    users_collection.delete_one({"id": user_id})
    delete_stored_token(user_id)

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
def auth_start():
    user = request.args.get("user", "user1")
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

    oauth_state_collection.insert_one({
        "state": state,
        "client_id": client_id,
        "client_secret": client_secret,
        "token_uri": token_endpoint,
        "redirect_uri": redirect_uri,
        "user": user,
        "created_at": datetime.utcnow(),
    })

    return jsonify({"auth_url": auth_url})


@app.route("/callback")
def callback():
    from requests_oauthlib import OAuth2Session

    state = request.args.get("state")
    state_doc = oauth_state_collection.find_one({"state": state}) if state else None
    if not state_doc:
        return "State expired or invalid. Please restart sign-in from the dashboard.", 400

    oauth = OAuth2Session(
        state_doc["client_id"], scope=SCOPES,
        redirect_uri=state_doc["redirect_uri"], state=state,
    )
    token = oauth.fetch_token(
        state_doc["token_uri"],
        authorization_response=request.url,
        client_secret=state_doc["client_secret"],
        include_client_id=True,
    )
    creds = Credentials(
        token         = token.get("access_token"),
        refresh_token = token.get("refresh_token"),
        token_uri     = state_doc["token_uri"],
        client_id     = state_doc["client_id"],
        client_secret = state_doc["client_secret"],
        scopes        = SCOPES,
    )
    user = state_doc.get("user", "user1")
    save_stored_token(user, creds)
    oauth_state_collection.delete_one({"state": state})

    return """
    <html><body style="font-family:sans-serif;max-width:500px;margin:80px auto;text-align:center">
      <h2 style="color:#2e7d32">&#10003; Authenticated successfully!</h2>
      <p>You may close this tab and return to the dashboard.</p>
    </body></html>"""


@app.route("/api/auth/status")
def auth_status():
    user = request.args.get("user", "user1")
    creds = get_credentials(user)
    return jsonify({"authenticated": creds is not None})

# ── Health data endpoint ──────────────────────────────────────────────────────

@app.route("/api/health")
@require_api_key
def health_data():
    user = request.args.get("user", "user1")
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
    """Liveness check for Railway's deploy health check — not the same as
    /api/health (Fitbit data). Verifies Mongo is reachable too."""
    try:
        _mongo_client.admin.command("ping")
        return jsonify({"status": "ok"}), 200
    except PyMongoError:
        return jsonify({"status": "degraded", "mongo": "unreachable"}), 503

# ── Serve frontend ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", api_key=API_KEY or "")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
