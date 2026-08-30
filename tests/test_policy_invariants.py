"""Every branch of policy.decide() (PRD section 16), one test per outcome."""
from tests.helpers import capture, claim, lookup_result, order_payments_result, refund, refund_history_result, state

from policy import decide


def test_pass_exact_match_sufficient_headroom():
    c = capture()
    v = decide(state(extracted=claim(), evidence=[lookup_result(matches=[c])]))
    assert v.decision == "pass"
    assert v.reason_code == "OK"
    assert v.max_refundable_paise == 249900


def test_block_ref_not_in_ledger():
    v = decide(state(extracted=claim(), evidence=[lookup_result()]))
    assert v.decision == "block"
    assert v.reason_code == "REF_NOT_IN_LEDGER"


def test_wrong_utr_evidence_does_not_verify_claimed_reference():
    """Regression: the agent picks get_payment_by_utr's search arg itself
    (agent.py:next_action), nothing constrains it to equal claim.claimed_reference.
    A real-but-unrelated capture returned for a different UTR (LLM confusion, or an
    injection attempt steering the tool call) must never verify *this* claim's
    reference just because some PaymentLookupResult with matches exists in evidence.
    Before this filter existed in decide(), this fixture resolved to an unsafe pass.
    """
    unrelated = capture(capture_id="pay_unrelated", utr="999999999999", amount_paise=249900)
    v = decide(state(extracted=claim(claimed_reference="526112345678"),
                      evidence=[lookup_result(matches=[unrelated])]))
    assert v.decision != "pass"
    assert v.reason_code == "REF_NOT_IN_LEDGER"


def test_block_ref_already_consumed_i3():
    c = capture()
    v = decide(
        state(extracted=claim(), evidence=[lookup_result(matches=[c])]),
        consumed_references=frozenset({"526112345678"}),
    )
    assert v.decision == "block"
    assert v.reason_code == "REF_ALREADY_CONSUMED"


def test_block_amount_mismatch():
    c = capture(amount_paise=249900)
    v = decide(state(extracted=claim(claimed_amount_paise=349900), evidence=[lookup_result(matches=[c])]))
    assert v.decision == "block"
    assert v.reason_code == "AMOUNT_MISMATCH"


def test_block_exceeds_captured_total():
    c = capture(amount_paise=249900)
    v = decide(state(
        extracted=claim(claimed_amount_paise=249900),
        evidence=[lookup_result(matches=[c]), refund_history_result([refund(amount_paise=249900)])],
    ))
    assert v.decision == "block"
    assert v.reason_code == "EXCEEDS_CAPTURED_TOTAL"


def test_escalate_ref_ambiguous_i2():
    c1, c2 = capture(capture_id="pay_1"), capture(capture_id="pay_2")
    v = decide(state(extracted=claim(), evidence=[lookup_result(matches=[c1, c2])]))
    assert v.decision == "escalate"
    assert v.reason_code == "REF_AMBIGUOUS"


def test_escalate_ref_near_match_typo():
    near = capture(utr="526112345679")  # one digit off from the claimed reference
    v = decide(state(extracted=claim(), evidence=[lookup_result(near_matches=[near])]))
    assert v.decision == "escalate"
    assert v.reason_code == "REF_NEAR_MATCH_TYPO"


def test_escalate_ref_outside_connected_ledger():
    v = decide(state(extracted=claim(), evidence=[lookup_result(unconnected=True)]))
    assert v.decision == "escalate"
    assert v.reason_code == "REF_OUTSIDE_CONNECTED_LEDGER"


def test_escalate_ref_may_be_in_flight():
    in_flight = capture(settled_at=None)
    v = decide(state(
        extracted=claim(),
        evidence=[lookup_result(), order_payments_result(captures=[in_flight])],
    ))
    assert v.decision == "escalate"
    assert v.reason_code == "REF_MAY_BE_IN_FLIGHT"


def test_escalate_ref_may_be_in_flight_even_on_exact_match():
    # Regression: an in-flight capture can still resolve as an *exact* UTR match --
    # the settlement check must fire there too, not only on the zero-match branch.
    unsettled = capture(settled_at=None)
    v = decide(state(extracted=claim(), evidence=[lookup_result(matches=[unsettled])]))
    assert v.decision == "escalate"
    assert v.reason_code == "REF_MAY_BE_IN_FLIGHT"


def test_escalate_contradictory_ledger_records():
    c1 = capture(capture_id="pay_1", amount_paise=249900)
    c2 = capture(capture_id="pay_1", amount_paise=349900)  # same id, different amount
    v = decide(state(
        extracted=claim(),
        evidence=[lookup_result(matches=[c1]), order_payments_result(captures=[c2])],
    ))
    assert v.decision == "escalate"
    assert v.reason_code == "CONTRADICTORY_LEDGER_RECORDS"


def test_escalate_possible_out_of_band_refund():
    c = capture(amount_paise=249900)
    v = decide(state(
        extracted=claim(claimed_amount_paise=249900),
        evidence=[lookup_result(matches=[c]), refund_history_result(
            [refund(amount_paise=0, channel="out_of_band")]
        )],
    ))
    assert v.decision == "escalate"
    assert v.reason_code == "POSSIBLE_OUT_OF_BAND_REFUND"


def test_escalate_insufficient_claim_data_no_parse():
    v = decide(state(extracted=None))
    assert v.decision == "escalate"
    assert v.reason_code == "INSUFFICIENT_CLAIM_DATA"


def test_escalate_insufficient_claim_data_no_reference_no_order():
    v = decide(state(extracted=claim(order_id=None, claimed_reference=None)))
    assert v.decision == "escalate"
    assert v.reason_code == "INSUFFICIENT_CLAIM_DATA"


def test_escalate_rail_not_covered():
    c = claim()
    c.claimed_instrument = "unknown"  # e.g. "paid by cheque" -- not a tracked rail
    v = decide(state(extracted=c, evidence=[lookup_result()]))
    assert v.decision == "escalate"
    assert v.reason_code == "RAIL_NOT_COVERED"


def test_escalate_ambiguous_order_risk_flag():
    v = decide(state(extracted=claim(), risk_flags=["AMBIGUOUS_ORDER"]))
    assert v.decision == "escalate"
    assert v.reason_code == "AMBIGUOUS_ORDER"


def test_escalate_contradictory_amounts_risk_flag():
    v = decide(state(extracted=claim(), risk_flags=["CONTRADICTORY_AMOUNTS"]))
    assert v.decision == "escalate"
    assert v.reason_code == "CONTRADICTORY_AMOUNTS"


def test_pass_via_check_payment_status_evidence_alone():
    # Found live: the agent used check_payment_status (not get_payment_by_utr) and
    # found the right capture. decide() must resolve I2 from that evidence too.
    from models import PaymentStatusResult
    c = capture()
    status = PaymentStatusResult(capture=c, found=True)
    v = decide(state(extracted=claim(), evidence=[status]))
    assert v.decision == "pass"


def test_decidable_false_on_order_evidence_alone_when_reference_claimed():
    # Regression: order-level evidence alone must NOT make a reference-bearing claim
    # look "decidable" -- decide() needs a real reference match (lookup or matching
    # check_payment_status), not just any evidence at all.
    from policy import decidable
    c = capture()
    st = state(extracted=claim(), evidence=[order_payments_result(captures=[c])])
    assert decidable(st) is False


def test_duplicate_charge_no_reference_resolves_via_order_evidence():
    c = capture(amount_paise=249900)
    v = decide(state(
        extracted=claim(claim_type="duplicate_charge", claimed_reference=None, claimed_amount_paise=249900),
        evidence=[order_payments_result(captures=[c])],
    ))
    assert v.decision == "pass"


def test_block_repeated_claim_abuse_at_threshold():
    # Third prior adverse claim on this order_id -> this one blocks outright, no
    # evidence needed at all (mirrors REPEATED_CLAIM_ABUSE firing before ledger
    # lookups run).
    v = decide(state(extracted=claim()), prior_adverse_attempts=3)
    assert v.decision == "block"
    assert v.reason_code == "REPEATED_CLAIM_ABUSE"


def test_pass_still_reachable_below_abuse_threshold():
    # Two prior adverse attempts is not yet abuse -- a legitimate claim on this order
    # must still be able to pass on its own evidence.
    c = capture()
    v = decide(
        state(extracted=claim(), evidence=[lookup_result(matches=[c])]),
        prior_adverse_attempts=2,
    )
    assert v.decision == "pass"


def test_decidable_true_on_abuse_threshold_alone():
    # The agent loop must short-circuit to decide() on claim history alone, without
    # spending a tool call gathering evidence it will never need.
    from policy import decidable
    st = state(extracted=claim())
    assert decidable(st, prior_adverse_attempts=3) is True
    assert decidable(st, prior_adverse_attempts=2) is False


if __name__ == "__main__":
    import sys
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-q"]))
