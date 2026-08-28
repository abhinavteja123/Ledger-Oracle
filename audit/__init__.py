"""Append-only, hash-chained audit trail. See PRD section 12.

Tamper-EVIDENT, not tamper-proof (PRD 12.1): a single process appending SHA-256-chained
JSONL detects partial tampering (an edited/deleted/reordered row) but cannot stop someone
who can rewrite the whole file and recompute the chain. That's the honest claim to make.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from config import AMOUNT_TOLERANCE_PAISE, ENGINE_VERSION, FUZZ_DISTANCE, SETTLEMENT_WINDOW_HOURS

GENESIS = "0" * 64


def _canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def _last_hash(path: Path) -> str:
    if not path.exists() or path.stat().st_size == 0:
        return GENESIS
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        chunk = 4096
        pos = max(size - chunk, 0)
        f.seek(pos)
        tail = f.read().decode("utf-8", errors="replace")
    last_line = [line for line in tail.splitlines() if line.strip()][-1]
    return json.loads(last_line)["hash"]


def append(record: dict, prev_hash: str, path: str = "audit.jsonl") -> tuple[str, str]:
    """Low-level append (PRD 12.2). Caller supplies prev_hash explicitly."""
    record = dict(record)
    record["prev_hash"] = prev_hash
    canonical = _canonical(record)
    h = hashlib.sha256(canonical.encode()).hexdigest()
    record["hash"] = h
    with open(path, "a", encoding="utf-8") as f:
        f.write(_canonical(record) + "\n")
    return h, canonical


def record_event(event_type: str, claim_id: str, payload: dict, path: str = "audit.jsonl") -> str:
    """Convenience wrapper: fills timestamp/engine_version/tolerance_config (PRD 12.3)
    and reads prev_hash from the file itself, so callers never track it by hand."""
    p = Path(path)
    record = {
        "event_type": event_type,
        "claim_id": claim_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
        "engine_version": ENGINE_VERSION,
        "tolerance_config": {
            "FUZZ_DISTANCE": FUZZ_DISTANCE,
            "AMOUNT_TOLERANCE_PAISE": AMOUNT_TOLERANCE_PAISE,
            "SETTLEMENT_WINDOW_HOURS": SETTLEMENT_WINDOW_HOURS,
        },
    }
    h, _ = append(record, _last_hash(p), path)
    return h
