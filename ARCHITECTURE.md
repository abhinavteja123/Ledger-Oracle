# Architecture

> The agent gathers evidence. The policy engine decides.

## System view

```
                    CUSTOMER CLAIM  (WhatsApp / email / ticket)
                             |
                             v
                +---------------------------+
                |  Input Sanitizer          |   strips control chars, caps length,
                |  + Injection Guard        |   flags instruction-shaped spans
                +-------------+-------------+   (defense-in-depth, NOT the defense)
                              v
                +---------------------------+
                |  Claim Extraction (LLM)   |   gpt-oss-120b via Cerebras Cloud API
                |  -> StructuredClaim       |   output type cannot express a decision
                +-------------+-------------+
                              v
          +-------------------------------------------+
          |      BOUNDED INVESTIGATION AGENT          |
          |                                           |
          |   max 5 steps  |  max 2 retries per tool  |
          |   read-only tool allowlist                |
          |   30s wall clock  ->  ESCALATE             |
          |   explicit InvestigationState             |
          +---------------------+---------------------+
                                |
         +----------------+-----+------+----------------+
         v                v            v                v
  get_payment_      get_order_    find_duplicate_   check_refund_
  by_utr()          payments()    captures()        history()
         |                |            |                |
         +----------------+-----+------+----------------+
                                v
                       EVIDENCE (typed, append-only in state)
                                |
                                v
                +---------------------------+
                |  DETERMINISTIC POLICY     |   I1 / I2 / I3 + tolerances
                |  ENGINE   -- DECIDES      |   no network. no model. no randomness.
                +-------------+-------------+
                              |
            +-----------------+-----------------+
            v                 v                 v
          PASS              BLOCK           ESCALATE
                                                |
                                                v
                                    +-----------------------+
                                    |  HUMAN REVIEW QUEUE   |
                                    |  approve / reject /   |
                                    |  request more info    |
                                    +-----------+-----------+
                                                v
                                         FINAL DECISION

every step above  ->  APPEND-ONLY HASH-CHAINED AUDIT TRAIL (audit/)
```

## The three architectural rules

Each enforced by a test, not by prose:

1. **The policy engine is offline.** `policy.decide()` takes an `InvestigationState`
   and a `consumed_references` set, returns a `Verdict`. No import from `parser.py`,
   `agent.py`, or `narrator.py`. No network call.
   `tests/test_policy_is_offline.py` monkeypatches `socket.socket` to raise, runs
   `decide()` across pass/block/escalate paths, confirms none of them touch it.

2. **The agent cannot write.** Every tool in `tools.TOOL_REGISTRY` has `mutates=False`,
   and the SQLite connection is opened `file:ledger.db?mode=ro`.
   `tests/test_tool_allowlist_is_readonly.py` asserts both, and confirms an `INSERT`
   through that connection actually raises `sqlite3.OperationalError` (not just that
   nothing in the code happens to call one).

3. **The model cannot decide.** `StructuredClaim` (the parser's output type) has no
   field that can express `pass`/`block`/`escalate`. Neither does any tool return type.
   `tests/test_no_decision_field_in_model_outputs.py` walks every field of every
   LLM-boundary model (recursing into nested types) and fails if any field's type could
   hold a `Decision` value.

Rule 3 is also the prompt-injection defense (see below) -- not a filter, a type-system
argument.

## Repository layout (as built)

```
models.py            all Pydantic models -- the shared contract every other file imports
config.py             the agent's constitution + policy tolerance knobs
tools.py              5 typed read-only tools + TOOL_REGISTRY, read-only SQLite
sanitize.py            defense-in-depth: flags, never drops content
llm_client.py          lazy Cerebras client construction (gpt-oss-120b)
parser.py              free text -> StructuredClaim (Cerebras, json_schema mode)
agent.py               bounded while-loop + InvestigationState -- investigate()
policy.py              DECIDES. no network, no model, no randomness. decide()
audit/                 append-only hash-chained audit trail
  __init__.py             record_event(), append(), chain verification primitives
  verify.py               python -m audit.verify audit.jsonl
app.py                 FastAPI: /verify, /review/*, serves static/
static/
  verify.html             claim -> verdict, single-claim UI
  review.html             human review queue UI
eval/
  generate.py             synthetic ledger + claims + MANIFEST.json (PRD 17)
  score.py                3x3 matrix + rupee cost, ablation run A, shared scoring helpers
  ablation.py             runs A / B / C, PRD 18.6
  sweep.py                FUZZ_DISTANCE tolerance sweep, PRD 18.5
  faults.py               deterministic fault injection at tool + LLM boundaries
  tamper.py               breaks the audit chain on purpose, for the demo
tests/                  one file per architectural property or PRD section
data/                   generated, gitignored: ledger.db, claims.jsonl, MANIFEST.json
```

## Why a plain loop, not LangGraph

The human-review pause is a separate FastAPI page reading a queue, not a graph
`interrupt()`. `InvestigationState` is a Pydantic model; `agent.py`'s loop is a `while`
loop with explicit stop conditions. Per PRD 8.2: LangGraph earns its dependency only if
the human-review pause is implemented as a checkpointed graph interrupt. It isn't here,
so the dependency isn't either -- one fewer thing that can fail on stage, and the
"AI judgment" axis explicitly rewards saying where a tool was deliberately not used.

## Why Cerebras + gpt-oss-120b, not the originally-planned model

The build started assuming a Llama model on Cerebras. Live docs (checked mid-build)
showed Cerebras' current public catalog is `gpt-oss-120b` and `gemma-4-31b` -- no Llama
family listed. Switched to `gpt-oss-120b`, logged in `FAILURES.md`. The Cerebras choice
itself (over a paid frontier API) is deliberate: **$0 cost on the free tier**, fast
inference, and it removes an entire class of demo-day risk (no API key billing, and the
system degrades to `MODEL_UNAVAILABLE` -> escalate rather than failing open if the LLM
is ever unreachable -- consistent with the rest of the failure-recovery design).

## Prompt injection: the structural argument

A customer message is untrusted content and arrives directly in the parser's context.
The weak answer is a regex filter -- a judge can always find a phrasing it misses. The
strong answer: `StructuredClaim` has no field that can express a decision. An injected
instruction can influence what gets *extracted*, and that's measured (parser accuracy),
but there is no channel through which it can reach the *decision*, because the decision
is arithmetic computed after the model is finished. The adversarial test class
(`eval/generate.py`'s `gen_adversarial`) demonstrates rather than asserts this: injected
claims parse cleanly into the schema and then get handled by an invariant (usually
`ESCALATE INSUFFICIENT_CLAIM_DATA`), not caught by a filter.

## Tamper-evident, not tamper-proof

Hash-chained append-only JSONL written by a single process detects *partial* tampering
(an edited/deleted/reordered row) -- it cannot stop someone who rewrites the whole file
and recomputes the chain. `audit/verify.py` + `eval/tamper.py` demonstrate exactly this:
`AUDIT INTEGRITY: OK` on a clean chain, `AUDIT INTEGRITY: FAILED` at the exact tampered
event once one field is changed without recomputing its hash.

## Explicit non-goals

- **Not** a fraud score. No probability, no risk rating, no ML model over transactions.
- **Not** a chargeback responder. Never drafts or files evidence.
- **Not** a reconciliation engine. Queries one ledger; never matches two files against
  each other.
- **Not** an image forensics tool. Never looks at a screenshot to decide anything --
  the dataset is structured claim records (UTR, amount, timestamp, VPA, instrument),
  zero image assets anywhere in the repo.
- **Not** an autonomous refunder. No write path. `POST /review/{claim_id}/decide` is a
  human action; `approve` is the only path that ever marks a reference consumed.
