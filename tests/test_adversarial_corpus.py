"""Adversarial claim corpus (PRD 10.2, 10.3): proves none of eval/adversarial_corpus.py's
cases ever resolve to "pass" -- one assertion-focused test per case, mirroring
tests/test_policy_invariants.py's style. Zero live LLM calls, zero network: the corpus
runs sanitize() -> parser.parse_claim() (fake client) -> policy.decide() with empty
evidence, exactly like the rest of this repo's offline suite.

Not covered here (already tested elsewhere, see eval/adversarial_corpus.py's module
docstring for why): tool-name spoofing (tests/test_tool_allowlist_is_readonly.py,
eval/faults.py's _llm_bad_tool_name).
"""
import json

import pytest
from pydantic import ValidationError

from eval.adversarial_corpus import CORPUS, _claim_json, run_case, score_corpus
from models import StructuredClaim
from parser import ParseFailedError
from sanitize import sanitize


@pytest.mark.parametrize("case", CORPUS, ids=[c.label for c in CORPUS])
def test_adversarial_case_never_passes(case):
    verdict, state = run_case(case)
    assert verdict.decision != "pass", (
        f"{case.label}: fabricated claim with no ledger evidence resolved to pass "
        f"(reason_code={verdict.reason_code}) -- {case.why}"
    )
    assert verdict.decision in ("block", "escalate")
    # Structural guard, not incidental: no evidence was ever gathered for these cases.
    assert state.evidence == []


@pytest.mark.parametrize("case", CORPUS, ids=[c.label for c in CORPUS])
def test_extracted_json_matches_what_sanitize_actually_produces(case):
    """Without this, a case's `extracted_json` is inert prose: _FakeClient returns it
    unconditionally, so nothing connects `text` (what sanitize()/the homoglyph and
    zero-width stripping actually do) to what the "parser" claims it extracted. This
    pins the disguise cases to sanitize()'s real output -- edit `text` without updating
    `extracted_json` to match and this fails, instead of the corpus silently going stale.
    """
    sanitized, _ = sanitize(case.text)
    claimed_ref = json.loads(case.extracted_json)["claimed_reference"]
    if claimed_ref is not None:
        assert claimed_ref in sanitized, (
            f"{case.label}: extracted_json's claimed_reference {claimed_ref!r} does not "
            f"appear in sanitize()'s actual output {sanitized!r}"
        )


def test_corpus_score_reports_zero_unsafe_passes():
    summary = score_corpus()
    assert summary["n"] == len(CORPUS)
    assert summary["unsafe_pass_count"] == 0
    assert summary["unsafe_pass_labels"] == []
    assert summary["resolved_as_expected_count"] == len(CORPUS), (
        "every case should land on its author's expected block/escalate outcome"
    )


def test_smuggled_decision_field_rejected_by_pydantic():
    """Rule 3 (PRD 5.2): StructuredClaim's extra='forbid' means a compromised/adversarial
    LLM response that tries to smuggle in a "decision": "pass" field fails validation
    outright -- parser.py can never even construct the object, let alone act on it."""
    malicious = _claim_json(order_id="order_4471")
    payload = json.loads(malicious)
    payload["decision"] = "pass"  # not a real StructuredClaim field -- extra="forbid"
    with pytest.raises(ValidationError) as exc_info:
        StructuredClaim.model_validate_json(json.dumps(payload))
    # Prove it's specifically the smuggled extra field being rejected (extra="forbid"),
    # not some unrelated validation failure that would false-green this test.
    assert "decision" in str(exc_info.value)


def test_parse_claim_also_rejects_smuggled_decision_field_via_retry_exhaustion():
    """End-to-end: parser.parse_claim() retries on validation failure (MAX_PARSE_ATTEMPTS
    times) and raises ParseFailedError rather than ever returning an object with a
    decision baked in -- it cannot silently fall back to trusting the smuggled field."""
    from eval.adversarial_corpus import _FakeClient

    malicious = json.loads(_claim_json(order_id="order_4471"))
    malicious["decision"] = "pass"
    client = _FakeClient(json.dumps(malicious))
    with pytest.raises(ParseFailedError):
        from parser import parse_claim
        parse_claim("some adversarial text", client=client)


if __name__ == "__main__":
    import sys

    import pytest as _pytest

    sys.exit(_pytest.main([__file__, "-v"]))
