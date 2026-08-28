"""Tolerance sweep (PRD 18.5): run ablation A at FUZZ_DISTANCE in {0,1,2}, show the
false_accusation_rate / recall_block tradeoff. No LLM needed -- reuses eval/score.py.

Usage: python -m eval.sweep --data data/ --fuzz 0,1,2
"""
import argparse
from pathlib import Path

import tools
from eval.score import score


def run_sweep(data_dir: Path, distances: list[int]) -> dict:
    """Returns {distance: score_result}. Patches tools.FUZZ_DISTANCE directly (that's
    the module-level constant get_payment_by_utr actually reads at call time -- verified
    it isn't captured in a closure at import, a plain attribute set/restore works)."""
    original = tools.FUZZ_DISTANCE
    results = {}
    try:
        for d in distances:
            tools.FUZZ_DISTANCE = d
            results[d] = score(data_dir)
    finally:
        tools.FUZZ_DISTANCE = original
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/")
    ap.add_argument("--fuzz", default="0,1,2")
    args = ap.parse_args()
    distances = [int(x) for x in args.fuzz.split(",")]

    results = run_sweep(Path(args.data), distances)

    print(f"{'FUZZ_DISTANCE':>14}  {'recall_block':>13}  {'false_accusation_rate':>22}  {'FP-C count':>10}")
    for d in distances:
        r = results[d]
        print(f"{d:>14}  {r['recall_block']:>13.3f}  {r['false_accusation_rate']:>22.3f}  "
              f"{r['false_accusation_count']:>10}")
    print()

    if len(distances) >= 2:
        lo, hi = distances[0], distances[-1]
        rlo, rhi = results[lo], results[hi]
        fp_c_delta = rlo["false_accusation_count"] - rhi["false_accusation_count"]
        recall_delta = rlo["recall_block"] - rhi["recall_block"]
        n_block = sum(rlo["matrix"]["block"].values())
        missed_fraud_count = round(recall_delta * n_block)
        print(f"Raising fuzzy-match distance from {lo} to {hi} converts {fp_c_delta} wrong "
              f"block(s) into escalations at the cost of {missed_fraud_count} missed fraud "
              f"case(s) (recall_block {rlo['recall_block']:.3f} -> {rhi['recall_block']:.3f}).")


if __name__ == "__main__":
    main()
