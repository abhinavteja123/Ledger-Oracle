"""Seeds a real, representative audit.jsonl from the zero-LLM ablation-A run, so
`make audit` (and the tamper demo, PRD 12.4) has a real chain to verify on a fresh
clone -- without needing a live CEREBRAS_API_KEY. A live deployment gets its audit
trail from real /verify calls through app.py instead; this is for the demo/eval path.

Usage: python -m eval.seed_audit --data data/ --path audit.jsonl
"""
import argparse
from pathlib import Path

from audit import record_event
from eval.score import evaluate, load_consumed_references, load_manifest


def seed(data_dir: Path, audit_path: str) -> int:
    manifest = load_manifest(data_dir)
    consumed = load_consumed_references(data_dir)
    db_path = str(data_dir / "ledger.db")
    n = 0
    for record in manifest:
        claim_id = record["claim_id"]
        record_event("claim_received", claim_id, {"class": record["class"]}, path=audit_path)
        n += 1
        verdict = evaluate(record, db_path, consumed)
        record_event("verdict", claim_id, {
            "decision": verdict.decision,
            "reason_code": verdict.reason_code,
            "max_refundable_paise": verdict.max_refundable_paise,
        }, path=audit_path)
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/")
    ap.add_argument("--path", default="audit.jsonl")
    args = ap.parse_args()
    n = seed(Path(args.data), args.path)
    print(f"Seeded {n} audit events to {args.path} from {args.data}")


if __name__ == "__main__":
    main()
