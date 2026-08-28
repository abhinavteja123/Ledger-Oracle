"""Three-run ablation (PRD 18.6) -- isolates what the model contributes.

  Run A: engine only, fed ground-truth structured fields. The ceiling. (eval/score.py)
  Run B: real free text -> parser (LLM) -> fixed tool order -> engine.
  Run C: real free text -> parser (LLM) -> LLM tool selection (agent.py) -> engine.

No model appears in the decision path in any of the three -- the difference between
runs is entirely the quality of evidence handed to the same deterministic policy engine.

Usage: python -m eval.ablation --data data/
"""
import argparse
import json
from pathlib import Path

import time

from cerebras.cloud.sdk import CerebrasError

import agent
import parser as claim_parser
from eval.score import _fmt, gather_evidence, load_consumed_references, load_manifest, print_report, score, summarize
from llm_client import get_client
from models import InvestigationState
from policy import decide

PACE_SECONDS = 0.5      # free-tier rate limits, not cost, are the constraint here (PRD 14.5)
PACE_SECONDS_C = 2.0    # run C makes up to 6 LLM calls per claim (1 parse + up to 5 tool
                         # selections), vs run B's 1 -- burned through the free-tier
                         # rate limit much faster in a live run, cascading into
                         # MODEL_UNAVAILABLE for the rest of the batch. See FAILURES.md.


def load_claims_text(data_dir: Path) -> dict:
    """claim_id -> raw free text, from claims.jsonl."""
    path = data_dir / "claims.jsonl"
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            out[row["claim_id"]] = row["text"]
    return out


def run_b(data_dir: Path, client=None) -> dict:
    """Engine + parser, fixed tool order (record['resolving_tools'])."""
    manifest = load_manifest(data_dir)
    texts = load_claims_text(data_dir)
    consumed_refs = load_consumed_references(data_dir)
    db_path = str(data_dir / "ledger.db")

    verdicts = {}
    for record in manifest:
        text = texts[record["claim_id"]]
        time.sleep(PACE_SECONDS)
        try:
            claim = claim_parser.parse_claim(text, client=client)
        except (claim_parser.ParseFailedError, CerebrasError):
            # A parse failure OR a transient LLM-boundary failure (rate limit, timeout)
            # both resolve the same way here: no structured claim, escalate on missing
            # data -- one claim's failure must never crash the whole batch (PRD 9.3).
            verdicts[record["claim_id"]] = decide(
                InvestigationState(claim_id=record["claim_id"], raw_message=text,
                                    extracted=None, started_at="2026-01-01T00:00:00Z"),
                consumed_references=consumed_refs,
            )
            continue
        evidence = gather_evidence(record, claim, db_path)
        state = InvestigationState(
            claim_id=record["claim_id"], raw_message=text, extracted=claim,
            evidence=evidence, started_at="2026-01-01T00:00:00Z",
        )
        verdicts[record["claim_id"]] = decide(state, consumed_references=consumed_refs)

    return summarize(manifest, verdicts)


def run_c(data_dir: Path, client=None) -> dict:
    """Full agent: parser + LLM-driven tool selection (agent.investigate)."""
    manifest = load_manifest(data_dir)
    texts = load_claims_text(data_dir)
    consumed_refs = load_consumed_references(data_dir)
    db_path = str(data_dir / "ledger.db")

    verdicts = {}
    for record in manifest:
        text = texts[record["claim_id"]]
        time.sleep(PACE_SECONDS_C)
        try:
            claim = claim_parser.parse_claim(text, client=client)
        except (claim_parser.ParseFailedError, CerebrasError):
            claim = None
        _, verdict = agent.investigate(
            record["claim_id"], text, extracted=claim, client=client,
            consumed_references=consumed_refs, db_path=db_path,
        )
        verdicts[record["claim_id"]] = verdict

    return summarize(manifest, verdicts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/")
    args = ap.parse_args()
    data_dir = Path(args.data)

    result_a = score(data_dir)
    print_report(result_a, label="RUN A -- engine only (ceiling)")
    print()

    try:
        client = get_client()
    except Exception as e:
        raise SystemExit(
            f"Runs B and C need a live Cerebras client (CEREBRAS_API_KEY not usable: {e}). "
            "Run A above needs no LLM and is already a complete result."
        )

    result_b = run_b(data_dir, client=client)
    print_report(result_b, label="RUN B -- engine + parser, fixed tool order")
    print()

    result_c = run_c(data_dir, client=client)
    print_report(result_c, label="RUN C -- full agent (LLM tool selection)")
    print()

    print("=== Comparison ===")
    print(f"A precision/recall (block): {_fmt(result_a['precision_block'])} / {_fmt(result_a['recall_block'])}")
    print(f"B precision/recall (block): {_fmt(result_b['precision_block'])} / {_fmt(result_b['recall_block'])}")
    print(f"C precision/recall (block): {_fmt(result_c['precision_block'])} / {_fmt(result_c['recall_block'])}")
    print("A is the ceiling: what the arithmetic gets right when handed perfect inputs.")
    print("B shows what the parser costs relative to A.")
    if result_c["total_fp_cost_paise"] >= result_b["total_fp_cost_paise"]:
        print("C is no better than B: fixed tool order is sufficient here, and LLM-driven "
              "tool selection is not earning its complexity on this dataset.")
    else:
        print("C improves on B: LLM-driven tool selection adds real value beyond a fixed order.")
    print("No model appears in any of the three decision paths -- the difference is "
          "entirely the quality of evidence handed to the same deterministic engine.")


if __name__ == "__main__":
    main()
