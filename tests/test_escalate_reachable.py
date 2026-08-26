"""Escalate is not optional (PRD 16.1). Guards the failure class already logged in
FAILURES.md: a tool-layer bug once made every HONEST_UNVERIFIABLE claim come back
`block` instead of `escalate`. This asserts escalate is reachable via several distinct
reason codes, and that pass/block are also still reachable -- i.e. the engine actually
uses all three decisions, not just one.
"""
from tests.helpers import capture, claim, lookup_result, order_payments_result, state

from policy import decide


def test_all_three_decisions_are_reachable():
    decisions = {
        decide(state(extracted=claim(), evidence=[lookup_result(matches=[capture()])])).decision,
        decide(state(extracted=claim(), evidence=[lookup_result()])).decision,
        decide(state(extracted=claim(), evidence=[lookup_result(unconnected=True)])).decision,
    }
    assert decisions == {"pass", "block", "escalate"}


def test_escalate_reachable_via_multiple_distinct_reason_codes():
    codes = {
        decide(state(extracted=claim(), evidence=[lookup_result(unconnected=True)])).reason_code,
        decide(state(extracted=claim(), evidence=[
            lookup_result(), order_payments_result(captures=[capture(settled_at=None)]),
        ])).reason_code,
        decide(state(extracted=None)).reason_code,
    }
    assert codes == {"REF_OUTSIDE_CONNECTED_LEDGER", "REF_MAY_BE_IN_FLIGHT", "INSUFFICIENT_CLAIM_DATA"}
