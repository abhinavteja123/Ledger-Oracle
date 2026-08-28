# Dashboards + Supabase Ledger + Render Deploy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the ledger + a new claims-history log to Supabase Postgres behind a
backend toggle that leaves all 84 existing tests untouched, replace the raw-JSON
verify page and in-memory review queue with a polished user dashboard + a new admin
dashboard (claims history, agent trace viewer, DB browser, audit status), and deploy
the whole thing on Render behind a no-auth "Demo User / Demo Admin" landing page.

**Architecture:** `db.py` is a thin connection-factory abstraction selected by
`LEDGER_BACKEND=sqlite|supabase` (default `sqlite`). `tools.py`'s five read functions
and `eval/generate.py`'s ledger writer route through it with a minimal diff (SQL text
is unchanged — `db.py` adapts `?`→`%s` for Postgres, and both backends return
dict-style rows). `app.py` gains a `claims_history` write-through on every `/verify`
call and a set of `/admin/*` endpoints reading from it. Static pages get a visual
rewrite (traffic-light cards, step timelines) replacing raw JSON dumps.

**Tech Stack:** FastAPI, SQLite (test/default), Supabase Postgres + `psycopg2-binary`
(deploy), vanilla HTML/CSS/JS (no build step, matching the existing static pages),
Render (Dockerfile deploy).

**Full design:** `docs/superpowers/specs/2026-08-28-dashboards-supabase-deploy-design.md`

---

### Task 1: Supabase schema + roles (handed to the user, not run by the agent)

**Files:**
- Create: `supabase/schema.sql`

- [ ] **Step 1: Write the schema + read-only role script**

```sql
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
```

- [ ] **Step 2: Hand off to the user**

Tell the user: "Run `supabase/schema.sql` in your Supabase project's SQL Editor.
Change `CHANGE_ME_STRONG_PASSWORD` to a real password before running. Then get two
connection strings from Supabase (Project Settings -> Database -> Connection string,
URI format): one using the default `postgres` user (owner) and one using
`ledger_reader` with the password you set. You'll paste both into `.env` in Task 2."

Do not attempt to run this SQL for the user or invent connection strings.

---

### Task 2: `db.py` backend abstraction

**Files:**
- Create: `db.py`
- Test: `tests/test_db_backend.py`
- Modify: `requirements.txt`
- Modify: `.env.example`

- [ ] **Step 1: Add the new dependency**

Add to `requirements.txt` (after `cerebras-cloud-sdk`/`groq`, keep alphabetical-ish
grouping already there):
```
psycopg2-binary
```

- [ ] **Step 2: Add the new env vars**

Add to `.env.example` (after the existing `GROQ_API_KEY=` line):
```
LEDGER_BACKEND=sqlite
SUPABASE_DB_URL=
SUPABASE_READONLY_DB_URL=
```

- [ ] **Step 3: Write the failing test**

```python
# tests/test_db_backend.py
"""LEDGER_BACKEND toggle. Supabase-specific network behaviour (the read-only role
actually rejecting a write) is verified live once against a real Supabase project,
not here -- these tests stay offline, matching every other test in this repo.
"""
import sqlite3

import db


def test_default_backend_is_sqlite(monkeypatch):
    monkeypatch.delenv("LEDGER_BACKEND", raising=False)
    assert db.backend() == "sqlite"


def test_sqlite_backend_returns_unmodified_sqlite_connection(tmp_path, monkeypatch):
    monkeypatch.delenv("LEDGER_BACKEND", raising=False)
    path = tmp_path / "t.db"
    sqlite3.connect(path).execute("CREATE TABLE x (a TEXT)").connection.commit()
    conn = db.get_readonly_connection(str(path))
    assert isinstance(conn, sqlite3.Connection)
    conn.close()


def test_supabase_backend_selected_by_env(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "supabase")
    assert db.backend() == "supabase"


def test_pg_wrapper_adapts_qmark_placeholders():
    calls = []

    class _FakeCursor:
        def execute(self, sql, params):
            calls.append((sql, params))
        def fetchall(self):
            return []

    class _FakeConn:
        def cursor(self, cursor_factory=None):
            return _FakeCursor()
        def commit(self):
            pass
        def close(self):
            pass

    wrapper = db._PgCursorWrapper(_FakeConn())
    wrapper.execute("SELECT * FROM captures WHERE utr=?", ("abc",))
    assert calls == [("SELECT * FROM captures WHERE utr=%s", ("abc",))]
```

- [ ] **Step 4: Run it, confirm it fails**

Run: `python -m pytest tests/test_db_backend.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'db'`

- [ ] **Step 5: Write `db.py`**

```python
"""Ledger connection abstraction. See
docs/superpowers/specs/2026-08-28-dashboards-supabase-deploy-design.md.

LEDGER_BACKEND=sqlite (default) | supabase. Tests never set this, so they always
exercise the sqlite path -- byte-for-byte the same behaviour tools.py had before this
module existed. Only the deployed app sets LEDGER_BACKEND=supabase.
"""
import os
import sqlite3


class _PgCursorWrapper:
    """Makes a psycopg2 connection quack like sqlite3.Connection's `.execute()`
    convenience method (`conn.execute(sql, params).fetchall()`), and returns
    dict-style rows (RealDictCursor) so `row["utr"]`-style access works identically
    to sqlite3.Row on the sqlite path. SQL is written once, in SQLite `?` style,
    everywhere else in the codebase -- this is the only place `?` becomes `%s`.
    """
    def __init__(self, pg_conn):
        self._conn = pg_conn

    def execute(self, sql: str, params=()):
        from psycopg2.extras import RealDictCursor
        cur = self._conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql.replace("?", "%s"), params)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def backend() -> str:
    return os.environ.get("LEDGER_BACKEND", "sqlite")


def get_readonly_connection(db_path: str):
    """Read-only connection for tools.py. sqlite: file:...?mode=ro, unchanged from
    the original implementation. supabase: a Postgres connection using the
    SELECT-only `ledger_reader` role (SUPABASE_READONLY_DB_URL) -- db_path is
    ignored in this mode."""
    if backend() == "supabase":
        import psycopg2
        return _PgCursorWrapper(psycopg2.connect(os.environ["SUPABASE_READONLY_DB_URL"]))
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_owner_connection(db_path: str | None = None):
    """Read-write connection for eval/generate.py's seeding and app.py's
    claims_history / consumed_references writes. sqlite: a normal rw connection to
    db_path. supabase: a Postgres connection using the owner role (SUPABASE_DB_URL)."""
    if backend() == "supabase":
        import psycopg2
        return _PgCursorWrapper(psycopg2.connect(os.environ["SUPABASE_DB_URL"]))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn
```

- [ ] **Step 6: Run the test again, confirm it passes**

Run: `python -m pytest tests/test_db_backend.py -v`
Expected: 4 passed

- [ ] **Step 7: Run the full suite to confirm zero regression**

Run: `python -m pytest -q`
Expected: all existing tests still pass (this task added a new file and a new test;
nothing else changed yet)

- [ ] **Step 8: Commit**

```bash
git add db.py tests/test_db_backend.py requirements.txt .env.example
git commit -m "feat: add LEDGER_BACKEND abstraction for sqlite/Supabase"
```

---

### Task 3: Route `tools.py` through `db.py`

**Files:**
- Modify: `tools.py:44-49` (the `_connect` function)

- [ ] **Step 1: Confirm no stray `?` characters exist outside SQL placeholders**

Run: `grep -n "?" tools.py`
Expected: every match is inside a SQL string as a placeholder (e.g. `WHERE utr=?`).
If any match is NOT a placeholder (e.g. a docstring with a literal `?`), note it —
`db.py`'s blind `.replace("?", "%s")` would corrupt it. Nothing in the current file
should trigger this, but confirm before proceeding.

- [ ] **Step 2: Replace `_connect`**

Find in `tools.py`:
```python
def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
```

Replace with:
```python
def _connect(db_path: str = DB_PATH):
    return db.get_readonly_connection(db_path)
```

Add `import db` near the top of `tools.py`, alongside the existing `import sqlite3`
(keep `import sqlite3` — it's still used for the `sqlite3.OperationalError` catches
and `sqlite3.Row` type hints elsewhere in the file).

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest -q`
Expected: all tests pass, unchanged — this is a pure passthrough on the default
sqlite path (`db.get_readonly_connection` does exactly what `_connect` did before
when `LEDGER_BACKEND` is unset).

- [ ] **Step 4: Commit**

```bash
git add tools.py
git commit -m "refactor: route tools.py's read-only connection through db.py"
```

---

### Task 4: `eval/generate.py` Supabase seeding path

**Files:**
- Modify: `eval/generate.py` (the `write_ledger_db` function)

- [ ] **Step 1: Read the current `write_ledger_db` function**

Run: `grep -n "def write_ledger_db" -A 40 eval/generate.py`

- [ ] **Step 2: Branch the connection + schema-creation on backend**

At the top of `write_ledger_db`, replace the direct `sqlite3.connect(path)` +
`CREATE TABLE` calls with:

```python
def write_ledger_db(path: Path, captures: list, refunds: list, consumed_references: list):
    import db as db_module
    backend = db_module.backend()
    if backend == "supabase":
        conn = db_module.get_owner_connection()
        conn.execute("TRUNCATE captures, refunds, consumed_references CASCADE", ())
    else:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS captures (...)   -- keep the existing CREATE
            TABLE statements from the current implementation here, unchanged
        """)
```

Keep every existing `INSERT INTO captures VALUES (...)` / `INSERT INTO refunds
VALUES (...)` / `INSERT INTO consumed_references VALUES (...)` loop exactly as it is
today — those already use `?` placeholders and `conn.execute(sql, params)`, which
now works unchanged against both backends (sqlite path: real sqlite3.Connection;
supabase path: `_PgCursorWrapper` adapting `?`→`%s`). Call `conn.commit()` and
`conn.close()` at the end exactly as the current implementation does — both are
present on both connection types.

- [ ] **Step 3: Verify the sqlite path is unaffected**

Run: `python -m eval.generate --seed 42 --out data/`
Expected: same output as before this task (`captures=500 refunds=120 ...`) —
`LEDGER_BACKEND` is unset, so this exercises the unchanged sqlite branch.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add eval/generate.py
git commit -m "feat: add Supabase seeding path to eval/generate.py"
```

---

### Task 5: `claims_history` write-through + `/admin/*` endpoints in `app.py`

**Files:**
- Modify: `app.py`
- Test: `tests/test_app.py` (add cases, don't remove existing ones)

- [ ] **Step 1: Read the current `/verify` handler and review-queue code**

Run: `grep -n "def verify\|review_queue\|def review\|consumed_ref" app.py`

- [ ] **Step 2: Write the failing tests first**

Add to `tests/test_app.py` (reuse the existing fixture DB + fake-client helpers
already in that file — read them before writing these):

```python
def test_verify_writes_to_claims_history(client_with_fixture_db):
    resp = client_with_fixture_db.post("/verify", json={"text": "test claim"})
    assert resp.status_code == 200
    history = client_with_fixture_db.app.state.history_store.all()
    assert len(history) == 1
    assert history[0]["decision"] == resp.json()["decision"]


def test_admin_claims_lists_every_decision_not_just_escalates(client_with_fixture_db):
    client_with_fixture_db.post("/verify", json={"text": "claim one"})
    client_with_fixture_db.post("/verify", json={"text": "claim two"})
    resp = client_with_fixture_db.get("/admin/claims")
    assert resp.status_code == 200
    assert len(resp.json()["claims"]) == 2


def test_admin_claim_detail_matches_verify_response(client_with_fixture_db):
    v = client_with_fixture_db.post("/verify", json={"text": "test claim"}).json()
    claim_id = client_with_fixture_db.app.state.history_store.all()[0]["claim_id"]
    detail = client_with_fixture_db.get(f"/admin/claims/{claim_id}").json()
    assert detail["decision"] == v["decision"]


def test_admin_ledger_browse_returns_rows(client_with_fixture_db):
    resp = client_with_fixture_db.get("/admin/ledger/captures")
    assert resp.status_code == 200
    assert "rows" in resp.json()


def test_admin_audit_returns_chain_status(client_with_fixture_db):
    resp = client_with_fixture_db.get("/admin/audit")
    assert resp.status_code == 200
    assert "ok" in resp.json()
```

If `client_with_fixture_db` isn't the exact fixture name already in
`tests/test_app.py`, use whatever the existing fixture is actually called — read the
file first, don't guess.

- [ ] **Step 3: Run the tests, confirm they fail**

Run: `python -m pytest tests/test_app.py -k "history_store or admin_claims or admin_claim_detail or admin_ledger or admin_audit" -v`
Expected: FAIL — endpoints/attributes don't exist yet.

- [ ] **Step 4: Add an in-process history-store abstraction**

In `app.py`, near the existing review-queue in-memory store, add:

```python
class HistoryStore:
    """Wraps claims_history writes/reads. sqlite/local dev: an in-memory list
    (same lifetime as the old review queue). Supabase: writes/reads the
    claims_history table via db.get_owner_connection(). Injectable for tests --
    app.state.history_store can be swapped for a fake in a test fixture."""

    def __init__(self):
        self._rows: list[dict] = []  # used only when LEDGER_BACKEND != supabase

    def record(self, claim_id: str, raw_message: str, extracted, verdict, tools_called, evidence, invariants) -> None:
        row = {
            "claim_id": claim_id,
            "raw_message": raw_message,
            "claim_type": extracted.claim_type if extracted else None,
            "order_id": extracted.order_id if extracted else None,
            "claimed_reference": extracted.claimed_reference if extracted else None,
            "claimed_amount_paise": extracted.claimed_amount_paise if extracted else None,
            "claimed_instrument": extracted.claimed_instrument if extracted else None,
            "decision": verdict.decision,
            "reason_code": verdict.reason_code,
            "max_refundable_paise": verdict.max_refundable_paise,
            "why_not_block": verdict.why_not_block,
            "tools_called": [t.model_dump() for t in tools_called],
            "evidence": [e.model_dump() for e in evidence],
            "invariants": [i.model_dump() for i in invariants],
            "status": "open" if verdict.decision == "escalate" else "closed",
            "reviewer_note": None,
        }
        if db.backend() == "supabase":
            conn = db.get_owner_connection()
            try:
                conn.execute(
                    "INSERT INTO claims_history (claim_id, raw_message, claim_type, "
                    "order_id, claimed_reference, claimed_amount_paise, "
                    "claimed_instrument, decision, reason_code, max_refundable_paise, "
                    "why_not_block, tools_called, evidence, invariants, status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["claim_id"], row["raw_message"], row["claim_type"],
                     row["order_id"], row["claimed_reference"], row["claimed_amount_paise"],
                     row["claimed_instrument"], row["decision"], row["reason_code"],
                     row["max_refundable_paise"], row["why_not_block"],
                     json.dumps(row["tools_called"]), json.dumps(row["evidence"]),
                     json.dumps(row["invariants"]), row["status"]),
                )
                conn.commit()
            finally:
                conn.close()
        else:
            self._rows.append(row)

    def all(self, status: str | None = None, decision: str | None = None) -> list[dict]:
        if db.backend() == "supabase":
            conn = db.get_owner_connection()
            try:
                sql = "SELECT * FROM claims_history WHERE 1=1"
                params = []
                if status:
                    sql += " AND status=?"
                    params.append(status)
                if decision:
                    sql += " AND decision=?"
                    params.append(decision)
                sql += " ORDER BY created_at DESC"
                return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
            finally:
                conn.close()
        rows = self._rows
        if status:
            rows = [r for r in rows if r["status"] == status]
        if decision:
            rows = [r for r in rows if r["decision"] == decision]
        return list(reversed(rows))

    def get(self, claim_id: str) -> dict | None:
        return next((r for r in self.all() if r["claim_id"] == claim_id), None)

    def update_status(self, claim_id: str, status: str, note: str | None) -> None:
        if db.backend() == "supabase":
            conn = db.get_owner_connection()
            try:
                conn.execute(
                    "UPDATE claims_history SET status=?, reviewer_note=?, decided_at=now() WHERE claim_id=?",
                    (status, note, claim_id),
                )
                conn.commit()
            finally:
                conn.close()
            return
        for r in self._rows:
            if r["claim_id"] == claim_id:
                r["status"] = status
                r["reviewer_note"] = note
```

Add `import db` and `import json` at the top of `app.py` if not already present.
Instantiate once at module load: `history_store = HistoryStore()`, and expose it on
the app for tests: `app.state.history_store = history_store`.

- [ ] **Step 5: Wire `/verify` to call `history_store.record(...)` after deciding**

Find the existing `/verify` handler's return point (after `investigate()`/`decide()`
produces `state` and `verdict`). Immediately before building the response, add:

```python
    try:
        history_store.record(
            claim_id, clean_text, state.extracted, verdict,
            state.tools_called, state.evidence, verdict.invariants,
        )
    except Exception:
        pass  # history logging must never block the decision path (Rule 1)
```

Use the actual local variable names already present in the existing handler (`state`,
`verdict`, `clean_text`, `claim_id` or whatever they're actually called — read the
handler first, this plan describes the shape, not guaranteed exact names).

- [ ] **Step 6: Add the admin endpoints**

```python
@app.get("/admin/claims")
def admin_claims(status: str | None = None, decision: str | None = None):
    return {"claims": history_store.all(status=status, decision=decision)}


@app.get("/admin/claims/{claim_id}")
def admin_claim_detail(claim_id: str):
    row = history_store.get(claim_id)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return row


@app.post("/admin/claims/{claim_id}/decide")
def admin_claim_decide(claim_id: str, body: dict):
    action = body.get("action")
    note = body.get("note")
    row = history_store.get(claim_id)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    status_map = {"approve": "approved", "reject": "rejected", "request_info": "request_info"}
    new_status = status_map.get(action)
    if new_status is None:
        return JSONResponse({"error": "invalid action"}, status_code=400)
    history_store.update_status(claim_id, new_status, note)
    if action == "approve" and row.get("claimed_reference"):
        conn = db.get_owner_connection("data/ledger.db")
        try:
            ref = row["claimed_reference"].replace(" ", "").replace("-", "").upper()
            conn.execute(
                "INSERT INTO consumed_references (reference, consumed_by, consumed_at, approved_by) VALUES (?,?,?,?)",
                (ref, claim_id, datetime.now(timezone.utc).isoformat(), "admin"),
            )
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}


@app.get("/admin/ledger/{table}")
def admin_ledger_browse(table: str, limit: int = 100):
    if table not in ("captures", "refunds", "consumed_references"):
        return JSONResponse({"error": "unknown table"}, status_code=400)
    conn = db.get_readonly_connection("data/ledger.db")
    try:
        rows = conn.execute(f"SELECT * FROM {table} LIMIT ?", (limit,)).fetchall()
        return {"rows": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/admin/audit")
def admin_audit():
    ok, break_at, detail = audit_verify("audit.jsonl")
    return {"ok": ok, "break_at": break_at, "detail": detail}
```

Import `audit.verify` as `audit_verify` (or whatever name doesn't collide with an
existing import — check `app.py`'s current imports first) and `JSONResponse`,
`datetime`/`timezone` if not already imported.

Note: table name in the `admin_ledger_browse` f-string is safe here only because it's
validated against a fixed allowlist (`("captures", "refunds", "consumed_references")`)
immediately before use — never remove that check or interpolate an unvalidated table
name into SQL.

- [ ] **Step 7: Run the new tests**

Run: `python -m pytest tests/test_app.py -v`
Expected: all pass, including the 5 new ones and every pre-existing test in the file.

- [ ] **Step 8: Run the full suite**

Run: `python -m pytest -q`
Expected: all tests pass (84 + new ones from Tasks 2 and 5).

- [ ] **Step 9: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat: add claims_history write-through and /admin/* endpoints"
```

---

### Task 6: Static pages — landing, polished verify, new admin dashboard

**Files:**
- Create: `static/landing.html`
- Modify: `static/verify.html`
- Create: `static/admin.html`
- Modify: `app.py` (route `/` to landing, add `/verify-page` and `/admin` routes if not already present — check current routing first)

- [ ] **Step 1: Check current static-file routing in `app.py`**

Run: `grep -n "StaticFiles\|@app.get(\"/\|verify.html\|review.html" app.py`

- [ ] **Step 2: `static/landing.html`**

Single self-contained file, inline CSS, no build step (matches every other static
page in this repo). Content: page title "Ledger Oracle", two large buttons —
"Demo User" (links to the verify page) and "Demo Admin" (links to `/admin`) — plus
one sentence under each button explaining what that role sees ("Submit a refund claim
and see the verdict" / "Browse every claim processed, the ledger, and the audit
trail"). No login form, no real auth — this is the "dummy demo login" the user asked
for. Monospace font, dark theme, consistent with `verify.html`'s existing look (read
that file's `<style>` block and reuse the same color variables/traffic-light classes).

- [ ] **Step 3: Rewrite `static/verify.html`'s verdict rendering**

Keep the existing textarea + VERIFY button + `/verify` POST call unchanged. Replace
the current raw-JSON evidence block with:
- A verdict card at the top: large colored badge (green=pass, red=block,
  amber=escalate — reuse the color values already defined in this file's `<style>`),
  the `reason_code`, `max_refundable_paise` formatted as `Rs {n/100}`.
- A step timeline below it: one row per entry in `tools_called`, showing the tool
  name and, matched by array index against `evidence[i]`, a one-line human summary
  (e.g. for a `PaymentLookupResult`-shaped evidence object: `${matches.length} match(es), ${near_matches.length} near`; for `OrderPaymentsResult`-shaped: `${captures.length} capture(s)`; fall back to `JSON.stringify(evidence[i]).slice(0,80)` for any other shape) plus `${query_latency_ms}ms`.
- An invariants table (already exists in some form — keep it, it's already
  well-designed per the screenshot).
- Do NOT delete the raw evidence JSON entirely — collapse it behind a
  `<details><summary>raw evidence</summary>...</details>` so power users/graders can
  still inspect it, but it's not the primary view.

- [ ] **Step 4: `static/admin.html`**

Single self-contained file, three tabs (plain JS tab-switching, no framework):

1. **Claims tab** (default): fetches `GET /admin/claims` on load, renders a table
   (claim_id, decision badge, reason_code, status, created_at) with dropdown filters
   for `status` and `decision` that re-fetch with query params. Clicking a row
   fetches `GET /admin/claims/{claim_id}` and renders the same verdict-card +
   step-timeline + invariants view built in Step 3 (reuse the same rendering
   function — factor it into a shared `<script>` block or a small inline JS function
   duplicated between the two files if factoring is awkward given no build step; a
   few dozen duplicated lines is fine here, YAGNI on a shared JS file for two pages).
   Below the trace: three buttons (APPROVE / REJECT / REQUEST INFO) + a note field,
   posting to `POST /admin/claims/{claim_id}/decide`, only enabled when
   `status === "open"`.
2. **Ledger tab**: three sub-tabs (Captures / Refunds / Consumed References), each
   fetching `GET /admin/ledger/{table}` and rendering a plain HTML table of the
   returned rows.
3. **Audit tab**: fetches `GET /admin/audit` on load, shows a big OK/FAILED badge
   and the detail text; a "Re-check" button re-fetches.

- [ ] **Step 5: Wire routes in `app.py`**

Add (or adjust existing static-serving routes to match):
```python
@app.get("/")
def landing():
    return FileResponse("static/landing.html")


@app.get("/verify-page")
def verify_page():
    return FileResponse("static/verify.html")


@app.get("/admin")
def admin_page():
    return FileResponse("static/admin.html")
```

If `app.py` already serves `verify.html` at `/` (per the earlier app.py build), move
that mapping to `/verify-page` and put the landing page at `/` instead — check
current behavior with `grep -n "@app.get(\"/\")" app.py` before editing, don't
duplicate a route.

- [ ] **Step 6: Manual smoke test**

Run: `uvicorn app:app --reload` (in one terminal), then in another:
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/verify-page
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/admin
```
Expected: `200` for all three. Then open `http://127.0.0.1:8000/` in an actual
browser, click through both demo buttons, submit a claim on the user page, confirm it
shows up in the admin claims tab.

- [ ] **Step 7: Commit**

```bash
git add static/landing.html static/verify.html static/admin.html app.py
git commit -m "feat: add landing page, admin dashboard, polish verify page"
```

---

### Task 7: Render deployment config

**Files:**
- Create: `Dockerfile`
- Create: `render.yaml`
- Create: `.dockerignore`

- [ ] **Step 1: `Dockerfile`**

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV LEDGER_BACKEND=supabase
EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: `.dockerignore`**

```
__pycache__/
*.pyc
.venv/
venv/
.env
audit.jsonl
data/
.pytest_cache/
.git/
docs/
```

- [ ] **Step 3: `render.yaml`**

```yaml
services:
  - type: web
    name: ledger-oracle
    env: docker
    plan: free
    envVars:
      - key: LEDGER_BACKEND
        value: supabase
      - key: GROQ_API_KEY
        sync: false
      - key: CEREBRAS_API_KEY
        sync: false
      - key: SUPABASE_DB_URL
        sync: false
      - key: SUPABASE_READONLY_DB_URL
        sync: false
```

`sync: false` means Render will prompt for these values in its dashboard rather than
committing secrets to the repo — the user pastes their real keys/connection strings
there after connecting the repo to Render.

- [ ] **Step 4: Hand off to the user**

Tell the user: "Push this repo to GitHub, then on Render: New -> Web Service ->
connect the repo -> it should auto-detect `render.yaml`. Paste your real
`GROQ_API_KEY`, `CEREBRAS_API_KEY`, `SUPABASE_DB_URL`, `SUPABASE_READONLY_DB_URL`
into the env var prompts. Before or right after the first deploy, run `python -m
eval.generate --seed 1337 --out data/` **locally** with `LEDGER_BACKEND=supabase`
and both Supabase URLs set in your local `.env`, to seed the live database once —
the deployed app itself never runs the generator automatically."

- [ ] **Step 5: Commit**

```bash
git add Dockerfile render.yaml .dockerignore
git commit -m "chore: add Render deployment config"
```

---

## Self-review notes

- **Spec coverage:** Task 1 covers the Supabase schema/roles section. Task 2-3 cover
  the storage backend toggle. Task 4 covers generator seeding. Task 5 covers
  claims_history + admin endpoints. Task 6 covers both static pages + landing. Task 7
  covers Render deployment. The "known risk, not fixing" (shared free-tier quota) from
  the spec is deliberately not a task — matches the spec's own scope decision.
- **No placeholders:** every SQL/Python/HTML step above has real, complete code or an
  explicit "read the existing file first, use its actual names" instruction where an
  exact pre-existing name genuinely can't be guessed without reading the file (this
  happens because `app.py`/`tools.py` were built by earlier agents this session and
  their exact local variable names aren't in this plan's author's direct view at
  spec-writing time — each such step names exactly what to grep for).
- **Type consistency:** `HistoryStore.record()`'s parameter shapes match what
  `agent.py`'s `InvestigationState`/`Verdict` (from `models.py`) actually expose
  (`.tools_called`, `.evidence`, `state.extracted`, `verdict.invariants`, all
  Pydantic models with `.model_dump()`) — confirmed against `models.py`'s existing
  field names before writing this plan.
