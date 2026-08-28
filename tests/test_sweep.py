"""Confirms the FUZZ_DISTANCE override in eval/sweep.py actually changes
tools.get_payment_by_utr's near-match behavior between runs -- verified for real,
not assumed (tools.py has its own module-level FUZZ_DISTANCE, separate from
config.FUZZ_DISTANCE, read live at call time -- direct attribute patching works).
"""
import sqlite3

import tools
from eval.sweep import run_sweep


def _fixture_data_dir(tmp_path):
    import json
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
    # real UTR 111111111112, claim will reference a 1-digit-off typo of it
    con.execute(
        "INSERT INTO captures VALUES ('pay_1','ord_1',249900,'upi','111111111112',NULL,"
        "'2026-01-01T10:00:00Z','2026-01-01T10:05:00Z','connected')"
    )
    con.commit()
    con.close()

    manifest = [{
        "claim_id": "s1", "ground_truth": "escalate", "class": "HONEST_UNVERIFIABLE",
        "defect_class": "typo_utr", "fp_cost_paise": 249900,
        "resolving_tools": ["get_payment_by_utr"], "sufficient_tools": ["get_payment_by_utr"],
        "expected_reason_code": "REF_NEAR_MATCH_TYPO", "injected_fault": None, "tone_pair_id": None,
        "ground_truth_fields": {
            "claim_type": "payment_not_recorded", "order_id": "ord_1",
            "claimed_reference": "111111111111",  # one digit off from the real capture
            "claimed_amount_paise": 249900, "claimed_instrument": "upi", "risk_flags": [],
        },
    }]
    (d / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    return d


def test_fuzz_distance_override_changes_near_match_behavior(tmp_path):
    d = _fixture_data_dir(tmp_path)
    original = tools.FUZZ_DISTANCE
    try:
        results = run_sweep(d, [0, 1])
        # at distance 0 the typo isn't a near-match -> resolves as REF_NOT_IN_LEDGER (block)
        assert results[0]["matrix"]["escalate"]["block"] == 1
        # at distance 1 it IS a near-match -> REF_NEAR_MATCH_TYPO (escalate)
        assert results[1]["matrix"]["escalate"]["escalate"] == 1
    finally:
        assert tools.FUZZ_DISTANCE == original  # run_sweep restores it


def test_run_sweep_restores_fuzz_distance_even_on_error(tmp_path):
    d = _fixture_data_dir(tmp_path)
    original = tools.FUZZ_DISTANCE
    try:
        run_sweep(d / "nonexistent", [0])
    except Exception:
        pass
    assert tools.FUZZ_DISTANCE == original
