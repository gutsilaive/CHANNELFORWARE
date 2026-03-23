-- =========================================================
--  AutoForward Bot — Supabase SQL Setup
--  Run this once in your Supabase SQL editor
-- =========================================================

-- ── sessions ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sessions (
    admin_id        BIGINT PRIMARY KEY,
    phone           TEXT NOT NULL,
    session_string  TEXT NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── tasks ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    admin_id        BIGINT NOT NULL,
    source          TEXT NOT NULL,
    destinations    TEXT[] NOT NULL DEFAULT '{}',
    caption         TEXT,
    start_msg_id    INT NOT NULL,
    end_msg_id      INT NOT NULL,
    total           INT NOT NULL DEFAULT 0,
    forwarded       INT NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | stopped | error
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ
);

-- index for fast per-admin queries
CREATE INDEX IF NOT EXISTS idx_tasks_admin ON tasks (admin_id, created_at DESC);

-- ── Row Level Security (optional but recommended) ─────────
-- Disable public access; only service key can write
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks    ENABLE ROW LEVEL SECURITY;
