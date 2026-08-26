"""Rule 3 (PRD 5.2): no model reachable from the LLM boundary can express a Decision.

Walks every field of StructuredClaim, ToolCall, and each ToolResult member type
(recursing into nested models like CaptureRow) and fails if any field's type
could hold "pass" / "block" / "escalate".
"""
import typing

from pydantic import BaseModel

from models import (
    Decision,
    DuplicateCheckResult,
    OrderPaymentsResult,
    PaymentLookupResult,
    PaymentStatusResult,
    RefundHistoryResult,
    StructuredClaim,
    ToolCall,
    ToolError,
)

DECISION_VALUES = set(typing.get_args(Decision))

LLM_BOUNDARY_MODELS = [
    StructuredClaim,
    ToolCall,
    PaymentLookupResult,
    OrderPaymentsResult,
    DuplicateCheckResult,
    RefundHistoryResult,
    PaymentStatusResult,
    ToolError,
]


def _type_contains_decision(tp, seen: set) -> bool:
    if typing.get_origin(tp) is typing.Literal:
        return bool(DECISION_VALUES & set(typing.get_args(tp)))
    args = typing.get_args(tp)
    if args:
        return any(_type_contains_decision(a, seen) for a in args)
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        if tp in seen:
            return False
        seen.add(tp)
        return any(
            _type_contains_decision(f.annotation, seen)
            for f in tp.model_fields.values()
        )
    return False


def test_no_decision_field_in_model_outputs():
    for model in LLM_BOUNDARY_MODELS:
        for name, field in model.model_fields.items():
            assert not _type_contains_decision(field.annotation, set()), (
                f"{model.__name__}.{name} can express a Decision value"
            )


if __name__ == "__main__":
    test_no_decision_field_in_model_outputs()
    print("OK: no LLM-boundary model can express a decision")
