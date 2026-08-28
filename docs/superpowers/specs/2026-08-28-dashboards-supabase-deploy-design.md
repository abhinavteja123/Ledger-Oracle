# Dashboards + Supabase-backed ledger + public deploy — design

## Context

Ledger Oracle currently runs locally: `static/verify.html` posts to `/verify`, shows a
raw JSON evidence dump. Escalated claims sit in an in-memory review queue
(`static/review.html`) that resets on server restart. The ledger is a local read-only
SQLite file (`data/ledger.db`).

The user wants this deployed publicly (Render) for recruiters to click through as a
real product: a **user dashboard** (submit a claim, see a clean verdict — no raw JSON)
and an **admin dashboard** (browse the full ledger DB, see every claim ever processed
with a visual trace of how the agent investigated it, verify the audit chain). Both
reached through a "Demo Admin / Demo User" landing page — no real auth, dummy
role-select buttons only. Data layer moves fully to Supabase (Postgres), including the
ledger, per the user's explicit choice after the cost/benefit was discussed.

## Goals

- Recruiters can visit a public URL, pick "Demo User" or "Demo Admin," and see the full
  system working — not text/JSON dumps, a real product UI.
- Admin can see: every claim processed (pass/block/escalate) with its full agent trace,
  the raw ledger tables (read-only), audit-chain status.
- Ledger (captures/refunds/consumed_references) and a new claims-history log both live
  in Supabase Postgres.
- Zero regression to the 84 existing tests — they must keep running offline, fast,
  deterministic, exactly as today.
- Deployed on Render.

## Non-goals

- Real authentication/authorization (dummy demo-role buttons only, explicitly requested).
- Rate-limiting or abuse protection on the public deployment (noted as a risk below,
  not built unless asked).
- Migrating anything about the decision logic itself (`policy.py` stays untouched, still
  offline, still Rule 1-compliant).

## Architecture

### Storage backend toggle

`LEDGER_BACKEND=sqlite|supabase` env var, default `sqlite`. `tools.py`'s five read
functions and `eval/generate.py`'s ledger-writing logic branch on this. Tests never set
it, so they always exercise the `sqlite` path — the exact fixture-DB pattern already in
`tests/test_tool_allowlist_is_readonly.py`, `tests/test_agent_bounds.py`,
`tests/test_recovery_paths.py`, `tests/test_app.py` continues working unchanged. Only
the Render deployment sets `LEDGER_BACKEND=supabase`.

Both backends are plain SQL (SQLite and Postgres), so `tools.py`'s query logic is reused
nearly as-is — placeholder syntax (`?` -> `%s`) and connection construction are the only
real differences. A small `db.py` module owns "get me a connection for the configured
backend" so `tools.py` doesn't grow backend-specific branches itself.

### Supabase roles (two connection strings)

- **Owner/service** (`SUPABASE_DB_URL`): full access. Used only by `eval/generate.py`
  when seeding the ledger, and by the app for writing `claims_history` rows.
- **Read-only** (`SUPABASE_READONLY_DB_URL`): a Postgres role with `GRANT SELECT` only
  on `captures`, `refunds`, `consumed_references`. Used by `tools.py` at runtime — the
  agent's "cannot write" guarantee (Rule 2) becomes DB-enforced, not just SQLite's
  `mode=ro` flag. The user runs a provided SQL script once in Supabase's SQL editor to
  create the role and grants (I cannot administer their Supabase project directly).

### New table: `claims_history`

Every `/verify` call — regardless of decision — writes one row: claim_id, raw_message,
extracted fields, decision, reason_code, max_refundable_paise, tools_called (jsonb),
evidence (jsonb), invariants (jsonb), why_not_block, status (`open`/`approved`/
`rejected`/`request_info`), reviewer_note, created_at, decided_at. This replaces the
in-memory review queue (fixes the "resets on restart" limitation as a side effect) and
is what the admin dashboard's claim list and trace view read from.

### New/changed endpoints (`app.py`)

- `POST /verify` — unchanged decision path; now also writes to `claims_history`
  (Supabase) after deciding. Write failure is logged, never blocks the response — the
  decision path stays pure (Rule 1) regardless of Supabase's availability.
- `GET /admin/claims?status=&decision=` — paginated list from `claims_history`.
- `GET /admin/claims/{claim_id}` — full detail (same shape `review.html` used, plus
  decision/status for pass/block claims too, not just escalates).
- `POST /admin/claims/{claim_id}/decide` — replaces `/review/{id}/decide`; same
  approve/reject/request_info semantics, `approve` still writes into
  `consumed_references` (now in Supabase) to close the I3 replay-protection loop.
- `GET /admin/ledger/{table}` — read-only browse of `captures`/`refunds`/
  `consumed_references`, paginated.
- `GET /admin/audit` — runs `audit.verify()`, returns chain status.
- `GET /` — landing page: "Demo User" / "Demo Admin" buttons (no real auth).

### New/changed static pages

- `static/landing.html` — the two demo-role buttons.
- `static/verify.html` — polish pass: verdict as a traffic-light card, agent
  investigation as a step timeline (tool name -> latency -> one-line result), not a
  pretty-printed JSON block. Same `/verify` contract, just rendered differently.
- `static/admin.html` — new: claims table (filter by decision/status) -> click a row
  for the same timeline view as verify.html plus the invariant table and why-not-block
  -> approve/reject/request-info buttons; a DB-browser tab (three ledger tables); an
  audit-chain status widget with a "verify now" button.

### Deployment (Render)

- `Dockerfile` (or Render's native Python runtime + `render.yaml`) running `uvicorn
  app:app`.
- Env vars set in Render's dashboard: `GROQ_API_KEY`, `CEREBRAS_API_KEY`,
  `SUPABASE_DB_URL`, `SUPABASE_READONLY_DB_URL`, `LEDGER_BACKEND=supabase`.
- `eval/generate.py` run once against Supabase (owner connection) before/at deploy time
  to seed the ledger — not run automatically on every deploy (seeding is a deliberate,
  one-time action, not a build step).

## Known risk, not building a fix unless asked

Public deployment shares one Groq/Cerebras free-tier quota across every visitor. This
session already exhausted a 200k-token daily Groq quota through testing alone; a
public demo getting real traffic will hit it faster. The system already degrades
safely when this happens (`escalate MODEL_UNAVAILABLE`, never crashes, never wrongly
passes — proven this session), so the failure mode is safe, just not always
demo-friendly. Flagging this now; no rate-limiting or caching layer is being built
unless explicitly requested.

## Testing

- All 84 existing tests: unchanged, `LEDGER_BACKEND` unset in test runs, still hit local
  SQLite fixtures, still zero network.
- New `tests/test_db_backend.py`: confirms the backend toggle dispatches to the right
  connection type; Supabase-specific behavior (the read-only role's grants actually
  block writes) is verified live once, against the user's real Supabase project, the
  same "unit tests offline + one live verification" pattern used for the LLM providers
  all session — not re-run on every `pytest` invocation.
- `claims_history` writes: unit-tested with an injectable history-store client (same
  dependency-injection pattern already used for the LLM client throughout this codebase).
- Admin endpoints: `TestClient`-based, same pattern as `tests/test_app.py`.

## Build order

1. SQL script for the user to run in Supabase (tables + read-only role) — handed over,
   not run by me.
2. `db.py` backend abstraction + `tools.py` changes, `LEDGER_BACKEND` toggle. Verify all
   84 existing tests still pass (sqlite path unchanged).
3. `eval/generate.py` Supabase-seeding path. One live run against the user's real
   Supabase project to seed real data.
4. `claims_history` table + `app.py`'s `/verify` write-through + new `/admin/*`
   endpoints. Unit tests with injected fakes.
5. `static/admin.html`, `static/landing.html`, polish `static/verify.html`.
6. Live smoke test against the deployed-shape stack locally (`LEDGER_BACKEND=supabase`
   pointed at the real project).
7. Render deployment config + deploy.
