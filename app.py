"""FastAPI service: /verify + the human review queue. See PRD 5.1, 11, 12, 13, 19.1.

Wires together everything else in the repo: sanitize -> parse_claim -> investigate
(which calls policy.decide internally) -> audit.record_event -> the review queue for
escalated claims. Every failure path returns an escalate-shaped 200 response rather
than a 500 -- "every failure resolves to a terminal state, default is escalate" holds
at the API boundary too, not just inside the agent loop.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import config
import db
import tools
from agent import CEREBRAS_ERRORS, investigate
from audit import record_event
from audit.verify import verify as audit_verify
from models import InvestigationState, Verdict
from parser import ParseFailedError, parse_claim
from sanitize import sanitize

app = FastAPI(title="Ledger Oracle")

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
    # Routed through db.py so this works against either backend (LEDGER_BACKEND).
    # No pre-check for file existence here (that was sqlite-specific and meaningless
    # for the supabase path) -- both the connect and the query are wrapped instead.
    try:
        conn = db.get_readonly_connection(db_path)
    except Exception:
        return frozenset()
    try:
        rows = conn.execute("SELECT reference FROM consumed_references", ()).fetchall()
        return frozenset(r["reference"] for r in rows)
    except Exception:
        return frozenset()
    finally:
        conn.close()


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


class HistoryStore:
    """Every processed claim -- pass, block, and escalate, not just escalates.
    Replaces the old REVIEW_QUEUE + APPROVED_REFERENCES pair: default backend keeps
    an in-memory dict (same process-lifetime scope the old REVIEW_QUEUE had -- a
    restart clears it, fine for a demo/eval service, not a production claim);
    LEDGER_BACKEND=supabase reads/writes the claims_history table via
    db.get_owner_connection() instead. Approving a claim now writes a real row into
    consumed_references (via db.py, on whichever backend is active) rather than
    tracking approvals in a separate in-memory set -- one less place state can drift.
    """

    def __init__(self):
        self._rows: dict[str, dict] = {}  # used only when db.backend() != "supabase"

    def record(self, claim_id: str, raw_message: str, sanitizer_flags: list, state, verdict) -> None:
        extracted = state.extracted
        base = _verdict_response(state, verdict)
        row = {
            "claim_id": claim_id,
            "status": "open" if verdict.decision == "escalate" else "closed",
            "raw_message": raw_message,
            "sanitizer_flags": sanitizer_flags,
            "stop_reason": verdict.reason_code,
            "stop_reason_text": _stop_reason_text(verdict.reason_code),
            "resolved_reference": _normalize_ref(extracted.claimed_reference) if extracted else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "decision_action": None,
            "decision_note": None,
            **base,
        }
        if db.backend() == "supabase":
            conn = db.get_owner_connection()
            try:
                conn.execute(
                    "INSERT INTO claims_history (claim_id, raw_message, claim_type, order_id, "
                    "claimed_reference, claimed_amount_paise, claimed_instrument, decision, "
                    "reason_code, max_refundable_paise, why_not_block, tools_called, evidence, "
                    "invariants, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        claim_id, raw_message,
                        extracted.claim_type if extracted else None,
                        extracted.order_id if extracted else None,
                        extracted.claimed_reference if extracted else None,
                        extracted.claimed_amount_paise if extracted else None,
                        extracted.claimed_instrument if extracted else None,
                        verdict.decision, verdict.reason_code, verdict.max_refundable_paise,
                        verdict.why_not_block, json.dumps(base["tools_called"]),
                        json.dumps(base["evidence"]), json.dumps(base["invariants"]), row["status"],
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        else:
            self._rows[claim_id] = row

    def all(self, status: Optional[str] = None) -> list[dict]:
        if db.backend() == "supabase":
            conn = db.get_owner_connection()
            try:
                sql = "SELECT * FROM claims_history"
                params: tuple = ()
                if status:
                    sql += " WHERE status=?"
                    params = (status,)
                sql += " ORDER BY created_at DESC"
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
            finally:
                conn.close()
        rows = list(self._rows.values())
        if status:
            rows = [r for r in rows if r["status"] == status]
        return list(reversed(rows))

    def get(self, claim_id: str) -> Optional[dict]:
        if db.backend() == "supabase":
            conn = db.get_owner_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM claims_history WHERE claim_id=?", (claim_id,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        return self._rows.get(claim_id)

    def decide(self, claim_id: str, action: str, note: str) -> Optional[dict]:
        row = self.get(claim_id)
        if row is None:
            return None
        if action == "approve" and row.get("resolved_reference"):
            conn = db.get_owner_connection(tools.DB_PATH)
            try:
                conn.execute(
                    "INSERT INTO consumed_references (reference, consumed_by, consumed_at, approved_by) "
                    "VALUES (?,?,?,?)",
                    (row["resolved_reference"], claim_id, datetime.now(timezone.utc).isoformat(), "admin"),
                )
                conn.commit()
            finally:
                conn.close()
        if db.backend() == "supabase":
            conn = db.get_owner_connection()
            try:
                conn.execute(
                    "UPDATE claims_history SET status=?, reviewer_note=?, decided_at=now() WHERE claim_id=?",
                    ("decided", note, claim_id),
                )
                conn.commit()
            finally:
                conn.close()
        else:
            row["status"] = "decided"
            row["decision_action"] = action
            row["decision_note"] = note
        return row


history_store = HistoryStore()

# Legacy in-memory review-queue store, still backing /review/* for the older
# static/review.html page. HistoryStore (above) is the newer, complete record --
# every claim, pass/block/escalate -- that /admin/* reads from. Both are populated
# from the same /verify call; kept side by side rather than migrating /review/*
# onto HistoryStore too, since static/review.html's contract wasn't part of this task.
REVIEW_QUEUE: dict[str, dict] = {}
APPROVED_REFERENCES: set[str] = set()

ADMIN_LEDGER_TABLES = ("captures", "refunds", "consumed_references")


class VerifyRequest(BaseModel):
    text: str


class DecideRequest(BaseModel):
    action: str
    note: str = ""


@app.get("/")
def landing():
    return FileResponse("static/landing.html")


@app.get("/verify-page")
def verify_page():
    return FileResponse("static/verify.html")


@app.get("/admin")
def admin_page():
    return FileResponse("static/admin.html")


@app.get("/review")
def review_page():
    return FileResponse("static/review.html")


@app.post("/verify")
def verify(req: VerifyRequest):
    claim_id = f"clm_{uuid.uuid4().hex[:8]}"
    clean_text, sanitizer_flags = sanitize(req.text)
    record_event("claim_received", claim_id, {"raw_message": req.text, "sanitizer_flags": sanitizer_flags})

    def _log_early_escalate(reason_code: str, detail: str) -> None:
        # Parse failures and MODEL_UNAVAILABLE are real, expected production failure
        # modes (see FAILURES.md's quota-exhaustion entries) -- they must show up on
        # the admin dashboard exactly like any other escalate, not vanish because they
        # happened before an InvestigationState existed. Never blocks the response.
        try:
            early_state = InvestigationState(
                claim_id=claim_id, raw_message=req.text,
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            early_verdict = Verdict(
                decision="escalate", max_refundable_paise=0,
                reason_code=reason_code, why_not_block=detail, invariants=[],
            )
            history_store.record(claim_id, req.text, sanitizer_flags, early_state, early_verdict)
        except Exception:
            pass

    try:
        extracted = parse_claim(clean_text)
    except ParseFailedError as e:
        record_event("parse_failed", claim_id, {"detail": str(e)})
        _log_early_escalate("PARSE_FAILED", str(e))
        return JSONResponse(_escalate_response("PARSE_FAILED", str(e)))
    except CEREBRAS_ERRORS as e:
        record_event("model_unavailable", claim_id, {"detail": str(e)})
        _log_early_escalate("MODEL_UNAVAILABLE", str(e))
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

    # Record every claim -- pass, block, and escalate -- for /admin/*. Never blocks
    # the response: history logging failing must not affect the decision path.
    try:
        history_store.record(claim_id, req.text, sanitizer_flags, state, verdict)
    except Exception:
        pass

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


@app.get("/admin/claims")
def admin_claims(status: Optional[str] = None):
    try:
        return {"claims": history_store.all(status=status)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=200)


@app.get("/admin/claims/{claim_id}")
def admin_claim_detail(claim_id: str):
    try:
        row = history_store.get(claim_id)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=200)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return row


@app.post("/admin/claims/{claim_id}/decide")
def admin_claim_decide(claim_id: str, req: DecideRequest):
    try:
        row = history_store.decide(claim_id, req.action, req.note)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=200)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    record_event("admin_decision", claim_id, {"action": req.action, "note": req.note})
    return {"ok": True}


@app.get("/admin/ledger/{table}")
def admin_ledger_browse(table: str, limit: int = 100):
    if table not in ADMIN_LEDGER_TABLES:
        return JSONResponse({"error": "unknown table"}, status_code=400)
    try:
        conn = db.get_readonly_connection(tools.DB_PATH)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=200)
    try:
        rows = conn.execute(f"SELECT * FROM {table} LIMIT ?", (limit,)).fetchall()
        return {"rows": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/admin/audit")
def admin_audit(path: str = "audit.jsonl"):
    try:
        ok, break_at, detail = audit_verify(path)
    except FileNotFoundError:
        return {"ok": False, "break_at": None, "detail": f"{path} not found"}
    return {"ok": ok, "break_at": break_at, "detail": detail}
