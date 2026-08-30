"""Engine-vs-human agreement (eval/agreement.py). Mirrors test_audit_chain.py's
pattern: build a small SYNTHETIC audit.jsonl via audit.record_event() in tmp_path,
with known, deliberately-chosen outcomes, then assert compute_agreement() matches
what was built by hand.
"""
import audit
from eval.agreement import compute_agreement


def test_all_agree(tmp_path):
    # Two escalates, both rejected by a human -> engine correctly cautious both times.
    p = str(tmp_path / "a.jsonl")
    audit.record_event("verdict", "clm_1", {"decision": "escalate", "reason_code": "REF_NOT_IN_LEDGER"}, path=p)
    audit.record_event("admin_decision", "clm_1", {"action": "reject", "note": ""}, path=p)
    audit.record_event("verdict", "clm_2", {"decision": "escalate", "reason_code": "REF_NOT_IN_LEDGER"}, path=p)
    audit.record_event("reviewer_decision", "clm_2", {"action": "reject", "note": ""}, path=p)

    result = compute_agreement(p)
    assert result["agreement_rate"] == 1.0
    assert result["agree_count"] == 2
    assert result["disagree_count"] == 0
    assert result["reviewed_count"] == 2
    assert result["by_reason_code"] == [
        {"reason_code": "REF_NOT_IN_LEDGER", "agree": 2, "disagree": 0, "inconclusive": 0}
    ]


def test_all_disagree(tmp_path):
    # Two escalates, both approved by a human -> engine over-escalated both times.
    p = str(tmp_path / "a.jsonl")
    audit.record_event("verdict", "clm_1", {"decision": "escalate", "reason_code": "REF_NEAR_MATCH_TYPO"}, path=p)
    audit.record_event("admin_decision", "clm_1", {"action": "approve", "note": ""}, path=p)
    audit.record_event("verdict", "clm_2", {"decision": "escalate", "reason_code": "REF_NEAR_MATCH_TYPO"}, path=p)
    audit.record_event("admin_decision", "clm_2", {"action": "approve", "note": ""}, path=p)

    result = compute_agreement(p)
    assert result["agreement_rate"] == 0.0
    assert result["agree_count"] == 0
    assert result["disagree_count"] == 2
    assert result["reviewed_count"] == 2
    assert result["by_reason_code"] == [
        {"reason_code": "REF_NEAR_MATCH_TYPO", "agree": 0, "disagree": 2, "inconclusive": 0}
    ]


def test_mixed(tmp_path):
    # One approve (disagree), one reject (agree), one request_info (inconclusive,
    # counted in neither), plus a pass/block claim with no human decision at all --
    # confirms non-escalate claims are excluded even when a verdict exists.
    p = str(tmp_path / "a.jsonl")
    audit.record_event("verdict", "clm_1", {"decision": "escalate", "reason_code": "REF_NEAR_MATCH_TYPO"}, path=p)
    audit.record_event("admin_decision", "clm_1", {"action": "approve", "note": ""}, path=p)
    audit.record_event("verdict", "clm_2", {"decision": "escalate", "reason_code": "REF_NOT_IN_LEDGER"}, path=p)
    audit.record_event("admin_decision", "clm_2", {"action": "reject", "note": ""}, path=p)
    audit.record_event("verdict", "clm_3", {"decision": "escalate", "reason_code": "REF_NOT_IN_LEDGER"}, path=p)
    audit.record_event("reviewer_decision", "clm_3", {"action": "request_info", "note": ""}, path=p)
    audit.record_event("verdict", "clm_4", {"decision": "pass", "reason_code": None,
                                             "max_refundable_paise": 100}, path=p)

    result = compute_agreement(p)
    assert result["agree_count"] == 1
    assert result["disagree_count"] == 1
    assert result["inconclusive_count"] == 1
    assert result["reviewed_count"] == 2
    assert result["agreement_rate"] == 0.5
    by_reason = {row["reason_code"]: row for row in result["by_reason_code"]}
    assert by_reason["REF_NEAR_MATCH_TYPO"] == {"reason_code": "REF_NEAR_MATCH_TYPO", "agree": 0, "disagree": 1, "inconclusive": 0}
    assert by_reason["REF_NOT_IN_LEDGER"] == {"reason_code": "REF_NOT_IN_LEDGER", "agree": 1, "disagree": 0, "inconclusive": 1}
    # sorted by disagree count descending -> REF_NEAR_MATCH_TYPO (1 disagree) before REF_NOT_IN_LEDGER (0 disagree)
    assert [r["reason_code"] for r in result["by_reason_code"]] == ["REF_NEAR_MATCH_TYPO", "REF_NOT_IN_LEDGER"]


def test_no_reviewed_claims(tmp_path):
    # Only pass/block verdicts, and an escalate nobody has reviewed yet -> must not
    # crash, must return None (zero-division guard) with empty breakdown.
    p = str(tmp_path / "a.jsonl")
    audit.record_event("verdict", "clm_1", {"decision": "pass", "reason_code": None}, path=p)
    audit.record_event("verdict", "clm_2", {"decision": "block", "reason_code": "REF_ALREADY_CONSUMED"}, path=p)
    audit.record_event("verdict", "clm_3", {"decision": "escalate", "reason_code": "REF_AMBIGUOUS"}, path=p)

    result = compute_agreement(p)
    assert result["agreement_rate"] is None
    assert result["agree_count"] == 0
    assert result["disagree_count"] == 0
    assert result["reviewed_count"] == 0
    assert result["by_reason_code"] == []


if __name__ == "__main__":
    import sys
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-q"]))
