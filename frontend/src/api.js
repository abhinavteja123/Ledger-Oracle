// Thin fetch wrappers over the FastAPI backend (app.py). Same-origin in
// production (FastAPI serves this build); vite.config.js proxies these paths
// to 127.0.0.1:8000 during `npm run dev`.

export async function verifyClaim(text) {
  const res = await fetch("/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  return res.json();
}

export async function getReviewQueue() {
  const res = await fetch("/review/queue");
  return res.json();
}

export async function getReviewClaim(claimId) {
  const res = await fetch("/review/" + encodeURIComponent(claimId));
  return res.json();
}

export async function decideReviewClaim(claimId, action, note) {
  const res = await fetch("/review/" + encodeURIComponent(claimId) + "/decide", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, note }),
  });
  return res.json();
}

export async function getAdminClaims(status) {
  const qs = status ? "?status=" + encodeURIComponent(status) : "";
  const res = await fetch("/admin/claims" + qs);
  return res.json();
}

export async function getAdminClaim(claimId) {
  const res = await fetch("/admin/claims/" + encodeURIComponent(claimId));
  return res.json();
}

export async function decideAdminClaim(claimId, action, note) {
  const res = await fetch("/admin/claims/" + encodeURIComponent(claimId) + "/decide", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, note }),
  });
  return res.json();
}

export async function getAdminLedgerTable(table) {
  const res = await fetch("/admin/ledger/" + encodeURIComponent(table));
  return res.json();
}

export async function getAdminAudit() {
  const res = await fetch("/admin/audit");
  return res.json();
}

export async function getAdminAgreement() {
  const res = await fetch("/admin/agreement");
  return res.json();
}

export function rupees(paise) {
  if (paise === null || paise === undefined) return "--";
  return "Rs " + (paise / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

export const DECISION_HEADLINE = {
  pass: "Refund authorized",
  block: "Claim blocked",
  escalate: "Sent for human review",
};

export function summarizeEvidence(ev) {
  if (!ev) return "--";
  if (ev.matches) return ev.matches.length + " match(es), " + (ev.near_matches?.length ?? 0) + " near";
  if (ev.captures) return ev.captures.length + " capture(s)";
  if (ev.refunds) return ev.refunds.length + " refund(s)";
  if (ev.duplicate_pairs) return ev.duplicate_pairs.length + " duplicate pair(s)";
  if (ev.found !== undefined) return ev.found ? "found" : "not found";
  if (ev.error_class) return "ERROR: " + ev.error_class;
  return "--";
}
