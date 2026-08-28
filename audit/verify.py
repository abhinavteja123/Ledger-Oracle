"""Chain integrity checker (PRD 12.4). Usage: python -m audit.verify audit.jsonl"""
import hashlib
import json
import sys

from audit import GENESIS, _canonical


def verify(path: str) -> tuple[bool, int | None, str | None]:
    """Returns (ok, break_event_number, detail). break_event_number is 1-indexed."""
    prev = GENESIS
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            stored_hash = record.get("hash")
            body = {k: v for k, v in record.items() if k != "hash"}
            if body.get("prev_hash") != prev:
                return False, i, f"prev_hash mismatch (expected {prev}, got {body.get('prev_hash')})"
            recomputed = hashlib.sha256(_canonical(body).encode()).hexdigest()
            if recomputed != stored_hash:
                return False, i, f"expected {recomputed}, got {stored_hash}"
            prev = stored_hash
    return True, None, None


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "audit.jsonl"
    ok, break_at, detail = verify(path)
    with open(path, encoding="utf-8") as f:
        total = sum(1 for line in f if line.strip())
    if ok:
        print(f"AUDIT INTEGRITY: OK      {total} events, chain intact, genesis {GENESIS}")
        sys.exit(0)
    else:
        print("AUDIT INTEGRITY: FAILED")
        print(f"  break at event {break_at}")
        print(f"  {detail}")
        print(f"  all {total - break_at + 1} events from this point are unverifiable")
        sys.exit(1)


if __name__ == "__main__":
    main()
