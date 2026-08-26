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
from models import InvestigationState, StructuredClaim
from policy import decide

SUPPORT_COST_PAISE = 25_000        # ~Rs 250, ~15 support minutes (PRD 18.4)
GOODWILL_COST_PAISE = 50_000       # Rs 500 -- accusation costs more than a refusal

TOOL_FUNCS = {
    "get_payment_by_utr": lambda claim, db: tools.get_payment_by_utr(
        claim.claimed_reference or "", db_path=db
    ),
    "get_order_payments": lambda claim, db: tools.get_order_payments(
        claim.order_id or "", db_path=db
    ),
    "find_duplicate_captures": lambda claim, db: tools.find_duplicate_captures(
        claim.order_id or "", db_path=db
    ),
    "check_refund_history": lambda claim, db: tools.check_refund_history(
        claim.order_id or "", db_path=db
    ),
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
        evidence.append(fn(claim, db_path))
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


def score(data_dir: Path) -> dict:
    manifest = load_manifest(data_dir)
    consumed_refs = load_consumed_references(data_dir)
    db_path = str(data_dir / "ledger.db")

    matrix = {a: {"pass": 0, "block": 0, "escalate": 0} for a in ("pass", "block", "escalate")}
    total_cost_paise = 0
    blocked_fraud_paise = 0
    rows = []

    for record in manifest:
        actual = record["ground_truth"]
        verdict = evaluate(record, db_path, consumed_refs)
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


def _rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.0f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/")
    args = ap.parse_args()

    result = score(Path(args.data))
    m = result["matrix"]
    print("                 PREDICTED")
    print(f"{'':>10}  {'pass':>8} {'block':>8} {'escalate':>9}")
    for actual in ("pass", "block", "escalate"):
        row = m[actual]
        print(f"{actual:>10}  {row['pass']:>8} {row['block']:>8} {row['escalate']:>9}")
    print()
    print(f"PRECISION (block) = {result['precision_block']:.3f}")
    print(f"RECALL    (block) = {result['recall_block']:.3f}")
    print(f"escalation_precision = {result['escalation_precision']:.3f}")
    print(f"escalation_recall    = {result['escalation_recall']:.3f}")
    print(f"false_accusation_rate (FP-C / {result['n_escalate_actual']}) = "
          f"{result['false_accusation_rate']:.3f}  ({result['false_accusation_count']} honest customers wrongly blocked)")
    print()
    print(f"Blocked {_rupees(result['blocked_fraud_paise'])} of fraudulent refund requests")
    print(f"Cost    {_rupees(result['total_fp_cost_paise'])} in false-positive harm")
    print(f"Net     {_rupees(result['blocked_fraud_paise'] - result['total_fp_cost_paise'])}")


if __name__ == "__main__":
    main()
