import { useEffect, useState } from "react";
import { ThemeToggle, Mesh } from "../components/Shared.jsx";
import {
  getAdminClaims,
  getAdminClaim,
  decideAdminClaim,
  getAdminLedgerTable,
  getAdminAudit,
  getAdminAgreement,
  rupees,
  DECISION_HEADLINE,
  summarizeEvidence,
} from "../api.js";

/*
 * Dual backend shape handling (see static/admin.html's script comment, the original
 * source of truth for this page): claim rows differ between the sqlite backend (what
 * pytest exercises -- nested `extracted` object, plus `stop_reason_text`,
 * `decision_action`, `decision_note`) and the deployed supabase backend (claim_type/
 * order_id/etc. as flat top-level columns, no `stop_reason_text`, `reviewer_note` /
 * `decided_at` instead of `decision_action` / `decision_note`). Every accessor below
 * falls back gracefully so both shapes render the same way.
 */
function reasonText(d) {
  const code = d.stop_reason ?? d.reason_code ?? "";
  return d.stop_reason_text || d.reason_text || (code ? code.replace(/_/g, " ").toLowerCase() : "") || "no reason given.";
}

const LEDGER_TABLES = [
  { key: "captures", label: "Captures" },
  { key: "refunds", label: "Refunds" },
  { key: "consumed_references", label: "Consumed References" },
];

function ClaimsList({ claims, loading, error, currentClaimId, onSelect }) {
  if (error) return <ul className="claim-list"><li className="placeholder fail-cell">failed to load: {error}</li></ul>;
  if (loading) return <ul className="claim-list"><li className="placeholder">loading...</li></ul>;
  if (!claims.length) return <ul className="claim-list"><li className="placeholder">no claims</li></ul>;
  return (
    <ul className="claim-list">
      {claims.map((c) => {
        const decision = c.decision || "escalate";
        const why = c.reason_code || c.stop_reason ? reasonText(c) : c.status ?? "";
        return (
          <li
            key={c.claim_id}
            className={c.claim_id === currentClaimId ? "active" : ""}
            title={c.reason_code ?? ""}
            onClick={() => onSelect(c.claim_id)}
          >
            <span>
              <span className="id">{c.claim_id}</span>
              <span className="sub">{why}</span>
            </span>
            <span className={"badge badge-" + decision}>{c.decision || "?"}</span>
          </li>
        );
      })}
    </ul>
  );
}

function ExtractedFields({ d }) {
  // supabase rows have no `extracted` nesting -- fall back to the row itself.
  const extracted = d.extracted || d;
  const fields = ["claim_type", "order_id", "claimed_reference", "claimed_amount_paise", "claimed_instrument"];
  const parsed = fields.some((f) => extracted[f] !== null && extracted[f] !== undefined);
  if (!parsed) return <span className="placeholder">-- did not parse --</span>;
  return fields.map((f) => {
    let val = extracted[f];
    if (f === "claimed_amount_paise" && val != null) val = rupees(val);
    return (
      <div className="field-row" key={f}>
        <span className="k">{f}</span>
        <span className="v">{val ?? "--"}</span>
      </div>
    );
  });
}

function ToolsTable({ d }) {
  const tools = d.tools_called || [];
  if (!tools.length) {
    return (
      <table>
        <thead><tr><th>#</th><th>tool</th><th>result</th><th>latency</th></tr></thead>
        <tbody><tr><td colSpan="4" className="placeholder">no tool calls</td></tr></tbody>
      </table>
    );
  }
  return (
    <table>
      <thead><tr><th>#</th><th>tool</th><th>result</th><th>latency</th></tr></thead>
      <tbody>
        {tools.map((tc, i) => {
          const ev = (d.evidence && d.evidence[i]) || {};
          const latency = ev.query_latency_ms != null ? ev.query_latency_ms + "ms" : "--";
          return (
            <tr key={i}>
              <td>{i + 1}</td>
              <td>{tc.tool ?? "--"}</td>
              <td>{summarizeEvidence(ev)}</td>
              <td>{latency}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function InvariantsTable({ d }) {
  const invariants = d.invariants || [];
  if (!invariants.length) {
    return (
      <table>
        <thead><tr><th>invariant</th><th>status</th><th>detail</th></tr></thead>
        <tbody><tr><td colSpan="3" className="placeholder">no invariants</td></tr></tbody>
      </table>
    );
  }
  return (
    <table>
      <thead><tr><th>invariant</th><th>status</th><th>detail</th></tr></thead>
      <tbody>
        {invariants.map((inv, i) => (
          <tr key={i}>
            <td>{inv.invariant}</td>
            <td className={inv.passed ? "pass-cell" : "fail-cell"}>{inv.passed ? "OK" : "FAIL"}</td>
            <td>{inv.detail ?? ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// "closed" = engine auto-decided pass/block, no human touched it. "decided" = an
// escalate that a human then approved/rejected/asked-info on. "open" = still waiting
// on a human -- each renders a different bottom panel; showing an editable note form
// on an already-decided claim would read as "you can still change this."
function DecisionPanel({ d, note, setNote, deciding, decideError, onDecide }) {
  if (d.status === "open") {
    return (
      <>
        <textarea
          placeholder="reviewer note..."
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
        {decideError && <div className="placeholder fail-cell" style={{ marginTop: 6 }}>{decideError}</div>}
        <div className="console-actions">
          <button className="approve" disabled={deciding} onClick={() => onDecide("approve")}>APPROVE</button>
          <button className="reject" disabled={deciding} onClick={() => onDecide("reject")}>REJECT</button>
          <button disabled={deciding} onClick={() => onDecide("request_info")}>REQUEST MORE INFO</button>
        </div>
      </>
    );
  }
  const decision = d.decision || "escalate";
  let text;
  if (d.status === "decided") {
    const action = d.decision_action ?? "reviewed";
    const reviewNote = d.decision_note ?? d.reviewer_note;
    const when = d.decided_at ? " at " + d.decided_at : "";
    text = "Human reviewed" + when + ": " + action.toUpperCase() + (reviewNote ? ' -- "' + reviewNote + '"' : " (no note left)");
  } else {
    text = "Auto-closed by the engine (" + decision + ") -- no human review needed.";
  }
  return <div className="placeholder">{text}</div>;
}

function ClaimDetail({ d, note, setNote, deciding, decideError, onDecide }) {
  const decision = d.decision || "escalate";
  return (
    <div>
      <div className="why-stopped">
        <div className="headline">{DECISION_HEADLINE[decision] || decision.toUpperCase()}</div>
        <div>{reasonText(d)}</div>
        <div className="field-row" style={{ marginTop: 6 }}>
          <span className="k">authorized up to</span>
          <span className="v">{rupees(d.max_refundable_paise)}</span>
        </div>
        <div className="field-row">
          <span className="k">reason code</span>
          <span className="code">{d.stop_reason ?? d.reason_code ?? "--"}</span>
        </div>
      </div>

      <div className="console-section">
        <h3>Customer claim</h3>
        <div className="claim-text">{d.raw_message ?? "--"}</div>
      </div>

      <div className="console-section">
        <h3>Extracted</h3>
        <ExtractedFields d={d} />
      </div>

      <div className="console-section">
        <h3>Investigation</h3>
        <ToolsTable d={d} />
      </div>

      <div className="console-section">
        <h3>Invariants</h3>
        <InvariantsTable d={d} />
      </div>

      {d.why_not_block && (
        <div className="console-section">
          <div className="why-not-block">WHY NOT BLOCK: {d.why_not_block}</div>
        </div>
      )}

      <div className="console-section">
        <h3>Human review</h3>
        <DecisionPanel d={d} note={note} setNote={setNote} deciding={deciding} decideError={decideError} onDecide={onDecide} />
      </div>
    </div>
  );
}

function LedgerTab() {
  const [table, setTable] = useState("captures");
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  async function load(t) {
    setTable(t);
    setLoading(true);
    setError(null);
    try {
      const data = await getAdminLedgerTable(t);
      if (data.error) {
        setError(data.error);
        setRows(null);
      } else {
        setRows(data.rows || []);
      }
    } catch (err) {
      setError(String(err));
      setRows(null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load("captures");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const cols = rows && rows.length ? Object.keys(rows[0]) : [];

  return (
    <div className="console-panel">
      <div className="inner">
        <h2>Ledger tables (read-only, under the hood)</h2>
        <div className="placeholder" style={{ marginBottom: 10 }}>
          Raw source-of-truth tables the engine reads to investigate claims. Technical --
          not needed to follow the claims story above, useful for verifying an individual
          decision against the real data.
        </div>
        <div className="tabbar">
          {LEDGER_TABLES.map((t) => (
            <button key={t.key} className={table === t.key ? "active" : ""} onClick={() => load(t.key)}>
              {t.label}
            </button>
          ))}
        </div>
        <div>
          {loading ? (
            "loading..."
          ) : error ? (
            <span className="fail-cell">{error}</span>
          ) : !rows.length ? (
            <span className="placeholder">no rows</span>
          ) : (
            <table>
              <thead>
                <tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i}>
                    {cols.map((c) => <td key={c}>{r[c] ?? ""}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

function AuditTab() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const d = await getAdminAudit();
      setData(d);
    } catch (err) {
      setError(String(err));
      setData(null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  let badgeText = "checking...";
  let badgeClass = "audit-badge";
  let detailText = "";
  if (!loading) {
    if (error) {
      badgeText = "AUDIT CHECK FAILED";
      badgeClass = "audit-badge audit-failed";
      detailText = error;
    } else if (data?.ok === null) {
      badgeText = "AUDIT: NO DATA YET";
      badgeClass = "audit-badge audit-empty";
      detailText = data?.detail ?? "";
    } else {
      badgeText = data?.ok ? "AUDIT INTEGRITY: OK" : "AUDIT INTEGRITY: FAILED";
      badgeClass = "audit-badge " + (data?.ok ? "audit-ok" : "audit-failed");
      detailText = data?.detail ?? (data?.break_at ? "break at event " + data.break_at : "");
    }
  }

  return (
    <div className="console-panel">
      <div className="inner">
        <h2>Audit chain integrity</h2>
        <div className="placeholder" style={{ marginBottom: 10 }}>
          Verifies the append-only audit log hasn't been tampered with (each event is
          hash-chained to the previous one).
        </div>
        <div className={badgeClass}>{badgeText}</div>
        <div>{detailText}</div>
        <div className="console-actions">
          <button onClick={load}>Re-check</button>
        </div>
      </div>
    </div>
  );
}

function AgreementTab() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const d = await getAdminAgreement();
        if (d.error) {
          setError(d.error);
        } else {
          setData(d);
        }
      } catch (err) {
        setError(String(err));
      }
    })();
  }, []);

  const top = data ? (data.by_reason_code || []).slice(0, 3) : [];

  return (
    <div className="console-panel">
      <div className="inner">
        <h2>Engine vs human agreement</h2>
        <div className="placeholder" style={{ marginBottom: 10 }}>
          How often a human reviewer overturns the engine's escalate decisions. High
          disagreement on a reason code means the engine is escalating too eagerly there.
        </div>
        <div className="field-row">
          <span className="k">agreement rate (escalates only)</span>
          <span className="v">
            {error ? "error: " + error : data?.agreement_rate != null ? (data.agreement_rate * 100).toFixed(1) + "%" : "--"}
          </span>
        </div>
        <div className="field-row">
          <span className="k">reviewed</span>
          <span className="v">
            {data
              ? `${data.reviewed_count} reviewed (${data.agree_count} agree / ${data.disagree_count} disagree), ${data.inconclusive_count} pending info`
              : "--"}
          </span>
        </div>
        <div className="console-section">
          <h3>Top over-escalating reason codes</h3>
          <table>
            <thead><tr><th>reason_code</th><th>disagree (overturned)</th><th>agree</th><th>inconclusive</th></tr></thead>
            <tbody>
              {top.length ? (
                top.map((r) => (
                  <tr key={r.reason_code}>
                    <td title={reasonText({ reason_code: r.reason_code })}>{r.reason_code}</td>
                    <td className="fail-cell">{r.disagree}</td>
                    <td className="pass-cell">{r.agree}</td>
                    <td>{r.inconclusive}</td>
                  </tr>
                ))
              ) : (
                <tr><td colSpan="4" className="placeholder">no reviewed escalates yet</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

export default function Admin() {
  const [tab, setTab] = useState("claims");

  const [statusFilter, setStatusFilter] = useState("");
  const [decisionFilter, setDecisionFilter] = useState("");
  const [allClaims, setAllClaims] = useState([]);
  const [claimsLoading, setClaimsLoading] = useState(true);
  const [claimsError, setClaimsError] = useState(null);

  const [currentClaimId, setCurrentClaimId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [detailError, setDetailError] = useState(null);
  const [note, setNote] = useState("");
  const [deciding, setDeciding] = useState(false);
  const [decideError, setDecideError] = useState(null);

  async function loadClaims() {
    setClaimsLoading(true);
    setClaimsError(null);
    try {
      const data = await getAdminClaims(statusFilter);
      setAllClaims(data.claims || []);
    } catch (err) {
      setClaimsError(String(err));
    } finally {
      setClaimsLoading(false);
    }
  }

  useEffect(() => {
    loadClaims();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter]);

  async function selectClaim(claimId) {
    setCurrentClaimId(claimId);
    setDetail(null);
    setDetailError(null);
    setNote("");
    setDecideError(null);
    try {
      const d = await getAdminClaim(claimId);
      setDetail(d);
    } catch (err) {
      setDetailError(String(err));
    }
  }

  async function decide(action) {
    if (!currentClaimId) return;
    setDeciding(true);
    setDecideError(null);
    try {
      const d = await decideAdminClaim(currentClaimId, action, note);
      if (d && d.error) {
        setDecideError(d.error);
        return;
      }
      setCurrentClaimId(null);
      setDetail(null);
      await loadClaims();
    } catch (err) {
      setDecideError("Decision failed: " + err);
    } finally {
      setDeciding(false);
    }
  }

  const filteredClaims = decisionFilter ? allClaims.filter((c) => c.decision === decisionFilter) : allClaims;

  return (
    <>
      <Mesh />
      <div className="console-header">
        <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
          <a className="back" href="/">← Landing</a>
          <a className="brand" href="/">
            <span className="mark">◆</span>
            <span className="word">Ledger Oracle</span>
          </a>
        </div>
        <ThemeToggle />
      </div>

      <div className="console-wrap">
        <div className="tabbar">
          <button className={tab === "claims" ? "active" : ""} onClick={() => setTab("claims")}>Claims</button>
          <button className={tab === "agreement" ? "active" : ""} onClick={() => setTab("agreement")}>Engine vs Human</button>
          <button className={tab === "audit" ? "active" : ""} onClick={() => setTab("audit")}>Audit chain</button>
          <button className={tab === "ledger" ? "active" : ""} onClick={() => setTab("ledger")}>Ledger (raw tables)</button>
        </div>

        {tab === "claims" && (
          <div className="console-layout">
            <div className="console-panel">
              <div className="inner">
                <h2>Claims</h2>
                <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                  <option value="">all statuses</option>
                  <option value="open">open</option>
                  <option value="closed">closed</option>
                  <option value="decided">decided</option>
                </select>
                <select value={decisionFilter} onChange={(e) => setDecisionFilter(e.target.value)}>
                  <option value="">all decisions</option>
                  <option value="pass">pass</option>
                  <option value="block">block</option>
                  <option value="escalate">escalate</option>
                </select>
                <ClaimsList
                  claims={filteredClaims}
                  loading={claimsLoading}
                  error={claimsError}
                  currentClaimId={currentClaimId}
                  onSelect={selectClaim}
                />
              </div>
            </div>

            <div className="console-panel">
              <div className="inner">
                {!currentClaimId ? (
                  <div className="placeholder">-- select a claim from the list --</div>
                ) : detailError ? (
                  <div className="placeholder fail-cell">failed to load claim: {detailError}</div>
                ) : !detail ? (
                  <div className="placeholder">loading...</div>
                ) : (
                  <ClaimDetail
                    d={detail}
                    note={note}
                    setNote={setNote}
                    deciding={deciding}
                    decideError={decideError}
                    onDecide={decide}
                  />
                )}
              </div>
            </div>
          </div>
        )}

        {tab === "agreement" && <AgreementTab />}
        {tab === "audit" && <AuditTab />}
        {tab === "ledger" && <LedgerTab />}
      </div>
    </>
  );
}
