"""One test per stop condition in PRD 6.1, plus the "every path terminates, default
is escalate never pass" property under several induced failure modes -- the single
most important property in the whole project.
"""
import sqlite3
import time

import pytest

from models import StructuredClaim, ToolError
from agent import investigate

CLAIM = StructuredClaim(
    claim_type="payment_not_recorded", order_id="ord_1",
    claimed_reference="111111111111", claimed_amount_paise=249900,
)


def _fixture_db(tmp_path):
    path = tmp_path / "ledger.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE captures (
            capture_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, amount_paise INTEGER NOT NULL,
            instrument TEXT NOT NULL, utr TEXT, payee_vpa TEXT, captured_at TEXT NOT NULL,
            settled_at TEXT, ledger_source TEXT NOT NULL
        );
        CREATE TABLE refunds (
            refund_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, capture_id TEXT,
            amount_paise INTEGER NOT NULL, issued_at TEXT NOT NULL, channel TEXT NOT NULL
        );
    """)
    con.execute(
        "INSERT INTO captures VALUES ('pay_1','ord_1',249900,'upi','111111111111',NULL,"
        "'2026-01-01T10:00:00Z','2026-01-01T10:05:00Z','connected')"
    )
    con.commit()
    con.close()
    return str(path)


class _FakeClient:
    """Replays a fixed sequence of tool-call requests, then stops."""
    def __init__(self, plan):
        self.plan = list(plan)
        self.calls = 0

    class _ToolCall:
        def __init__(self, name, args):
            self.function = type("F", (), {"name": name, "arguments": args})()

    def _response(self, tool_calls):
        msg = type("M", (), {"tool_calls": tool_calls})()
        choice = type("C", (), {"message": msg})()
        return type("R", (), {"choices": [choice]})()

    @property
    def chat(self):
        outer = self
        class _Chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    outer.calls += 1
                    if not outer.plan:
                        return outer._response(None)
                    name, args = outer.plan.pop(0)
                    return outer._response([outer._ToolCall(name, args)])
        return _Chat()


class _RaisingClient:
    def __init__(self, exc):
        self.exc = exc

    @property
    def chat(self):
        exc = self.exc
        class _Chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise exc
        return _Chat()


def test_no_evidence_escalates_never_passes(tmp_path):
    db = _fixture_db(tmp_path)
    client = _FakeClient([])  # stops immediately, no tool calls
    state, verdict = investigate("c1", "msg", extracted=CLAIM, client=client, db_path=db)
    assert verdict.decision == "escalate"


def test_step_budget_exhausted_still_returns_escalate_verdict(tmp_path):
    db = _fixture_db(tmp_path)
    # check_refund_history alone never satisfies decidable(); vary the order_id each
    # call so DUPLICATE_CALL_SUPPRESSED doesn't short-circuit before the step cap does.
    plan = [("check_refund_history", f'{{"order_id": "ord_{i}"}}') for i in range(10)]
    client = _FakeClient(plan)
    state, verdict = investigate("c2", "msg", extracted=CLAIM, client=client, db_path=db)
    assert verdict.decision == "escalate"
    assert state.steps_used == 5  # config.MAX_INVESTIGATION_STEPS, actually hit
    assert state.stop_reason == "STEP_BUDGET_EXHAUSTED"


def test_wall_clock_budget_exhausted(tmp_path, monkeypatch):
    db = _fixture_db(tmp_path)
    import config
    monkeypatch.setattr(config, "WALL_CLOCK_BUDGET_SECONDS", 0)
    client = _FakeClient([("check_refund_history", '{"order_id": "ord_1"}')])
    state, verdict = investigate("c3", "msg", extracted=CLAIM, client=client, db_path=db)
    assert verdict.decision == "escalate"
    assert state.stop_reason == "TIME_BUDGET_EXHAUSTED"


def test_risk_flags_param_reaches_decide_and_short_circuits(tmp_path):
    """Regression: investigate()'s risk_flags param (fed by sanitize.detect_risk_flags()
    in app.py) must actually seed state.risk_flags so policy.decide()'s DECIDABILITY
    GATE sees it -- this used to go nowhere, making PRD 10.3's AMBIGUOUS_ORDER /
    CONTRADICTORY_AMOUNTS / CONTRADICTORY_CLAIM escalation branches unreachable in
    production even though tests exercised them directly against decide(). One tool
    call is enough to make decidable() True; the risk_flags check then fires before
    any evidence-based reasoning, regardless of what that evidence actually contains.
    """
    db = _fixture_db(tmp_path)
    # CLAIM has claimed_reference set, so decidable() specifically needs a
    # PaymentLookupResult (get_payment_by_utr), not just any evidence -- see
    # policy.decidable()'s own comment/FAILURES.md on this exact distinction.
    client = _FakeClient([("get_payment_by_utr", '{"utr": "111111111111"}')])
    state, verdict = investigate("c5", "msg", extracted=CLAIM, client=client, db_path=db,
                                  risk_flags=["AMBIGUOUS_ORDER"])
    assert verdict.decision == "escalate"
    assert verdict.reason_code == "AMBIGUOUS_ORDER"


def test_tool_error_past_retry_budget_escalates_tool_unavailable(tmp_path):
    # Nonexistent db path -> every tool call returns ToolError(unavailable).
    bad_db = str(tmp_path / "does_not_exist.db")
    plan = [("check_refund_history", '{"order_id": "ord_1"}')] * 10
    client = _FakeClient(plan)
    state, verdict = investigate("c4", "msg", extracted=CLAIM, client=client, db_path=bad_db)
    assert verdict.decision == "escalate"
    assert all(isinstance(e, ToolError) for e in state.failed_tools)
    assert len(state.evidence) == 0


def test_duplicate_call_suppressed_does_not_increment_steps(tmp_path):
    db = _fixture_db(tmp_path)
    plan = [("check_refund_history", '{"order_id": "ord_1"}')] * 3
    client = _FakeClient(plan)
    state, verdict = investigate("c5", "msg", extracted=CLAIM, client=client, db_path=db)
    assert "DUPLICATE_CALL_SUPPRESSED" in state.risk_flags
    # only the first call actually executed; steps_used counts real calls only
    assert state.steps_used == 1


def test_model_unavailable_on_sdk_error(tmp_path):
    from llm_client import LLMProviderError
    db = _fixture_db(tmp_path)
    exc = LLMProviderError("simulated connection failure")
    client = _RaisingClient(exc)
    state, verdict = investigate("c6", "msg", extracted=CLAIM, client=client, db_path=db)
    assert verdict.decision == "escalate"
    assert state.stop_reason == "MODEL_UNAVAILABLE"


def test_insufficient_claim_data_when_nothing_parsed(tmp_path):
    db = _fixture_db(tmp_path)
    client = _FakeClient([])
    state, verdict = investigate("c7", "msg", extracted=None, client=client, db_path=db)
    assert verdict.decision == "escalate"
    assert verdict.reason_code == "INSUFFICIENT_CLAIM_DATA"


def test_tool_not_permitted_escalates_after_retries(tmp_path):
    db = _fixture_db(tmp_path)
    plan = [("issue_refund", '{"order_id": "ord_1"}')] * 5  # not in the allowlist
    client = _FakeClient(plan)
    state, verdict = investigate("c8", "msg", extracted=CLAIM, client=client, db_path=db)
    assert verdict.decision == "escalate"
    assert state.stop_reason == "TOOL_NOT_PERMITTED"
    assert "TOOL_NOT_PERMITTED" in state.risk_flags


def test_invalid_tool_args_escalates_after_retries(tmp_path):
    db = _fixture_db(tmp_path)
    plan = [("check_refund_history", "not valid json")] * 5
    client = _FakeClient(plan)
    state, verdict = investigate("c9", "msg", extracted=CLAIM, client=client, db_path=db)
    assert verdict.decision == "escalate"
    assert state.stop_reason == "INVALID_TOOL_CALL"


def test_successful_resolution_can_pass_when_decidable(tmp_path):
    db = _fixture_db(tmp_path)
    plan = [("get_payment_by_utr", '{"utr": "111111111111"}')]
    client = _FakeClient(plan)
    state, verdict = investigate("c10", "msg", extracted=CLAIM, client=client, db_path=db)
    assert verdict.decision == "pass"


@pytest.mark.parametrize("failure_mode", ["no_tools", "all_errors", "wall_clock", "model_down"])
def test_every_failure_mode_terminates_and_never_passes_without_evidence(tmp_path, monkeypatch, failure_mode):
    """The single most important property: whatever goes wrong, the loop always
    returns a Verdict, and it is never `pass` when no real evidence was gathered."""
    import config

    if failure_mode == "no_tools":
        db = _fixture_db(tmp_path)
        client = _FakeClient([])
    elif failure_mode == "all_errors":
        db = str(tmp_path / "missing.db")
        client = _FakeClient([("check_refund_history", '{"order_id": "ord_1"}')] * 10)
    elif failure_mode == "wall_clock":
        db = _fixture_db(tmp_path)
        monkeypatch.setattr(config, "WALL_CLOCK_BUDGET_SECONDS", 0)
        client = _FakeClient([("check_refund_history", '{"order_id": "ord_1"}')])
    else:  # model_down
        from llm_client import LLMProviderError
        db = _fixture_db(tmp_path)
        client = _RaisingClient(LLMProviderError("rate limited"))

    state, verdict = investigate("cx", "msg", extracted=CLAIM, client=client, db_path=db)
    assert verdict is not None
    assert verdict.decision in ("escalate", "block")  # never a bare pass without evidence
    if not state.evidence:
        assert verdict.decision == "escalate"
