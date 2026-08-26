"""All Pydantic models for Ledger Oracle. See PRD-Ledger-Oracle.md sections 7-8, 14-16.

No model reachable from the LLM boundary (StructuredClaim, ToolCall, any tool result)
may contain a field whose type includes the Decision literal. See Rule 3, PRD 5.2 --
enforced by test_no_decision_field_in_model_outputs.
"""
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field

Decision = Literal["pass", "block", "escalate"]


# ---------------------------------------------------------------------------
# Claim extraction (LLM boundary -- no decision field, ever)
# ---------------------------------------------------------------------------

class StructuredClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")  # Cerebras strict json_schema needs
                                                 # additionalProperties:false; Pydantic
                                                 # only emits that with extra="forbid"

    claim_type: Literal["duplicate_charge", "payment_not_recorded", "other"]
    order_id: Optional[str] = None
    claimed_reference: Optional[str] = Field(
        None, description="UTR/RRN exactly as written, including typos"
    )
    claimed_amount_paise: Optional[int] = Field(
        None, description="paise. Rs 2,499 -> 249900"
    )
    claimed_instrument: Optional[
        Literal["upi", "card", "netbanking", "wallet", "unknown"]
    ] = None
    claimed_payee_vpa: Optional[str] = None
    claimed_timestamp_iso: Optional[str] = None
    customer_asserts_count: Optional[int] = None
    # NOTE: no decision field here, deliberately. See Rule 3, PRD 5.2 / 10.1.


# ---------------------------------------------------------------------------
# Ledger rows (SQLite -- see PRD 15.1)
# ---------------------------------------------------------------------------

class CaptureRow(BaseModel):
    capture_id: str
    order_id: str
    amount_paise: int
    instrument: Literal["upi", "card", "netbanking", "wallet"]
    utr: Optional[str] = None
    payee_vpa: Optional[str] = None
    captured_at: str  # ISO-8601 UTC
    settled_at: Optional[str] = None  # None while in flight (T+0 -> T+1)
    ledger_source: Literal["connected", "external"]


class RefundRow(BaseModel):
    refund_id: str
    order_id: str
    capture_id: Optional[str] = None  # None if issued out-of-band
    amount_paise: int
    issued_at: str
    channel: Literal["gateway", "out_of_band"]


# ---------------------------------------------------------------------------
# Tool layer -- typed results carry provenance, not just data (PRD 7.3, 7.4)
# ---------------------------------------------------------------------------

class ToolError(BaseModel):
    tool: str
    error_class: Literal["timeout", "unavailable", "invalid_args", "not_found"]
    attempt: int
    detail: str


class PaymentLookupResult(BaseModel):
    matches: list[CaptureRow] = []
    near_matches: list[CaptureRow] = []  # within FUZZ_DISTANCE, for the typo case
    searched_sources: list[str] = []  # ['connected'] -- never includes 'external'
    unconnected_sources_exist: bool = False  # True -> absence here is NOT proof of absence
    query_latency_ms: int = 0


class OrderPaymentsResult(BaseModel):
    captures: list[CaptureRow] = []
    unconnected_sources_exist: bool = False
    query_latency_ms: int = 0


class DuplicateCheckResult(BaseModel):
    duplicate_pairs: list[tuple[CaptureRow, CaptureRow]] = []
    query_latency_ms: int = 0


class RefundHistoryResult(BaseModel):
    refunds: list[RefundRow] = []
    query_latency_ms: int = 0


class PaymentStatusResult(BaseModel):
    capture: Optional[CaptureRow] = None
    found: bool = False
    query_latency_ms: int = 0


ToolResult = (
    PaymentLookupResult
    | OrderPaymentsResult
    | DuplicateCheckResult
    | RefundHistoryResult
    | PaymentStatusResult
    | ToolError
)


class ToolCall(BaseModel):
    tool: str
    args: dict
    timestamp: str


# ---------------------------------------------------------------------------
# Agent state (PRD 8.1) -- the audit record, threaded through the investigation
# ---------------------------------------------------------------------------

class InvestigationState(BaseModel):
    claim_id: str
    raw_message: str
    sanitizer_flags: list[str] = []  # e.g. ['INSTRUCTION_SHAPED_SPAN']
    extracted: Optional[StructuredClaim] = None  # None until parse succeeds
    parse_attempts: int = 0

    evidence: list[ToolResult] = []  # append-only within a run
    tools_called: list[ToolCall] = []  # name + args + timestamp, in order
    failed_tools: list[ToolError] = []
    retries: dict[str, int] = {}  # tool name -> attempts consumed

    risk_flags: list[str] = []  # INJECTION_DETECTED, CONTRADICTORY_AMOUNTS, ...
    steps_used: int = 0
    started_at: str
    status: Literal["investigating", "decided", "escalated", "failed"] = "investigating"
    stop_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Verdict (PRD 15.2) -- produced only by policy.decide(), never by the model
# ---------------------------------------------------------------------------

class InvariantResult(BaseModel):
    invariant: Literal["I1", "I2", "I3"]
    passed: bool
    detail: str
    evidence_rows: list[str] = []  # capture_id / refund_id actually consulted


class Verdict(BaseModel):
    decision: Decision
    max_refundable_paise: int  # bounded money action; 0 on block/escalate
    invariants: list[InvariantResult] = []
    reason_code: str  # 'REF_NOT_IN_LEDGER', 'STEP_BUDGET_EXHAUSTED', ...
    resolved_capture_id: Optional[str] = None
    why_not_block: Optional[str] = None  # populated on escalate. Shown in review UI.
    degraded_path: bool = False  # True if any tool needed a retry/fallback
