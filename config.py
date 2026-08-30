"""The agent's constitution + policy tolerances. See PRD sections 6 and 16.2.

Every value here is quoted in the audit record (PRD 12.3, tolerance_config).
"""

MAX_INVESTIGATION_STEPS = 5       # tool calls per claim, hard stop
MAX_RETRIES_PER_TOOL = 2          # then the tool is marked failed, not retried again
WALL_CLOCK_BUDGET_SECONDS = 30    # whole investigation, not per call
MAX_PARSE_ATTEMPTS = 2            # invalid LLM output -> reparse once, then escalate

TOOL_ALLOWLIST = {                # read-only. adding a mutating tool fails a test.
    "get_payment_by_utr",
    "get_order_payments",
    "find_duplicate_captures",
    "check_refund_history",
    "check_payment_status",
}

# Policy tolerance knobs (PRD 16.2) -- what makes precision/recall non-vacuous.
FUZZ_DISTANCE = 1              # higher catches more typos as escalate; also drags real
                                # non-matches into escalate
AMOUNT_TOLERANCE_PAISE = 0     # higher tolerates partial capture; also lets inflated
                                # claims through
SETTLEMENT_WINDOW_HOURS = 30   # higher escalates more in-flight payments; also delays
                                # real blocks

# Repeated-claim abuse guard (see policy.py's REPEATED_CLAIM_ABUSE branch). Counts
# only prior claims on the same order_id that ended in one of these reason codes --
# a genuine terminal denial, not a retry-invited state (REF_MAY_BE_IN_FLIGHT,
# TOOL_UNAVAILABLE, MODEL_UNAVAILABLE, PARSE_FAILED, REF_NEAR_MATCH_TYPO all
# legitimately expect a resubmission and must never count towards this).
ADVERSE_REASON_CODES = frozenset({
    "REF_ALREADY_CONSUMED",
    "REF_NOT_IN_LEDGER",
    "AMOUNT_MISMATCH",
    "EXCEEDS_CAPTURED_TOTAL",
    "CONTRADICTORY_LEDGER_RECORDS",
    "AMBIGUOUS_ORDER",
    "CONTRADICTORY_AMOUNTS",
    "CONTRADICTORY_CLAIM",
})
ABUSE_REPEAT_THRESHOLD = 3     # this many prior adverse claims on one order_id ->
                                # the next one blocks outright, no fresh investigation

ENGINE_VERSION = "2.0"
