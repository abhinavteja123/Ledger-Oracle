"""3x3 confusion matrix + rupee cost (PRD 18). Ablation run A: engine only, fed the
generator's ground-truth structured fields directly -- zero LLM, zero parser, the
ceiling of what the arithmetic gets right when handed perfect inputs (PRD 18.6).

Usage: python -m eval.score --data data/
"""
import argparse
import json
import sqlite3
from pathlib import Path

import tools
from models import InvestigationState, StructuredClaim, ToolError
from policy import decide

SUPPORT_COST_PAISE = 25_000        # ~Rs 250, ~15 support minutes (PRD 18.4)
GOODWILL_COST_PAISE = 50_000       # Rs 500 -- accusation costs more than a refusal

def _dispatch(name, arg):
    # Route through TOOL_REGISTRY, not the module-level function directly -- this is
    # the same dispatch path agent.py uses, and it's the only thing eval/faults.py's
    # inject_tool_failure actually patches. Calling tools.<fn> directly here silently
    # bypassed every fault injection (found by score_with_fault() reporting a suspicious
    # 0 unsafe_failures with the fault supposedly "always on" -- it never fired).
    def call(claim, db):
        return tools.TOOL_REGISTRY[name]["fn"](arg(claim) or "", db_path=db)
    return call


TOOL_FUNCS = {
    "get_payment_by_utr": _dispatch("get_payment_by_utr", lambda c: c.claimed_reference),
    "get_order_payments": _dispatch("get_order_payments", lambda c: c.order_id),
    "find_duplicate_captures": _dispatch("find_duplicate_captures", lambda c: c.order_id),
    "check_refund_history": _dispatch("check_refund_history", lambda c: c.order_id),
}


def load_manifest(data_dir: Path) -> list[dict]:
    return json.loads((data_dir / "MANIFEST.json").read_text(encoding="utf-8"))


def load_consumed_references(data_dir: Path) -> frozenset:
    con = sqlite3.connect(f"file:{data_dir / 'ledger.db'}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT reference FROM consumed_references").fetchall()
    finally:
        con.close()
    return frozenset(r[0] for r in rows)


def build_claim(gt: dict) -> StructuredClaim:
    return StructuredClaim(
        claim_type=gt["claim_type"],
        order_id=gt["order_id"],
        claimed_reference=gt["claimed_reference"],
        claimed_amount_paise=gt["claimed_amount_paise"],
        claimed_instrument=gt["claimed_instrument"],
    )


def gather_evidence(record: dict, claim: StructuredClaim, db_path: str) -> list:
    evidence = []
    for name in record["resolving_tools"]:
        fn = TOOL_FUNCS.get(name)
        if fn is None:
            continue  # check_payment_status not exercised by resolving_tools in this dataset
        result = fn(claim, db_path)
        if not hasattr(result, "model_dump"):
            # A tool (or an injected fault, see eval/faults.py's malformed_row) returned
            # something that isn't a typed Result/ToolError -- e.g. a plain dict. Do NOT
            # let it into evidence: InvestigationState's Pydantic validation on
            # construction will silently coerce a loosely-shaped dict into a plausible
            # but WRONG empty Result (matches=[], etc, since every field has a default)
            # instead of raising -- this is agent.py's same defensive check, applied
            # here for the same reason (found via score_with_fault reporting a lower
            # graceful_recovery_rate than expected for malformed_row; see FAILURES.md).
            result = ToolError(tool=name, error_class="invalid_args", attempt=1,
                                detail="tool returned an unexpected (non-Pydantic) result shape")
        evidence.append(result)
    return evidence


def evaluate(record: dict, db_path: str, consumed_refs: frozenset):
    gt = record["ground_truth_fields"]
    claim = build_claim(gt)
    evidence = gather_evidence(record, claim, db_path)
    state = InvestigationState(
        claim_id=record["claim_id"],
        raw_message="",
        extracted=claim,
        evidence=evidence,
        risk_flags=gt.get("risk_flags", []),
        started_at="2026-01-01T00:00:00Z",
    )
    return decide(state, consumed_references=consumed_refs)


def cost_paise(actual: str, predicted: str, fp_cost_paise: int) -> int:
    """PRD 18.4's cost model, keyed by (ground-truth row, predicted column)."""
    if actual == "pass":
        if predicted == "block":       # FP-A: true claim wrongly blocked
            return fp_cost_paise + SUPPORT_COST_PAISE
        if predicted == "escalate":    # FP-B: true claim delayed
            return SUPPORT_COST_PAISE
        return 0                        # TP
    if actual == "block":
        if predicted == "pass":        # FN-A: fraud paid out
            return fp_cost_paise
        if predicted == "escalate":    # FN-B: caught by a human, not the tool
            return SUPPORT_COST_PAISE
        return 0                        # TP
    # actual == "escalate"
    if predicted == "block":           # FP-C: honest customer accused. Worst cell.
        return fp_cost_paise + SUPPORT_COST_PAISE + GOODWILL_COST_PAISE
    if predicted == "pass":            # FN-C: paid out on something unresolvable
        return fp_cost_paise // 2
    return 0                            # TP


def summarize(manifest: list[dict], verdicts: dict) -> dict:
    """Matrix + cost + precision/recall math (PRD 18.2-18.4), independent of how each
    verdict was produced. `verdicts` maps claim_id -> Verdict. Shared by ablation runs
    A (this file), B, and C (eval/ablation.py) so the scoring logic exists exactly once.
    """
    matrix = {a: {"pass": 0, "block": 0, "escalate": 0} for a in ("pass", "block", "escalate")}
    total_cost_paise = 0
    blocked_fraud_paise = 0
    rows = []

    for record in manifest:
        actual = record["ground_truth"]
        verdict = verdicts[record["claim_id"]]
        predicted = verdict.decision
        matrix[actual][predicted] += 1
        c = cost_paise(actual, predicted, record["fp_cost_paise"])
        total_cost_paise += c
        if actual == "block" and predicted == "block":
            blocked_fraud_paise += record["fp_cost_paise"]
        rows.append({
            "claim_id": record["claim_id"], "class": record["class"],
            "defect_class": record["defect_class"], "actual": actual, "predicted": predicted,
            "reason_code": verdict.reason_code, "expected_reason_code": record["expected_reason_code"],
            "cost_paise": c,
        })

    tp_block = matrix["block"]["block"]
    fp_a = matrix["pass"]["block"]
    fp_c = matrix["escalate"]["block"]
    tp_escalate = matrix["escalate"]["escalate"]
    total_escalate_predicted = sum(matrix[a]["escalate"] for a in matrix)
    n_block = sum(matrix["block"].values())
    n_escalate = sum(matrix["escalate"].values())

    precision_block = tp_block / (tp_block + fp_a + fp_c) if (tp_block + fp_a + fp_c) else None
    recall_block = tp_block / n_block if n_block else None
    escalation_precision = tp_escalate / total_escalate_predicted if total_escalate_predicted else None
    escalation_recall = tp_escalate / n_escalate if n_escalate else None
    false_accusation_rate = fp_c / n_escalate if n_escalate else None

    return {
        "matrix": matrix,
        "n_claims": len(manifest),
        "precision_block": precision_block,
        "recall_block": recall_block,
        "escalation_precision": escalation_precision,
        "escalation_recall": escalation_recall,
        "false_accusation_rate": false_accusation_rate,
        "false_accusation_count": fp_c,
        "n_escalate_actual": n_escalate,
        "blocked_fraud_paise": blocked_fraud_paise,
        "total_fp_cost_paise": total_cost_paise,
        "rows": rows,
    }


def score(data_dir: Path) -> dict:
    """Ablation run A (PRD 18.6): engine only, fed the generator's ground-truth
    structured fields directly -- zero LLM, zero parser."""
    manifest = load_manifest(data_dir)
    consumed_refs = load_consumed_references(data_dir)
    db_path = str(data_dir / "ledger.db")
    verdicts = {r["claim_id"]: evaluate(r, db_path, consumed_refs) for r in manifest}
    return summarize(manifest, verdicts)


def _rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.0f}"


def _fmt(x) -> str:
    # A zero-division guard upstream (summarize()) returns None for an undefined
    # rate (e.g. no claims were predicted "block" at all) -- print "n/a", don't crash.
    # Found live: run C's print crashed mid-report when a rate-limited run degraded
    # to 100% escalate, and TypeError killed the process before A/B's own numbers
    # (already printed) even got saved anywhere. See FAILURES.md.
    return f"{x:.3f}" if x is not None else "n/a"


def print_report(result: dict, label: str = "") -> None:
    if label:
        print(f"=== {label} ===")
    m = result["matrix"]
    print("                 PREDICTED")
    print(f"{'':>10}  {'pass':>8} {'block':>8} {'escalate':>9}")
    for actual in ("pass", "block", "escalate"):
        row = m[actual]
        print(f"{actual:>10}  {row['pass']:>8} {row['block']:>8} {row['escalate']:>9}")
    print()
    print(f"PRECISION (block) = {_fmt(result['precision_block'])}")
    print(f"RECALL    (block) = {_fmt(result['recall_block'])}")
    print(f"escalation_precision = {_fmt(result['escalation_precision'])}")
    print(f"escalation_recall    = {_fmt(result['escalation_recall'])}")
    print(f"false_accusation_rate (FP-C / {result['n_escalate_actual']}) = "
          f"{_fmt(result['false_accusation_rate'])}  ({result['false_accusation_count']} honest customers wrongly blocked)")
    print()
    print(f"Blocked {_rupees(result['blocked_fraud_paise'])} of fraudulent refund requests")
    print(f"Cost    {_rupees(result['total_fp_cost_paise'])} in false-positive harm")
    print(f"Net     {_rupees(result['blocked_fraud_paise'] - result['total_fp_cost_paise'])}")


def score_with_fault(data_dir: Path, mode: str, tool_name: str | None = None) -> dict:
    """PRD 9.5's recovery metric, restricted to tool-boundary faults (db_timeout,
    db_unavailable, malformed_row, contradictory) -- the ones this engine-only,
    no-retry ablation-A path can meaningfully exercise. LLM-boundary faults
    (llm_unavailable, llm_invalid_json, llm_bad_tool_name, llm_bad_tool_args) need a
    real agent run (eval/ablation.py's run C) with a live CEREBRAS_API_KEY.

    The fault fires on every call to `tool_name` (or every tool, if None) for the whole
    run -- there's no retry loop here to test "one-shot glitch, retry recovers" (that's
    agent.py's job, tested in tests/test_recovery_paths.py instead). What this proves is
    the decidability gate: when evidence a claim needs is unavailable, does the engine
    correctly degrade to escalate/block instead of ever wrongly passing.
    """
    from eval.faults import inject_tool_failure

    with inject_tool_failure(mode, tool_name=tool_name, always=True):
        result = score(data_dir)

    unsafe = [r for r in result["rows"] if r["predicted"] == "pass" and r["actual"] != "pass"]
    correct_terminal = [
        r for r in result["rows"]
        if r["predicted"] == r["actual"] or r["predicted"] == "escalate"
    ]
    result["graceful_recovery_rate"] = len(correct_terminal) / len(result["rows"])
    result["unsafe_failures"] = len(unsafe)
    result["unsafe_failure_claim_ids"] = [r["claim_id"] for r in unsafe]
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/")
    ap.add_argument("--inject-failure", default=None,
                     help="tool-boundary fault mode: db_timeout, db_unavailable, malformed_row, contradictory")
    ap.add_argument("--tool", default=None, help="restrict the fault to one tool (default: all)")
    args = ap.parse_args()

    if args.inject_failure:
        result = score_with_fault(Path(args.data), args.inject_failure, args.tool)
        print_report(result, label=f"FAULT: {args.inject_failure}" + (f" on {args.tool}" if args.tool else ""))
        print()
        print(f"graceful_recovery_rate = {result['graceful_recovery_rate']:.3f}")
        print(f"unsafe_failures = {result['unsafe_failures']}   TARGET: 0")
        if result["unsafe_failures"]:
            print(f"  claim_ids: {result['unsafe_failure_claim_ids']}")
    else:
        print_report(score(Path(args.data)))


if __name__ == "__main__":
    main()
