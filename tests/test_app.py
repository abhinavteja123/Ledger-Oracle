"""End-to-end FastAPI tests for app.py: /verify, the review queue, and the
approve-writes-consumed_references loop-closing property (PRD 11.3).

Everything runs against a fixture ledger.db placed at the literal relative path
"data/ledger.db" (via monkeypatch.chdir), matching every hardcoded default in
tools.py/agent.py -- no need to thread a db_path override through app.py itself.
The LLM client is faked at both parser.get_client and agent.get_client; no
network call happens anywhere in this file.
"""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import agent
import parser as parser_module

CLAIM_JSON = json.dumps({
    "claim_type": "payment_not_recorded", "order_id": "ord_1",
    "claimed_reference": "111111111111", "claimed_amount_paise": 249900,
    "claimed_instrument": "upi", "claimed_payee_vpa": None,
    "claimed_timestamp_iso": None, "customer_asserts_count": None,
})

BLOCK_CLAIM_JSON = json.dumps({
    "claim_type": "payment_not_recorded", "order_id": "ord_1",
    "claimed_reference": "999999999999", "claimed_amount_paise": 249900,
    "claimed_instrument": "upi", "claimed_payee_vpa": None,
    "claimed_timestamp_iso": None, "customer_asserts_count": None,
})

ESCALATE_CLAIM_JSON = json.dumps({
    "claim_type": "other", "order_id": None,
    "claimed_reference": None, "claimed_amount_paise": None,
    "claimed_instrument": None, "claimed_payee_vpa": None,
    "claimed_timestamp_iso": None, "customer_asserts_count": None,
})


class _FakeSmartClient:
    """Shape-detects a parser-style (response_format) vs agent-style (tools) call and
    answers accordingly. tool_plan drives which tool the fake 'model' calls next."""

    def __init__(self, claim_json: str, tool_plan: list):
        self.claim_json = claim_json
        self.tool_plan = list(tool_plan)

    @property
    def chat(self):
        outer = self

        class _Chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    if "response_format" in kwargs:
                        msg = type("M", (), {"content": outer.claim_json})()
                    elif outer.tool_plan:
                        name, args = outer.tool_plan.pop(0)
                        tc = type("TC", (), {
                            "function": type("F", (), {"name": name, "arguments": args})()
                        })()
                        msg = type("M", (), {"tool_calls": [tc]})()
                    else:
                        msg = type("M", (), {"tool_calls": None})()
                    choice = type("C", (), {"message": msg})()
                    return type("R", (), {"choices": [choice]})()

        return _Chat()


def _install_fake_client(monkeypatch, claim_json, tool_plan):
    client = _FakeSmartClient(claim_json, tool_plan)
    monkeypatch.setattr(parser_module, "get_client", lambda: client)
    monkeypatch.setattr(agent, "get_client", lambda: client)
    return client


def _make_fixture_ledger(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "ledger.db"
    con = sqlite3.connect(db_path)
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


@pytest.fixture
def client_app(tmp_path, monkeypatch):
    _make_fixture_ledger(tmp_path)
    monkeypatch.chdir(tmp_path)
    import app as app_module
    app_module.REVIEW_QUEUE.clear()
    app_module.APPROVED_REFERENCES.clear()
    app_module.history_store._rows.clear()
    return TestClient(app_module.app)


def test_verify_resolves_to_pass(client_app, monkeypatch):
    _install_fake_client(monkeypatch, CLAIM_JSON, [("get_payment_by_utr", '{"utr": "111111111111"}')])
    res = client_app.post("/verify", json={"text": "paid 2499 for order ord_1, UTR 111111111111"})
    assert res.status_code == 200
    body = res.json()
    assert body["decision"] == "pass"
    assert body["extracted"]["order_id"] == "ord_1"


def test_verify_resolves_to_block(client_app, monkeypatch):
    _install_fake_client(monkeypatch, BLOCK_CLAIM_JSON, [("get_payment_by_utr", '{"utr": "999999999999"}')])
    res = client_app.post("/verify", json={"text": "paid via UTR 999999999999"})
    assert res.status_code == 200
    assert res.json()["decision"] == "block"


def test_verify_escalate_appears_in_queue(client_app, monkeypatch):
    _install_fake_client(monkeypatch, ESCALATE_CLAIM_JSON, [])
    res = client_app.post("/verify", json={"text": "refund pls"})
    body = res.json()
    assert body["decision"] == "escalate"

    q = client_app.get("/review/queue").json()
    assert len(q["claims"]) == 1
    claim_id = q["claims"][0]["claim_id"]

    detail = client_app.get(f"/review/{claim_id}").json()
    assert detail["raw_message"] == "refund pls"
    assert detail["stop_reason_text"]


def test_approve_writes_consumed_references_and_blocks_replay(client_app, monkeypatch):
    _install_fake_client(monkeypatch, CLAIM_JSON, [("get_payment_by_utr", '{"utr": "111111111111"}')])
    first = client_app.post("/verify", json={"text": "paid 2499, UTR 111111111111"}).json()
    assert first["decision"] == "pass"

    # A pass isn't reviewable, so manufacture an escalated claim on the SAME reference
    # to exercise the review->approve path, then replay the reference through /verify.
    _install_fake_client(monkeypatch, json.dumps({
        "claim_type": "payment_not_recorded", "order_id": "ord_1",
        "claimed_reference": "111111111111", "claimed_amount_paise": 249900,
        "claimed_instrument": "upi", "claimed_payee_vpa": None,
        "claimed_timestamp_iso": None, "customer_asserts_count": None,
    }), [])  # no tool calls -> undecidable -> escalates, lands in the queue
    escalated = client_app.post("/verify", json={"text": "paid 2499, UTR 111111111111 again"}).json()
    assert escalated["decision"] == "escalate"

    claim_id = client_app.get("/review/queue").json()["claims"][-1]["claim_id"]
    decide_res = client_app.post(f"/review/{claim_id}/decide", json={"action": "approve", "note": "checked manually"})
    assert decide_res.status_code == 200

    _install_fake_client(monkeypatch, CLAIM_JSON, [("get_payment_by_utr", '{"utr": "111111111111"}')])
    replay = client_app.post("/verify", json={"text": "paid 2499, UTR 111111111111"}).json()
    assert replay["decision"] == "block"
    assert replay["reason_code"] == "REF_ALREADY_CONSUMED"


def test_missing_ledger_db_does_not_crash(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()  # dir exists, but no ledger.db inside it
    monkeypatch.chdir(tmp_path)
    import app as app_module
    app_module.REVIEW_QUEUE.clear()
    app_module.APPROVED_REFERENCES.clear()
    app_module.history_store._rows.clear()
    client = TestClient(app_module.app)
    _install_fake_client(monkeypatch, CLAIM_JSON, [("get_payment_by_utr", '{"utr": "111111111111"}')] * 6)
    res = client.post("/verify", json={"text": "paid 2499, UTR 111111111111"})
    assert res.status_code == 200
    assert res.json()["decision"] != "pass"  # tools all fail against a missing DB -> never a false pass


def test_admin_claims_lists_every_decision_not_just_escalates(client_app, monkeypatch):
    _install_fake_client(monkeypatch, CLAIM_JSON, [("get_payment_by_utr", '{"utr": "111111111111"}')])
    client_app.post("/verify", json={"text": "paid 2499, UTR 111111111111"})
    _install_fake_client(monkeypatch, BLOCK_CLAIM_JSON, [("get_payment_by_utr", '{"utr": "999999999999"}')])
    client_app.post("/verify", json={"text": "paid via UTR 999999999999"})

    resp = client_app.get("/admin/claims")
    assert resp.status_code == 200
    claims = resp.json()["claims"]
    assert len(claims) == 2
    assert {c["decision"] for c in claims} == {"pass", "block"}


def test_admin_claim_detail_matches_verify_response(client_app, monkeypatch):
    _install_fake_client(monkeypatch, CLAIM_JSON, [("get_payment_by_utr", '{"utr": "111111111111"}')])
    v = client_app.post("/verify", json={"text": "paid 2499, UTR 111111111111"}).json()
    claim_id = client_app.get("/admin/claims").json()["claims"][0]["claim_id"]
    detail = client_app.get(f"/admin/claims/{claim_id}").json()
    assert detail["decision"] == v["decision"]
    assert detail["extracted"]["order_id"] == v["extracted"]["order_id"]


def test_admin_ledger_browse_returns_rows(client_app):
    resp = client_app.get("/admin/ledger/captures")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert any(r["capture_id"] == "pay_1" for r in rows)


def test_admin_ledger_browse_rejects_unknown_table(client_app):
    resp = client_app.get("/admin/ledger/not_a_real_table")
    assert resp.status_code == 400


def test_admin_audit_returns_chain_status(client_app, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # audit.jsonl doesn't exist here
    resp = client_app.get("/admin/audit")
    assert resp.status_code == 200
    assert "ok" in resp.json()


def test_admin_approve_also_blocks_replay_via_verify(client_app, monkeypatch):
    # Same loop-closing property as the /review/*-based test above, exercised through
    # /admin/* instead, since /admin/* is meant to become the primary interface.
    _install_fake_client(monkeypatch, json.dumps({
        "claim_type": "payment_not_recorded", "order_id": "ord_1",
        "claimed_reference": "111111111111", "claimed_amount_paise": 249900,
        "claimed_instrument": "upi", "claimed_payee_vpa": None,
        "claimed_timestamp_iso": None, "customer_asserts_count": None,
    }), [])  # no tool calls -> undecidable -> escalates
    escalated = client_app.post("/verify", json={"text": "paid 2499, UTR 111111111111"}).json()
    assert escalated["decision"] == "escalate"

    claim_id = client_app.get("/admin/claims?status=open").json()["claims"][0]["claim_id"]
    resp = client_app.post(f"/admin/claims/{claim_id}/decide", json={"action": "approve", "note": "checked"})
    assert resp.status_code == 200

    _install_fake_client(monkeypatch, CLAIM_JSON, [("get_payment_by_utr", '{"utr": "111111111111"}')])
    replay = client_app.post("/verify", json={"text": "paid 2499, UTR 111111111111 again"}).json()
    assert replay["decision"] == "block"
    assert replay["reason_code"] == "REF_ALREADY_CONSUMED"


def test_llm_unavailable_does_not_crash(client_app, monkeypatch):
    from llm_client import LLMProviderError

    class _RaisingClient:
        @property
        def chat(self):
            class _Chat:
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        raise LLMProviderError("simulated outage")
            return _Chat()

    monkeypatch.setattr(parser_module, "get_client", lambda: _RaisingClient())
    res = client_app.post("/verify", json={"text": "paid 2499, UTR 111111111111"})
    assert res.status_code == 200
    body = res.json()
    assert body["decision"] == "escalate"
    assert body["reason_code"] == "MODEL_UNAVAILABLE"
