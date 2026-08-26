"""Shared test builders for policy.py tests. Not a fixture framework -- just the
repetitive InvestigationState/CaptureRow construction every policy test needs.
"""
from models import (
    CaptureRow,
    DuplicateCheckResult,
    InvestigationState,
    OrderPaymentsResult,
    PaymentLookupResult,
    RefundHistoryResult,
    RefundRow,
    StructuredClaim,
)

NOW = "2026-08-20T10:00:00Z"


def capture(
    capture_id="pay_1",
    order_id="order_1",
    amount_paise=249900,
    instrument="upi",
    utr="526112345678",
    captured_at="2026-08-01T10:00:00Z",
    settled_at="2026-08-01T10:05:00Z",
    ledger_source="connected",
) -> CaptureRow:
    return CaptureRow(
        capture_id=capture_id,
        order_id=order_id,
        amount_paise=amount_paise,
        instrument=instrument,
        utr=utr,
        captured_at=captured_at,
        settled_at=settled_at,
        ledger_source=ledger_source,
    )


def claim(
    claim_type="payment_not_recorded",
    order_id="order_1",
    claimed_reference="526112345678",
    claimed_amount_paise=249900,
) -> StructuredClaim:
    return StructuredClaim(
        claim_type=claim_type,
        order_id=order_id,
        claimed_reference=claimed_reference,
        claimed_amount_paise=claimed_amount_paise,
    )


def state(
    extracted=None,
    evidence=None,
    failed_tools=None,
    risk_flags=None,
    claim_id="clm_test",
) -> InvestigationState:
    return InvestigationState(
        claim_id=claim_id,
        raw_message="test message",
        extracted=extracted,
        evidence=evidence or [],
        failed_tools=failed_tools or [],
        risk_flags=risk_flags or [],
        started_at=NOW,
    )


def lookup_result(matches=None, near_matches=None, unconnected=False) -> PaymentLookupResult:
    return PaymentLookupResult(
        matches=matches or [],
        near_matches=near_matches or [],
        searched_sources=["connected"],
        unconnected_sources_exist=unconnected,
    )


def order_payments_result(captures=None, unconnected=False) -> OrderPaymentsResult:
    return OrderPaymentsResult(captures=captures or [], unconnected_sources_exist=unconnected)


def refund_history_result(refunds=None) -> RefundHistoryResult:
    return RefundHistoryResult(refunds=refunds or [])


def refund(refund_id="ref_1", order_id="order_1", capture_id="pay_1",
           amount_paise=0, issued_at=NOW, channel="gateway") -> RefundRow:
    return RefundRow(
        refund_id=refund_id, order_id=order_id, capture_id=capture_id,
        amount_paise=amount_paise, issued_at=issued_at, channel=channel,
    )
