import { useState, useEffect, useCallback } from "react";
import { ThemeToggle, Mesh } from "../components/Shared.jsx";
import { getReviewQueue, getReviewClaim, decideReviewClaim, rupees, summarizeEvidence } from "../api.js";

const FIELDS = ["claim_type", "order_id", "claimed_reference", "claimed_amount_paise", "claimed_instrument"];

function QueueList({ claims, selectedId, onSelect }) {
  if (!claims) return <ul className="claim-list"><li className="placeholder">loading...</li></ul>;
  if (!claims.length) return <ul className="claim-list"><li className="placeholder">queue is empty</li></ul>;
  return (
    <ul className="claim-list">
      {claims.map((c) => (
        <li
          key={c.claim_id}
          className={c.claim_id === selectedId ? "active" : ""}
          onClick={() => onSelect(c.claim_id)}
        >
          <span>
            <span className="id">{c.claim_id}</span>
            <span className="sub">{c.reason_code ?? c.status ?? ""}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

function ExtractedFields({ extracted }) {
  if (!extracted) return <span className="placeholder">-- did not parse --</span>;
  return FIELDS.map((f) => {
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

function ToolsTable({ toolsCalled, evidence }) {
  const tools = toolsCalled || [];
  return (
    <table>
      <thead><tr><th>#</th><th>tool</th><th>result</th><th>latency</th></tr></thead>
      <tbody>
        {tools.length ? tools.map((tc, i) => {
          const ev = (evidence && evidence[i]) || {};
          const latency = ev.query_latency_ms != null ? ev.query_latency_ms + "ms" : "--";
          return (
            <tr key={i}>
              <td>{i + 1}</td>
              <td>{tc.tool ?? "--"}</td>
              <td>{summarizeEvidence(ev)}</td>
              <td>{latency}</td>
            </tr>
          );
        }) : <tr><td colSpan="4" className="placeholder">no tool calls</td></tr>}
      </tbody>
    </table>
  );
}

function InvariantsTable({ invariants }) {
  const rows = invariants || [];
  return (
    <table>
      <thead><tr><th>invariant</th><th>status</th><th>detail</th></tr></thead>
      <tbody>
        {rows.length ? rows.map((inv, i) => (
          <tr key={i}>
            <td>{inv.invariant}</td>
            <td className={inv.passed ? "pass-cell" : "fail-cell"}>{inv.passed ? "OK" : "FAIL"}</td>
            <td>{inv.detail ?? ""}</td>
          </tr>
        )) : <tr><td colSpan="3" className="placeholder">no invariants</td></tr>}
      </tbody>
    </table>
  );
}

export default function Review() {
  const [claims, setClaims] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [detailError, setDetailError] = useState(null);
  const [note, setNote] = useState("");
  const [deciding, setDeciding] = useState(false);

  const loadQueue = useCallback(async () => {
    try {
      const data = await getReviewQueue();
      setClaims(data.claims || []);
    } catch (err) {
      setClaims([]);
    }
  }, []);

  useEffect(() => { loadQueue(); }, [loadQueue]);

  async function selectClaim(claimId) {
    setSelectedId(claimId);
    setDetail(null);
    setDetailError(null);
    setNote("");
    try {
      const d = await getReviewClaim(claimId);
      setDetail(d);
    } catch (err) {
      setDetailError("failed to load claim: " + err);
    }
  }

  async function decide(action) {
    if (!selectedId) return;
    setDeciding(true);
    try {
      await decideReviewClaim(selectedId, action, note);
      setSelectedId(null);
      setDetail(null);
      setDetailError(null);
      setNote("");
      await loadQueue();
    } catch (err) {
      alert("Decision failed: " + err);
    } finally {
      setDeciding(false);
    }
  }

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
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <span className="meta">({claims ? claims.length : "--"} open)</span>
          <ThemeToggle />
        </div>
      </div>

      <div className="console-wrap">
        <div className="console-layout">
          <div className="console-panel">
            <div className="inner">
              <h2>Queue</h2>
              <QueueList claims={claims} selectedId={selectedId} onSelect={selectClaim} />
            </div>
          </div>

          <div className="console-panel">
            <div className="inner">
              {!selectedId ? (
                <div className="placeholder">-- select a claim from the queue --</div>
              ) : detailError ? (
                <div className="placeholder">{detailError}</div>
              ) : !detail ? (
                <div className="placeholder">loading...</div>
              ) : (
                <>
                  <div className="why-stopped">
                    <div className="headline">WHY AUTOMATION STOPPED</div>
                    <div className="code">{detail.stop_reason ?? detail.reason_code ?? "--"}</div>
                    <div>{detail.stop_reason_text ?? ""}</div>
                  </div>

                  <div className="console-section">
                    <h3>Customer claim</h3>
                    <div className="claim-text">{detail.raw_message ?? "--"}</div>
                  </div>

                  <div className="console-section">
                    <h3>Extracted</h3>
                    <ExtractedFields extracted={detail.extracted} />
                  </div>

                  <div className="console-section">
                    <h3>Investigation</h3>
                    <ToolsTable toolsCalled={detail.tools_called} evidence={detail.evidence} />
                  </div>

                  <div className="console-section">
                    <h3>Invariants</h3>
                    <InvariantsTable invariants={detail.invariants} />
                  </div>

                  <div className="console-section">
                    <div className="why-not-block">WHY NOT BLOCK: {detail.why_not_block ?? "--"}</div>
                  </div>

                  <div className="console-section">
                    <h3>Decision</h3>
                    <textarea
                      placeholder="reviewer note..."
                      value={note}
                      onChange={(e) => setNote(e.target.value)}
                    />
                    <div className="console-actions">
                      <button className="approve" disabled={deciding} onClick={() => decide("approve")}>
                        APPROVE REFUND
                      </button>
                      <button className="reject" disabled={deciding} onClick={() => decide("reject")}>
                        REJECT
                      </button>
                      <button disabled={deciding} onClick={() => decide("request_info")}>
                        REQUEST MORE INFO
                      </button>
                    </div>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
