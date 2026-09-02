# Ledger Oracle — Video Brief

Source material for an AI-generated explainer (NotebookLM Video Overview, Gemini,
or similar). Written to answer, in order: why this exists, what's broken today,
what we built, how it works, and what to watch in the live demo.

Razorpay AI Buildathon 2026 — Track 02: AI Risk Manager.

---

## 1. The problem (why this exists)

A customer messages support: "I was charged twice for order #4471, refund one
of them." Every refund guardrail on the market today checks whether that claim
is **allowed** — is the amount inside the limit, is the scope right, are the
approvals in place. Almost none of them check whether the claim is **true**.

Walk that claim through a typical stack: the amount (₹2,499) is inside the
refund limit. Scope is fine. PII is clean. Every config guardrail says
approved. The refund pays out — even though there was only one charge, not
two — because nothing upstream ever looked at the actual payments ledger. Four
major Indian payment processors have publicly written about this exact scam
pattern. Their own advice is "verify the UTR against your bank statement" —
precisely the check nothing in the stack automates.

That's the gap: **claim → agent decides → config guardrails check policy →
money moves**, with nothing in that chain ever asking "does the ledger agree
with this story?"

## 2. What we built

Ledger Oracle sits in that gap. One narrow job: given a refund claim in plain
text, decide **pass** (refund authorized), **block** (claim contradicts the
ledger), or **escalate** (evidence insufficient — a human decides, not a
guess) — by actually checking the claim against the real payments ledger.

It is explicitly **not**: a fraud-scoring system, a chargeback responder, an
image-forensics tool, or an autonomous refunder. It doesn't move money. It
answers one upstream question other systems never ask.

## 3. How it decides (the architecture, in one sentence)

**A bounded LLM agent investigates. A deterministic function decides. The
model is never in the room when money is authorized — structurally, not by
convention.**

Two halves, a hard wall between them:

- **Investigation agent** (`agent.py`) — an LLM that gathers evidence by
  calling read-only tools against the ledger. Hard budget: max 5 tool calls,
  max 2 retries per tool, 30-second wall clock. When it runs out of budget it
  stops and escalates — it never hangs, never guesses.
- **Five read-only tools** (`tools.py`) — `get_payment_by_utr`,
  `get_order_payments`, `find_duplicate_captures`, `check_refund_history`,
  `check_payment_status`. Every tool is registered `mutates=False`; the
  database connection itself opens in read-only mode underneath them. There
  is no `issue_refund` tool. It was left out on purpose — that's the safety
  argument, not a limitation of the demo.
- **Deterministic policy engine** (`policy.py` — `decide()`) — takes the
  evidence the agent collected and returns a verdict by plain comparison
  against the ledger. No LLM call, no network call, no randomness, in this
  function or anywhere it imports from. Its own docstring states the
  contract in four words: *"DECIDES. No network. No model. No randomness."*

Every claim resolves to exactly one of three terminal states:

| Verdict | Meaning |
|---|---|
| **Pass** | Claim matches ledger evidence within tolerance. Refund authorized up to `max_refundable_paise`. |
| **Block** | Claim contradicts what the ledger shows — reused reference, amount mismatch, exceeds captured total, etc. |
| **Escalate** | Evidence is insufficient, ambiguous, or in-flight. A human decides — the engine never guesses its way to a pass. |

## 4. The abuse guard (what makes this more than a lookup)

A determined bad actor doesn't need a clever exploit — they can just keep
resubmitting the same denied claim, hoping a non-deterministic model
eventually mis-escalates into an approval. The engine counts an order's own
history: **three prior claims that ended in a genuine, terminal denial**
(`REF_NOT_IN_LEDGER`, `AMOUNT_MISMATCH`, `EXCEEDS_CAPTURED_TOTAL`, and
similar), and the next automatic attempt on that order blocks outright —
before a single tool call runs.

The guard is deliberately narrow: it only counts denials the customer wasn't
invited to retry. A capture still settling, a temporarily unavailable model,
or a fat-fingered typo are all cases where resubmitting is the *correct*
behavior — the engine says so in its own reason text — and none of those
count against an honest customer. Only a real, repeated "no" trips it.

## 5. Closing the loop against replay

A refund is a one-time authorization, not a standing pass. The moment a claim
resolves to **pass** — matched against a real settled payment — that
reference is immediately marked consumed. Anyone attempting to file the exact
same claim again is blocked with `REF_ALREADY_CONSUMED`. One refund per
reference, not infinite.

## 6. Proof, not promises

- **0 unsafe passes** on a dedicated adversarial corpus — prompt injection,
  homoglyph and zero-width UTR disguises, contradictory-amount claims — every
  case built to trick the engine into an unsafe pass.
- **100% of the agent's tool allowlist is read-only** by construction — not
  by convention, the connection itself can't write.
- **138 automated tests**, several asserting the architecture itself — e.g.
  that `policy.decide()` makes zero network calls across every pass / block /
  escalate path, including the repeat-abuse gate.
- **Hash-chained, append-only audit log** — every decision writes a
  SHA-256-chained event. Tamper with one field and the verifier reports
  failure at the exact broken event, checkable live in the admin console.

## 7. The live demo — what to show, in order

This is the part that should carry the video: it's not a mockup, every step
below is a real write into a real database, verified end-to-end this session.

1. **Land on the site.** One line of framing: *"the claim isn't the fact, the
   ledger is."*
2. **Simulate a payment** (`/pay`) — enter an amount, hit Pay Now. This is a
   real row landing in the ledger's `captures` table, with a real UTR
   generated on the spot. The screen then shows **"Payment failed"** — the
   exact gap this product exists to catch: the ledger confirms the payment,
   the customer's own client doesn't.
3. **File the complaint** — one click hands the real UTR into the claim form
   and files it automatically. Watch the investigation run live: the agent
   calls `get_payment_by_utr`, finds the real match, the policy engine
   returns **pass** — refund authorized, full evidence trail and invariant
   checks visible on screen.
4. **Try to file the same claim again.** Second attempt: **blocked**,
   `REF_ALREADY_CONSUMED`. One refund, not infinite — proven live, not
   claimed in a slide.
5. **Open the admin console** (`/admin`) — show the claim history, the
   engine-vs-human agreement view, and the audit chain integrity check
   (hash-chain verified, or explain honestly if it's a cold instance showing
   "no data yet" — that's a deliberately honest state, not a hidden failure).
6. **Optional, if time:** the repeat-claim abuse guard — submit an unknown
   reference 4 times on the same order; the 4th attempt blocks on history
   alone, before any ledger lookup runs.

## 8. One-line pitch (for a title card / hook)

*"Every refund guardrail checks if a claim is allowed. Ledger Oracle checks
if it's true — a bounded AI agent investigates, a deterministic engine
decides, and the model is never in the room when money gets authorized."*
