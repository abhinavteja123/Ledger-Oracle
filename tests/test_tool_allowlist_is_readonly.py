"""Rule 2 (PRD 5.2): the agent cannot write.

Asserts (1) the tool registry contains exactly the five allowlisted tools, all
declared mutates=False, (2) the read-only connection mode actually rejects a
write, not just that the code claims to be read-only, and (3) the
ledger_source='external' leakage the PRD calls out (7.3, 15.1, and the
worked example in 21) does not happen: absence of an external row is never
reported as absence, and its data is never returned.
"""
import os
import sqlite3
import tempfile

import pytest

from models import PaymentLookupResult
from tools import (
    TOOL_REGISTRY,
    check_payment_status,
    get_order_payments,
    get_payment_by_utr,
)

EXPECTED_TOOLS = {
    "get_payment_by_utr",
    "get_order_payments",
    "find_duplicate_captures",
    "check_refund_history",
    "check_payment_status",
}

SCHEMA = """
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


@pytest.fixture()
def ledger_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO captures VALUES ('pay_1','ord_1',249900,'upi','111111111111',NULL,"
        "'2026-01-01T10:00:00Z',NULL,'connected')"
    )
    conn.execute(
        "INSERT INTO captures VALUES ('pay_2','ord_1',249900,'upi','222222222222',NULL,"
        "'2026-01-01T10:05:00Z',NULL,'external')"
    )
    conn.commit()
    conn.close()
    yield path
    os.remove(path)


def test_tool_registry_is_exactly_the_allowlist_and_readonly():
    assert set(TOOL_REGISTRY.keys()) == EXPECTED_TOOLS
    assert all(spec["mutates"] is False for spec in TOOL_REGISTRY.values())


def test_readonly_connection_rejects_writes(ledger_db):
    conn = sqlite3.connect(f"file:{ledger_db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO captures VALUES ('x','y',1,'upi',NULL,NULL,'t',NULL,'connected')")
    conn.close()


def test_external_utr_absent_from_matches_but_flags_unconnected(ledger_db):
    r = get_payment_by_utr("222222222222", db_path=ledger_db)
    assert isinstance(r, PaymentLookupResult)
    assert r.matches == [], "external row must never be returned as a match"
    assert r.searched_sources == ["connected"]
    assert r.unconnected_sources_exist is True, (
        "absence of an external-only reference must be flagged, not silently treated as proof of absence"
    )


def test_external_order_captures_excluded_but_flagged(ledger_db):
    r = get_order_payments("ord_1", db_path=ledger_db)
    assert [c.capture_id for c in r.captures] == ["pay_1"]
    assert r.unconnected_sources_exist is True


def test_external_capture_id_reads_as_not_found(ledger_db):
    r = check_payment_status("pay_2", db_path=ledger_db)
    assert r.found is False
    assert r.capture is None


if __name__ == "__main__":
    import pytest as _pytest

    raise SystemExit(_pytest.main([__file__, "-q"]))
