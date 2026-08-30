"""Engine-vs-human agreement (PRD 19-ish: nothing currently joins verdict events
against reviewer_decision/admin_decision events -- this does). Reads audit.jsonl
sequentially, same idiom as audit.verify.verify(): one line at a time, incremental
per-claim_id state, ignore hash-chain fields entirely (that's audit.verify's job).

Only engine decision == "escalate" claims ever reach a human (see app.py's
/review/{id}/decide and /admin/claims/{id}/decide -- both only fire for claims
that entered REVIEW_QUEUE / have status "open", which only happens for escalate,
confirmed by reading app.py:333-343 and the HistoryStore.record status line).
So pass/block claims are never compared against a human action.

Semantics: escalate + approve = engine over-escalated (human cleared it) ->
disagreement. escalate + reject = engine and human agree something was wrong ->
agreement. escalate + request_info = inconclusive/pending, counted in neither.

Usage: python -m eval.agreement audit.jsonl
"""
import json
import sys


def compute_agreement(path: str = "audit.jsonl") -> dict:
    claims: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            claim_id = record["claim_id"]
            event_type = record["event_type"]
            payload = record.get("payload", {})
            c = claims.setdefault(claim_id, {})
            if event_type == "verdict":
                c["decision"] = payload.get("decision")
                c["reason_code"] = payload.get("reason_code")
            elif event_type in ("reviewer_decision", "admin_decision"):
                c["human_action"] = payload.get("action")

    agree = disagree = inconclusive = 0
    by_reason: dict[str, dict[str, int]] = {}

    for c in claims.values():
        if c.get("decision") != "escalate" or "human_action" not in c:
            continue
        reason = c.get("reason_code") or "UNKNOWN"
        rb = by_reason.setdefault(reason, {"agree": 0, "disagree": 0, "inconclusive": 0})
        action = c["human_action"]
        if action == "approve":
            disagree += 1
            rb["disagree"] += 1
        elif action == "reject":
            agree += 1
            rb["agree"] += 1
        elif action == "request_info":
            inconclusive += 1
            rb["inconclusive"] += 1
        # any other action value: not one of the three known actions, skip

    reviewed = agree + disagree
    rate = agree / reviewed if reviewed else None  # zero-division guard, mirrors eval/score.py's _fmt() discipline

    breakdown = sorted(
        ({"reason_code": rc, **counts} for rc, counts in by_reason.items()),
        key=lambda x: x["disagree"],
        reverse=True,
    )

    return {
        "agreement_rate": rate,
        "agree_count": agree,
        "disagree_count": disagree,
        "inconclusive_count": inconclusive,
        "reviewed_count": reviewed,
        "by_reason_code": breakdown,
    }


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "audit.jsonl"
    result = compute_agreement(path)
    rate = result["agreement_rate"]
    print(f"agreement_rate = {rate:.3f}" if rate is not None else "agreement_rate = n/a")
    print(f"agree={result['agree_count']} disagree={result['disagree_count']} "
          f"inconclusive={result['inconclusive_count']} reviewed={result['reviewed_count']}")
    for row in result["by_reason_code"]:
        print(f"  {row['reason_code']:<30} disagree={row['disagree']} agree={row['agree']} inconclusive={row['inconclusive']}")


if __name__ == "__main__":
    main()
