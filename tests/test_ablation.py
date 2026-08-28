"""Ablation runs B and C (PRD 18.6) against a tiny in-process fixture: 2 synthetic
claims, a fixture ledger.db, and a fake client (no real network -- no API key is
available in this environment). Confirms the matrix math is correct for a known case
and that both runs execute end to end without error.
"""
import json
import sqlite3

from eval.ablation import run_b, run_c

CLAIM_TRUE_JSON = json.dumps({
    "claim_type": "payment_not_recorded", "order_id": "ord_1",
    "claimed_reference": "111111111111", "claimed_amount_paise": 249900,
    "claimed_instrument": "upi", "claimed_payee_vpa": None,
    "claimed_timestamp_iso": None, "customer_asserts_count": None,
})
CLAIM_FALSE_JSON = json.dumps({
    "claim_type": "payment_not_recorded", "order_id": "ord_2",
    "claimed_reference": "999999999999", "claimed_amount_paise": 100000,
    "claimed_instrument": "upi", "claimed_payee_vpa": None,
    "claimed_timestamp_iso": None, "customer_asserts_count": None,
})


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeResponse:
    def __init__(self, message):
        self.choices = [_FakeChoice(message)]


class _FakeToolCall:
    def __init__(self, name, arguments):
        self.function = type("F", (), {"name": name, "arguments": arguments})()


class _CombinedFakeClient:
    """Plays back a fixed plan: a str item is a parser JSON response; a (name, args)
    tuple is a tool_call; None means 'no tool call, stop'."""
    def __init__(self, plan):
        self.plan = list(plan)

    @property
    def chat(self):
        outer = self
        class _Chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    item = outer.plan.pop(0)
                    if item is None:
                        return _FakeResponse(_FakeMessage(tool_calls=None))
                    if isinstance(item, tuple):
                        name, args = item
                        return _FakeResponse(_FakeMessage(tool_calls=[_FakeToolCall(name, args)]))
                    return _FakeResponse(_FakeMessage(content=item))
        return _Chat()


def _fixture_data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    con = sqlite3.connect(d / "ledger.db")
    con.executescript("""
        CREATE TABLE captures (
            capture_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, amount_paise INTEGER NOT NULL,
            instrument TEXT NOT NULL, utr TEXT, payee_vpa TEXT, captured_at TEXT NOT NULL,
            settled_at TEXT, ledger_source TEXT NOT NULL
        );
        CREATE TABLE refunds (
            refund_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, capture_id TEXT,
            amount_paise INTEGER NOT NULL, issued_at TEXT NOT NULL, channel TEXT NOT NULL
        );
        CREATE TABLE consumed_references (
            reference TEXT PRIMARY KEY, consumed_by TEXT NOT NULL,
            consumed_at TEXT NOT NULL, approved_by TEXT
        );
    """)
    con.execute(
        "INSERT INTO captures VALUES ('pay_1','ord_1',249900,'upi','111111111111',NULL,"
        "'2026-01-01T10:00:00Z','2026-01-01T10:05:00Z','connected')"
    )
    con.commit()
    con.close()

    manifest = [
        {
            "claim_id": "t1", "ground_truth": "pass", "class": "TRUE", "defect_class": None,
            "fp_cost_paise": 249900, "resolving_tools": ["get_payment_by_utr"],
            "sufficient_tools": ["get_payment_by_utr"], "expected_reason_code": "OK",
            "injected_fault": None, "tone_pair_id": None,
        },
        {
            "claim_id": "t2", "ground_truth": "block", "class": "INJECTED_FALSE",
            "defect_class": "utr_absent", "fp_cost_paise": 100000,
            "resolving_tools": ["get_payment_by_utr"], "sufficient_tools": ["get_payment_by_utr"],
            "expected_reason_code": "REF_NOT_IN_LEDGER", "injected_fault": None, "tone_pair_id": None,
        },
    ]
    (d / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")

    claims = [
        {"claim_id": "t1", "text": "paid 2499 for order ord_1, UTR 111111111111, please refund"},
        {"claim_id": "t2", "text": "paid 1000 for order ord_2, UTR 999999999999, please refund"},
    ]
    with open(d / "claims.jsonl", "w", encoding="utf-8") as f:
        for c in claims:
            f.write(json.dumps(c) + "\n")

    return d


def test_run_b_matrix_matches_known_ground_truth(tmp_path):
    d = _fixture_data_dir(tmp_path)
    client = _CombinedFakeClient([CLAIM_TRUE_JSON, CLAIM_FALSE_JSON])
    result = run_b(d, client=client)
    assert result["matrix"]["pass"]["pass"] == 1
    assert result["matrix"]["block"]["block"] == 1
    assert result["n_claims"] == 2


def test_run_c_executes_end_to_end_without_error(tmp_path):
    d = _fixture_data_dir(tmp_path)
    # Each claim: 1 parse call + 1 tool-selection call + 1 stop call.
    plan = [
        CLAIM_TRUE_JSON, ("get_payment_by_utr", '{"utr": "111111111111"}'), None,
        CLAIM_FALSE_JSON, ("get_payment_by_utr", '{"utr": "999999999999"}'), None,
    ]
    client = _CombinedFakeClient(plan)
    result = run_c(d, client=client)
    assert result["n_claims"] == 2
    assert result["matrix"]["pass"]["pass"] == 1
    assert result["matrix"]["block"]["block"] == 1
