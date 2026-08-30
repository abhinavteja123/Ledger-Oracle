-- supabase/schema.sql
-- Run this once in Supabase's SQL editor (Project -> SQL Editor -> New query).

CREATE TABLE IF NOT EXISTS captures (
    capture_id      TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL,
    amount_paise    INTEGER NOT NULL,
    instrument      TEXT NOT NULL,
    utr             TEXT,
    payee_vpa       TEXT,
    captured_at     TEXT NOT NULL,
    settled_at      TEXT,
    ledger_source   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_captures_order ON captures(order_id);
CREATE INDEX IF NOT EXISTS idx_captures_utr   ON captures(utr);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id       TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL,
    capture_id      TEXT,
    amount_paise    INTEGER NOT NULL,
    issued_at       TEXT NOT NULL,
    channel         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refunds_order ON refunds(order_id);

CREATE TABLE IF NOT EXISTS consumed_references (
    reference       TEXT PRIMARY KEY,
    consumed_by     TEXT NOT NULL,
    consumed_at     TEXT NOT NULL,
    approved_by     TEXT
);

CREATE TABLE IF NOT EXISTS claims_history (
    claim_id                TEXT PRIMARY KEY,
    raw_message              TEXT NOT NULL,
    claim_type                TEXT,
    order_id                  TEXT,
    claimed_reference          TEXT,
    claimed_amount_paise        INTEGER,
    claimed_instrument           TEXT,
    decision                     TEXT NOT NULL,
    reason_code                   TEXT NOT NULL,
    max_refundable_paise            INTEGER NOT NULL DEFAULT 0,
    why_not_block                     TEXT,
    tools_called                       JSONB NOT NULL DEFAULT '[]',
    evidence                            JSONB NOT NULL DEFAULT '[]',
    invariants                           JSONB NOT NULL DEFAULT '[]',
    status                                TEXT NOT NULL DEFAULT 'open',
    reviewer_note                          TEXT,
    created_at                              TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at                               TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims_history(status);
CREATE INDEX IF NOT EXISTS idx_claims_decision ON claims_history(decision);

-- Read-only role: tools.py connects as this at runtime. It can SELECT the ledger
-- tables and nothing else -- this is what makes "the agent cannot write" a database
-- guarantee, not just an application convention.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ledger_reader') THEN
        CREATE ROLE ledger_reader WITH LOGIN PASSWORD 'CHANGE_ME_STRONG_PASSWORD';
    END IF;
END
$$;
GRANT CONNECT ON DATABASE postgres TO ledger_reader;
GRANT USAGE ON SCHEMA public TO ledger_reader;
GRANT SELECT ON captures, refunds, consumed_references TO ledger_reader;
-- Deliberately NOT granted: INSERT/UPDATE/DELETE on anything, and no access at all
-- to claims_history (that table is written only by the app's owner connection).

-- Correction to an earlier version of this file: disabling RLS here (reasoning: "GRANT
-- SELECT above already restricts access, RLS is redundant") only accounted for the
-- app's direct Postgres pooler connections (postgres.<ref>, ledger_reader.<ref>) -- it
-- missed that Supabase auto-exposes every public table over its REST API (PostgREST)
-- to the anon/authenticated roles too, independently of ledger_reader's own grants.
-- Confirmed live via Supabase's advisors + a direct grants query: anon and
-- authenticated held SELECT, INSERT, UPDATE, DELETE, and TRUNCATE on all three tables
-- (Supabase's default privileges for tables created via the SQL editor) -- anyone with
-- the anon/publishable key could read AND write the ledger the policy engine trusts,
-- bypassing the app and policy.py entirely. Real bug, not hypothetical -- see
-- HANDOFF.md / FAILURES.md.
--
-- Fix: REVOKE those default grants, then ENABLE RLS with a permissive policy for
-- ledger_reader ONLY (it lacks BYPASSRLS -- confirmed via pg_roles -- so a blank
-- ENABLE reproduces the exact zero-rows bug this file used to work around; see the
-- comment that used to live here and the matching FAILURES.md entry). No policy is
-- added for anon/authenticated, which is the point: default-deny for both roles, for
-- both reads and writes. postgres and service_role have rolbypassrls=true (confirmed
-- live), so the app's owner connection and Supabase's own service role are unaffected.
REVOKE ALL ON captures, refunds, consumed_references FROM anon, authenticated;

ALTER TABLE captures ENABLE ROW LEVEL SECURITY;
ALTER TABLE refunds ENABLE ROW LEVEL SECURITY;
ALTER TABLE consumed_references ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS ledger_reader_select ON captures;
CREATE POLICY ledger_reader_select ON captures FOR SELECT TO ledger_reader USING (true);
DROP POLICY IF EXISTS ledger_reader_select ON refunds;
CREATE POLICY ledger_reader_select ON refunds FOR SELECT TO ledger_reader USING (true);
DROP POLICY IF EXISTS ledger_reader_select ON consumed_references;
CREATE POLICY ledger_reader_select ON consumed_references FOR SELECT TO ledger_reader USING (true);
