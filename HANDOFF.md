# Session handoff — Ledger Oracle

Written at end of session. Read this before doing anything else next session.

## What this project is

Razorpay AI Buildathon 2026, Track 02 (AI Risk Manager). A bounded LLM agent
investigates refund claims against a merchant ledger; a deterministic policy engine
(no LLM in the decision path) decides pass/block/escalate. Full design:
`PRD-Ledger-Oracle.md`, `ARCHITECTURE.md`. Full flow + how to test every layer:
`TESTING.md`. Every real bug found across all sessions, with root cause: `FAILURES.md`.

**Submission requirement (PRD §19): public repo + a 5-minute pitch video +
architecture doc. No live deployed URL required.** The video is still the only
missing deliverable — see below.

## Current state: 136/136 tests passing, committed (`phase-5`, `55ee20f`)

`python -m pytest -q` → 136 passed (was 129 at the last handoff; +7 this session).
Everything from this session is committed under the user's own name — no
uncommitted-work gap carrying into next session, unlike the last two handoffs.

Server run this session: `LEDGER_BACKEND=supabase uvicorn app:app --host 127.0.0.1
--port 8000`. All four routes (`/`, `/verify-page`, `/admin`, `/review`) were driven
end-to-end with a real Playwright browser against the live Supabase project — not
just curl, not just pytest. Every tab, every decide flow, both example-chip abuse
scenarios, all exercised for real.

## What this session did

### 1. Repeated-claim abuse guard (new feature, user-requested)

A customer (or attacker) can keep resubmitting the same denied order hoping a
non-deterministic model eventually mis-escalates into an approval. Fixed:

- **`config.py`** — `ADVERSE_REASON_CODES` (the terminal-denial reason codes that
  count against an order — `REF_NOT_IN_LEDGER`, `AMOUNT_MISMATCH`,
  `EXCEEDS_CAPTURED_TOTAL`, `CONTRADICTORY_LEDGER_RECORDS`, `REF_ALREADY_CONSUMED`,
  `AMBIGUOUS_ORDER`, `CONTRADICTORY_AMOUNTS`, `CONTRADICTORY_CLAIM`) and
  `ABUSE_REPEAT_THRESHOLD = 3`. Deliberately **excludes** retry-invited outcomes
  (`REF_MAY_BE_IN_FLIGHT`, `TOOL_UNAVAILABLE`, `MODEL_UNAVAILABLE`, `PARSE_FAILED`,
  `REF_NEAR_MATCH_TYPO`) — those are cases the engine itself says to resubmit, and
  must never count against an honest customer.
- **`policy.py`** — new `REPEATED_CLAIM_ABUSE` gate in `decide()`, fires before any
  ledger evidence is consulted (mirrors the existing risk_flags short-circuit).
  `decidable()` also short-circuits on the threshold so the agent loop doesn't waste
  a real tool call gathering evidence for a claim that's going to block on history
  alone. Still zero I/O in `policy.py` — the count is a plain int the caller computes
  and passes in, same pattern as `consumed_references`.
- **`app.py`** — `HistoryStore.count_adverse_attempts(order_id)`, handles both
  backend shapes (Supabase's flat `order_id`/`reason_code` columns vs. the in-memory
  path's nested `extracted.order_id`). Wired into `/verify` before `investigate()`.
- **Live-verified, not just unit-tested**: submitted the same denied claim
  (order `4471`) 4x against the real Supabase-backed server — 3x `REF_NOT_IN_LEDGER`,
  4th `REPEATED_CLAIM_ABUSE`, blocked before any tool call ran. Confirmed again via
  Playwright with the actual React UI's abuse-note callout rendering correctly.

### 2. Frontend rewired to a real React SPA (user-requested, replaces all static HTML)

`static/{landing,verify,admin,review}.html` deleted. New Vite + React 18 +
react-router SPA under `frontend/`, four pages (`Landing`, `Verify`, `Admin`,
`Review`), shared design system (`frontend/src/theme.css`) and API layer
(`frontend/src/api.js`). Built output committed at `static/dist/` so a fresh clone
still runs via `uvicorn app:app` alone — no npm step required to demo, though
`cd frontend && npm run dev`/`npm run build` both work for further edits.
`app.py`'s `/`, `/verify-page`, `/admin`, `/review` all now serve `static/dist/index.html`;
`/static` is mounted at `static/dist`.

`Landing.jsx`/`Verify.jsx` ported directly (same content/architecture explanation as
the prior static pages, restyled blue/gold gradient, motion via IntersectionObserver
reveals — no framer-motion dependency, kept the build zero-config). `Admin.jsx`/
`Review.jsx` were built by two parallel subagents from the old pages' exact
behavior as spec, including **preserving the dual-backend shape fallback** admin.html
had already fixed (Supabase flat columns vs. in-memory nested `extracted`) — verified
correct live via Playwright, not just by reading the diff.

**Scope cut, deliberate**: this was scoped to landing + verify + admin + review (all
four, since the user asked for full conversion) but *not* a rewrite of any backend
response shape — `claims_history`'s schema drift (see Known gaps) is still exactly
as flagged in the last two handoffs, just now handled client-side in React instead
of vanilla JS.

### 3. Two real bugs found live via Playwright E2E testing (not from inspection)

- **`.env` cold-start bug, `llm_client.py` + `app.py`**: `SUPABASE_DB_URL` /
  `SUPABASE_READONLY_DB_URL` were only ever loaded into `os.environ` as a side effect
  of `llm_client.get_client()` running first — `db.py` reads them directly and never
  triggered that load. Hitting `/admin/claims` cold (before any `/verify` call in the
  process) threw a raw `KeyError`. Fixed: `llm_client._load_dotenv` made public
  (`load_dotenv`), called once at `app.py` import time — but **gated on
  `LEDGER_BACKEND=supabase`**, not unconditional. First attempt at this fix was
  unconditional and silently broke 13 tests (each passed alone, failed together —
  real GROQ/GEMINI keys got loaded into the test session's env, so tests that forgot
  to inject a fake client made real, rate-limited network calls instead of failing
  fast on `LLMProviderError("no LLM provider configured")`). Caught by running the
  full suite, not by reasoning about it in advance.
- **Gemini fallback was completely dead, `llm_client.py`**: `_PROVIDER_MODEL_IDS["gemini"]`
  was `gemini-2.5-flash`, confirmed working *last* session — now 404s live
  ("no longer available to new users... use models/gemini-3.6-flash"). This matters
  because Groq's known intermittent parse-step failure (see LLM providers section
  below) had **zero working fallback** until this fix landed. Updated to
  `gemini-3.6-flash`, live-verified (real content returned) before landing.
  Live-reconfirmed after the fix: an ambiguous-order claim that previously died with
  `MODEL_UNAVAILABLE` now correctly resolves to `AMBIGUOUS_ORDER`.

### 4. Two known gaps from the last handoff, closed

- **Groq schema-validation 400s now retried** (`parser.py`) — a provider-side
  rejection (e.g. `claim_type: null` failing Groq's strict schema) used to bubble
  past the retry loop on attempt 1; now retried up to `MAX_PARSE_ATTEMPTS`, same as a
  local Pydantic failure, before surfacing `MODEL_UNAVAILABLE`. Tested with a fake
  client that raises once then succeeds (`tests/test_parser.py`).
- **`admin_claim_decide` now guards against deciding an already-decided claim**
  (`HistoryStore.decide()` in `app.py`) — raises a clear error instead of silently
  double-writing `consumed_references` or overwriting a prior note. Covered by
  `tests/test_app.py::test_admin_claim_decide_rejects_second_decision`; also
  effectively unreachable via the new React UI, which renders a read-only summary
  instead of the decide form once `status != "open"`.

## LLM providers: both confirmed live this session, Groq's known flake unfixed

Groq: live-tested repeatedly (parse calls, tool-calling, both through
`get_client()`'s real wrapper) — works. Still shows the same intermittent issue as
every prior handoff: an ambiguous/Hinglish claim occasionally gets `claim_type: null`
from the model, which Groq's strict-schema validator 400s server-side. That failure
is now **retried** (see §4 above) rather than surfacing immediately, which reduces
but does not eliminate the risk — if it 400s on every attempt within
`MAX_PARSE_ATTEMPTS`, it still exhausts to `MODEL_UNAVAILABLE`... except now Gemini
is a real, live-confirmed fallback underneath the retry too (§3 above), which is the
practical fix.

Gemini: live-tested this session with the corrected model id (`gemini-3.6-flash`) —
raw completion confirmed working. Not separately tested as the *sole* provider this
session (Groq was configured throughout) — worth a quick Groq-unset check next
session if you want to re-confirm Gemini's own tool-calling/strict-schema paths
independently, the way last session did for the old id.

## Known gaps, not fixed this session

- **`claims_history` schema drift between backends** — unchanged from the last two
  handoffs. In-memory path and Supabase path still return different field shapes for
  logically the same data; both the old admin.html and the new `Admin.jsx` handle it
  client-side, but the underlying `app.py`/`supabase/schema.sql` inconsistency is
  still there.
- **CONTRADICTORY_CLAIM detector is narrow** (`sanitize.detect_risk_flags()`) —
  unchanged, a specific negation+refund-request phrase pattern, not general
  contradiction detection. Documented ceiling, not meant to be exhaustive.
- **Supabase test data is now "poisoned" for demo purposes** — order `4471` and a
  handful of claim IDs (`clm_98cc10b7`, `clm_d2dd495e`, others) carry real
  `REPEATED_CLAIM_ABUSE`/decided history from this session's live testing. The
  `verify-page`'s "Unknown ref → BLOCK (click 4x for abuse guard)" chip will now
  block on the *first* click against this Supabase project, not the 4th, since the
  threshold was already crossed. Not a bug — proves the feature works — but pick a
  fresh order_id if you want a clean 4-click demo for the video, or reset
  `claims_history` for that order first.

## What's NOT done yet

- **Video (PRD step 16)** — still the only actually-required-and-missing deliverable.
- **Deploy** — user said "next we will deploy it after all done" this session; not
  started. `render.yaml` exists from an earlier session but is unverified against the
  current frontend build step (`static/dist` is committed so it shouldn't need a
  build hook, but confirm Render serves the repo as-is before relying on that).
- **One clean final ablation run (B/C)** — still not re-run, still needs sustained
  LLM availability. README's caveat on this stands.
- **Admin auth** — still optional/deferred, unchanged from every prior handoff.
- **Buildathon eligibility** ("students-only per the live page") — still unconfirmed
  across four sessions now. Two-minute check, not a code task, worth doing before
  more build time goes in.

## Immediate next steps, in order

1. `python -m pytest -q` to reconfirm state (136/136 expected); `git log --oneline -3`
   should show `phase-5` at HEAD.
2. If presenting soon: record the video using the local `uvicorn` demo
   (`LEDGER_BACKEND=supabase`, or `sqlite` if network's unreliable that day) — same
   reasoning as every prior handoff, a fresh deploy breaking pre-submission is
   unrecoverable, local isn't. Use a fresh order_id for the abuse-guard chip demo
   (see the data-pollution note above) if you want the full 4-click arc on camera.
3. Deploy, if that's still wanted before the video — confirm Render's build actually
   serves `static/dist` as committed rather than trying to run `npm run build` itself
   (no `frontend/node_modules` is committed, by design).
4. If time remains: `claims_history` schema drift is the most concrete next fix —
   everything else client-side now handles it, but the root cause (two backends, two
   shapes) is still open.
