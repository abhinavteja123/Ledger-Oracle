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

ENGINE_VERSION = "2.0"
