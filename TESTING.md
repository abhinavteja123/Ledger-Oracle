# Testing guide

Everything below was actually run against this repo (not just unit-tested) during
build. See `FAILURES.md` for the real bugs each layer of testing caught.

## The full flow, end to end

```
1. Customer message (raw text)
        |
        v
2. sanitize.py -- strip control chars, cap length, flag instruction-shaped spans
        |
        v
3. parser.py -- Groq/Gemini (gpt-oss-120b), json_schema structured output
   -> StructuredClaim  (claim_type, order_id, claimed_reference, claimed_amount_paise, ...)
   -- cannot express pass/block/escalate, structurally (Rule 3)
        |
        v
4. agent.py -- bounded while-loop (InvestigationState)
   asks the model which read-only tool to call next (or stop), up to 5 steps / 30s:
     get_payment_by_utr | get_order_payments | find_duplicate_captures |
     check_refund_history | check_payment_status
   each call dispatches through tools.TOOL_REGISTRY -> read-only SQLite (data/ledger.db)
        |
        v
5. policy.py -- decide(state, consumed_references)
   pure arithmetic over gathered evidence: I1 (headroom) / I2 (reference resolution) /
   I3 (replay) -- no network, no model, no randomness
   -> Verdict{decision: pass|block|escalate, reason_code, max_refundable_paise, ...}
        |
        +---------------------------+---------------------------+
        v                           v                           v
      PASS                       BLOCK                      ESCALATE
   (nothing else                (nothing else            -> app.py's in-memory
    happens -- this              happens)                   review queue
    project never
    moves money)                                             |
                                                               v
                                                    6. Human reviewer (static/review.html)
                                                       sees: why automation stopped,
                                                       raw claim, every tool call,
                                                       invariant table, why not block
                                                               |
                                                     approve / reject / request info
                                                               |
                                                    approve -> writes the reference into
                                                    the app's consumed_references set,
                                                    so a future replay of the same
                                                    reference hits I3 and blocks

Every step (1-6) is written to audit.jsonl -- append-only, SHA-256 hash-chained.
```

**The zero-LLM path** (`eval/score.py`'s `score()`): skips steps 2-4 entirely, feeds the
synthetic generator's ground-truth structured fields straight into step 5. This is
"ablation run A" -- proves the deterministic engine is correct in isolation, no API key
needed, $0 cost.

## What was actually tested, and how

### 1. Unit tests -- `pytest -q` (84 tests, all offline, zero network)

```bash
pip install -r requirements.txt
python -m pytest -q
```

Covers every architectural rule (Rules 1-3, PRD 5.2), every policy invariant branch
(I1/I2/I3, pass/block/escalate), every agent stop condition (step budget, wall clock,
tool failure, duplicate suppression), every fault-recovery path, the audit hash chain,
tone invariance (structural), the multi-provider LLM fallback logic, and the FastAPI
endpoints (via `TestClient`, fake LLM client, zero network).

### 2. The zero-cost engine, for real -- no API key needed

```bash
python -m eval.generate --seed 42 --out data/     # synthetic ledger + 58 claims
python -m eval.score --data data/                  # 3x3 matrix + rupee cost
python -m eval.score --data data/ --inject-failure db_timeout       # fault injection
python -m eval.score --data data/ --inject-failure db_unavailable
python -m eval.score --data data/ --inject-failure malformed_row
python -m eval.score --data data/ --inject-failure contradictory
python -m eval.seed_audit --data data/ --path audit.jsonl
python -m audit.verify audit.jsonl                  # AUDIT INTEGRITY: OK
python -m eval.tamper --event 1 --field amount_paise --to 999900 --path audit.jsonl
python -m audit.verify audit.jsonl                  # AUDIT INTEGRITY: FAILED (by design)
```

All of this was run for real during this build. `unsafe_failures = 0` and
`graceful_recovery_rate = 1.000` on every tool-boundary fault mode, confirmed.

### 3. The live LLM path -- needs `GROQ_API_KEY` and/or `GEMINI_API_KEY` in `.env`

```bash
cp .env.example .env   # fill in GROQ_API_KEY and/or GEMINI_API_KEY
```

Multi-provider fallback (`llm_client.py`): tries Groq first if `GROQ_API_KEY` is set,
falls back to Gemini (via its OpenAI-compatible endpoint) if `GEMINI_API_KEY` is set.
With only one key set, it's just that one provider, unchanged behaviour. During this
build, Groq and (at the time) Cerebras were tested live -- Cerebras returned `402
Payment Required` on this account (billing not active on the free tier), Groq worked
once two real schema bugs were fixed (see `FAILURES.md`: `required` must list every
property key in strict mode; every tool parameter schema needs
`additionalProperties: false`). Cerebras has since been removed and replaced by
Gemini as the fallback provider; the Gemini model id in `llm_client.py` is unverified
against a live key.

**Parser alone:**
```bash
python -c "
from parser import parse_claim
print(parse_claim('Hi, I was charged twice for order order_4471. UTR 526112345678. Please refund one of them, Rs 2499.').model_dump_json(indent=2))
"
```

**Full agent investigation:**
```bash
python -c "
from parser import parse_claim
from agent import investigate
text = '...'
claim = parse_claim(text)
state, verdict = investigate('test_1', text, extracted=claim, db_path='data/ledger.db')
print(verdict.decision, verdict.reason_code, [t.tool for t in state.tools_called])
"
```

**Full HTTP service:**
```bash
make run     # or: uvicorn app:app --reload
```
Then open `http://127.0.0.1:8000/` (verify page) or `http://127.0.0.1:8000/review`
(reviewer queue), or curl it directly:
```bash
curl -X POST http://127.0.0.1:8000/verify -H "Content-Type: application/json" \
  -d '{"text":"<claim text>"}'
curl http://127.0.0.1:8000/review/queue
curl http://127.0.0.1:8000/review/<claim_id>
curl -X POST http://127.0.0.1:8000/review/<claim_id>/decide \
  -H "Content-Type: application/json" -d '{"action":"approve","note":"..."}'
```

Verified live this session: a real claim with a matching capture correctly `pass`ed
(one tool call, `get_payment_by_utr`); a cheque-payment claim correctly `escalate`d
(`RAIL_NOT_COVERED`) and appeared in the review queue; the reviewer detail view showed
why automation stopped, the raw claim, tool calls, invariants, and why-not-block;
approving it returned `{"ok": true}` and cleared the queue.

### 4. Ablation A/B/C -- needs a live key, real cost is $0 on free tier

```bash
python -m eval.ablation --data data/
```

Runs all three (engine-only ceiling / +parser fixed-order / +agent tool-selection),
prints all three matrices and the comparison. Paces requests (`PACE_SECONDS`) to stay
under free-tier rate limits and degrades a single claim's LLM failure to an escalate
rather than crashing the whole batch.

### 5. Fuzz-distance tolerance sweep -- no API key needed

```bash
python -m eval.sweep --data data/ --fuzz 0,1,2
```

## Known live-testing caveats

- **LLM outputs aren't perfectly deterministic run-to-run** even at `temperature=0` on
  these providers -- the same claim resubmitted can occasionally pick a different (but
  still eventually-correct-or-safely-escalating) tool order. The replay-protection
  property (`approve` -> `consumed_references` -> future replay blocked) is proven by a
  deterministic fake-client unit test in `tests/test_app.py`, not by live resubmission,
  for exactly this reason.
- **Groq's free tier has rate limits.** A full 100+ claim ablation run may hit them;
  `eval/ablation.py` paces requests and degrades gracefully per-claim rather than
  crashing.
