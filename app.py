"""FastAPI service: /verify + the human review queue. See PRD 5.1, 11, 12, 13, 19.1.

Wires together everything else in the repo: sanitize -> parse_claim -> investigate
(which calls policy.decide internally) -> audit.record_event -> the review queue for
escalated claims. Every failure path returns an escalate-shaped 200 response rather
than a 500 -- "every failure resolves to a terminal state, default is escalate" holds
at the API boundary too, not just inside the agent loop.
"""
import json
import os
import pathlib
import random
import string
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import db
import tools
from agent import LLM_ERRORS, investigate
from audit import record_event
from audit.verify import verify as audit_verify
from eval.agreement import compute_agreement
from llm_client import load_dotenv
from models import InvestigationState, Verdict
from parser import ParseFailedError, parse_claim
from sanitize import detect_risk_flags, sanitize

# Populate os.environ from .env once, at process startup -- not lazily on first LLM
# call (see llm_client.load_dotenv's docstring). db.py reads SUPABASE_DB_URL /
# SUPABASE_READONLY_DB_URL directly via os.environ, so /admin/* and /review/*
# routes need this done before they run too, not just /verify.
#
# Gated on LEDGER_BACKEND=supabase, not unconditional: tests import this module
# with LEDGER_BACKEND unset (sqlite default) and deliberately never configure real
# LLM keys, relying on get_client() raising LLMProviderError fast when no provider
# is configured to catch any test that forgot to inject a fake client. Loading real
# GROQ_API_KEY/GEMINI_API_KEY from .env unconditionally here silently removed that
# safety net for the whole suite -- several tests started making real, rate-limited
# network calls instead of failing fast. Found by running the full suite, not by
# inspection: 13 tests failed together, each passed in isolation (shared network
# rate limit across the session), the classic fingerprint of this exact mistake.
if os.environ.get("LEDGER_BACKEND") == "supabase":
    load_dotenv()

app = FastAPI(title="Ledger Oracle")
# All four pages (/, /verify-page, /admin, /review) are now one Vite-built React SPA
# (see frontend/), built into static/dist and served as static assets here; react-router
# picks the right page client-side from the URL. Absolute path, not "static/dist" -- tests
# chdir into a tmp_path fixture dir before this module's first import, and a relative
# StaticFiles directory is resolved at mount time (this line), not per-request, so a
# relative path would 404 the moment any test imports app.py from outside the repo root.
STATIC_DIST = pathlib.Path(__file__).resolve().parent / "static" / "dist"
app.mount("/static", StaticFiles(directory=str(STATIC_DIST)), name="static")

REASON_CODE_TEXT = {
    "OK": "The claim matched a real, settled payment for the full amount -- refund authorized up to that amount.",
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
    "REPEATED_CLAIM_ABUSE": "This order has already had multiple claims denied; further automatic attempts are blocked.",
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
        "reason_text": _stop_reason_text(reason_code),
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
        "reason_text": _stop_reason_text(verdict.reason_code),
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

    def count_adverse_attempts(self, order_id: str) -> int:
        """How many *prior* claims on this order_id ended in one of
        config.ADVERSE_REASON_CODES -- feeds policy.decide()'s REPEATED_CLAIM_ABUSE
        gate (see app.py's /verify). Handles both backends' shapes: the supabase
        claims_history table stores a flat `order_id` column, the in-memory row nests
        it under row["extracted"]["order_id"] (see _verdict_response()) -- same
        dual-shape defensiveness admin.html already needed for this table.
        """
        if not order_id:
            return 0
        codes = tuple(config.ADVERSE_REASON_CODES)
        if db.backend() == "supabase":
            conn = db.get_owner_connection()
            try:
                placeholders = ",".join("?" * len(codes))
                row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM claims_history WHERE order_id=? "
                    f"AND reason_code IN ({placeholders})",
                    (order_id, *codes),
                ).fetchone()
                return int(row["n"]) if row else 0
            finally:
                conn.close()
        count = 0
        for row in self._rows.values():
            extracted = row.get("extracted") or {}
            if extracted.get("order_id") == order_id and row.get("reason_code") in config.ADVERSE_REASON_CODES:
                count += 1
        return count

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
        if row.get("status") == "decided":
            # Real gap flagged in an earlier session, never fixed: nothing stopped a
            # second /admin/claims/{id}/decide call on a claim already decided -- a
            # double-approve would try to INSERT the same reference into
            # consumed_references twice (fails loudly on the PK, but only by luck of
            # that table having one), and an approve-then-reject would silently
            # overwrite the note/action with no record either happened.
            raise ValueError(f"claim {claim_id} was already decided; refusing to decide it again")
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
# Review page (frontend/src/pages/Review.jsx). HistoryStore (above) is the newer,
# complete record -- every claim, pass/block/escalate -- that /admin/* reads from.
# Both are populated from the same /verify call; kept side by side rather than
# migrating /review/* onto HistoryStore too, since that contract wasn't part of this task.
REVIEW_QUEUE: dict[str, dict] = {}
APPROVED_REFERENCES: set[str] = set()

ADMIN_LEDGER_TABLES = ("captures", "refunds", "consumed_references")


class VerifyRequest(BaseModel):
    text: str


class DecideRequest(BaseModel):
    action: str
    note: str = ""


class PaySimulateRequest(BaseModel):
    amount_rupees: float
    instrument: str = "upi"


# The simulator writes into the same `captures` table the policy engine trusts as
# ground truth, via the app's owner connection -- deliberately outside the agent's
# read-only tool boundary (that boundary constrains what the LLM can act on, not
# what the app itself can write). Unauthenticated + unbounded would let anyone mint
# an arbitrarily large max_refundable_paise ceiling; this caps the blast radius to
# a plausible demo amount. Not a substitute for real auth if this endpoint is ever
# exposed beyond a demo.
MAX_SIMULATED_PAYMENT_RUPEES = 50_000


@app.get("/")
def landing():
    return FileResponse(str(STATIC_DIST / "index.html"))


@app.get("/verify-page")
def verify_page():
    return FileResponse(str(STATIC_DIST / "index.html"))


@app.get("/admin")
def admin_page():
    return FileResponse(str(STATIC_DIST / "index.html"))


@app.get("/review")
def review_page():
    return FileResponse(str(STATIC_DIST / "index.html"))


@app.get("/pay")
def pay_page():
    return FileResponse(str(STATIC_DIST / "index.html"))


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
    except LLM_ERRORS as e:
        record_event("model_unavailable", claim_id, {"detail": str(e)})
        _log_early_escalate("MODEL_UNAVAILABLE", str(e))
        return JSONResponse(_escalate_response("MODEL_UNAVAILABLE", str(e)))

    record_event("parsed", claim_id, {"extracted": extracted.model_dump()})

    consumed = _load_ledger_consumed_references(tools.DB_PATH) | frozenset(APPROVED_REFERENCES)
    prior_adverse_attempts = history_store.count_adverse_attempts(extracted.order_id)
    state, verdict = investigate(claim_id, req.text, extracted=extracted, consumed_references=consumed,
                                  risk_flags=detect_risk_flags(clean_text),
                                  prior_adverse_attempts=prior_adverse_attempts)

    for call, evidence in zip(state.tools_called, state.evidence):
        record_event("tool_call", claim_id, {"tool": call.tool, "args": call.args})
    record_event("verdict", claim_id, {
        "decision": verdict.decision, "reason_code": verdict.reason_code,
        "max_refundable_paise": verdict.max_refundable_paise,
    })

    if verdict.decision == "pass":
        # A straight auto-pass used to never mark its reference consumed (only an
        # admin-approved escalate did), so the same real UTR could be disputed and
        # refunded repeatedly. Consume it immediately, same as an approved escalate.
        resolved_ref = _normalize_ref(extracted.claimed_reference)
        if resolved_ref:
            try:
                conn = db.get_owner_connection(tools.DB_PATH)
                try:
                    conn.execute(
                        "INSERT INTO consumed_references (reference, consumed_by, consumed_at, approved_by) "
                        "VALUES (?,?,?,?)",
                        (resolved_ref, claim_id, datetime.now(timezone.utc).isoformat(), "auto"),
                    )
                    conn.commit()
                finally:
                    conn.close()
            except Exception as e:
                # Never blocks the pass response (same discipline as history_store.record
                # below) -- but silently swallowing this specific failure would mean the
                # reference never gets consumed and replay works forever, so it must be
                # visible on the audit trail even though the API response stays "pass".
                record_event("consume_failed", claim_id, {"reference": resolved_ref, "detail": str(e)})

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


@app.post("/pay/simulate")
def pay_simulate(req: PaySimulateRequest):
    """Simulates a real payment: inserts a genuinely settled row into `captures` so
    the user can immediately dispute a UTR that actually exists in the ledger --
    the exact scenario this product is built to adjudicate. Not a UI mock: this is
    a real write via db.get_owner_connection(), same table /verify's tools.py reads."""
    if req.amount_rupees <= 0 or req.amount_rupees > MAX_SIMULATED_PAYMENT_RUPEES:
        return JSONResponse(
            {"error": f"amount must be between 0 and {MAX_SIMULATED_PAYMENT_RUPEES} rupees"},
            status_code=400,
        )
    instrument = req.instrument if req.instrument in ("upi", "card", "netbanking") else "upi"
    amount_paise = round(req.amount_rupees * 100)
    order_id = f"order_{uuid.uuid4().hex[:8]}"
    capture_id = f"cap_{uuid.uuid4().hex[:10]}"
    utr = "".join(random.choices(string.digits, k=12))
    payee_vpa = f"user{uuid.uuid4().hex[:4]}@oksbi" if instrument == "upi" else None
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = db.get_owner_connection(tools.DB_PATH)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=200)
    try:
        conn.execute(
            "INSERT INTO captures (capture_id, order_id, amount_paise, instrument, utr, "
            "payee_vpa, captured_at, settled_at, ledger_source) VALUES (?,?,?,?,?,?,?,?,?)",
            (capture_id, order_id, amount_paise, instrument, utr, payee_vpa, now, now, "connected"),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "order_id": order_id, "utr": utr, "capture_id": capture_id,
        "amount_paise": amount_paise, "instrument": instrument,
        "payee_vpa": payee_vpa, "captured_at": now,
    }


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


@app.get("/admin/agreement")
def admin_agreement(path: str = "audit.jsonl"):
    # Engine-vs-human agreement (PRD gap: nothing else joins verdict events against
    # reviewer_decision/admin_decision events). Reads audit.jsonl, not HistoryStore --
    # HistoryStore.decide() is only ever reached via /admin/claims/{id}/decide, so it
    # would silently miss every reviewer_decision made through the older /review/*
    # queue (frontend/src/pages/Review.jsx). audit.jsonl has both event types.
    try:
        return compute_agreement(path)
    except FileNotFoundError:
        return {"agreement_rate": None, "agree_count": 0, "disagree_count": 0,
                 "inconclusive_count": 0, "reviewed_count": 0, "by_reason_code": []}
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
        # Distinct from ok=True (chain verified intact): nothing to verify yet is not
        # the same claim as "verified, and it's clean" -- a cold instance (ephemeral
        # disk, gitignored audit.jsonl) must not render as a green integrity check.
        return {"ok": None, "break_at": None, "detail": "no audit log yet"}
    return {"ok": ok, "break_at": break_at, "detail": detail}
