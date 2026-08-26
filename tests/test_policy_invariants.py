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


def test_duplicate_charge_no_reference_resolves_via_order_evidence():
    c = capture(amount_paise=249900)
    v = decide(state(
        extracted=claim(claim_type="duplicate_charge", claimed_reference=None, claimed_amount_paise=249900),
        evidence=[order_payments_result(captures=[c])],
    ))
    assert v.decision == "pass"


if __name__ == "__main__":
    import sys
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-q"]))
