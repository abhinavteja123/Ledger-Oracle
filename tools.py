"""Five typed read-only tools over the ledger. See PRD-Ledger-Oracle.md section 7.

The agent never touches SQL directly -- it calls these functions, which return
Pydantic objects from models.py. Every tool is a read (Rule 2, PRD 5.2): the
connection is opened `mode=ro` and the registry records `mutates=False` for
each one, so both halves of "the agent cannot write" are enforced by the
driver and by a test, not by convention.
"""
import sqlite3
import time

import db
from config import FUZZ_DISTANCE
from models import (
    CaptureRow,
    DuplicateCheckResult,
    OrderPaymentsResult,
    PaymentLookupResult,
    PaymentStatusResult,
    RefundHistoryResult,
    RefundRow,
    ToolError,
)

DB_PATH = "data/ledger.db"
# FUZZ_DISTANCE's default comes from config.py (PRD 16.2 -- "the constitution, shown in
# the demo"). It's still a plain module-level name here, not re-read from config live,
# so eval/sweep.py patches `tools.FUZZ_DISTANCE` directly to vary it between runs.
DUPLICATE_WINDOW_SECONDS = 15 * 60

TOOL_REGISTRY: dict[str, dict] = {}


def tool(mutates: bool):
    """Registers a function in TOOL_REGISTRY so the read-only property is
    provable (assert over the registry) rather than just a claim in prose."""

    def decorator(fn):
        TOOL_REGISTRY[fn.__name__] = {"fn": fn, "mutates": mutates}
        return fn

    return decorator


def _connect(db_path: str = DB_PATH):
    return db.get_readonly_connection(db_path)


def _row_to_capture(row: sqlite3.Row) -> CaptureRow:
    return CaptureRow(
        capture_id=row["capture_id"],
        order_id=row["order_id"],
        amount_paise=row["amount_paise"],
        instrument=row["instrument"],
        utr=row["utr"],
        payee_vpa=row["payee_vpa"],
        captured_at=row["captured_at"],
        settled_at=row["settled_at"],
        ledger_source=row["ledger_source"],
    )


def _row_to_refund(row: sqlite3.Row) -> RefundRow:
    return RefundRow(
        refund_id=row["refund_id"],
        order_id=row["order_id"],
        capture_id=row["capture_id"],
        amount_paise=row["amount_paise"],
        issued_at=row["issued_at"],
        channel=row["channel"],
    )


def _edit_distance(a: str, b: str) -> int:
    # Damerau-Levenshtein (optimal string alignment): substitution, insertion,
    # deletion, and adjacent transposition each cost 1. Plain Levenshtein/SequenceMatcher
    # charges 2 for an adjacent-digit swap, which made the typo defect class (PRD 17.3,
    # a transposed pair of digits) permanently unreachable at FUZZ_DISTANCE=1 -- caught
    # via eval/score.py against the generator's typo_utr claims, logged in FAILURES.md.
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


@tool(mutates=False)
def get_payment_by_utr(utr: str, *, db_path: str = DB_PATH) -> PaymentLookupResult | ToolError:
    """Resolve a UTR to a capture in the connected ledger.
    Returns matches=[] when absent -- absence is a result, not an error."""
    if not isinstance(utr, str) or not utr.strip():
        return ToolError(tool="get_payment_by_utr", error_class="invalid_args", attempt=1,
                          detail="utr must be a non-empty string")
    start = time.monotonic()
    try:
        conn = _connect(db_path)
    except db.connection_errors() as e:
        return ToolError(tool="get_payment_by_utr", error_class="unavailable", attempt=1, detail=str(e))
    try:
        connected = conn.execute(
            "SELECT * FROM captures WHERE ledger_source='connected'"
        ).fetchall()
        exact = [r for r in connected if r["utr"] == utr]
        near = []
        if not exact:
            for r in connected:
                if r["utr"] and r["utr"] != utr and _edit_distance(r["utr"], utr) <= FUZZ_DISTANCE:
                    near.append(r)
        unconnected_exists = conn.execute(
            "SELECT 1 FROM captures WHERE ledger_source='external' AND utr=? LIMIT 1", (utr,)
        ).fetchone() is not None
        return PaymentLookupResult(
            matches=[_row_to_capture(r) for r in exact],
            near_matches=[_row_to_capture(r) for r in near],
            searched_sources=["connected"],
            unconnected_sources_exist=unconnected_exists,
            query_latency_ms=int((time.monotonic() - start) * 1000),
        )
    finally:
        conn.close()


@tool(mutates=False)
def get_order_payments(order_id: str, *, db_path: str = DB_PATH) -> OrderPaymentsResult | ToolError:
    """All connected-ledger captures for an order, with captured/settled timestamps."""
    if not isinstance(order_id, str) or not order_id.strip():
        return ToolError(tool="get_order_payments", error_class="invalid_args", attempt=1,
                          detail="order_id must be a non-empty string")
    start = time.monotonic()
    try:
        conn = _connect(db_path)
    except db.connection_errors() as e:
        return ToolError(tool="get_order_payments", error_class="unavailable", attempt=1, detail=str(e))
    try:
        connected = conn.execute(
            "SELECT * FROM captures WHERE ledger_source='connected' AND order_id=?", (order_id,)
        ).fetchall()
        unconnected_exists = conn.execute(
            "SELECT 1 FROM captures WHERE ledger_source='external' AND order_id=? LIMIT 1", (order_id,)
        ).fetchone() is not None
        return OrderPaymentsResult(
            captures=[_row_to_capture(r) for r in connected],
            unconnected_sources_exist=unconnected_exists,
            query_latency_ms=int((time.monotonic() - start) * 1000),
        )
    finally:
        conn.close()


@tool(mutates=False)
def find_duplicate_captures(order_id: str, *, db_path: str = DB_PATH) -> DuplicateCheckResult | ToolError:
    """Captures on this order with equal amount within a 15-minute window.
    This is the tool that answers 'was I actually charged twice'."""
    if not isinstance(order_id, str) or not order_id.strip():
        return ToolError(tool="find_duplicate_captures", error_class="invalid_args", attempt=1,
                          detail="order_id must be a non-empty string")
    start = time.monotonic()
    try:
        conn = _connect(db_path)
    except db.connection_errors() as e:
        return ToolError(tool="find_duplicate_captures", error_class="unavailable", attempt=1, detail=str(e))
    try:
        rows = conn.execute(
            "SELECT * FROM captures WHERE ledger_source='connected' AND order_id=? ORDER BY captured_at",
            (order_id,),
        ).fetchall()
        pairs = []
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                if a["amount_paise"] != b["amount_paise"]:
                    continue
                t_a = _parse_iso(a["captured_at"])
                t_b = _parse_iso(b["captured_at"])
                if t_a is None or t_b is None:
                    continue
                if abs((t_b - t_a).total_seconds()) <= DUPLICATE_WINDOW_SECONDS:
                    pairs.append((_row_to_capture(a), _row_to_capture(b)))
        return DuplicateCheckResult(
            duplicate_pairs=pairs,
            query_latency_ms=int((time.monotonic() - start) * 1000),
        )
    finally:
        conn.close()


@tool(mutates=False)
def check_refund_history(order_id: str, *, db_path: str = DB_PATH) -> RefundHistoryResult | ToolError:
    """Refunds already issued for the order, including out_of_band rows.
    Feeds invariant I1 headroom."""
    if not isinstance(order_id, str) or not order_id.strip():
        return ToolError(tool="check_refund_history", error_class="invalid_args", attempt=1,
                          detail="order_id must be a non-empty string")
    start = time.monotonic()
    try:
        conn = _connect(db_path)
    except db.connection_errors() as e:
        return ToolError(tool="check_refund_history", error_class="unavailable", attempt=1, detail=str(e))
    try:
        rows = conn.execute("SELECT * FROM refunds WHERE order_id=?", (order_id,)).fetchall()
        return RefundHistoryResult(
            refunds=[_row_to_refund(r) for r in rows],
            query_latency_ms=int((time.monotonic() - start) * 1000),
        )
    finally:
        conn.close()


@tool(mutates=False)
def check_payment_status(capture_id: str, *, db_path: str = DB_PATH) -> PaymentStatusResult | ToolError:
    """Settlement state of one capture. Distinguishes 'not present' from
    'present but not yet settled' -- the T+0/T+1 escalate case."""
    if not isinstance(capture_id, str) or not capture_id.strip():
        return ToolError(tool="check_payment_status", error_class="invalid_args", attempt=1,
                          detail="capture_id must be a non-empty string")
    start = time.monotonic()
    try:
        conn = _connect(db_path)
    except db.connection_errors() as e:
        return ToolError(tool="check_payment_status", error_class="unavailable", attempt=1, detail=str(e))
    try:
        # ledger_source='connected' only -- a capture_id the tool layer never handed out
        # (i.e. an external row) must read as not-found, not leak its settlement state.
        row = conn.execute(
            "SELECT * FROM captures WHERE ledger_source='connected' AND capture_id=?", (capture_id,)
        ).fetchone()
        return PaymentStatusResult(
            capture=_row_to_capture(row) if row else None,
            found=row is not None,
            query_latency_ms=int((time.monotonic() - start) * 1000),
        )
    finally:
        conn.close()


def _parse_iso(ts: str):
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


if __name__ == "__main__":
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    setup = sqlite3.connect(path)
    setup.executescript(
        """
        CREATE TABLE captures (
            capture_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, amount_paise INTEGER NOT NULL,
            instrument TEXT NOT NULL, utr TEXT, payee_vpa TEXT, captured_at TEXT NOT NULL,
            settled_at TEXT, ledger_source TEXT NOT NULL
        );
        CREATE TABLE refunds (
            refund_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, capture_id TEXT,
            amount_paise INTEGER NOT NULL, issued_at TEXT NOT NULL, channel TEXT NOT NULL
        );
        """
    )
    setup.execute(
        "INSERT INTO captures VALUES ('pay_1','ord_1',249900,'upi','111111111111',NULL,"
        "'2026-01-01T10:00:00Z',NULL,'connected')"
    )
    setup.execute(
        "INSERT INTO captures VALUES ('pay_2','ord_1',249900,'upi','222222222222',NULL,"
        "'2026-01-01T10:05:00Z',NULL,'external')"
    )
    setup.commit()
    setup.close()

    try:
        r = get_payment_by_utr("999999999999", db_path=path)
        assert isinstance(r, PaymentLookupResult) and r.matches == [], "unknown UTR must return empty matches"

        r = get_payment_by_utr("222222222222", db_path=path)
        assert r.matches == [] and r.unconnected_sources_exist is True, (
            "external-only UTR must not match, but must flag unconnected_sources_exist"
        )

        r = get_order_payments("ord_1", db_path=path)
        assert len(r.captures) == 1 and r.captures[0].capture_id == "pay_1"
        assert r.unconnected_sources_exist is True

        r = check_payment_status("pay_2", db_path=path)
        assert r.found is False, "external capture_id must read as not-found, never leaked"

        assert all(spec["mutates"] is False for spec in TOOL_REGISTRY.values())
        print("OK: tools.py smoke checks passed")
    finally:
        os.remove(path)
