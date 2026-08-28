"""Hash-chain integrity (PRD 12) -- builds a small chain, verifies it, tampers one
event, and confirms verify() detects exactly where the break is.
"""
from pathlib import Path

import audit
from audit.verify import verify
from eval.tamper import tamper


def test_chain_verifies_clean(tmp_path):
    p = str(tmp_path / "a.jsonl")
    audit.record_event("claim_received", "clm_1", {"amount_paise": 100}, path=p)
    audit.record_event("verdict", "clm_1", {"decision": "pass"}, path=p)
    audit.record_event("claim_received", "clm_2", {"amount_paise": 200}, path=p)
    ok, break_at, detail = verify(p)
    assert ok is True
    assert break_at is None


def test_tamper_is_detected_at_the_right_event(tmp_path):
    p = str(tmp_path / "a.jsonl")
    audit.record_event("claim_received", "clm_1", {"amount_paise": 100}, path=p)
    audit.record_event("verdict", "clm_1", {"decision": "pass"}, path=p)
    audit.record_event("claim_received", "clm_2", {"amount_paise": 200}, path=p)

    tamper(p, event_number=2, field="decision", value="block")

    ok, break_at, detail = verify(p)
    assert ok is False
    assert break_at == 2


def test_first_event_prev_hash_is_genesis(tmp_path):
    p = str(tmp_path / "a.jsonl")
    audit.record_event("claim_received", "clm_1", {}, path=p)
    first = Path(p).read_text().splitlines()[0]
    import json
    assert json.loads(first)["prev_hash"] == audit.GENESIS


if __name__ == "__main__":
    import sys
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-q"]))
