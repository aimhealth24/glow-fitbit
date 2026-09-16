--- Updated Schema
-- Database: fitbit_data

-- DROP DATABASE IF EXISTS fitbit_data;

CREATE TABLE users (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label      TEXT NOT NULL,
    initials   TEXT NOT NULL,
    email      TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tokens (
    user_id    UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    token      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE oauth_state (
    state         TEXT PRIMARY KEY,
    client_id     TEXT NOT NULL,
    client_secret TEXT NOT NULL,
    token_uri     TEXT NOT NULL,
    redirect_uri  TEXT NOT NULL,
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_oauth_state_created_at
    ON oauth_state (created_at);

CREATE TABLE health_snapshots (
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    period_start  TIMESTAMPTZ NOT NULL,
    period_end    TIMESTAMPTZ NOT NULL,
    data          JSONB NOT NULL,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (user_id, period_start, period_end),

    CONSTRAINT health_snapshots_period_check
        CHECK (period_end > period_start)
);

CREATE TABLE health_readings (
    id           BIGSERIAL,
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    metric_type  TEXT NOT NULL,
    recorded_at  TIMESTAMPTZ NOT NULL,
    value_data   JSONB NOT NULL,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (id, recorded_at),
    UNIQUE (user_id, metric_type, recorded_at)
) PARTITION BY RANGE (recorded_at);


-- Index propagates to partitions.
CREATE INDEX idx_health_readings_lookup
    ON health_readings (
        user_id,
        metric_type,
        recorded_at DESC
    );


-- ── Partition maintenance ────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION create_health_readings_partition(
    for_month DATE
)
RETURNS void AS $$
DECLARE
    partition_name TEXT :=
        'health_readings_' || to_char(for_month, 'YYYY_MM');

    range_start DATE :=
        date_trunc('month', for_month)::date;

    range_end DATE :=
        (date_trunc('month', for_month) + INTERVAL '1 month')::date;
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class
        WHERE relname = partition_name
    ) THEN

        EXECUTE format(
            'CREATE TABLE %I PARTITION OF health_readings
             FOR VALUES FROM (%L) TO (%L)',
            partition_name,
            range_start,
            range_end
        );

    END IF;
END;
$$ LANGUAGE plpgsql;


-- ── Initial partitions ──────────────────────────────────────────────────
-- Current month + next two months.
SELECT create_health_readings_partition(
    date_trunc('month', now())::date
);

SELECT create_health_readings_partition(
    (date_trunc('month', now()) + INTERVAL '1 month')::date
);

SELECT create_health_readings_partition(
    (date_trunc('month', now()) + INTERVAL '2 months')::date
);





-- Health Dashboard schema (Postgres)
-- Run this once against your new Azure Database for PostgreSQL server.
-- Safe to run top-to-bottom on a fresh database.

-- ── users ────────────────────────────────────────────────────────────────
CREATE TABLE users (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label      TEXT NOT NULL,
    initials   TEXT NOT NULL,
    email      TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── tokens ───────────────────────────────────────────────────────────────
-- One row per user. Mirrors the old Mongo "tokens" collection: the whole
-- Google credentials object (access_token, refresh_token, token_uri,
-- client_id, client_secret, scopes) stored as JSON.
CREATE TABLE tokens (
    user_id    TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    token      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── oauth_state ──────────────────────────────────────────────────────────
-- Short-lived, single-use rows for in-progress Google sign-ins.
-- No native TTL in Postgres — expiry is enforced at read time in the app
-- (WHERE created_at > now() - interval '10 minutes'), and this index keeps
-- that lookup and any periodic cleanup cheap.
CREATE TABLE oauth_state (
    state        TEXT PRIMARY KEY,
    client_id    TEXT NOT NULL,
    client_secret TEXT NOT NULL,
    token_uri    TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_oauth_state_created_at ON oauth_state (created_at);

-- ── health_daily ─────────────────────────────────────────────────────────
-- One row per user per day — same shape as today's /api/health response.
-- Keeps that endpoint working close to unchanged.
CREATE TABLE health_daily (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date       DATE NOT NULL,
    data       JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, date)
);

-- ── health_readings ──────────────────────────────────────────────────────
-- New time-series table for intraday polling (e.g. steps/heart rate every
-- 5 minutes). Partitioned by month on recorded_at so old data can be
-- archived or dropped cheaply later, and so indexes stay small and fast
-- as this grows into millions of rows.
CREATE TABLE health_readings (
    id           BIGSERIAL,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    metric_type  TEXT NOT NULL,           -- e.g. 'steps', 'heart_rate'
    recorded_at  TIMESTAMPTZ NOT NULL,    -- when the reading occurred
    value_data   JSONB NOT NULL,          -- the actual metric payload
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, recorded_at),
    UNIQUE (user_id, metric_type, recorded_at)
) PARTITION BY RANGE (recorded_at);

-- Index propagates automatically to every partition (Postgres 11+).
CREATE INDEX idx_health_readings_lookup
    ON health_readings (user_id, metric_type, recorded_at DESC);

-- ── Partition maintenance ───────────────────────────────────────────────
-- Creates a partition covering [start_of_month, start_of_next_month) for
-- the given month. Safe to call again for a month that already has a
-- partition (does nothing).
CREATE OR REPLACE FUNCTION create_health_readings_partition(for_month DATE)
RETURNS void AS $$
DECLARE
    partition_name TEXT := 'health_readings_' || to_char(for_month, 'YYYY_MM');
    range_start    DATE := date_trunc('month', for_month);
    range_end      DATE := range_start + INTERVAL '1 month';
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = partition_name) THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF health_readings FOR VALUES FROM (%L) TO (%L)',
            partition_name, range_start, range_end
        );
    END IF;
END;
$$ LANGUAGE plpgsql;

-- Create partitions for the current month and the next two, so there's
-- headroom before you need to think about this again.
SELECT create_health_readings_partition(date_trunc('month', now())::date);
SELECT create_health_readings_partition((date_trunc('month', now()) + INTERVAL '1 month')::date);
SELECT create_health_readings_partition((date_trunc('month', now()) + INTERVAL '2 months')::date);

-- To add another month later (e.g. from a monthly cron job or the ingestion
-- worker itself), just call:
--   SELECT create_health_readings_partition('2026-12-01');