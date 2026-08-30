"""Bounded investigation agent. See PRD sections 5.1, 6, 8, 9, 14.3.

The agent gathers evidence. The policy engine decides.

Every path out of this loop is a terminal state, and the default terminal state is
ESCALATE, never pass. An investigation that cannot complete must never end in money
moving: `investigate()` always returns a real Verdict, and only a state that
`policy.decidable()` accepts ever produces pass/block -- every other exit escalates.

Design note: this takes an already-parsed `extracted: StructuredClaim` rather than
calling parser.py itself. parser.py's parse_claim() is being built separately; keeping
the seam here means this file has no hard import dependency on it, and is directly
testable without any parsing step at all.
"""
import json
import time
from datetime import datetime, timezone
from typing import Optional

import config
import tools
from llm_client import MODEL, LLMProviderError, get_client
from models import InvestigationState, StructuredClaim, ToolCall, ToolError, Verdict
from policy import decidable, decide

# LLMProviderError is llm_client.py's provider-neutral exception -- every configured
# provider's own SDK exception (rate limit, auth, connection, ...) gets normalized to
# this one type by _FallbackClient before it ever reaches here, including the config
# error raised when no API key is set at all. Catching this one type here means "any
# way the LLM boundary can fail" reliably degrades to MODEL_UNAVAILABLE, not just the
# subset of failure modes anyone happened to enumerate.
LLM_ERRORS = (LLMProviderError,)

# Each tool's one string argument, and which decidable()-relevant evidence type it
# feeds (used only for building schemas here -- decidable() itself lives in policy.py).
_ARG_NAME = {
    "get_payment_by_utr": "utr",
    "get_order_payments": "order_id",
    "find_duplicate_captures": "order_id",
    "check_refund_history": "order_id",
    "check_payment_status": "capture_id",
}

AGENT_SYSTEM = (
    "You investigate a customer refund claim by calling read-only ledger tools, one at "
    "a time. Call a tool to gather evidence. If the claim states a payment reference "
    "(UTR/RRN), you must call get_payment_by_utr to check it against the ledger -- "
    "seeing that reference in an order-level listing is not the same as confirming it "
    "resolves cleanly, and the claim cannot be verified without that specific check. "
    "Once you have enough evidence to resolve the claim, or no further tool would "
    "help, respond with no tool call to stop."
)


def _tool_schemas() -> list[dict]:
    """OpenAI-style function schemas from tools.TOOL_REGISTRY, restricted to
    config.TOOL_ALLOWLIST -- Rule 2 (PRD 5.2) enforced here too, not just at dispatch."""
    schemas = []
    for name in sorted(config.TOOL_ALLOWLIST):
        spec = tools.TOOL_REGISTRY[name]
        arg = _ARG_NAME[name]
        doc = (spec["fn"].__doc__ or name).strip().splitlines()[0]
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": doc,
                "parameters": {
                    "type": "object",
                    "properties": {arg: {"type": "string"}},
                    "required": [arg],
                    "additionalProperties": False,  # strict mode (Groq) requires this
                    # on every object schema, not just the top-level response_format one
                    # -- found by a live 400 against the real API, see FAILURES.md.
                },
                "strict": True,
            },
        })
    return schemas


TOOL_SCHEMAS = _tool_schemas()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_action(state: InvestigationState, client, remaining_tools: set[str]):
    """Ask the model which tool to call next. Returns (tool_name, args_dict), a
    ("__not_permitted__"|"__invalid_args__", detail_dict) error tuple, or None to stop.
    Raises only LLM_ERRORS (caller handles as MODEL_UNAVAILABLE)."""
    schemas = [s for s in TOOL_SCHEMAS if s["function"]["name"] in remaining_tools]
    if not schemas:
        return None

    messages = [
        {"role": "system", "content": AGENT_SYSTEM},
        {"role": "user", "content": json.dumps({
            "claim": state.extracted.model_dump() if state.extracted else None,
            "evidence_gathered_so_far": [e.model_dump() for e in state.evidence],
            "tools_already_called": [c.tool for c in state.tools_called],
        }, default=str)},
    ]
    response = client.chat.completions.create(
        model=MODEL,
        temperature=0,
        max_completion_tokens=512,
        tools=schemas,
        tool_choice="auto",
        messages=messages,
    )
    msg = response.choices[0].message
    tool_calls = getattr(msg, "tool_calls", None)
    if not tool_calls:
        return None

    call = tool_calls[0]
    name = call.function.name
    if name not in config.TOOL_ALLOWLIST:
        return ("__not_permitted__", {"tool": name})
    try:
        args = json.loads(call.function.arguments)
    except (json.JSONDecodeError, TypeError):
        return ("__invalid_args__", {"tool": name, "raw": call.function.arguments})
    return (name, args)


def investigate(
    claim_id: str,
    raw_message: str,
    extracted: Optional[StructuredClaim] = None,
    client=None,
    consumed_references: frozenset = frozenset(),
    db_path: str = tools.DB_PATH,
    risk_flags: Optional[list[str]] = None,
) -> tuple[InvestigationState, Verdict]:
    """Run one bounded investigation. Never raises past this function; always returns
    a real Verdict. Only a decidable() state reaches pass/block -- everything else,
    including every failure path below, escalates.

    `risk_flags` seeds state.risk_flags with claim-text-level findings gathered
    before this call (e.g. sanitize.detect_risk_flags()) -- policy.decide()'s
    DECIDABILITY GATE reads these to short-circuit AMBIGUOUS_ORDER /
    CONTRADICTORY_AMOUNTS / CONTRADICTORY_CLAIM claims (PRD 10.3) before any ledger
    evidence is consulted. Previously nothing populated this, so those branches were
    unreachable in production -- see FAILURES.md.
    """
    state = InvestigationState(claim_id=claim_id, raw_message=raw_message,
                                extracted=extracted, started_at=_now(),
                                risk_flags=list(risk_flags) if risk_flags else [])

    _ADVERSARIAL_FLAGS = ("AMBIGUOUS_ORDER", "CONTRADICTORY_AMOUNTS", "CONTRADICTORY_CLAIM")

    def finish(status: str, stop_reason: str) -> tuple[InvestigationState, Verdict]:
        state.status = status
        state.stop_reason = stop_reason
        if decidable(state):
            verdict = decide(state, consumed_references=consumed_references)
        else:
            # decide()'s own risk_flags check (policy.py's DECIDABILITY GATE) never
            # runs here since decidable() is False -- e.g. the model extracted nothing
            # to search on and stopped before calling any tool. Prefer the specific
            # adversarial-class flag over the generic stop_reason when one was set, so
            # a live AMBIGUOUS_ORDER/CONTRADICTORY_AMOUNTS/CONTRADICTORY_CLAIM claim
            # doesn't surface as an uninformative MODEL_STOPPED. Found live this
            # session testing the real /verify endpoint against this exact case.
            reason_code = next((f for f in _ADVERSARIAL_FLAGS if f in state.risk_flags), stop_reason)
            verdict = Verdict(
                decision="escalate", max_refundable_paise=0, reason_code=reason_code,
                why_not_block="Investigation stopped before enough evidence was gathered "
                               f"to resolve the claim ({stop_reason}).",
                degraded_path=True,
            )
        return state, verdict

    if extracted is None:
        return finish("failed", "INSUFFICIENT_CLAIM_DATA")

    if client is None:
        client = get_client()

    start_time = time.monotonic()
    remaining_tools = set(config.TOOL_ALLOWLIST)
    seen_calls: set[tuple] = set()

    while True:
        if decidable(state):
            return finish("decided", "DECIDABLE")
        if state.steps_used >= config.MAX_INVESTIGATION_STEPS:
            return finish("escalated", "STEP_BUDGET_EXHAUSTED")
        if time.monotonic() - start_time >= config.WALL_CLOCK_BUDGET_SECONDS:
            return finish("escalated", "TIME_BUDGET_EXHAUSTED")
        if not remaining_tools:
            return finish("escalated", "TOOL_UNAVAILABLE")

        try:
            action = next_action(state, client, remaining_tools)
        except LLM_ERRORS:
            return finish("escalated", "MODEL_UNAVAILABLE")

        if action is None:
            return finish("decided" if decidable(state) else "escalated", "MODEL_STOPPED")

        name, payload = action

        if name == "__not_permitted__":
            state.risk_flags.append("TOOL_NOT_PERMITTED")
            attempts = state.retries.get("__not_permitted__", 0) + 1
            state.retries["__not_permitted__"] = attempts
            if attempts >= config.MAX_RETRIES_PER_TOOL:
                return finish("escalated", "TOOL_NOT_PERMITTED")
            continue

        if name == "__invalid_args__":
            attempts = state.retries.get("__invalid_args__", 0) + 1
            state.retries["__invalid_args__"] = attempts
            if attempts >= config.MAX_RETRIES_PER_TOOL:
                return finish("escalated", "INVALID_TOOL_CALL")
            continue

        arg_name = _ARG_NAME[name]
        arg_value = payload.get(arg_name, "")
        call_key = (name, arg_value)

        if call_key in seen_calls:
            state.risk_flags.append("DUPLICATE_CALL_SUPPRESSED")
            remaining_tools.discard(name)  # already asked and answered; move on
            continue

        fn = tools.TOOL_REGISTRY[name]["fn"]
        result = fn(arg_value, db_path=db_path)
        state.steps_used += 1
        state.tools_called.append(ToolCall(tool=name, args={arg_name: arg_value}, timestamp=_now()))

        if isinstance(result, ToolError):
            attempts = state.retries.get(name, 0) + 1
            state.retries[name] = attempts
            state.failed_tools.append(result)
            if attempts >= config.MAX_RETRIES_PER_TOOL:
                remaining_tools.discard(name)
            continue

        if not hasattr(result, "model_dump"):
            # Tool returned something that isn't a typed Result or a ToolError -- e.g.
            # a malformed/unexpected shape. Never silently trust it as evidence (PRD
            # 9.3's "malformed row" row): treat exactly like a ToolError instead of
            # letting it corrupt state.evidence and crash later on .model_dump().
            attempts = state.retries.get(name, 0) + 1
            state.retries[name] = attempts
            state.failed_tools.append(ToolError(
                tool=name, error_class="invalid_args", attempt=attempts,
                detail="tool returned an unexpected (non-Pydantic) result shape",
            ))
            if attempts >= config.MAX_RETRIES_PER_TOOL:
                remaining_tools.discard(name)
            continue

        seen_calls.add(call_key)  # marks it "answered" -- a later identical request is
                                   # now a true duplicate, not a retry of a failed attempt
        state.evidence.append(result)
        remaining_tools.discard(name)


if __name__ == "__main__":
    # ponytail: smallest possible smoke check -- a fake client that never calls a tool,
    # proving the "no evidence -> escalate, never pass" floor without any network.
    class _StopImmediately:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    class _Msg:
                        tool_calls = None
                    class _Choice:
                        message = _Msg()
                    class _Resp:
                        choices = [_Choice()]
                    return _Resp()

    claim = StructuredClaim(claim_type="other", order_id=None, claimed_reference=None)
    state, verdict = investigate("smoke_0001", "test", extracted=claim, client=_StopImmediately())
    assert verdict.decision == "escalate", "no evidence gathered must never pass"
    print(f"OK: agent.py smoke check -- no-evidence investigation escalated ({verdict.reason_code})")
