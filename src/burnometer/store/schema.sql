-- burn-o-meter store schema, version 1.
--
-- Design notes that are load-bearing:
--   * event_key is the PRIMARY KEY, so INSERT OR IGNORE gives idempotent
--     rescans for free. Claude Code writes the same message up to 7 times.
--   * input_tokens is UNCACHED input only. Cache reads live in their own
--     column. Adapters normalise; nothing downstream re-checks.
--   * reasoning_tokens is a display-only SUBSET of output_tokens. Never add it
--     to a total.
--   * cache writes are split by TTL because they bill at different multipliers
--     (1.25x base input for 5m, 2.0x for 1h). This split is the single largest
--     accuracy difference between this tool and every other one.
--   * cost_usd is NULL when unpriced. It is never 0.0 as a stand-in, because
--     "free" and "unknown" are different facts.

CREATE TABLE IF NOT EXISTS usage_events (
    event_key    TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,
    session_id   TEXT,
    project      TEXT,
    git_branch   TEXT,
    model        TEXT NOT NULL,
    model_family TEXT NOT NULL,
    effort       TEXT,
    -- Who served the tokens, when the tool is not the provider. NULL for tools
    -- that are their own provider; 'ollama' or 'openai' for a router like
    -- OpenCode. Decides unpriced (rate unknown) from not_metered (no rate).
    upstream_provider TEXT,
    ts           TEXT NOT NULL,

    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,

    cost_usd     REAL,
    cost_basis   TEXT NOT NULL
                 CHECK (cost_basis IN ('api_billed', 'api_equivalent',
                                       'unpriced', 'not_metered')),
    price_source TEXT,

    raw_file     TEXT,
    raw_line     INTEGER,

    CHECK (cost_usd IS NULL OR cost_usd >= 0),
    CHECK (input_tokens >= 0 AND output_tokens >= 0 AND reasoning_tokens >= 0),
    CHECK (cache_read_tokens >= 0),
    CHECK (cache_write_5m_tokens >= 0 AND cache_write_1h_tokens >= 0),
    -- A row carries a cost exactly when one applies. 'unpriced' means the rate
    -- is unknown; 'not_metered' means there is no per-token rate at all (local
    -- inference, or a plan-included model). Neither may claim $0.00.
    CHECK ((cost_basis IN ('unpriced', 'not_metered')) = (cost_usd IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_events_ts          ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_events_provider_ts ON usage_events(provider, ts);
CREATE INDEX IF NOT EXISTS idx_events_model_ts    ON usage_events(model, ts);
CREATE INDEX IF NOT EXISTS idx_events_project     ON usage_events(project);

-- Incremental scan bookkeeping. A file is re-read from zero when its inode
-- changes or its size regresses below the recorded offset, which covers both
-- log rotation and truncation.
--
-- Keyed by a hash of the absolute path, never the path itself: Claude Code
-- names project directories after the full working directory, so storing paths
-- here would leak the account name and client name that privacy.project_paths
-- is meant to strip. Files are rediscovered by glob each scan, so the real path
-- is never needed from storage. path_label holds the bare filename (a session
-- UUID) purely so `doctor` has something to show.
CREATE TABLE IF NOT EXISTS scan_state (
    path_key   TEXT PRIMARY KEY,
    path_label TEXT,
    inode      INTEGER,
    size       INTEGER,
    mtime_ns   INTEGER,
    offset     INTEGER NOT NULL DEFAULT 0,
    last_scan  TEXT,
    CHECK (offset >= 0)
);

-- Rate-limit readings. source='exact' means the provider reported it;
-- 'estimated' means we derived it locally and every display must say so.
CREATE TABLE IF NOT EXISTS quota_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    provider       TEXT NOT NULL,
    window_name    TEXT NOT NULL,
    used_percent   REAL,
    window_minutes INTEGER,
    resets_at      TEXT,
    plan_type      TEXT,
    observed_at    TEXT NOT NULL,
    source         TEXT NOT NULL CHECK (source IN ('exact', 'estimated')),
    UNIQUE (provider, window_name, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_quota_lookup
    ON quota_snapshots(provider, window_name, observed_at DESC);
