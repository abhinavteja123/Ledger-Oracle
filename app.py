"""FastAPI service: /verify + the human review queue. See PRD 5.1, 11, 12, 13, 19.1.

Wires together everything else in the repo: sanitize -> parse_claim -> investigate
(which calls policy.decide internally) -> audit.record_event -> the review queue for
escalated claims. Every failure path returns an escalate-shaped 200 response rather
than a 500 -- "every failure resolves to a terminal state, default is escalate" holds
at the API boundary too, not just inside the agent loop.
"""
import sqlite3
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import config
import tools
from agent import CEREBRAS_ERRORS, investigate
from audit import record_event
from parser import ParseFailedError, parse_claim
from sanitize import sanitize

app = FastAPI(title="Ledger Oracle")

# In-memory review queue and approved-reference store. Not data/ledger.db (read-only,
# Rule 2) -- this is app-level, process-lifetime state. A restart clears it; that's fine
# for a demo/eval service, not a production claim.
REVIEW_QUEUE: dict[str, dict] = {}
APPROVED_REFERENCES: set[str] = set()

REASON_CODE_TEXT = {
    "REF_NOT_IN_LEDGER": "The claimed reference does not exist anywhere in our ledger.",
    "REF_AMBIGUOUS": "The reference matches more than one capture; the engine will not guess.",
    "REF_NEAR_MATCH_TYPO": "The reference nearly matches a real capture -- likely a typo, not fraud.",
    "REF_MAY_BE_IN_FLIGHT": "The matching capture has not settled yet; absence now is not absence once it settles.",
    "REF_OUTSIDE_CONNECTED_LEDGER": "The reference isn't in the ledger we can see, but this merchant has sources we aren't connected to.",
    "REF_ALREADY_CONSUMED": "This reference was already used to approve a prior claim.",
    "CONTRADICTORY_LEDGER_RECORDS": "Two pieces of evidence disagree on the same capture.",
    "POSSIBLE_OUT_OF_BAND_REFUND": "A manually-issued refund already exists for this order.",
    "INSUFFICIENT_CLAIM_DATA": "The claim doesn't contain enough information to investigate.",
    "AMBIGUOUS_ORDER": "The claim names more than one possible order.",
    "CONTRADICTORY_AMOUNTS": "The claim states more than one amount.",
    "CONTRADICTORY_CLAIM": "The claim contradicts itself.",
    "RAIL_NOT_COVERED": "The claimed payment rail isn't one this ledger tracks at all.",
    "TOOL_UNAVAILABLE": "The ledger tools were unavailable during the investigation.",
    "STEP_BUDGET_EXHAUSTED": "The investigation used its full step budget without resolving.",
    "TIME_BUDGET_EXHAUSTED": "The investigation ran out of time without resolving.",
    "MODEL_UNAVAILABLE": "The language model was unavailable during the investigation.",
    "PARSE_FAILED": "The claim text could not be parsed into a structured record.",
}


def _stop_reason_text(reason_code: str) -> str:
    return REASON_CODE_TEXT.get(reason_code, reason_code.replace("_", " ").title())


def _normalize_ref(ref: Optional[str]) -> Optional[str]:
    if ref is None:
        return None
    return ref.replace(" ", "").replace("-", "").upper()


def _load_ledger_consumed_references(db_path: str) -> frozenset:
    if not Path(db_path).exists():
        return frozenset()
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT reference FROM consumed_references").fetchall()
        finally:
            con.close()
        return frozenset(r[0] for r in rows)
    except sqlite3.OperationalError:
        return frozenset()


def _tolerance_config() -> dict:
    return {
        "FUZZ_DISTANCE": config.FUZZ_DISTANCE,
        "AMOUNT_TOLERANCE_PAISE": config.AMOUNT_TOLERANCE_PAISE,
        "SETTLEMENT_WINDOW_HOURS": config.SETTLEMENT_WINDOW_HOURS,
    }


def _escalate_response(reason_code: str, detail: str) -> dict:
    return {
        "decision": "escalate",
        "reason_code": reason_code,
        "max_refundable_paise": 0,
        "why_not_block": detail,
        "extracted": None,
        "tools_called": [],
        "evidence": [],
        "invariants": [],
        "engine_version": config.ENGINE_VERSION,
        "tolerance_config": _tolerance_config(),
    }


def _verdict_response(state, verdict) -> dict:
    return {
        "decision": verdict.decision,
        "reason_code": verdict.reason_code,
        "max_refundable_paise": verdict.max_refundable_paise,
        "why_not_block": verdict.why_not_block,
        "extracted": state.extracted.model_dump() if state.extracted else None,
        "tools_called": [t.model_dump() for t in state.tools_called],
        "evidence": [e.model_dump() for e in state.evidence],
        "invariants": [i.model_dump() for i in verdict.invariants],
        "engine_version": config.ENGINE_VERSION,
        "tolerance_config": _tolerance_config(),
    }


class VerifyRequest(BaseModel):
    text: str


class DecideRequest(BaseModel):
    action: str
    note: str = ""


@app.get("/")
def index():
    return FileResponse("static/verify.html")


@app.get("/review")
def review_page():
    return FileResponse("static/review.html")


@app.post("/verify")
def verify(req: VerifyRequest):
    claim_id = f"clm_{uuid.uuid4().hex[:8]}"
    clean_text, sanitizer_flags = sanitize(req.text)
    record_event("claim_received", claim_id, {"raw_message": req.text, "sanitizer_flags": sanitizer_flags})

    try:
        extracted = parse_claim(clean_text)
    except ParseFailedError as e:
        record_event("parse_failed", claim_id, {"detail": str(e)})
        return JSONResponse(_escalate_response("PARSE_FAILED", str(e)))
    except CEREBRAS_ERRORS as e:
        record_event("model_unavailable", claim_id, {"detail": str(e)})
        return JSONResponse(_escalate_response("MODEL_UNAVAILABLE", str(e)))

    record_event("parsed", claim_id, {"extracted": extracted.model_dump()})

    consumed = _load_ledger_consumed_references(tools.DB_PATH) | frozenset(APPROVED_REFERENCES)
    state, verdict = investigate(claim_id, req.text, extracted=extracted, consumed_references=consumed)

    for call, evidence in zip(state.tools_called, state.evidence):
        record_event("tool_call", claim_id, {"tool": call.tool, "args": call.args})
    record_event("verdict", claim_id, {
        "decision": verdict.decision, "reason_code": verdict.reason_code,
        "max_refundable_paise": verdict.max_refundable_paise,
    })

    response = _verdict_response(state, verdict)

    if verdict.decision == "escalate":
        REVIEW_QUEUE[claim_id] = {
            "claim_id": claim_id,
            "status": "open",
            "raw_message": req.text,
            "sanitizer_flags": sanitizer_flags,
            "stop_reason": verdict.reason_code,
            "stop_reason_text": _stop_reason_text(verdict.reason_code),
            "resolved_reference": _normalize_ref(extracted.claimed_reference),
            **response,
        }

    return JSONResponse(response)


@app.get("/review/queue")
def review_queue():
    claims = [
        {
            "claim_id": c["claim_id"], "status": c["status"],
            "reason_code": c["stop_reason"], "stopped_at": c.get("stopped_at", ""),
        }
        for c in REVIEW_QUEUE.values() if c["status"] == "open"
    ]
    return {"claims": claims}


@app.get("/review/{claim_id}")
def review_detail(claim_id: str):
    claim = REVIEW_QUEUE.get(claim_id)
    if claim is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return claim


@app.post("/review/{claim_id}/decide")
def review_decide(claim_id: str, req: DecideRequest):
    claim = REVIEW_QUEUE.get(claim_id)
    if claim is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    if req.action == "approve" and claim.get("resolved_reference"):
        APPROVED_REFERENCES.add(claim["resolved_reference"])

    record_event("reviewer_decision", claim_id, {"action": req.action, "note": req.note})
    claim["status"] = "decided"
    claim["decision_action"] = req.action
    claim["decision_note"] = req.note
    return {"ok": True}
