"""Tone invariance (PRD 17.6, 18.3): a claim written calmly and the same claim written
with urgency/pleading must get the identical verdict -- the policy engine never sees
prose, only evidence.

This checks the generator's own manifest invariant (every tone_pair_id links two claims
with identical ground_truth and identical ground_truth_fields) and runs both through the
engine-only path (ablation run A, no LLM) to confirm the *engine* is provably indifferent
to which claim_id it's given -- tone never reaches it. It does NOT prove the *parser* is
tone-invariant on raw prose (that needs a live LLM call against real free text, no
GROQ_API_KEY/GEMINI_API_KEY is available in this environment -- run this for real once a key exists,
per PRD 17.7/20 step 14's held-out run).
"""
import json
from pathlib import Path

import pytest

from eval.score import evaluate, load_consumed_references


def _manifest_and_claims(data_dir: Path):
    manifest = json.loads((data_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    claims = {c["claim_id"]: c["text"] for c in
              (json.loads(l) for l in (data_dir / "claims.jsonl").read_text(encoding="utf-8").splitlines())}
    return manifest, claims


def _tone_pairs(manifest):
    by_id = {r["claim_id"]: r for r in manifest}
    pairs = []
    for r in manifest:
        if r.get("tone_pair_id"):
            base = by_id.get(r["tone_pair_id"])
            if base:
                pairs.append((base, r))
    return pairs


@pytest.mark.skipif(not Path("data/MANIFEST.json").exists(), reason="run `make gen` first")
def test_tone_pairs_share_ground_truth_and_fields():
    data_dir = Path("data")
    manifest, _ = _manifest_and_claims(data_dir)
    pairs = _tone_pairs(manifest)
    assert len(pairs) > 0, "generator produced no tone pairs -- PRD 17.6 wants 8"
    for base, urgent in pairs:
        assert base["ground_truth"] == urgent["ground_truth"]
        assert base["ground_truth_fields"] == urgent["ground_truth_fields"]


@pytest.mark.skipif(not Path("data/MANIFEST.json").exists(), reason="run `make gen` first")
def test_engine_only_verdict_identical_across_tone_pairs():
    data_dir = Path("data")
    manifest, _ = _manifest_and_claims(data_dir)
    by_id = {r["claim_id"]: r for r in manifest}
    consumed = load_consumed_references(data_dir)
    db_path = str(data_dir / "ledger.db")

    pairs = _tone_pairs(manifest)
    identical = 0
    for base, urgent in pairs:
        v_base = evaluate(by_id[base["claim_id"]], db_path, consumed)
        v_urgent = evaluate(by_id[urgent["claim_id"]], db_path, consumed)
        if v_base.decision == v_urgent.decision:
            identical += 1
    tone_invariance = identical / len(pairs)
    assert tone_invariance == 1.0, f"tone_invariance = {identical}/{len(pairs)}, target 8/8"
