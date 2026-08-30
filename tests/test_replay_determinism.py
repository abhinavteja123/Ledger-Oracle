"""Replays real historical claims from audit.jsonl back through policy.decide(),
proving "deterministic engine, no LLM in the decision path" empirically instead of
just by assertion.

## Why this is valid at all

audit.jsonl `tool_call` events log `{tool, args}` only, not the tool's result
(app.py:318) -- so "replay" here means re-executing each logged tool call for real
against a ledger snapshot and recomputing the verdict, not literally replaying stored
results. That's only sound if the ledger hasn't mutated since those tool calls ran.

All 5 tools in tools.py are registered `mutates=False` (asserted below), so nothing in
the investigation path itself can have changed data/ledger.db -- only an external
reseed (eval/generate.py, live dev-session testing -- this repo had plenty this
session per HANDOFF.md) could have. To stay honest about that risk without gambling:
this test scopes to claims whose *earliest* audit event timestamp is at or after
data/ledger.db's own last-modified time. If the DB was written after a claim's events
were logged, that claim is skipped -- only claims that ran entirely against the ledger
snapshot as it exists right now are replayed. As of this writing that's a small,
recent, contiguous slice of audit.jsonl (4 claims), not "all of it" -- see
test_replay_determinism_is_nonvacuous below for why sample size doesn't matter to the
strength of this proof; the mutation test does that work.

Also guarded: LEDGER_BACKEND must be sqlite (db.py defaults to it; only the deployed
app sets it to supabase) and each claim's logged tolerance_config must match the
current config.py values -- otherwise a config change would look like a determinism
break instead of what it is.
"""
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

import config
import tools
from models import InvestigationState, StructuredClaim, Verdict
from policy import _normalize_ref, decide

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_PATH = REPO_ROOT / "audit.jsonl"
DB_PATH = REPO_ROOT / "data" / "ledger.db"

# Reused for both real historical claims and the synthetic UTR planted in the
# mutation test -- any claim in the eligible set is missing this reference from the
# ledger today (that's *why* it's REF_NOT_IN_LEDGER), which is exactly the precondition
# the mutation test needs: plant it, and the claim's own tool call must find it.


def _load_events() -> list[dict]:
    if not AUDIT_PATH.exists():
        return []
    with open(AUDIT_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _tolerance_matches_current(event: dict) -> bool:
    tc = event.get("tolerance_config", {})
    return (
        tc.get("AMOUNT_TOLERANCE_PAISE") == config.AMOUNT_TOLERANCE_PAISE
        and tc.get("FUZZ_DISTANCE") == config.FUZZ_DISTANCE
        and tc.get("SETTLEMENT_WINDOW_HOURS") == config.SETTLEMENT_WINDOW_HOURS
    )


def _eligible_claims() -> list[dict]:
    """Group audit.jsonl by claim_id, keep only claims with a parsed + verdict event,
    at least one tool_call (nothing to replay otherwise), entirely logged at/after
    data/ledger.db's mtime, and matching the current policy tolerance config."""
    if not DB_PATH.exists():
        return []
    cutoff = os.path.getmtime(DB_PATH)
    by_claim: dict[str, list[dict]] = {}
    for e in _load_events():
        by_claim.setdefault(e["claim_id"], []).append(e)

    out = []
    for claim_id, evs in by_claim.items():
        types = {e["event_type"] for e in evs}
        if "parsed" not in types or "verdict" not in types:
            continue
        tool_calls = [e for e in evs if e["event_type"] == "tool_call"]
        if not tool_calls:
            continue
        if not all(_tolerance_matches_current(e) for e in evs):
            continue
        # ISO timestamps compare lexically fine here (all UTC, same format), but go
        # through fromisoformat for correctness rather than relying on that.
        from datetime import datetime

        earliest = min(
            datetime.fromisoformat(e["timestamp"]).timestamp() for e in evs
        )
        if earliest < cutoff:
            continue  # this claim's tool calls may have run against a different ledger
        out.append({"claim_id": claim_id, "events": evs})
    out.sort(key=lambda c: c["claim_id"])
    return out[:20]


def _replay(claim_events: list[dict], db_path: str, consumed_references: frozenset) -> Verdict:
    """Rebuild a claim's StructuredClaim + evidence by re-executing its logged tool
    calls for real against db_path, then call policy.decide() on the result."""
    parsed = next(e for e in claim_events if e["event_type"] == "parsed")
    claim = StructuredClaim(**parsed["payload"]["extracted"])
    tool_calls = [e for e in claim_events if e["event_type"] == "tool_call"]

    evidence = []
    for tc in tool_calls:
        fn = tools.TOOL_REGISTRY[tc["payload"]["tool"]]["fn"]
        evidence.append(fn(**tc["payload"]["args"], db_path=db_path))

    claim_id = claim_events[0]["claim_id"]
    state = InvestigationState(
        claim_id=claim_id,
        raw_message="replay (not used by decide())",
        extracted=claim,
        evidence=evidence,
        started_at="2026-01-01T00:00:00+00:00",
    )
    return decide(state, consumed_references=consumed_references)


def _logged_verdict_tuple(claim_events: list[dict]) -> tuple:
    v = next(e for e in claim_events if e["event_type"] == "verdict")["payload"]
    return (v["decision"], v["reason_code"], v["max_refundable_paise"])


def _skip_unless_sqlite():
    if os.environ.get("LEDGER_BACKEND", "sqlite") != "sqlite":
        pytest.skip("replay only validated against the sqlite backend")


def test_all_tools_are_read_only():
    # Precondition this whole file leans on: nothing the investigation path does can
    # mutate data/ledger.db, so an external reseed is the only way it could have
    # changed -- which is exactly what the mtime cutoff below guards against.
    assert all(spec["mutates"] is False for spec in tools.TOOL_REGISTRY.values())


def test_replay_matches_logged_verdicts():
    """Re-executes each eligible historical claim's real tool calls against the
    current data/ledger.db and asserts the recomputed verdict matches what was
    actually logged in audit.jsonl at the time.

    Known limitation: consumed_references is loaded fresh from data/ledger.db right
    now, not reconstructed as of each claim's original timestamp. Every claim in the
    eligible slice for this run resolves via REF_NOT_IN_LEDGER, so REF_ALREADY_CONSUMED
    doesn't come into play -- if a future eligible claim depends on consumed_references
    and mismatches for this reason, this is why.
    """
    _skip_unless_sqlite()
    claims = _eligible_claims()
    if not claims:
        pytest.skip("no audit.jsonl claims are entirely newer than data/ledger.db's mtime")

    from app import _load_ledger_consumed_references

    consumed = _load_ledger_consumed_references(str(DB_PATH))

    replayed_any = False
    for c in claims:
        recomputed = _replay(c["events"], str(DB_PATH), consumed)
        logged = _logged_verdict_tuple(c["events"])
        recomputed_tuple = (recomputed.decision, recomputed.reason_code, recomputed.max_refundable_paise)
        assert recomputed_tuple == logged, (
            f"{c['claim_id']}: recomputed {recomputed_tuple} != logged {logged}"
        )
        replayed_any = True
    assert replayed_any


def test_replay_determinism_is_nonvacuous(tmp_path):
    """Proves the replay harness above would actually catch a real determinism break,
    not just pass trivially: takes a frozen copy of data/ledger.db, mutates one
    capture's UTR to match a claim's claimed reference (planting the exact match the
    live ledger doesn't have), replays the same claim against the mutated copy, and
    asserts the verdict changes.
    """
    _skip_unless_sqlite()
    claims = _eligible_claims()

    # Two independent things must both accept the planted value for the mutation to be
    # meaningful: tools.get_payment_by_utr matches by exact string equality against
    # whatever the agent's tool call actually searched for, while decide() (post the
    # evidence-binding fix in policy.py, see test_policy_invariants.py) only accepts a
    # match whose utr normalizes equal to the CLAIM's own claimed_reference. Real parser
    # output sometimes embeds label text into that field (e.g. "UTR 000000000000"
    # instead of "000000000000") -- found live via this test picking exactly such a
    # claim and failing no matter which of the two strings got planted, because no
    # single DB value can satisfy both checks when they disagree. So: only a claim
    # where the tool call's searched utr and the claim's own reference already
    # normalize equal is usable for this test -- that's not a workaround, it's the
    # actual precondition the mutation needs (a claim that alone would resolve if the
    # ledger had this reference at all).
    target = None
    utr = None
    for c in claims:
        tc = next((e for e in c["events"] if e["event_type"] == "tool_call"
                    and e["payload"]["tool"] == "get_payment_by_utr"), None)
        if tc is None:
            continue
        parsed = next(e for e in c["events"] if e["event_type"] == "parsed")
        claimed_ref = parsed["payload"]["extracted"].get("claimed_reference")
        searched_utr = tc["payload"]["args"].get("utr")
        # Must also be a claim the live ledger genuinely has no match for -- otherwise
        # the "mutation creates a match where none existed" premise doesn't hold.
        if (claimed_ref and searched_utr and _normalize_ref(claimed_ref) == _normalize_ref(searched_utr)
                and _logged_verdict_tuple(c["events"])[1] == "REF_NOT_IN_LEDGER"):
            target, utr = c, searched_utr
            break
    if target is None:
        pytest.skip("no eligible claim whose tool-call utr matches its own claimed_reference")

    mutated_db = tmp_path / "ledger.db"
    shutil.copy2(DB_PATH, mutated_db)
    con = sqlite3.connect(mutated_db)
    row = con.execute(
        "SELECT capture_id FROM captures WHERE ledger_source='connected' AND settled_at IS NOT NULL LIMIT 1"
    ).fetchone()
    assert row is not None, "fixture assumption: at least one settled connected capture exists"
    con.execute("UPDATE captures SET utr=? WHERE capture_id=?", (utr, row[0]))
    con.commit()
    con.close()

    logged = _logged_verdict_tuple(target["events"])
    mutated_verdict = _replay(target["events"], str(mutated_db), frozenset())
    mutated_tuple = (mutated_verdict.decision, mutated_verdict.reason_code, mutated_verdict.max_refundable_paise)

    assert mutated_tuple != logged, (
        "mutation harness is vacuous: planting a matching UTR did not change the verdict"
    )


if __name__ == "__main__":
    import sys

    import pytest as _pytest

    sys.exit(_pytest.main([__file__, "-v"]))
