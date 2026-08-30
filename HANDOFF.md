# Session handoff — Ledger Oracle

Written at end of session. Read this before doing anything else next session.

## What this project is

Razorpay AI Buildathon 2026, Track 02 (AI Risk Manager). A bounded LLM agent
investigates refund claims against a merchant ledger; a deterministic policy engine
(no LLM in the decision path) decides pass/block/escalate. Full design:
`PRD-Ledger-Oracle.md`, `ARCHITECTURE.md`. Full flow + how to test every layer:
`TESTING.md`. Every real bug found across all sessions, with root cause: `FAILURES.md`.

**Submission requirement (PRD §19): public repo + a 5-minute pitch video +
architecture doc. No live deployed URL required.** The only undone deliverable is
the video. Nothing is committed either — that's now also a real gap, see below.

## Current state: 129/129 tests passing, nothing committed

`python -m pytest -q` → 129 passed (was 98 at the last handoff; +31 this session).
Everything is sitting **uncommitted**, deliberately — user commits under their own
name. `git status --short` shows ~34 changed/new files. Check it before doing
anything destructive.

Server run this session: `LEDGER_BACKEND=supabase uvicorn app:app --host 127.0.0.1
--port 8000`. Landing (`/`), verify (`/verify-page`), admin (`/admin`), review
(`/review`) all live-tested against the real Supabase project this session, not just
pytest.

## What this session did (long session, several distinct workstreams)

### 1. Safety hardening — adversarial corpus, evidence-binding fix, risk-flag wiring

- **`eval/adversarial_corpus.py`** (new) — prompt injection, homoglyph/zero-width UTR
  disguises, fake `<tool>` spans, amount/order contradictions, run through the real
  `sanitize -> parse -> decide` pipeline, zero live LLM. `unsafe_pass_count` is now a
  headline metric on every `eval.score` run (`eval/score.py`), not just
  fault-injection mode.
- **Real bug fixed, `policy.py`**: `decide()` accepted `get_payment_by_utr` evidence
  without checking the returned row's UTR matched the claim's own
  `claimed_reference`. The agent picks its own tool-call args — nothing forced them
  to match the claim. A wrong-UTR search (confusion or injection) could smuggle in
  unrelated real evidence as if it verified the claim. Fixed + regression test in
  `tests/test_policy_invariants.py`.
- **Real bug fixed, production-path dead code**: `AMBIGUOUS_ORDER` /
  `CONTRADICTORY_AMOUNTS` / `CONTRADICTORY_CLAIM` escalation branches existed in
  `policy.py` (PRD 10.3) but nothing in the live request path ever populated
  `risk_flags` — only the eval harness's synthetic ground-truth generator did. Dev-set
  cleanliness on this class was partly an artifact of that. Fixed:
  `sanitize.detect_risk_flags()` (new, regex heuristic, same defense-in-depth
  convention as the existing instruction-pattern detector) runs on every real claim
  now, wired through `agent.investigate()`'s new `risk_flags` param into `app.py`.
  Also fixed in `agent.py`'s `finish()`: when the model stops before calling any
  tool, `decide()` never runs at all, so its risk_flags check is unreachable — added
  a fallback that prefers the specific adversarial flag over a generic
  `MODEL_STOPPED` when one was set. Found live, not by inspection.
- **`tests/test_replay_determinism.py`** (new) — re-executes real historical claims'
  logged tool calls against the live ledger, asserts recomputed verdict matches what
  was actually decided. Includes a mutation test proving the harness is non-vacuous.
  Took two rounds to get right: the mutation planting logic initially assumed a tool
  call's search arg always equals the claim's own `claimed_reference` — real parser
  output sometimes bakes a label into the reference (`"UTR 000000000000"` instead of
  `"000000000000"`), which is a *different*, separately-fixed bug (see below), and
  broke that assumption. Fixed by requiring the target claim's tool-arg and
  claimed_reference to already agree, which is the real precondition.
- **`eval/agreement.py`** (new) + `GET /admin/agreement` — engine-vs-human agreement
  rate from `audit.jsonl`, broken down by `reason_code`, surfaced in the admin UI.

### 2. Supabase RLS — fixed live, not just written

Confirmed via Supabase MCP before touching anything: `captures`/`refunds`/
`consumed_references` had RLS **disabled**, and `anon`/`authenticated` held not just
`SELECT` but **INSERT/UPDATE/DELETE/TRUNCATE** (Supabase's default grants on
SQL-editor-created tables — worse than the prior handoff's own finding, which only
flagged the read exposure). Anyone with the anon key could have wiped the ledger.

Applied live via `apply_migration`: `REVOKE ALL` from `anon`/`authenticated`,
`ENABLE ROW LEVEL SECURITY`, explicit `SELECT`-only policy for the app's
`ledger_reader` role (confirmed via `pg_roles` it lacks `BYPASSRLS`, so a blank
enable would've reproduced the exact zero-rows bug already in `FAILURES.md`).
Verified: critical `rls_disabled` advisory cleared, `pg_policies`/grants queries
confirm the fix, and a real end-to-end `/verify` call against the live database
post-fix returned a correct `pass` with real evidence. `supabase/schema.sql` updated
to match so a fresh run reproduces this.

### 3. Cerebras removed end-to-end, Gemini added as second provider

`CerebrasError` was the *universal* "any LLM failure" exception type, imported
directly from Cerebras's SDK and threaded through `agent.py` (`CEREBRAS_ERRORS`),
`app.py`, `eval/faults.py`, `eval/ablation.py`, and 4 test files — not just
`llm_client.py`. All of it renamed to a provider-neutral `LLMProviderError`, defined
in `llm_client.py`. Gemini added via its **OpenAI-compatible endpoint**
(`generativelanguage.googleapis.com/v1beta/openai/`, through the `openai` package) —
deliberately not Google's native SDK, since the OpenAI-compat shape means zero
changes to `parser.py`/`agent.py`'s existing tool-calling/schema code.
`requirements.txt`, `.env.example`, `render.yaml` updated (`GEMINI_API_KEY` replaces
`CEREBRAS_API_KEY`). `grep -rn cerebras --include=*.py .` is clean.

**`GEMINI_API_KEY` was added to `.env` later this session and confirmed live**: raw
completion, strict-mode `json_schema` extraction (`parser.py`), and tool-calling
(`agent.py`) all tested with Gemini as the *only* configured provider (Groq
temporarily unset for the test, so nothing was masking a Gemini failure) — real
end-to-end `pass/OK` verdict against live Supabase data. Model id
`gemini-2.5-flash` confirmed working as used, not just plausible. Groq alone also
still works (live-tested repeatedly, real pass/block/escalate verdicts).

### 4. Frontend: landing page, verify page, admin page

- **`static/landing.html`** — rebuilt from a 49-line role-picker stub into a real
  explainer (problem, how it decides, explicit "never moves money" section framing
  `max_refundable_paise` as an authorization ceiling not a disbursement, proof stats,
  demo entry cards). No React/Vite — plain HTML held up fine, matches this repo's
  whole frontend approach; a framework rewrite was explicitly cut earlier this
  session as zero-judging-value scope creep.
- **`static/verify.html`** — `/verify`'s JSON response never included a
  human-readable explanation, only the raw `reason_code`; the page rendered that raw
  code as the primary thing a reader saw. Fixed: `app.py`'s `_verdict_response()`/
  `_escalate_response()` now include `reason_text` (from the already-existing
  `REASON_CODE_TEXT` map, previously only used for `/admin`), and the page shows a
  plain-English headline ("Refund authorized" / "Claim blocked" / "Sent for human
  review") with the raw code demoted to small secondary detail. Also added **6
  one-click example-claim chips** (pass / block / near-match typo / ambiguous order /
  prompt injection / Hinglish) so every decision path is reachable without typing —
  chosen against real, live-verified Supabase data (order_4158 / UTR
  565463516618 is a real settled capture).
- **`static/admin.html`** — real bug found: the page's own code comment asserted a
  response shape it had only validated against the in-memory backend; the live
  `LEDGER_BACKEND=supabase` shape is materially flatter (`claims_history` has no
  `stop_reason_text` column, no nested `extracted`, different note field names — see
  `supabase/schema.sql`). This made the "Extracted" panel show `-- did not parse --`
  for claims that parsed fine, and `max_refundable_paise` wasn't shown anywhere.
  Fixed defensively (handles both shapes) and reorganized into 4 clearly-labeled tabs
  (Claims / Engine vs Human / Audit chain / Ledger). **Flagged, not fixed**: the two
  backends returning different shapes for the same logical data is a real schema/
  `app.py` gap, not a frontend one — `claims_history` should carry the same fields
  the in-memory path does.
- **Parser bug fixed**: `claimed_reference` sometimes comes back with a label baked
  in (`"UTR 526112345678"` instead of `"526112345678"`) — confirmed live, twice,
  intermittent (clean phrasing parses clean). A real reference the ledger has would
  wrongly resolve to `REF_NOT_IN_LEDGER`. Fixed in `parser.py`: strips `UTR`/`RRN`
  label prefixes after extraction, deterministic hygiene, not prompt-tuning.

### 5. README

Extended (not rewritten — the existing positioning/results/cost-model content was
already strong) with: a route table for the live demo pages, honest Gemini-untested
caveat, and a new "Safety hardening beyond the ablation runs" section documenting
everything in §1-2 above with file references.

## LLM providers: Groq confirmed live, flaky same as before; Gemini untested

Groq: live-tested many times this session, real results. Still shows the same
intermittent issue as prior sessions — an ambiguous/Hinglish claim occasionally gets
`claim_type: null` from the model, which fails Groq's strict-schema validation
server-side as a `400`, and that's **not retried** by `parser.py` (its retry loop
only catches local Pydantic validation failures, not the provider's own schema
rejection) — surfaces as `MODEL_UNAVAILABLE` on the first attempt. Not fixed this
session (flagged, not chased — would need either prompt tuning or extending the
retry loop to catch provider-side 400s too). Real risk for the demo: a judge typing
an ambiguous claim could hit this.

Gemini: code-complete, `GEMINI_API_KEY` never added to `.env` this session, so never
actually called. Add the key and re-test before relying on it as a real fallback.

## Known gaps, not fixed this session

- **`claims_history` schema drift between backends** — see §4 above (admin.html
  finding). In-memory path and Supabase path return different field shapes for
  logically the same data.
- **Groq schema-validation 400s aren't retried** — see above.
- **`admin_claim_decide` has no server-side guard** against deciding an
  already-non-open claim (flagged in an earlier pass this session, still open — one
  real log entry hit this).
- **CONTRADICTORY_CLAIM detector is narrow** (`sanitize.detect_risk_flags()`) — a
  specific negation+refund-request phrase pattern, not general contradiction
  detection. Documented ceiling, matches this repo's existing defense-in-depth
  convention (not meant to be exhaustive).

## What's NOT done yet

- **Video (PRD step 16)** — still the only actually-required-and-missing deliverable.
- **Nothing committed** — `git status --short` shows ~34 files. Public repo is a
  graded deliverable; this now blocks it same as last handoff did.
- **One clean final ablation run (B/C)** — still not re-run, still needs sustained
  LLM availability this session didn't reliably have. README's caveat on this stands.
- **Render deploy + admin auth** — still optional/deferred, unchanged from last
  handoff.
- **Buildathon eligibility** ("students-only per the live page") — still unconfirmed
  across three sessions now. Two-minute check, not a code task, worth doing before
  more build time goes in.

## Immediate next steps, in order

1. `git status --short` + `python -m pytest -q` to reconfirm state (129/129 expected).
2. If presenting soon: record the video using the local `uvicorn` demo
   (`LEDGER_BACKEND=supabase`, or `sqlite` if network's unreliable that day) — same
   reasoning as every prior handoff, a fresh deploy breaking pre-submission is
   unrecoverable, local isn't.
3. Commit. Nothing about this being deferred has changed across sessions — do it
   before the next context runs out too.
4. If time remains: the Groq-400-not-retried gap and the `claims_history` schema
   drift are the two most concrete, scoped next fixes.
