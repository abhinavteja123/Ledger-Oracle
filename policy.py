"""The deterministic policy engine. See PRD section 16.

DECIDES. No network. No model. No randomness. Rule 1 (PRD 5.2): this module imports
nothing from parser.py, agent.py, or narrator.py, and makes no network calls.

consumed_references is passed in as a plain set rather than read from a DB connection
here, precisely so this module never needs an I/O layer of its own -- the caller (app.py /
eval/score.py) is responsible for loading it from data/ledger.db's consumed_references
table before calling decide(). That keeps this file's only dependency on the outside
world a Python set literal, which is what test_policy_is_offline actually needs to hold.
"""
from typing import Optional

from config import AMOUNT_TOLERANCE_PAISE, ENGINE_VERSION
from models import (
    CaptureRow,
    DuplicateCheckResult,
    InvariantResult,
    InvestigationState,
    OrderPaymentsResult,
    PaymentLookupResult,
    PaymentStatusResult,
    RefundHistoryResult,
    Verdict,
)


def _normalize_ref(ref: Optional[str]) -> Optional[str]:
    if ref is None:
        return None
    return ref.replace(" ", "").replace("-", "").upper()


def _all(state: InvestigationState, cls) -> list:
    return [e for e in state.evidence if isinstance(e, cls)]


def _latest(state: InvestigationState, cls):
    items = _all(state, cls)
    return items[-1] if items else None


def decidable(state: InvestigationState) -> bool:
    """Can decide() reach a verdict from the evidence gathered so far?

    Used by the agent loop (PRD 9.2) for graceful degradation: if a tool fails but
    enough evidence already exists to decide, there is no need to escalate on that
    failure alone.
    """
    if state.extracted is None:
        return False
    lookup = _latest(state, PaymentLookupResult)
    order_payments = _latest(state, OrderPaymentsResult)
    dup_check = _latest(state, DuplicateCheckResult)
    return any([lookup, order_payments, dup_check])


def _escalate(reason_code: str, invariants: list, why_not_block: str, degraded: bool) -> Verdict:
    return Verdict(
        decision="escalate",
        max_refundable_paise=0,
        invariants=invariants,
        reason_code=reason_code,
        why_not_block=why_not_block,
        degraded_path=degraded,
    )


def _block(reason_code: str, invariants: list, degraded: bool) -> Verdict:
    return Verdict(
        decision="block",
        max_refundable_paise=0,
        invariants=invariants,
        reason_code=reason_code,
        degraded_path=degraded,
    )


def decide(state: InvestigationState, consumed_references: frozenset = frozenset()) -> Verdict:
    invariants: list[InvariantResult] = []
    degraded = bool(state.failed_tools) or bool(state.risk_flags)
    claim = state.extracted

    # 0. DECIDABILITY GATE
    if claim is None:
        return _escalate(
            "INSUFFICIENT_CLAIM_DATA", invariants,
            "Claim did not parse into a structured record; nothing to evaluate.", degraded,
        )
    # Claim-level red flags (PRD 10.3 ADVERSARIAL class) short-circuit before any
    # ledger evidence is consulted -- these are about the claim text itself being
    # unreasonable to act on, set upstream by the sanitizer/parser into risk_flags.
    for flag, reason in (
        ("AMBIGUOUS_ORDER", "AMBIGUOUS_ORDER"),
        ("CONTRADICTORY_AMOUNTS", "CONTRADICTORY_AMOUNTS"),
        ("CONTRADICTORY_CLAIM", "CONTRADICTORY_CLAIM"),
    ):
        if flag in state.risk_flags:
            return _escalate(
                reason, invariants,
                f"Claim text itself is unreasonable to act on ({flag}); a human must read it.",
                degraded,
            )

    if not decidable(state) and state.failed_tools:
        # Only a TOOL_UNAVAILABLE if tools were actually attempted and failed. A claim
        # with no reference and no order_id legitimately has no evidence to gather --
        # that falls through to the no-reference branch below, which escalates
        # INSUFFICIENT_CLAIM_DATA instead.
        return _escalate(
            "TOOL_UNAVAILABLE", invariants,
            "No usable ledger evidence was gathered; required tools failed.", degraded,
        )

    lookup = _latest(state, PaymentLookupResult)
    order_payments = _latest(state, OrderPaymentsResult)
    dup_check = _latest(state, DuplicateCheckResult)
    refund_history = _latest(state, RefundHistoryResult)

    # 1. NORMALISE
    ref = _normalize_ref(claim.claimed_reference)

    # CONTRADICTION CHECK (moved up from step 4 -- a contradiction makes every later
    # invariant computed over that evidence untrustworthy, so catch it before I3/I2/I1
    # rather than after. Two lookups disagreeing on the same capture's amount/instrument.)
    all_captures: list[CaptureRow] = []
    for lr in _all(state, PaymentLookupResult):
        all_captures.extend(lr.matches)
    if order_payments:
        all_captures.extend(order_payments.captures)
    seen_by_id: dict[str, CaptureRow] = {}
    for c in all_captures:
        prior = seen_by_id.get(c.capture_id)
        if prior is not None and (prior.amount_paise != c.amount_paise or prior.utr != c.utr):
            invariants.append(InvariantResult(
                invariant="I2", passed=False,
                detail=f"conflicting evidence for {c.capture_id}", evidence_rows=[c.capture_id],
            ))
            return _escalate(
                "CONTRADICTORY_LEDGER_RECORDS", invariants,
                "Two evidence rows disagree on the same capture; a human must reconcile this.",
                degraded,
            )
        seen_by_id[c.capture_id] = c

    # 2. I3 REPLAY (cheapest, most decisive -- check first)
    if ref and ref in consumed_references:
        invariants.append(InvariantResult(
            invariant="I3", passed=False,
            detail="reference already consumed by a prior settled claim", evidence_rows=[],
        ))
        return _block("REF_ALREADY_CONSUMED", invariants, degraded)
    invariants.append(InvariantResult(
        invariant="I3", passed=True, detail="reference not previously consumed", evidence_rows=[],
    ))

    # 3. I2 REFERENCE RESOLUTION
    resolved_capture: Optional[CaptureRow] = None
    unconnected_flag = bool(lookup and lookup.unconnected_sources_exist) or bool(
        order_payments and order_payments.unconnected_sources_exist
    )

    if ref is not None:
        if lookup is None:
            return _escalate(
                "TOOL_UNAVAILABLE", invariants,
                "Claim named a reference but no lookup evidence was gathered for it.", degraded,
            )
        exact = lookup.matches
        near = lookup.near_matches
        if len(exact) > 1:
            invariants.append(InvariantResult(
                invariant="I2", passed=False, detail="reference matches more than one capture",
                evidence_rows=[c.capture_id for c in exact],
            ))
            return _escalate(
                "REF_AMBIGUOUS", invariants,
                "Reference resolves to more than one capture; the engine will not guess.", degraded,
            )
        if len(exact) == 1:
            resolved_capture = exact[0]
            if resolved_capture.settled_at is None:
                # Exact match, but not settled yet -- this must be checked here too, not
                # only in the zero-exact-match branch below: an in-flight capture can
                # still resolve by UTR (the row exists), it just hasn't cleared.
                invariants.append(InvariantResult(
                    invariant="I2", passed=False, detail="capture in flight, not yet settled",
                    evidence_rows=[resolved_capture.capture_id],
                ))
                return _escalate(
                    "REF_MAY_BE_IN_FLIGHT", invariants,
                    "This capture has not settled yet; absence of a clean match now is "
                    "not absence once it settles.", degraded,
                )
            invariants.append(InvariantResult(
                invariant="I2", passed=True, detail="reference resolved to exactly one capture",
                evidence_rows=[resolved_capture.capture_id],
            ))
        else:  # zero exact matches
            if len(near) == 1:
                invariants.append(InvariantResult(
                    invariant="I2", passed=False, detail="near match only, distance>0 (possible typo)",
                    evidence_rows=[near[0].capture_id],
                ))
                return _escalate(
                    "REF_NEAR_MATCH_TYPO", invariants,
                    "Reference nearly matches a real capture; likely a fat-fingered typo, "
                    "not fraud. We will not accuse an honest customer over a digit.", degraded,
                )
            in_flight = [
                c for c in (order_payments.captures if order_payments else [])
                if c.settled_at is None
            ]
            if in_flight:
                invariants.append(InvariantResult(
                    invariant="I2", passed=False, detail="capture in flight, not yet settled",
                    evidence_rows=[c.capture_id for c in in_flight],
                ))
                return _escalate(
                    "REF_MAY_BE_IN_FLIGHT", invariants,
                    "A capture on this order has not settled yet; absence now is not "
                    "absence once it settles.", degraded,
                )
            if unconnected_flag:
                invariants.append(InvariantResult(
                    invariant="I2", passed=False, detail="not found in connected ledger, "
                    "but unconnected sources exist", evidence_rows=[],
                ))
                return _escalate(
                    "REF_OUTSIDE_CONNECTED_LEDGER", invariants,
                    "The reference is not in the ledger we can see, but this merchant has "
                    "sources we are not connected to. Absence here is not proof.", degraded,
                )
            if claim.claimed_instrument == "unknown":
                # The claim references a payment rail we don't parse into a known
                # instrument (e.g. cheque) -- absence in our ledger says nothing,
                # because we were never going to carry this rail at all.
                invariants.append(InvariantResult(
                    invariant="I2", passed=False,
                    detail="claimed instrument is not one this ledger tracks", evidence_rows=[],
                ))
                return _escalate(
                    "RAIL_NOT_COVERED", invariants,
                    "This payment rail is outside what our connected ledger tracks at all; "
                    "a human must check it through the original channel.", degraded,
                )
            invariants.append(InvariantResult(
                invariant="I2", passed=False, detail="not found in connected ledger, "
                "no unconnected sources exist", evidence_rows=[],
            ))
            return _block("REF_NOT_IN_LEDGER", invariants, degraded)
    else:
        # ponytail: no claimed_reference (typically a duplicate_charge claim, which is
        # about capture *count* on an order, not one specific reference). Resolve via
        # order-level evidence instead of I2. Ceiling: doesn't yet disambiguate multiple
        # candidate order_ids or contradictory claimed amounts (PRD 10.3's AMBIGUOUS_ORDER /
        # CONTRADICTORY_AMOUNTS adversarial cases) -- add that branch here if the
        # adversarial eval class shows it's needed.
        if claim.order_id is None or order_payments is None:
            return _escalate(
                "INSUFFICIENT_CLAIM_DATA", invariants,
                "No reference and no order evidence to reason about.", degraded,
            )
        invariants.append(InvariantResult(
            invariant="I2", passed=True,
            detail="no reference claimed; resolving via order-level evidence instead",
            evidence_rows=[c.capture_id for c in order_payments.captures],
        ))
        if order_payments.captures:
            resolved_capture = order_payments.captures[0]

    # 5. AMOUNT TOLERANCE (only if a capture resolved)
    if resolved_capture is not None and claim.claimed_amount_paise is not None:
        diff = abs(resolved_capture.amount_paise - claim.claimed_amount_paise)
        if diff > AMOUNT_TOLERANCE_PAISE:
            invariants.append(InvariantResult(
                invariant="I1", passed=False,
                detail=f"claimed amount differs from captured amount by {diff} paise",
                evidence_rows=[resolved_capture.capture_id],
            ))
            return _block("AMOUNT_MISMATCH", invariants, degraded)

    # 6. I1 REFUND HEADROOM
    captured_total = sum(c.amount_paise for c in {c.capture_id: c for c in all_captures}.values())
    refunded_total = sum(r.amount_paise for r in (refund_history.refunds if refund_history else []))
    headroom = captured_total - refunded_total
    claimed = claim.claimed_amount_paise or 0

    out_of_band_covers = any(
        r.channel == "out_of_band" for r in (refund_history.refunds if refund_history else [])
    )
    if out_of_band_covers:
        invariants.append(InvariantResult(
            invariant="I1", passed=False, detail="an out-of-band refund already exists for this order",
            evidence_rows=[r.refund_id for r in refund_history.refunds if r.channel == "out_of_band"],
        ))
        return _escalate(
            "POSSIBLE_OUT_OF_BAND_REFUND", invariants,
            "A manually-issued refund already exists for this order; a human must confirm "
            "it doesn't already cover this claim.", degraded,
        )

    if claimed > headroom:
        invariants.append(InvariantResult(
            invariant="I1", passed=False,
            detail=f"claimed {claimed} exceeds headroom {headroom}", evidence_rows=[],
        ))
        return _block("EXCEEDS_CAPTURED_TOTAL", invariants, degraded)

    invariants.append(InvariantResult(
        invariant="I1", passed=True, detail=f"headroom {headroom} covers claimed {claimed}",
        evidence_rows=[],
    ))

    # 7. PASS
    return Verdict(
        decision="pass",
        max_refundable_paise=min(claimed, headroom),
        invariants=invariants,
        reason_code="OK",
        resolved_capture_id=resolved_capture.capture_id if resolved_capture else None,
        degraded_path=degraded,
    )


if __name__ == "__main__":
    # ponytail: smallest possible smoke check, not a substitute for tests/test_policy_invariants.py
    from models import StructuredClaim

    state = InvestigationState(
        claim_id="smoke_0001",
        raw_message="test",
        extracted=StructuredClaim(claim_type="other", order_id=None, claimed_reference=None),
        started_at="2026-01-01T00:00:00Z",
    )
    v = decide(state)
    assert v.decision == "escalate" and v.reason_code == "INSUFFICIENT_CLAIM_DATA"
    print(f"OK: engine v{ENGINE_VERSION}, empty claim escalates as expected -> {v.reason_code}")
