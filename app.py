import os
import uuid
import json
import math
from functools import wraps
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
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
from flask_swagger_ui import get_swaggerui_blueprint

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

# The timezone used to decide which *calendar days* a request window touches.
# Day-driven metrics (resting HR, HRV, ...) and the health_snapshots cache are
# keyed by local day, so this must stay stable once you have data stored —
# changing it shifts the period_start/period_end keys of new snapshot rows.
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "America/Chicago")

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

# ── API docs (Swagger UI) ────────────────────────────────────────────────
# Serves an interactive docs page at /apidocs, reading the spec from
# static/openapi.yaml (served automatically since static_folder="static"
# above). Nothing here validates requests/responses against the spec —
# it's docs only, kept in sync by hand. If the two ever drift, the spec is
# wrong, not the app: trust app.py's actual behavior over the YAML.
SWAGGER_URL = "/apidocs"
API_SPEC_URL = "/static/openapi.yaml"
app.register_blueprint(
    get_swaggerui_blueprint(
        SWAGGER_URL,
        API_SPEC_URL,
        config={"app_name": "Health Dashboard API"},
    ),
    url_prefix=SWAGGER_URL,
)

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


# ── Snapshots (health_snapshots) ────────────────────────────────────────────
# health_snapshots is a *day-level* store: one row per user per local calendar
# day, keyed by (user_id, period_start, period_end) = (00:00:00, 23:59:59).
#
# What goes in it: ONLY day-driven metrics (resting HR, HRV, breathing rate,
# SpO2, skin temp, VO2 max) plus the stress score derived from them. These are
# valid regardless of what time window a caller asked for, and they are what
# the stress baseline reads back.
#
# What never goes in it: time-driven metrics (steps, distance, swim strokes,
# heart-rate stats, sleep sessions). Those are computed for the *requested
# window*, so a partial window (say 08:00-12:00) would otherwise be stored as
# if it were the whole day and silently corrupt that day's row.
#
# Writes are MERGES (jsonb ||) and never overwrite a value with null, so:
#   * a day fetched twice keeps its best-known values,
#   * a transient Google error (value comes back null) can't erase good data,
#   * older rows that still carry full-day fields (steps, sleep, ...) from the
#     previous version of this app are left intact.

def _period_bounds(date, tz_name=DEFAULT_TIMEZONE):
    """Returns the (period_start, period_end) timestamps used to key a day's
    row in health_snapshots:
        Start: YYYY-MM-DD 00:00:00
        End:   YYYY-MM-DD 23:59:59
    Shared by save_daily_snapshot (to write a day) and the stress-baseline
    lookup below (to window past days), so both agree on exactly what a
    "day" means for this table. Unchanged from the previous version so that
    existing rows keep matching on conflict.
    """
    tz = ZoneInfo(tz_name)
    period_start = datetime.strptime(date, "%Y-%m-%d").replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=tz,
    )
    period_end = period_start.replace(hour=23, minute=59, second=59, microsecond=0)
    return period_start, period_end


def save_daily_snapshot(user_id, date, data):
    """Merge one local day's day-driven metrics into health_snapshots.

    Returns "saved", "skipped" (nothing non-null to store) or "failed".
    """
    cleaned = {}
    for key, value in data.items():
        if value is None:
            continue
        if key == "stress" and isinstance(value, dict) and value.get("score") is None:
            continue  # don't overwrite a real earlier score with "insufficient history"
        cleaned[key] = value
    if not cleaned:
        return "skipped"

    try:
        period_start, period_end = _period_bounds(date)
        with db() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO health_snapshots (user_id, period_start, period_end, data, fetched_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (user_id, period_start, period_end)
                DO UPDATE SET
                    data = health_snapshots.data || EXCLUDED.data,
                    fetched_at = now()
                """,
                (user_id, period_start, period_end, Jsonb(cleaned)),
            )
        return "saved"
    except psycopg.Error as e:
        print(f"[db] Failed to save snapshot for {user_id} on {date}: {e}")
        return "failed"


SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]
BASE_URL = "https://health.googleapis.com/v4/users/me"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
HTTP_TIMEOUT = 30  # seconds, per Google Health API request

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
        cur.execute("SELECT id, label, initials, email FROM users ORDER BY created_at")
        return cur.fetchall()


def find_user(users, user_id):
    return next((u for u in users if u["id"] == user_id), None)


def find_user_id_by_email(email):
    """Case-insensitive lookup. users.email is only populated once the user
    completes Google sign-in (see /callback)."""
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM users WHERE lower(email) = lower(%s) ORDER BY created_at LIMIT 1",
            (email,),
        )
        row = cur.fetchone()
        return row["id"] if row else None


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


# ── Small parsing helpers ─────────────────────────────────────────────────────

def _to_int(v, default=0):
    """Google returns int64 fields as JSON strings (e.g. "407"); this accepts
    either form. Returns `default` if the value is missing or not numeric."""
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _utc_iso(dt):
    """RFC 3339 UTC string with a trailing Z — the only form the physical-time
    filters and rollUp ranges need."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _civil_date_str(d):
    """{"year": 2026, "month": 10, "day": 12} -> "2026-10-12" (None if unusable)."""
    try:
        return f"{int(d['year']):04d}-{int(d['month']):02d}-{int(d['day']):02d}"
    except (TypeError, KeyError, ValueError):
        return None


def _parse_timestamp(value, tz_name=DEFAULT_TIMEZONE):
    """Accepts a Unix timestamp (seconds or milliseconds, as a number or numeric
    string) or an ISO 8601 string. Returns a timezone-aware datetime in `tz_name`.
    Naive ISO strings are interpreted as `tz_name`. Raises ValueError if unparseable."""
    tz = ZoneInfo(tz_name)

    if isinstance(value, bool) or value is None:
        raise ValueError("invalid timestamp")

    if isinstance(value, str):
        s = value.strip()
        try:
            value = float(s)
        except ValueError:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))  # raises ValueError
            return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)

    if isinstance(value, (int, float)):
        seconds = value / 1000 if abs(value) > 1e11 else value  # ms vs s
        try:
            return datetime.fromtimestamp(seconds, tz=tz)
        except (OverflowError, OSError, ValueError):
            raise ValueError("timestamp out of range")

    raise ValueError("invalid timestamp")


# ── Google Health API client helpers ──────────────────────────────────────────
# Every call goes through _gh_request, which raises GoogleHealthError on any
# non-200 or network failure. The previous version swallowed errors and
# returned [] / 0, which made "Google failed" indistinguishable from "no data".
# Callers now isolate failures per metric (see fetch_time_driven /
# fetch_day_driven) and report them in the response's "errors" object.

class GoogleHealthError(Exception):
    def __init__(self, status, detail=""):
        msg = "Google Health API request failed" if not status else f"Google Health API error {status}"
        super().__init__(msg)
        self.status = status
        self.detail = detail


def _gh_request(method, url, creds, *, params=None, json_body=None):
    try:
        resp = requests.request(
            method, url,
            headers=get_headers(creds),
            params=params,
            json=json_body,
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"[google-health] {method} {url} failed: {e}")
        raise GoogleHealthError(0, str(e)) from e
    if resp.status_code != 200:
        print(f"[google-health] {method} {url} -> {resp.status_code}: {resp.text[:500]}")
        raise GoogleHealthError(resp.status_code, resp.text[:500])
    return resp.json()


def _iter_pages(creds, data_type, filter_str, page_size):
    """Yields one list of dataPoints per page of a `list` call. Streaming (rather
    than collecting everything) keeps memory flat for high-volume types like
    heart-rate (1-second resolution)."""
    url = f"{BASE_URL}/dataTypes/{data_type}/dataPoints"
    page_token = None
    while True:
        params = {"filter": filter_str, "pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token
        body = _gh_request("GET", url, creds, params=params)
        yield body.get("dataPoints", [])
        page_token = body.get("nextPageToken")
        if not page_token:
            break


def list_points(creds, data_type, filter_str, page_size=25):
    points = []
    for page in _iter_pages(creds, data_type, filter_str, page_size):
        points.extend(page)
    return points


def list_by_date(creds, data_type, filter_name, date):
    next_day = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    filter_str = f'{filter_name}.date >= "{date}" AND {filter_name}.date < "{next_day}"'
    return list_points(creds, data_type, filter_str)


def list_by_date_range(creds, data_type, filter_name, start_date, end_date):
    """[start_date, end_date) in one call instead of one request per day.
    Used for batched day-driven fetches and by the stress-baseline fallback.

    data_type is the hyphenated API path segment (e.g. "daily-heart-rate-
    variability"); filter_name is the underscored field name the API expects
    inside the filter expression (e.g. "daily_heart_rate_variability").
    """
    filter_str = f'{filter_name}.date >= "{start_date}" AND {filter_name}.date < "{end_date}"'
    return list_points(creds, data_type, filter_str, page_size=1000)


def _rollup_buckets(creds, data_type, start_dt, end_dt):
    """rollUp over the exact physical window [start_dt, end_dt) as ONE bucket
    (windowSize == window length). Interval types only (steps, distance,
    swim-lengths-data). Max range is 90 days for these types."""
    url = f"{BASE_URL}/dataTypes/{data_type}/dataPoints:rollUp"
    window_seconds = int((end_dt - start_dt).total_seconds())
    body = {
        "range": {"startTime": _utc_iso(start_dt), "endTime": _utc_iso(end_dt)},
        "windowSize": f"{window_seconds}s",
    }
    buckets = []
    while True:
        resp = _gh_request("POST", url, creds, json_body=body)
        buckets.extend(resp.get("rollupDataPoints", []))
        token = resp.get("nextPageToken")
        if not token:
            break
        body["pageToken"] = token
    return buckets


def _sum_bucket_field(buckets, payload_key, field):
    """Sums `field` across buckets. Returns None when no bucket carried a value —
    Google leaves the value unset when there was no on-wrist data, which is
    different from a real zero."""
    total = None
    for b in buckets:
        v = _to_float((b.get(payload_key) or {}).get(field))
        if v is not None:
            total = (total or 0.0) + v
    return total


# ── Fetchers ──────────────────────────────────────────────────────────────────
#
# Every metric is either TIME-DRIVEN or DAY-DRIVEN, per the Google Health API
# data types table (record type + available operations):
#
#   TIME-DRIVEN  (honours the exact start/end instants)
#     steps              Interval  -> rollUp over the window
#     distance           Interval  -> rollUp over the window
#     swim-lengths-data  Interval  -> rollUp over the window
#     heart-rate         Sample    -> list, physical_time filter, stats computed here
#     sleep              Session   -> list, interval.end_time filter (whole sessions
#                                     whose END falls inside the window)
#
#   DAY-DRIVEN   (one value per local calendar day; list/reconcile by .date only)
#     daily-resting-heart-rate, daily-heart-rate-variability,
#     daily-respiratory-rate, daily-oxygen-saturation,
#     daily-sleep-temperature-derivations, daily-vo2-max
#     + stress, derived from the daily metrics above
#
# Day-driven values can't be narrowed to a time window, so for a window we
# return the value for every local day the window touches.

# (output key, API data type, filter name, payload key, value field)
DAILY_METRICS = [
    ("resting_hr",     "daily-resting-heart-rate",            "daily_resting_heart_rate",            "dailyRestingHeartRate",           "beatsPerMinute"),
    ("hrv",            "daily-heart-rate-variability",        "daily_heart_rate_variability",        "dailyHeartRateVariability",       "averageHeartRateVariabilityMilliseconds"),
    ("breathing_rate", "daily-respiratory-rate",              "daily_respiratory_rate",              "dailyRespiratoryRate",            "breathsPerMinute"),
    ("spo2",           "daily-oxygen-saturation",             "daily_oxygen_saturation",             "dailyOxygenSaturation",           "averagePercentage"),
    ("skin_temp",      "daily-sleep-temperature-derivations", "daily_sleep_temperature_derivations", "dailySleepTemperatureDerivations", "nightlyTemperatureCelsius"),
    ("vo2max",         "daily-vo2-max",                       "daily_vo2_max",                       "dailyVo2Max",                     "vo2MaxMlPerMinPerKg"),
]
DAILY_KEYS = [m[0] for m in DAILY_METRICS]
TIME_DRIVEN_KEYS = ["steps", "distance", "swim", "heart_rate", "sleep"]  # error-key names


def _heart_rate_stats(creds, start_dt, end_dt):
    s, e = _utc_iso(start_dt), _utc_iso(end_dt)
    filter_str = (
        f'heart_rate.sample_time.physical_time >= "{s}" '
        f'AND heart_rate.sample_time.physical_time < "{e}"'
    )
    count, total, lo, hi = 0, 0, None, None
    for page in _iter_pages(creds, "heart-rate", filter_str, 10000):
        for p in page:
            bpm = _to_int((p.get("heartRate") or {}).get("beatsPerMinute"), 0)
            if not bpm:
                continue
            count += 1
            total += bpm
            lo = bpm if lo is None else min(lo, bpm)
            hi = bpm if hi is None else max(hi, bpm)
    return {
        "avg": round(total / count) if count else None,
        "min": lo,
        "max": hi,
        "samples": count,
    }


def _parse_sleep_session(point):
    s = point.get("sleep") or {}
    summary = s.get("summary") or {}
    interval = s.get("interval") or {}
    # Only a main sleep gets a score: the regression behind calculate_sleep_score
    # was fit on full nights and would rate a 20-minute nap as "Poor".
    is_main = bool((s.get("metadata") or {}).get("main", True))
    return {
        "start_time": interval.get("startTime"),
        "end_time": interval.get("endTime"),
        "type": s.get("type"),
        "main": is_main,
        "minutes_asleep": _to_int(summary.get("minutesAsleep")),
        "minutes_awake": _to_int(summary.get("minutesAwake")),
        "minutes_in_bed": _to_int(summary.get("minutesInSleepPeriod")),
        "stages": {
            st["type"]: _to_int(st.get("minutes"))
            for st in summary.get("stagesSummary", []) if st.get("type")
        },
        "sleep_score": calculate_sleep_score(summary) if (is_main and summary) else None,
    }


def _sleep_sessions(creds, start_dt, end_dt):
    s, e = _utc_iso(start_dt), _utc_iso(end_dt)
    filter_str = f'sleep.interval.end_time >= "{s}" AND sleep.interval.end_time < "{e}"'
    points = list_points(creds, "sleep", filter_str, page_size=25)
    sessions = [_parse_sleep_session(p) for p in points]
    sessions.sort(key=lambda x: x["end_time"] or "")
    return sessions


def fetch_time_driven(creds, start_dt, end_dt):
    """All time-driven metrics for the exact window [start_dt, end_dt).
    Returns (metrics, errors). A failure in one metric is recorded in `errors`
    and leaves that metric as None; it does not abort the others."""
    errors = {}

    def guard(name, fn):
        try:
            return fn()
        except GoogleHealthError as e:
            errors[name] = str(e)
            return None

    steps = guard("steps", lambda: _sum_bucket_field(
        _rollup_buckets(creds, "steps", start_dt, end_dt), "steps", "countSum"))

    mm = guard("distance", lambda: _sum_bucket_field(
        _rollup_buckets(creds, "distance", start_dt, end_dt), "distance", "millimetersSum"))

    strokes = guard("swim", lambda: _sum_bucket_field(
        _rollup_buckets(creds, "swim-lengths-data", start_dt, end_dt), "swimLengthsData", "strokeCountSum"))

    metrics = {
        "steps": int(steps) if steps is not None else None,
        "distance_miles": round(mm / 1_609_344, 2) if mm is not None else None,
        "swim_strokes": int(strokes) if strokes else None,
        "heart_rate": guard("heart_rate", lambda: _heart_rate_stats(creds, start_dt, end_dt)),
        "sleep": guard("sleep", lambda: _sleep_sessions(creds, start_dt, end_dt)),
    }
    return metrics, errors


def _fetch_daily_metric(creds, spec, dates):
    """One day-driven metric for every date in `dates` (sorted 'YYYY-MM-DD').
    Uses a single range query, then groups by each point's own `date`. If a
    point ever comes back without a usable date, falls back to one query per
    day so values can't be attributed to the wrong day."""
    _, data_type, filter_name, payload_key, value_key = spec
    wanted = set(dates)
    end_exclusive = (datetime.strptime(dates[-1], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    values, undated = {}, False
    for p in list_by_date_range(creds, data_type, filter_name, dates[0], end_exclusive):
        payload = p.get(payload_key) or {}
        d = _civil_date_str(payload.get("date"))
        if d is None:
            undated = True
            break
        if d in wanted and d not in values:
            v = _to_float(payload.get(value_key))
            if v is not None:
                values[d] = v

    if undated:
        values = {}
        for d in dates:
            pts = list_by_date(creds, data_type, filter_name, d)
            if pts:
                v = _to_float((pts[0].get(payload_key) or {}).get(value_key))
                if v is not None:
                    values[d] = v

    return {d: values.get(d) for d in dates}


def fetch_day_driven(creds, dates):
    """All day-driven metrics (pre-stress) for each local date in `dates`.
    Returns ({metric: {date: value}}, errors)."""
    errors = {}
    out = {}
    for spec in DAILY_METRICS:
        key = spec[0]
        try:
            out[key] = _fetch_daily_metric(creds, spec, dates)
        except GoogleHealthError as e:
            errors[f"daily.{key}"] = str(e)
            out[key] = {d: None for d in dates}
    return out, errors


def _local_dates(start_dt, end_dt, tz_name=DEFAULT_TIMEZONE):
    """Every local calendar day touched by [start_dt, end_dt) as 'YYYY-MM-DD'."""
    tz = ZoneInfo(tz_name)
    first = start_dt.astimezone(tz).date()
    last = (end_dt.astimezone(tz) - timedelta(seconds=1)).date()  # end is exclusive
    return [(first + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((last - first).days + 1)]


def fetch_all(creds, user_id, start_dt, end_dt, tz_name=DEFAULT_TIMEZONE):
    """Fetch everything for the window [start_dt, end_dt).

    Returns {"window", "windowed", "daily", "errors"}:
      windowed — time-driven metrics, exact to the requested window
      daily    — one entry per local day touched: the day-driven metrics, the
                 derived stress score, and what happened to that day's snapshot
      errors   — per-metric failures (a failed metric is null, not a failed request)

    Snapshots: only the `daily` entries are persisted (see "Snapshots" above).
    Days are processed oldest-first and each snapshot is saved immediately, so
    later days in the same request can use earlier ones as stress baseline.
    """
    dates = _local_dates(start_dt, end_dt, tz_name)

    windowed, errors = fetch_time_driven(creds, start_dt, end_dt)
    daily_map, daily_errors = fetch_day_driven(creds, dates)
    errors.update(daily_errors)

    daily = []
    for d in dates:
        metrics = {key: daily_map[key][d] for key in DAILY_KEYS}
        try:
            stress = calculate_stress_score(
                user_id, d,
                hrv=metrics["hrv"],
                resting_hr=metrics["resting_hr"],
                breathing_rate=metrics["breathing_rate"],
                creds=creds,
            )
        except Exception as e:  # stress is derived; never let it sink the response
            print(f"[health_data] stress score failed for {user_id}/{d}: {e}")
            stress = {"score": None, "category": "Unavailable"}

        snapshot = save_daily_snapshot(user_id, d, {**metrics, "stress": stress})
        daily.append({"date": d, **metrics, "stress": stress, "snapshot": snapshot})

    return {
        "window": {
            "start_utc": _utc_iso(start_dt),
            "end_utc": _utc_iso(end_dt),
            "timezone": tz_name,
            "days": dates,
        },
        "windowed": windowed,
        "daily": daily,
        "errors": errors,
    }


def calculate_sleep_score(summary: dict) -> dict:
    """
    Estimates the Google Health sleep score from a `summary` object.

    Model: score ≈ 0.165 * minutes_asleep - 0.108 * minutes_awake + 18.17
    Fit via linear regression against 4 real (summary, actual_score) examples.

    NOTE: This is a statistical approximation, not Google's real (proprietary,
    personalized) algorithm.
    """
    stages = {e.get("type"): _to_int(e.get("minutes")) for e in summary.get("stagesSummary", [])}

    minutes_asleep = _to_int(
        summary.get("minutesAsleep"),
        default=stages.get("LIGHT", 0) + stages.get("DEEP", 0) + stages.get("REM", 0),
    )
    minutes_awake = _to_int(summary.get("minutesAwake"), default=stages.get("AWAKE", 0))

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


# ── Stress score ──────────────────────────────────────────────────────────
# HRV and resting heart rate are well-established stress/recovery correlates
# in the literature, but their *absolute* values vary hugely person to
# person — a resting HRV of 30ms is normal for one person and a red flag for
# another. So rather than using population thresholds, this compares each
# day against that same user's own trailing baseline.
#
# Baseline is read from Postgres (health_snapshots) first, since that's a
# cache of exactly the daily values we'd otherwise re-fetch from Google. If
# the DB doesn't have enough *recent* history for this user yet (new user,
# gap in syncing), and Google credentials are available, we fall back to
# pulling the trailing window straight from the Google Health API for that
# one request. Every /api/health call saves its days' snapshots as a side
# effect (see fetch_all), so the DB fills in naturally and the API fallback
# stops firing once enough days have accumulated.

BASELINE_WINDOW_DAYS = 14
MIN_BASELINE_DAYS = 5  # below this, we don't have enough history to trust a personal baseline


def _fetch_baseline_from_api(creds, before_date, days=BASELINE_WINDOW_DAYS):
    """Pulls up to `days` of trailing HRV / resting-HR history directly from
    Google Health, for the window [before_date - days, before_date).
    Used only as a fallback when Postgres doesn't have enough recent rows —
    see _fetch_baseline_metrics below."""
    start_date = (datetime.strptime(before_date, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")

    hrv_pts = list_by_date_range(
        creds, "daily-heart-rate-variability", "daily_heart_rate_variability", start_date, before_date
    )
    hrv_hist = [
        p.get("dailyHeartRateVariability", {}).get("averageHeartRateVariabilityMilliseconds")
        for p in hrv_pts
    ]
    hrv_hist = [float(v) for v in hrv_hist if v is not None]

    rhr_pts = list_by_date_range(
        creds, "daily-resting-heart-rate", "daily_resting_heart_rate", start_date, before_date
    )
    rhr_hist = [p.get("dailyRestingHeartRate", {}).get("beatsPerMinute") for p in rhr_pts]
    rhr_hist = [float(v) for v in rhr_hist if v is not None]

    return hrv_hist, rhr_hist


def _fetch_baseline_metrics(user_id, before_date, creds=None, days=BASELINE_WINDOW_DAYS):
    """Returns the hrv/resting_hr history to compare `before_date` against:
    up to `days` most recent stored health_snapshots rows strictly before
    `before_date`'s period, and no older than `days` days back — a genuine
    trailing window, never including the day being scored itself, and never
    silently reaching back months just to find enough rows.

    If Postgres doesn't have enough recent history for either metric and
    `creds` is provided, falls back to fetching that metric's window
    straight from the Google Health API instead of under-reporting."""
    period_start, _ = _period_bounds(before_date)
    window_start = period_start - timedelta(days=days)

    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT data FROM health_snapshots
            WHERE user_id = %s
              AND period_start < %s
              AND period_start >= %s
            ORDER BY period_start DESC
            LIMIT %s
            """,
            (user_id, period_start, window_start, days),
        )
        rows = cur.fetchall()
    hrv_hist = [float(r["data"].get("hrv")) for r in rows if r["data"].get("hrv") is not None]
    rhr_hist = [float(r["data"].get("resting_hr")) for r in rows if r["data"].get("resting_hr") is not None]

    if creds and (len(hrv_hist) < MIN_BASELINE_DAYS or len(rhr_hist) < MIN_BASELINE_DAYS):
        try:
            api_hrv, api_rhr = _fetch_baseline_from_api(creds, before_date, days)
        except Exception as e:
            print(f"[stress] API baseline fallback failed for {user_id}: {e}")
            api_hrv, api_rhr = [], []
        if len(hrv_hist) < MIN_BASELINE_DAYS:
            hrv_hist = api_hrv
        if len(rhr_hist) < MIN_BASELINE_DAYS:
            rhr_hist = api_rhr

    return hrv_hist, rhr_hist


def _mean_std(values):
    """Sample mean and standard deviation. Returns (None, None) if there
    isn't enough history to compute a meaningful baseline."""
    if len(values) < MIN_BASELINE_DAYS:
        return None, None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return mean, variance ** 0.5


def calculate_stress_score(user_id, date, hrv, resting_hr, breathing_rate, creds=None):
    """
    Estimates a same-day physiological stress score (0-100, higher = more
    stress) by comparing today's HRV and resting heart rate against this
    user's own trailing 14-day baseline, with breathing rate as a smaller
    supporting signal.

    `creds`, if provided, lets the baseline lookup fall back to the Google
    Health API when Postgres doesn't have enough recent history yet (see
    _fetch_baseline_metrics).

    NOTE: This is a heuristic approximation, not a validated clinical
    measure — it flags days that look unusual FOR THIS PERSON, not a
    diagnosis. Returns score=None until at least MIN_BASELINE_DAYS of prior
    history exist, rather than guessing from population norms that don't
    transfer well between individuals (especially for HRV).
    """
    # Nothing to score yet (e.g. today, before the overnight data has synced).
    if hrv is None and resting_hr is None:
        return {"score": None, "category": "No data", "days_of_baseline": 0}

    hrv_hist, rhr_hist = _fetch_baseline_metrics(user_id, date, creds=creds)
    hrv_mean, hrv_std = _mean_std(hrv_hist)
    rhr_mean, rhr_std = _mean_std(rhr_hist)
    baseline_days = max(len(hrv_hist), len(rhr_hist))

    contributions = []  # (z_score, weight) — positive z always means "more stress"

    if hrv_std and hrv is not None:
        # Lower HRV than this person's own baseline => more stress.
        contributions.append(((hrv_mean - hrv) / hrv_std, 0.50))
    if rhr_std and resting_hr is not None:
        # Higher resting HR than this person's own baseline => more stress.
        contributions.append(((resting_hr - rhr_mean) / rhr_std, 0.35))

    # Without a usable personal baseline for HRV/RHR there's nothing to anchor
    # the score to; breathing rate alone is not enough.
    if not contributions:
        return {"score": None, "category": "Insufficient history", "days_of_baseline": baseline_days}

    if breathing_rate is not None:
        # No personal baseline tracked for this one — a light, population-level
        # nudge (typical resting adult range is ~12-20 breaths/min) rather
        # than a full z-score contribution.
        contributions.append((max(0.0, (breathing_rate - 16) / 4), 0.15))

    weighted_z = sum(z * w for z, w in contributions) / sum(w for _, w in contributions)

    # Squash the unbounded, dampened z-score into 0-100, centered on 50
    # ("a typical day for this person"). The /1.5 damping keeps a single
    # 1-sigma deviation from swinging the score too aggressively.
    score = round(100 / (1 + math.exp(-weighted_z / 1.5)))
    score = max(0, min(100, score))

    if score >= 75:
        category = "High"
    elif score >= 55:
        category = "Elevated"
    elif score >= 30:
        category = "Typical"
    else:
        category = "Low"

    return {"score": score, "category": category, "days_of_baseline": baseline_days}


# ── /api/health request limits ────────────────────────────────────────────
# A request is one time window. Limits:
#   * MIN_WINDOW_SECONDS — steps/distance are stored at 1-minute resolution, so
#     rollups over a shorter window are not meaningful.
#   * MAX_WINDOW_DAYS    — bounds the work per request (heart-rate `list` is
#     high volume; day-driven metrics cost ~6 range queries). Also keeps us
#     inside Google's 90-day rollUp range limit for steps/distance/swim.
MIN_WINDOW_SECONDS = 60
MAX_WINDOW_DAYS = 31


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
            "email": u["email"],  # None until the user completes Google sign-in
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

    return jsonify({**new_user, "email": None, "authenticated": False}), 201


@app.route("/api/users/<user_id>", methods=["DELETE"])
@login_required
def delete_user(user_id):
    users = load_users()
    u = find_user(users, uuid.UUID(user_id))
    if not u:
        return jsonify({"error": "User not found"}), 404

    with db() as conn, conn.cursor() as cur:
        # ON DELETE CASCADE on tokens/oauth_state/health_snapshots/health_readings
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

@app.route("/api/health", methods=["POST"])
@require_api_key
def health_data():
    """POST /api/health
    {"user_email": "...", "start_time": <ts>, "end_time": <ts>}

    start_time / end_time: Unix timestamp (seconds or milliseconds) or ISO 8601.
    The window is [start_time, end_time) — end exclusive — truncated to whole
    seconds. Time-driven metrics are computed for exactly that window; day-driven
    metrics are returned per local day the window touches. See fetch_all.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    user_email = body.get("user_email")
    start_raw = body.get("start_time")
    end_raw = body.get("end_time")

    if not isinstance(user_email, str) or not user_email.strip():
        return jsonify({"error": "'user_email' is required"}), 400
    if start_raw is None or end_raw is None:
        return jsonify({"error": "'start_time' and 'end_time' are required"}), 400

    try:
        start_dt = _parse_timestamp(start_raw).replace(microsecond=0)
        end_dt = _parse_timestamp(end_raw).replace(microsecond=0)
    except ValueError:
        return jsonify({"error": "Invalid 'start_time'/'end_time' — use a Unix timestamp or ISO 8601 string"}), 400

    if end_dt <= start_dt:
        return jsonify({"error": "'end_time' must be after 'start_time'"}), 400
    window = end_dt - start_dt
    if window < timedelta(seconds=MIN_WINDOW_SECONDS):
        return jsonify({"error": f"Window too small — minimum is {MIN_WINDOW_SECONDS} seconds"}), 400
    if window > timedelta(days=MAX_WINDOW_DAYS):
        return jsonify({
            "error": f"Window too large — max {MAX_WINDOW_DAYS} days per request. Split into smaller requests."
        }), 400

    user = find_user_id_by_email(user_email.strip())
    if not user:
        return jsonify({"error": "No user found for that email"}), 404

    creds = get_credentials(user)
    if not creds:
        return jsonify({"error": "Not authenticated. Please sign in first."}), 401

    try:
        result = fetch_all(creds, user, start_dt, end_dt)
    except psycopg.Error as e:
        print(f"[health_data] database error for {user}: {e}")
        return jsonify({"error": "Failed to fetch health data"}), 500
    except Exception as e:
        print(f"[health_data] unexpected error for {user}: {e}")
        return jsonify({"error": "Failed to fetch health data"}), 500

    # Every single metric failed (revoked token, Google outage, ...): report it
    # as a failure instead of a 200 full of nulls.
    if len(result["errors"]) >= len(TIME_DRIVEN_KEYS) + len(DAILY_KEYS):
        return jsonify({"error": "Failed to fetch health data", "errors": result["errors"]}), 502

    return jsonify({
        "user_email": user_email.strip(),
        "start_time": start_raw,
        "end_time": end_raw,
        **result,
    })

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