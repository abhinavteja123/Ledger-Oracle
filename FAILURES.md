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
