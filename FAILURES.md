# Failure log

Kept from day one, per the PRD (`PRD-Ledger-Oracle.md` §21). Appended as things break,
not reconstructed from memory at the end. Not sanitised — a specific, well-reasoned bug
beats a polished one.

## 2026-08-26 — Model choice (Cerebras Llama) didn't exist

**Symptom.** PRD specified `llama-3.3-70b` via Cerebras Cloud API for the parser and
agent tool-selection calls.

**What I assumed.** Cerebras still serves the Llama 3.x family on its public free-tier
endpoint, as it did historically.

**What was actually true.** Live Cerebras docs and pricing page (checked 2026-08-26) list
only `gpt-oss-120b` and `gemma-4-31b` on the current public endpoint. No Llama model is
listed. Cerebras rotated its public catalog at some point after the PRD's source material
was written.

**Fix.** Confirmed with the user, switched the model ID to `gpt-oss-120b` throughout the
PRD (§13, §14, diagram, repo layout). Kept `gemma-4-31b` as the documented fallback if
free-tier rate limits or latency are a problem during the held-out run.

**What I'd do differently.** Should have verified the model catalog against live docs
before writing a specific model ID into the PRD in the first place, rather than trusting
a plausible-sounding name from training data. Caught before any code was written against
it, so cost was zero — but it could easily have cost a rewrite of `parser.py`/`agent.py`
if found later.

## 2026-08-26 — Typo defect class was unreachable (edit-distance bug)

**Symptom.** Running `eval/score.py` (ablation run A) against the dev set, every
`typo_utr` / `transposed_digit_utr` claim came back `block REF_NOT_IN_LEDGER` instead of
the expected `escalate REF_NEAR_MATCH_TYPO`.

**What I assumed.** `tools.py`'s `_edit_distance` (a `difflib.SequenceMatcher`-based
proxy) would rate an adjacent two-digit swap as distance 1, matching `FUZZ_DISTANCE=1`.

**What was actually true.** The defect class specifically constructs a *transposition*
(two adjacent digits swapped). Plain edit distance (and the SequenceMatcher proxy) charges
2 for a transposition — one delete, one insert — not 1. Only Damerau-Levenshtein, which
gives transposition its own unit cost, rates it as 1. So `REF_NEAR_MATCH_TYPO` was
structurally unreachable for the exact case it exists to catch.

**Fix.** Replaced `_edit_distance` with a real Damerau-Levenshtein (optimal string
alignment) implementation in `tools.py`. No threshold or schema change needed.

**What I'd do differently.** The unit test for `get_payment_by_utr`'s fuzzy match used a
single-character substitution, not a transposition — it passed while the actual defect
class stayed broken. A near-match test should exercise the same construction the
generator actually uses, not a simpler stand-in.

## 2026-08-26 — In-flight capture escaped detection on exact UTR match

**Symptom.** Same `eval/score.py` run: `in_flight_t0` claims came back `pass` instead of
`escalate REF_MAY_BE_IN_FLIGHT`.

**What I assumed.** An unsettled capture would only ever show up in `near_matches`, so the
in-flight check inside `policy.py`'s zero-exact-match branch was sufficient.

**What was actually true.** An in-flight capture still has its real UTR in the connected
ledger — `get_payment_by_utr` returns it as an *exact* match, `settled_at IS NULL` and all.
The in-flight branch never ran because the exact-match branch returned first.

**Fix.** Added the `settled_at is None` check right after exact-match resolution in
`policy.py`, before falling through to amount/headroom checks — not only in the
zero-match branch.

**What I'd do differently.** Should have asked "which branch actually receives this
resolved capture" before writing the in-flight check, instead of assuming it would only
ever arrive via the near-match path.

## 2026-08-28 — Retry mechanism was structurally unreachable

**Symptom.** Writing `tests/test_recovery_paths.py` (the fault-injection harness, PRD
9.4/9.5) to prove a one-shot transient tool failure recovers via retry: a claim with a
real, resolvable capture, faulted only on its first lookup attempt, still came back
`escalate MODEL_STOPPED` instead of the correct `pass`. `state.risk_flags` showed
`DUPLICATE_CALL_SUPPRESSED` where a successful retry should have been.

**What I assumed.** `agent.py`'s duplicate-call suppression (`seen_calls`) and its
retry-on-`ToolError` logic were independent concerns that wouldn't interact.

**What was actually true.** They weren't independent. `agent.py` added `(tool, args)` to
`seen_calls` *before* dispatching the call, regardless of outcome. So when a tool call
failed and the model correctly asked to retry with the identical arguments, that retry
request matched an already-`seen` key and was misclassified as a true duplicate --
`DUPLICATE_CALL_SUPPRESSED` fired, the tool was discarded from `remaining_tools`, and the
retry never actually happened. This wasn't a rare edge case: it broke the *default* retry
path (same tool, same args, after a transient failure) -- the single most common recovery
scenario in the whole design, and the one PRD 9.2's recovery ladder is built around.

**Fix.** Moved `seen_calls.add(call_key)` in `agent.py` from before dispatch to only
after a successful result. A failed attempt no longer blocks its own retry; a call that
already *succeeded* still correctly triggers duplicate suppression if asked again.

**What I'd do differently.** This is exactly the kind of interaction a unit test scoped
to one feature (duplicate suppression, or retry, tested separately) won't catch -- it only
showed up once a test exercised both together end-to-end (a fault-injected retry
scenario). Recovery-path tests that combine features should have been written before,
not after, declaring the bounded-loop tests "done."

## 2026-08-28 — /verify returned 500 instead of MODEL_UNAVAILABLE with no API key

**Symptom.** `app.py`'s test suite was 74/74 green, but actually starting `uvicorn
app:app` and curling `POST /verify` (no `CEREBRAS_API_KEY` configured, the real state of
this environment) returned a raw 500 Internal Server Error, not the designed
`escalate MODEL_UNAVAILABLE` response. Caught by running the server and hitting it for
real, not by the test suite -- the tests all injected a fake client, so none of them
exercised the actual "no key at all" path through `llm_client.get_client()`.

**What I assumed.** `agent.py`'s `CEREBRAS_ERRORS` tuple (six specific Cerebras SDK
exception classes) covered every way a Cerebras call could fail.

**What was actually true.** `Cerebras()`'s constructor raises `CerebrasError` directly
-- the SDK's *base* exception class -- when no API key is present at all, before any
network call is even attempted. `CerebrasError` is the superclass of all six exceptions
`CEREBRAS_ERRORS` enumerated, but the tuple never included the base class itself, so this
specific failure mode (no key configured) fell through every `except CEREBRAS_ERRORS`
clause uncaught.

**Fix.** Changed `CEREBRAS_ERRORS = (CerebrasError,)` in `agent.py` -- the one shared
definition `app.py` also imports -- catching the base class instead of an enumerated
subset. Verified by restarting the server and re-curling `/verify`: now returns
`{"decision":"escalate","reason_code":"MODEL_UNAVAILABLE",...}` as designed.

**What I'd do differently.** "No API key configured" was the single most likely real
condition in this exact build session, and no test exercised it because every test used
a fake client by design (to avoid real network calls). The gap between "unit tests green"
and "the actual running server works" is exactly why the project's own standard is to
start the server and hit it, not just trust the test count.

## 2026-08-28 — Fault injection was silently a no-op in eval/score.py

**Symptom.** Ran `python -m eval.score --data data/ --inject-failure db_timeout` (every
tool call, on every claim, always faulted) and got back the *exact same* perfect
confusion matrix as the unfaulted run -- 100% precision/recall, zero cost, as if nothing
had been injected at all. A fault that changes nothing is more suspicious than a fault
that breaks things.

**What I assumed.** `eval/faults.py`'s `inject_tool_failure` (which patches
`tools.TOOL_REGISTRY[name]["fn"]`) would affect every caller that uses the five tools,
since that's the one registry both `agent.py` and `eval/score.py` are supposed to share.

**What was actually true.** `agent.py` dispatches through `tools.TOOL_REGISTRY[name]
["fn"]`, so patching the registry works for it. But `eval/score.py`'s `TOOL_FUNCS`
lambdas called `tools.get_payment_by_utr(...)` etc. -- the module-level functions --
*directly*, entirely bypassing the registry. Two different dispatch paths existed for
the same five tools, and the fault injector only covered one of them. The zero-cost,
perfect-matrix result wasn't evidence of resilience; it was evidence the fault never ran.

**Fix.** Changed `eval/score.py`'s `TOOL_FUNCS` to dispatch through
`tools.TOOL_REGISTRY[name]["fn"]`, the same path `agent.py` uses -- one dispatch path,
shared by both callers, so `eval/faults.py`'s single patch point actually covers both.
Re-ran the same fault: the matrix now visibly degrades (recall_block drops from 1.000 to
0.188 with every tool always down), and critically `unsafe_failures` stays 0 and
`graceful_recovery_rate` stays 1.000 -- the safety property holds, now actually tested
instead of vacuously true.

**What I'd do differently.** A metric that comes back suspiciously perfect after
"breaking" something is a bug report, not a result -- should have distrusted the
identical-matrix output on sight rather than needing to notice it. This is the second
time in this build a "too clean" result (see the earlier 1.000/1.000/1.000 ablation-A
milestone, which was legitimately correct, vs. this one, which wasn't) needed a second
look to tell the difference between "the system is good" and "the test isn't testing
anything."

## 2026-08-28 — malformed_row fault silently coerced into a wrong-but-valid evidence object

**Symptom.** After fixing the dispatch-path bug above, `--inject-failure db_timeout`,
`db_unavailable`, and `contradictory` all showed `graceful_recovery_rate = 1.000`, but
`malformed_row` alone showed `0.444` (later `1.000` after this fix) -- lower, not because
of unsafe passes (`unsafe_failures` was already 0), but because of extra wrong `block`
decisions on claims that should have passed or escalated.

**What I assumed.** `eval/faults.py`'s `malformed_row` fault (a plain
`{"malformed": True, "tool": name}` dict, deliberately not a typed Result or ToolError)
would either get rejected outright or simply be invisible to `policy.py`'s
isinstance-based evidence readers, matching the ceiling-comment already written for it.

**What was actually true.** `eval/score.py::evaluate()` builds `InvestigationState(...,
evidence=evidence, ...)` through the real Pydantic constructor, which *validates* the
`evidence` list against the `ToolResult` union. `PaymentLookupResult` (the first union
member) has every field defaulted (`matches=[]`, `near_matches=[]`,
`unconnected_sources_exist=False`, ...) and doesn't forbid extra keys, so Pydantic's
union coercion happily turned the malformed dict into a *valid-looking, empty*
`PaymentLookupResult` instead of failing or being ignored. Policy then correctly read
that as "we looked, found nothing, no unconnected sources" and blocked -- wrong for
claims whose real ground truth was pass or escalate, but never a wrong *pass* (an empty
lookup can only lead to block/escalate), which is why the zero-tolerance metric never
caught it while `graceful_recovery_rate` did.

**Fix.** `eval/score.py::gather_evidence()` now applies the same defensive check
`agent.py` already had (`hasattr(result, "model_dump")`) before evidence ever reaches
`InvestigationState`'s constructor -- converting an unexpected shape into a real
`ToolError` up front, so Pydantic never gets a chance to coerce it into something that
merely looks valid.

**What I'd do differently.** "Pydantic will just reject invalid data" is true for a
single field, not automatically true for a Union where one member's schema is all
defaults -- a permissive member can silently absorb garbage meant for a stricter one.
Should have checked what each `ToolResult` union member's schema actually requires
before assuming validation failure was the only possible outcome of feeding it garbage.

## 2026-08-28 — Groq strict json_schema mode rejected StructuredClaim's schema

**Symptom.** First live call to `parser.parse_claim()` through Groq (once a
`GROQ_API_KEY` was added and the multi-provider fallback tried it) failed with
`groq.BadRequestError: 400 ... the following properties must be listed in required:
claimed_amount_paise, claimed_instrument, ...`.

**What I assumed.** Earlier research (docs-summary based, before any live key existed)
suggested strict json_schema mode didn't require every property in `required` --
optionality via `anyOf:[type,null]` looked sufficient on its own.

**What was actually true.** Groq's strict-mode validator (the real OpenAI-strict-mode
convention) requires *every* property key to appear in `required`, full stop --
nullability is expressed by the type union, not by omitting the key from `required`.
Pydantic's `model_json_schema()` only lists non-defaulted fields in `required`, so
`StructuredClaim` (seven of eight fields `Optional[...] = None`) generated a schema
Groq's validator rejected outright.

**Fix.** `parser.py` now overwrites `_SCHEMA["required"]` to include every property key
after generating the schema, once, at import time.

**What I'd do differently.** This is exactly the ambiguity flagged in the PRD-writing
session as "a coin flip written as fact" -- and it stayed a coin flip until a real API
key existed to call. No amount of docs-summary research substitutes for one live call
against the real endpoint. Should have run this the moment any key became available,
before assuming the earlier research was settled.

## 2026-08-28 — Groq strict mode also rejected the tool parameter schemas

**Symptom.** Immediately after fixing the parser's schema (above), the first live
`agent.investigate()` call still failed -- `escalate MODEL_UNAVAILABLE` at 0 steps, no
tool ever called. `agent.py`'s broad `except CerebrasError` (deliberately broad, see the
earlier "base exception" fix) masked the real error until it was called manually.

**What was actually true.** Same class of bug as the parser one, different location:
Groq's strict mode requires `additionalProperties: false` on *every* object schema in
a tool-calling request, not just the top-level `response_format` one. `agent.py`'s
`_tool_schemas()` built each tool's `parameters` object without it.

**Fix.** Added `"additionalProperties": False` to every tool's parameter schema in
`_tool_schemas()`.

**What I'd do differently.** Once one strict-mode gap turned up in the parser's schema,
the tool schemas should have been checked for the same class of issue immediately,
rather than waiting for a second live call to fail the same way. A single root cause
("Groq's strict mode is stricter than Cerebras' was") should prompt checking every
schema in the codebase for it, not just the one that broke first.

**Result: the full pipeline now works live.** `parse_claim()` -> `investigate()` ->
one real tool call (`get_payment_by_utr`) -> `policy.decide()` -> correct verdict
(`block REF_NOT_IN_LEDGER` for a claim referencing a UTR that doesn't exist in the
synthetic ledger), end to end, through Groq via the fallback client, zero cost.

## 2026-08-28 — A resolvable claim wrongly escalated: three compounding gaps

**Symptom.** Live `/verify` on a claim that genuinely should `pass` (real UTR, real
order, real matching capture in the dev-set ledger) came back `escalate
TOOL_UNAVAILABLE`, then after one fix `escalate MODEL_STOPPED` -- never `pass` -- across
three separate live test runs.

**Investigation, three real issues, found in sequence:**

1. `agent.py` called `get_order_payments` first (a defensible choice -- it's evidence
   too), and `policy.decidable()` counted that alone as "ready to decide" for *any*
   claim. But `decide()`'s I2 branch specifically requires a `PaymentLookupResult` when
   the claim has a reference -- order-level evidence alone can't resolve I2. Result: the
   agent stopped early, `decide()` then hit `if lookup is None: escalate
   TOOL_UNAVAILABLE` on a claim it could have resolved correctly if it had kept going.
   **Fix:** `decidable()` now requires a real reference-resolving match (see #2) when
   the claim has one, not just "any of three evidence types."

2. After that fix, a second live run showed the agent using `check_payment_status`
   (not `get_payment_by_utr`) and actually finding the right capture (`found: true`,
   matching UTR) -- but `policy.decide()` never read `PaymentStatusResult` evidence at
   all for I2 resolution. The right evidence existed and was structurally invisible to
   the decision. **Fix:** added `_status_matches()` -- a `check_payment_status` result
   whose own capture's UTR matches the claimed reference now resolves I2 exactly like a
   `PaymentLookupResult` exact match would, used in both `decidable()` and `decide()`.

3. After *that* fix, a third live run showed the agent gathering `get_order_payments`
   evidence containing an exact match in plain sight, then stopping (`MODEL_STOPPED`)
   without ever calling `get_payment_by_utr` to formally verify it -- a genuine
   tool-selection quality gap in the open model, not a code bug. **Fix:** clarified
   `AGENT_SYSTEM` to explicitly state that a claimed reference must be checked via
   `get_payment_by_utr` specifically -- seeing it in an order-level listing isn't the
   same as confirming it. Re-tested: correct `pass`, one tool call, first try.

**What I'd do differently.** Each of these three was found only by testing the *same*
live claim repeatedly after each fix, not by reasoning about the code in the abstract.
Two were real gaps in the deterministic engine (which is supposed to be the trustworthy,
fully-tested half of this system) that no unit test had caught, because the unit tests
all supplied evidence combinations by hand rather than letting a real agent choose what
to gather. A live end-to-end run surfaces exactly the evidence-shape combinations unit
tests don't think to construct.

**Follow-on (found writing the regression test for #2):** fixing I2 to read
`check_payment_status` evidence wasn't enough on its own -- the I1 headroom calculation
(`all_captures`, used for the contradiction check and the refund-headroom sum) still
only pulled from `PaymentLookupResult`/`OrderPaymentsResult`, so a claim resolved purely
via `check_payment_status` correctly passed I2 but then wrongly `BLOCK
EXCEEDS_CAPTURED_TOTAL`'d on I1 (headroom computed as zero, since its one known capture
was invisible to that sum). Added the same capture to `all_captures`. A fix that makes
one invariant see new evidence doesn't automatically make every invariant see it --
each evidence-reading site needs checking independently, not assumed to share a source.

## 2026-08-28 — Generator's amount_text() silently truncated paise in claim text

**Symptom.** Real ablation run B (engine + parser, real free text, live Groq calls)
predicted `pass` for **zero** of 58 dev-set claims -- including all 15 genuinely true
ones. A metric that comes back suspiciously *wrong* in every case is the same red flag
as suspiciously *perfect*: distrust it before reporting it.

**What I assumed.** The generator's claim text always described the exact amount its
own `ground_truth_fields` recorded, so a correct parse of the text would always agree
with ground truth (modulo real parser error, which is what run B is supposed to
measure).

**What was actually true.** `eval/generate.py::amount_text()` always rendered
`paise // 100` -- every format branch, including the one literally named "decimal",
which hardcoded `.00` regardless of the real cents. A capture amount like Rs 366.34.45
became claim text saying "36634/-" or "36,634.00" -- both silently dropping the .45.
The parser, reading the text faithfully, extracted the rounded-down amount. Policy then
correctly compared that against the *true* (un-rounded) ground truth under
`AMOUNT_TOLERANCE_PAISE = 0` and correctly blocked it as `AMOUNT_MISMATCH` -- every
invariant and every parse was right; the generator's text just didn't describe the
amount it claimed to.

**Fix.** `amount_text()` now includes the paise remainder in every format when it's
non-zero (`36634.45/-`, `Rs. 36,634.45`, etc.), so a faithful parse of the text always
agrees with the ground truth it was generated from.

**What I'd do differently.** This is the generator's core promise -- "the text
faithfully describes a known ground truth" -- and it was broken from the very first
commit of `eval/generate.py`, invisible the whole time because ablation run A (the only
run exercised until a live API key existed) never reads the claim text at all, only the
ground-truth fields directly. A generator's most basic invariant (text agrees with the
truth it was built from) deserves its own explicit test, independent of whether any
downstream consumer happens to read the text yet.

## 2026-08-28 — Groq free-tier daily token limit hit mid-session

**Symptom.** A third full ablation run (after fixing the `print_report` crash) came
back 100% escalate for both runs B and C -- worse than the crash-interrupted run before
it, which had at least shown some real signal (6/24 correct passes in run B). A direct
trivial call to the model still succeeded with no error, which briefly looked like it
ruled out rate limiting.

**What was actually true.** `RateLimitError: ... on tokens per day (TPD): Limit 200000,
Used 198894 ...`. Every live test this session -- parser smoke tests, agent
investigations, five separate `/verify` curls, three full-ish ablation attempts --
draws from the same account's daily token budget, and it ran out mid-session. The
trivial call that "succeeded" used ~5 tokens and happened to fit in what was left; the
full structured-output calls (hundreds of tokens each, x58 claims x up to 6 calls in run
C) didn't.

**Not a code bug.** This is exactly the constraint PRD 14.5 named in advance: "the
constraint is throughput, not money." The graceful-degradation design worked as
intended -- every claim that hit the rate limit correctly escalated (`MODEL_UNAVAILABLE`
under the hood), nothing crashed, `unsafe_failures` stayed at 0 even here. The *numbers*
from this particular run are not meaningful (the model never really ran), but the
*system's behavior under total LLM unavailability* is exactly what it's supposed to be.

**What I'd do differently, and what to do next.** A live ablation run this size should
be planned as a single deliberate run against a fresh daily quota, not repeated
speculatively during debugging -- every debugging call (however small) spends from the
same shared budget as the "real" run. The most trustworthy real numbers from this
session are the ones captured *before* the quota ran out (see README's Results
section); a clean final run should wait for the daily quota to reset (or use a second
key) and be run once, not iterated on.

## 2026-08-28 — Supabase direct-connection host is IPv6-only

**Symptom.** `SUPABASE_DB_URL`/`SUPABASE_READONLY_DB_URL` set to the direct connection
string (`postgresql://postgres:...@db.<project-ref>.supabase.co:5432/postgres`) failed
with `could not translate host name ... to address: Name or service not known`.

**What was actually true.** `nslookup` showed the host resolves only to an IPv6
address. This network (a university campus network) had no IPv6 route out. Not a
Supabase or credentials problem.

**Fix.** Switched to Supabase's **pooler** connection string
(`aws-0-<region>.pooler.supabase.com:5432` or `:6543`, IPv4-reachable). Username
changes format in pooler mode: `<role>.<project-ref>` instead of plain `<role>`
(e.g. `postgres.yngbbsrzxpimvogtqvpm`, `ledger_reader.yngbbsrzxpimvogtqvpm`).

**What I'd do differently.** Should have tried raw `nslookup`/socket-level TCP
connectivity before assuming a credentials issue -- the first symptom (DNS resolution
failure) already pointed at network, not auth, and jumping straight to "check the
password" would have wasted a round trip.

## 2026-08-28 — Supabase auto-enabled RLS silently zeroed out ledger_reader's SELECTs

**Symptom.** After fixing connectivity, `ledger_reader` could authenticate and run
queries with no error, but every query against `captures`/`refunds`/
`consumed_references` returned zero rows -- even though the owner connection confirmed
500+ rows existed, and `GRANT SELECT ... TO ledger_reader` had already been run.

**What I assumed.** `GRANT SELECT` alone was sufficient to make the tables readable by
`ledger_reader`, since that's all standard Postgres needs.

**What was actually true.** Supabase auto-enables Row Level Security on tables created
through its SQL editor (a project-level default, not something `supabase/schema.sql`
asked for). With RLS on and zero policies defined, Postgres denies all rows to any
role except the table owner -- a `GRANT` doesn't override RLS; RLS is evaluated in
addition to grants, and a table with RLS on and no permissive policy is default-deny
for everyone but the owner. `SELECT relrowsecurity FROM pg_class` confirmed all three
tables had it enabled.

**Fix.** `ALTER TABLE captures/refunds/consumed_references DISABLE ROW LEVEL
SECURITY`. Not a security downgrade here -- `GRANT SELECT` already scopes
`ledger_reader` to exactly these three read-only tables, so RLS was redundant, not the
thing providing the restriction. Added the same `ALTER TABLE` statements to
`supabase/schema.sql` so a fresh run of the script won't hit this again.

**What I'd do differently.** "GRANT SELECT succeeded, no error" was treated as proof
the read path worked -- it wasn't. Should have checked `pg_class.relrowsecurity`
immediately when the query silently returned zero rows instead of an error, rather
than re-testing the same query repeatedly assuming a connection-string or
role-creation mistake. A silent zero-rows result with no exception is exactly the kind
of failure that looks like "it worked, there's just no data" and needs to be
distrusted the same way an unexpectedly perfect number does.

## 2026-08-28 — Admin dashboard silently missed parse-failure and MODEL_UNAVAILABLE escalates

**Symptom.** Live-testing `/verify` against the real Supabase-backed deployment: a
call that hit `MODEL_UNAVAILABLE` returned the correct escalate response, but never
showed up in `/admin/claims` afterward.

**What was actually true.** `app.py`'s `/verify` handler has two early-return paths
(`except ParseFailedError`, `except CEREBRAS_ERRORS`) that build an escalate-shaped
JSON response directly, before an `InvestigationState`/`Verdict` object ever exists --
and `history_store.record(...)` was only called later, on the normal success path.
Every parse failure or model-unavailable escalate was invisible to the admin
dashboard, which is exactly the failure mode an admin most needs visibility into.

**Fix.** Both early-exit branches in `app.py`'s `/verify` now construct a minimal
`InvestigationState`/`Verdict` and call `history_store.record(...)` before returning,
wrapped in the same try/except-and-ignore pattern the main path already uses (history
logging must never block the decision response).

**What I'd do differently.** The `HistoryStore.record()` call being present on the
"happy path" doesn't mean it's present on every path that returns a response --
should have grepped every `return` statement in the handler for whether it went
through the logging call, not assumed one code path implied all of them did.
