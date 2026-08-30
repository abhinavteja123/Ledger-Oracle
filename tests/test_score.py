"""eval/score.py's summarize() + print_report(): confirms the unsafe_pass_count /
unsafe_pass_claim_ids headline safety metric (actual is block/escalate, predicted is
pass, on a PLAIN run -- no fault injection) is computed correctly and printed first,
before the accuracy matrix.

summarize(manifest, verdicts) needs no DB or LLM -- it's pure arithmetic over a
manifest list and a claim_id -> Verdict map, so the fixtures here are plain dicts and
models.Verdict instances, not a full sqlite ledger (contrast tests/test_sweep.py's
fixture, which needs a real DB because it drives tools.py through the FUZZ_DISTANCE
override; summarize() never touches the DB at all).
"""
from models import Verdict
from eval.score import print_report, summarize


def _record(claim_id, ground_truth, fp_cost_paise=100_00):
    return {
        "claim_id": claim_id, "ground_truth": ground_truth, "class": "TEST",
        "defect_class": "none", "fp_cost_paise": fp_cost_paise,
        "expected_reason_code": "OK",
    }


def _verdict(decision, reason_code="OK"):
    return Verdict(decision=decision, max_refundable_paise=0, reason_code=reason_code)


def test_summarize_reports_zero_unsafe_passes_when_none_occurred():
    manifest = [_record("c1", "pass"), _record("c2", "block"), _record("c3", "escalate")]
    verdicts = {"c1": _verdict("pass"), "c2": _verdict("block"), "c3": _verdict("escalate")}
    result = summarize(manifest, verdicts)
    assert result["unsafe_pass_count"] == 0
    assert result["unsafe_pass_claim_ids"] == []


def test_summarize_counts_unsafe_passes():
    # c2 and c3 are genuinely block/escalate but the engine predicted pass -- exactly
    # the unsafe-pass vector this metric exists to catch.
    manifest = [_record("c1", "pass"), _record("c2", "block"), _record("c3", "escalate")]
    verdicts = {"c1": _verdict("pass"), "c2": _verdict("pass"), "c3": _verdict("pass")}
    result = summarize(manifest, verdicts)
    assert result["unsafe_pass_count"] == 2
    assert sorted(result["unsafe_pass_claim_ids"]) == ["c2", "c3"]


def test_print_report_prints_unsafe_pass_count_before_matrix(capsys):
    manifest = [_record("c1", "block")]
    verdicts = {"c1": _verdict("pass")}
    result = summarize(manifest, verdicts)
    print_report(result)
    out = capsys.readouterr().out
    assert "unsafe_pass_count = 1" in out
    assert "c1" in out
    assert out.index("unsafe_pass_count") < out.index("PREDICTED"), (
        "the core safety claim must print before the accuracy matrix"
    )
