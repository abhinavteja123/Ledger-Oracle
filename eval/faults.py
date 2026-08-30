"""Deterministic, replayable fault injection at the tool and LLM boundaries (PRD 9.4).

Off by default -- production path (agent.py, tools.py) has zero fault-injection code in
it. Two kinds of injection, both call-counted (not random, not time-based) so the same
--at-call always hits the same point in an investigation:

  - Tool-boundary faults (db_timeout, db_unavailable, malformed_row, contradictory):
    monkeypatch tools.TOOL_REGISTRY[name]["fn"] for the Nth call. Transparent to
    agent.py, which dispatches through the registry.
  - LLM-boundary faults (llm_unavailable, llm_invalid_json, llm_bad_tool_name,
    llm_bad_tool_args): FaultInjectingClient, a fake client passed as agent.py's
    injectable `client` param.
"""
import contextlib

from llm_client import LLMProviderError

import tools
from models import OrderPaymentsResult, PaymentLookupResult, ToolError

# ---------------------------------------------------------------------------
# Tool-boundary faults
# ---------------------------------------------------------------------------


def _make_error(mode):
    def _err(tool_name):
        return ToolError(tool=tool_name, error_class=mode, attempt=1, detail=f"injected: simulated {mode}")
    return _err


def _malformed_row(tool_name):
    # ponytail: not a real Pydantic-validation-failure-on-a-DB-row (that lives inside
    # tools.py's own row conversion); this simulates the tool boundary handing back
    # something that isn't a valid Result/ToolError at all. Evidence of an unexpected
    # shape is filtered out by policy.py's isinstance-based evidence readers, so the
    # investigation degrades to "no evidence from this call" rather than crashing --
    # upgrade to a true malformed-DB-row simulation if a specific EVIDENCE_INVALID
    # reason code needs to be exercised precisely.
    return {"malformed": True, "tool": tool_name}


def _contradictory(result):
    if isinstance(result, PaymentLookupResult) and result.matches:
        row = result.matches[0]
        conflicting = row.model_copy(update={"amount_paise": row.amount_paise + 1})
        return result.model_copy(update={"matches": result.matches + [conflicting]})
    if isinstance(result, OrderPaymentsResult) and result.captures:
        row = result.captures[0]
        conflicting = row.model_copy(update={"amount_paise": row.amount_paise + 1})
        return result.model_copy(update={"captures": result.captures + [conflicting]})
    return result  # nothing to contradict -- pass through unchanged


TOOL_FAULT_MODES = {
    "db_timeout": _make_error("timeout"),
    "db_unavailable": _make_error("unavailable"),
    "malformed_row": _malformed_row,
}


@contextlib.contextmanager
def inject_tool_failure(mode: str, at_call: int = 1, tool_name: str | None = None, always: bool = False):
    """Patches every registered tool so the `at_call`-th invocation (counting across all
    tools, or only `tool_name` if given) is replaced by the injected fault. If `always`
    is True, every matching call is faulted (a persistently-down dependency that exhausts
    the agent's retry budget) instead of just the `at_call`-th (a one-shot transient glitch
    the agent's retry should recover from)."""
    if mode not in TOOL_FAULT_MODES and mode != "contradictory":
        raise ValueError(f"unknown tool fault mode: {mode}")
    counter = {"n": 0}
    originals = {name: entry["fn"] for name, entry in tools.TOOL_REGISTRY.items()}

    def make_wrapper(name, original_fn):
        def wrapper(*args, **kwargs):
            hit = tool_name is None or name == tool_name
            if hit:
                counter["n"] += 1
            if hit and (always or counter["n"] == at_call):
                if mode == "contradictory":
                    return _contradictory(original_fn(*args, **kwargs))
                return TOOL_FAULT_MODES[mode](name)
            return original_fn(*args, **kwargs)
        return wrapper

    for name in list(tools.TOOL_REGISTRY.keys()):
        tools.TOOL_REGISTRY[name]["fn"] = make_wrapper(name, originals[name])
    try:
        yield
    finally:
        for name, fn in originals.items():
            tools.TOOL_REGISTRY[name]["fn"] = fn


# ---------------------------------------------------------------------------
# LLM-boundary faults
# ---------------------------------------------------------------------------

class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, name, arguments):
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, tool_calls=None):
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeResponse:
    def __init__(self, message):
        self.choices = [_FakeChoice(message)]


def _llm_unavailable():
    raise LLMProviderError("injected: simulated connection failure")


def _llm_invalid_json():
    return _FakeResponse(_FakeMessage(tool_calls=[_FakeToolCall("get_payment_by_utr", '{"utr": ')]))


def _llm_bad_tool_name():
    return _FakeResponse(_FakeMessage(tool_calls=[_FakeToolCall("issue_refund", "{}")]))


def _llm_bad_tool_args():
    return _FakeResponse(_FakeMessage(tool_calls=[_FakeToolCall("get_payment_by_utr", '{"utr": null}')]))


LLM_FAULT_MODES = {
    "llm_unavailable": _llm_unavailable,
    "llm_invalid_json": _llm_invalid_json,
    "llm_bad_tool_name": _llm_bad_tool_name,
    "llm_bad_tool_args": _llm_bad_tool_args,
}


class FaultInjectingClient:
    """Fake OpenAI-shaped client. Pass as agent.py's `client` param. Injects the
    fault on the `at_call`-th chat.completions.create() call; every other call passes
    through to `passthrough_client` (a real or another fake client) so a multi-step
    investigation still progresses normally around the injected failure."""

    def __init__(self, mode: str, at_call: int = 1, passthrough_client=None):
        if mode not in LLM_FAULT_MODES:
            raise ValueError(f"unknown llm fault mode: {mode}")
        self.mode = mode
        self.at_call = at_call
        self.passthrough_client = passthrough_client
        self._n = 0
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self._n += 1
        if self._n == self.at_call:
            return LLM_FAULT_MODES[self.mode]()
        if self.passthrough_client is None:
            raise RuntimeError("FaultInjectingClient: no passthrough_client for a non-injected call")
        return self.passthrough_client.chat.completions.create(**kwargs)


ALL_FAULT_MODES = sorted(set(TOOL_FAULT_MODES) | {"contradictory"} | set(LLM_FAULT_MODES))


if __name__ == "__main__":
    # ponytail: smallest possible smoke check, not a substitute for tests/test_recovery_paths.py
    with inject_tool_failure("db_timeout", at_call=1):
        result = tools.TOOL_REGISTRY["get_payment_by_utr"]["fn"]("526112345678")
        assert isinstance(result, ToolError) and result.error_class == "timeout"

    with inject_tool_failure("contradictory", at_call=1):
        result = tools.TOOL_REGISTRY["get_payment_by_utr"]["fn"]("nonexistent_utr_zzz")
        assert isinstance(result, PaymentLookupResult)  # no matches to contradict -> passthrough

    print(f"OK: tool-boundary fault injection works. {len(ALL_FAULT_MODES)} fault modes registered.")
