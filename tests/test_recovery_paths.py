"""One test per row of PRD 9.3's failure catalogue that eval/faults.py can inject,
plus the zero-tolerance property (PRD 9.5): a fault-injected run must never end in
`pass` unless the agent genuinely recovered real evidence for it.
"""
import sqlite3

import pytest

from agent import investigate
from eval.faults import inject_tool_failure
from models import StructuredClaim, ToolError

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
    """Same convention as tests/test_agent_bounds.py: replays a fixed plan of tool calls."""
    def __init__(self, plan):
        self.plan = list(plan)

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
                    if not outer.plan:
                        return outer._response(None)
                    name, args = outer.plan.pop(0)
                    return outer._response([outer._ToolCall(name, args)])
        return _Chat()


def _plan_for_utr_lookup():
    return [("get_payment_by_utr", '{"utr": "111111111111"}')]


def test_transient_db_timeout_recovers_via_retry(tmp_path):
    db = _fixture_db(tmp_path)
    # Retry is model-driven (agent.py asks again after a ToolError, it doesn't
    # silently re-call) -- the plan needs the retry request too.
    client = _FakeClient(_plan_for_utr_lookup() * 2)
    with inject_tool_failure("db_timeout", at_call=1, tool_name="get_payment_by_utr"):
        state, verdict = investigate("r1", "msg", extracted=CLAIM, client=client, db_path=db)
    # one-shot glitch, agent's own retry (config.MAX_RETRIES_PER_TOOL) should recover
    # real evidence and reach the correct verdict -- this ground-truth claim is real.
    assert verdict.decision == "pass"
    assert len(state.evidence) >= 1


def test_persistent_db_unavailable_exhausts_retries_and_escalates(tmp_path):
    db = _fixture_db(tmp_path)
    client = _FakeClient(_plan_for_utr_lookup() * 5)
    with inject_tool_failure("db_unavailable", tool_name="get_payment_by_utr", always=True):
        state, verdict = investigate("r2", "msg", extracted=CLAIM, client=client, db_path=db)
    assert verdict.decision == "escalate"
    assert verdict.decision != "pass"
    assert any(isinstance(e, ToolError) for e in state.failed_tools)


def test_malformed_row_never_crashes_and_never_wrongly_passes(tmp_path):
    db = _fixture_db(tmp_path)
    client = _FakeClient(_plan_for_utr_lookup() * 5)
    with inject_tool_failure("malformed_row", tool_name="get_payment_by_utr", always=True):
        state, verdict = investigate("r3", "msg", extracted=CLAIM, client=client, db_path=db)
    assert verdict.decision in ("block", "escalate")


def test_contradictory_evidence_escalates_not_passes(tmp_path):
    db = _fixture_db(tmp_path)
    client = _FakeClient(_plan_for_utr_lookup())
    with inject_tool_failure("contradictory", at_call=1, tool_name="get_payment_by_utr"):
        state, verdict = investigate("r4", "msg", extracted=CLAIM, client=client, db_path=db)
    assert verdict.decision == "escalate"
    assert verdict.reason_code == "CONTRADICTORY_LEDGER_RECORDS"


@pytest.mark.parametrize("mode", ["db_timeout", "db_unavailable", "malformed_row", "contradictory"])
def test_zero_tolerance_no_unsafe_pass_when_persistently_faulted(tmp_path, mode):
    """PRD 9.5: unsafe_failures (a fault-injected run ending in `pass` without having
    actually recovered real evidence) must never happen. Persistent (always=True) faults
    on every claim in the plan leave no real evidence path to a legitimate pass."""
    db = _fixture_db(tmp_path)
    client = _FakeClient(_plan_for_utr_lookup() * 5)
    with inject_tool_failure(mode, tool_name="get_payment_by_utr", always=True):
        state, verdict = investigate(f"r5_{mode}", "msg", extracted=CLAIM, client=client, db_path=db)
    if mode == "contradictory":
        # contradictory evidence is itself a real (bad) signal, not a missing-evidence
        # case -- the correct terminal state is escalate, asserted in its own test above.
        assert verdict.decision == "escalate"
    else:
        assert verdict.decision != "pass"
