# Session handoff — Ledger Oracle

Written at end of session. Read this before doing anything else next session.

## What this project is

Razorpay AI Buildathon 2026, Track 02 (AI Risk Manager). A bounded LLM agent
investigates refund claims against a merchant ledger; a deterministic policy engine
(no LLM in the decision path) decides pass/block/escalate. Full design:
`PRD-Ledger-Oracle.md`, `ARCHITECTURE.md`. Full flow + how to test every layer:
`TESTING.md`. Every real bug found across all sessions, with root cause: `FAILURES.md`
— read this one, it's the most valuable file in the repo for understanding what's
fragile.

**Submission requirement (PRD §19, confirmed by reading it directly): public repo + a
5-minute pitch video + architecture doc. No live deployed URL is required.** Render
deploy is optional polish, not a blocker. The only undone deliverable in the PRD's own
build order (§20, step 16) is recording the video.

## Current state: 98/98 tests passing, nothing committed

`python -m pytest -q` → 98 passed (was 94 at last handoff; +4 this session, see below).
Everything is sitting **uncommitted** in the working tree, deliberately — the user
commits under their own name, no auto-commits. Check `git status --short` before doing
anything destructive. Current uncommitted set: `app.py`, `db.py` (new), `llm_client.py`,
`tools.py`, `eval/generate.py`, `static/verify.html`, `supabase/schema.sql`,
`tests/test_app.py`, `tests/test_db_backend.py`, `tests/test_llm_client.py`,
`FAILURES.md`, plus untracked `Dockerfile`/`render.yaml`/`.dockerignore`,
`static/admin.html`, `static/landing.html`, this file.

## What this session did (continuation of the dashboards/Supabase/deploy session)

Started from last handoff's state (94/94, Supabase live-verified, dashboards built,
nothing deployed). Verified end-to-end with a fresh Groq key and Supabase MCP access,
found and fixed **three real, previously-latent bugs** by actually running the system
against real conditions — not just trusting the green test suite, per this project's
own established discipline.

### Bug 1 — `tools.py` only caught `sqlite3.OperationalError`, not Supabase's errors

Every one of the 5 tool functions' DB-error handlers only caught
`sqlite3.OperationalError` — a leftover from when the backend was SQLite-only. The
Supabase backend (added last session) raises `psycopg2.Error` on connection failure
instead, which fell through uncaught and crashed `/verify` with a raw 500 — directly
violating the project's own core safety claim (`unsafe_failures == 0`, every failure
resolves to escalate, never a crash). Same failure *class* as the `CEREBRAS_ERRORS`
bug already in `FAILURES.md` (catching too narrow an exception type when a new backend
is added later).

**Fix:** `db.py` gained `connection_errors()` — returns the right exception tuple per
backend (mirrors `agent.py`'s `CEREBRAS_ERRORS` pattern exactly). `tools.py`'s 5
`except sqlite3.OperationalError` clauses all changed to `except db.connection_errors()`.
Regression tests in `tests/test_db_backend.py`.

### Bug 2 — `/admin/claims*` endpoints had zero error handling

Unlike `/admin/ledger/{table}` (already wrapped in try/except), `/admin/claims`,
`/admin/claims/{id}`, and `/admin/claims/{id}/decide` had no exception handling at all
— also 500'd on any Supabase hiccup. Inconsistent with the pattern already established
in the same file. **Fix:** wrapped all three in try/except, matching
`admin_ledger_browse`'s existing pattern (`JSONResponse({"error": str(e)}, 200)`).
Verified live and in the browser (Playwright) — Supabase down now renders a clean
inline error message in the admin UI instead of a blank crash.

### Bug 3 — single-LLM-provider mode was structurally broken (the big one)

Found only because Cerebras got disabled this session (see below) — this bug was
**latent the entire project lifetime** because Cerebras was always configured
alongside Groq, so the code path that has the bug never ran.

`llm_client.get_client()` had: `_client = providers[0][1] if len(providers) == 1 else
_FallbackClient(providers)` — i.e. with only one provider configured, it skipped the
`_FallbackClient` wrapper and used the raw SDK client directly. But `_FallbackClient`
is also what applies `_PROVIDER_MODEL_IDS`'s per-provider model-ID remap (Cerebras's
bare `gpt-oss-120b` isn't Groq's real ID, `openai/gpt-oss-120b`) and normalizes every
provider's own exception type to `CerebrasError` (which is all `agent.py`/`app.py`
ever catch). Single-provider mode skipped **both**: Groq got the wrong model ID
(`404 model_not_found`) and that exception went uncaught by `except CerebrasError`
anywhere → raw 500, live, with a Groq-only config.

**Fix:** always construct `_FallbackClient`, even for one provider — deleted the
special case instead of adding another. Regression test in `tests/test_llm_client.py`
(`test_get_client_wraps_single_provider_too`).

**Side-effect caught and fixed:** that new test was the first test in the suite to
call the real `get_client()`, which calls the real `_load_dotenv()`. `_load_dotenv()`
uses `os.environ.setdefault(...)`, which silently leaked `LEDGER_BACKEND=supabase`
(and the real Supabase URLs) from the repo's `.env` into the shared pytest process
environment for every test running after it in the same run — flipping later DB-backed
tests onto live Supabase mid-suite (6 failures, 79s runtime instead of 7s). Fixed by
having the test pin `LEDGER_BACKEND=sqlite` via `monkeypatch.setenv` before calling
`get_client()`, so `setdefault()` becomes a no-op. **Lesson: any future test that
touches the real `get_client()`/`_load_dotenv()` must do the same, or it will leak.**

## LLM providers: Cerebras disabled, Groq carrying it alone, verified live

`CEREBRAS_API_KEY` is **commented out** in `.env` — Cerebras returns a real
`402 payment_required` ("Payment required to access this resource. Visit your billing
tab."), a genuine account-level billing block, not fixable from code. Re-enable by
uncommenting the line once billing is sorted on the Cerebras dashboard; the fallback
architecture still supports it (see Bug 3 fix above — it'll work correctly as a second
provider again with no further changes needed).

Groq key was rotated this session to a new key (also in `.env`). With Bug 3 fixed,
Groq-only mode is **confirmed live end-to-end**: parse → agent tool selection → real
Supabase query → policy decision → real `block REF_NOT_IN_LEDGER` verdict, twice in a
row, logged correctly to `/admin/claims`.

**Caveat: both Groq and the Supabase pooler showed real intermittent flakiness during
this session's heavy testing** (rapid-fire calls, tens of LLM calls and DB connections
in a short window) — request-to-request failures that cleared on retry, consistent
with hitting short burst-window rate limits / connection-pool pressure rather than any
code defect. Ruled out as a code issue by testing every layer in isolation (raw
provider calls, `parse_claim()` directly, `agent.next_action()` directly — all worked
standalone even when the live `/verify` endpoint was failing moments before/after).
**Test on the actual demo machine/network before presenting** — if it's still flaky
there, fall back to the zero-LLM engine path (`make eval`, ablation run A) as the
resilience story, per the advisor's plan from earlier this session.

## Supabase: still live, one real security finding not yet acted on

Confirmed via Supabase MCP (`list_tables`, `get_advisors` against project
`yngbbsrzxpimvogtqvpm`, "Ledger Oracle", ACTIVE_HEALTHY): schema matches expectations,
500 captures / 120 refunds / 6 consumed_references / claims_history all present and
populated.

**Security finding, flagged, deliberately not auto-fixed:** `captures`, `refunds`,
`consumed_references` have RLS disabled and are exposed over Supabase's public REST
API (PostgREST) to the `anon`/`authenticated` roles. Last session's reasoning for
disabling RLS ("redundant with GRANT, not a downgrade") only covers the app's direct
Postgres pooler connections (`postgres.<ref>`, `ledger_reader.<ref>` — real Postgres
roles) — it misses that Supabase auto-exposes every public table over REST to
`anon`/`authenticated` too. **Anyone with the Supabase anon/publishable key can
currently read/write these three tables directly over HTTP, bypassing the app and the
policy engine entirely.** Re-enabling RLS blank would reproduce the exact
`ledger_reader` zero-rows bug already in `FAILURES.md` — it needs a policy scoped to
the app's specific roles, not a blind re-enable. Needs the user's decision; SQL can be
written on request. `claims_history` is fine as-is (RLS on, no policy, but the app
writes via the owner role which bypasses RLS anyway).

`.env` still has real Supabase credentials (gitignored, never committed) — the user
was already told last session to consider rotating the DB password once convenient;
that advice still stands, and now extends to both LLM keys too (all of them have been
pasted in chat across sessions).

## What's NOT done yet

- **Video (PRD step 16)** — the only actually-required-and-missing deliverable. Script
  exists in PRD §19 (5-minute beat-by-beat table). Nothing recorded yet.
- **Supabase RLS policy** — see security finding above. Needs user's go-ahead.
- **One clean final ablation run** — `README.md`'s run B/C numbers are still from a
  quota-contaminated run per `FAILURES.md`'s last entry. Not re-run this session
  (would need sustained LLM availability; see flakiness caveat above). Run A (zero-LLM,
  engine-only) numbers are already clean and final.
- **Vercel frontend split** — still explicitly cut from the plan (advisor's call,
  agreed this session): zero judging-axis value, Render already serves the static
  pages via `FileResponse`, adds CORS + 4-page refactor for nothing. Don't do this
  unless the user explicitly asks again and it gets its own brainstorm/spec.
- **Not deployed to Render** — still optional per the PRD (see top of this file).
  Config exists (`render.yaml`, `Dockerfile`, `.dockerignore`), nothing pushed/deployed.
  Before deploying: grep those three files for a real Supabase connection string —
  none currently found, but check again if they change.
- **Admin auth** — still explicitly conditional on Render deploy happening. `/admin/*`
  has zero auth; cosmetic risk locally, real risk if publicly deployed. Skip if the
  demo stays local.
- **Buildathon eligibility** — PRD flags "students-only per the live page," unconfirmed
  this session too. Worth confirming before more build time, not a code task.

## Immediate next steps, in order

1. `git status --short` to see the full uncommitted diff (listed above) before doing
   anything.
2. `python -m pytest -q` to reconfirm 98/98 (state may have drifted).
3. Check Groq's dashboard (console.groq.com) for real quota/rate-limit state before
   any live demo — this session hit real flakiness under heavy testing volume and
   burned real quota; don't assume it's fully recovered without checking.
4. If presenting soon: record the video (PRD §19 script) using the local `uvicorn`
   demo path, not a fresh deploy — a deploy that breaks 30 minutes before submission is
   unrecoverable, local isn't.
5. If time remains after the video: decide on the Supabase RLS policy (needs user
   input on what roles/access pattern to scope it to), then optionally Render deploy
   and admin auth, in that order, per this session's advisor-endorsed priority.
