import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ThemeToggle, Mesh } from "../components/Shared.jsx";
import { simulatePayment, rupees } from "../api.js";
import "./Verify.css";
import "./Pay.css";

function claimText(receipt) {
  return (
    "I paid " + (receipt.amount_paise / 100) + " rupees, UTR " + receipt.utr +
    ", order " + receipt.order_id + ", payment was not recorded on my end."
  );
}

export default function Pay() {
  const navigate = useNavigate();
  const [amount, setAmount] = useState("2499");
  const [instrument, setInstrument] = useState("upi");
  const [loading, setLoading] = useState(false);
  const [receipt, setReceipt] = useState(null);
  const [error, setError] = useState(null);

  async function pay() {
    const value = parseFloat(amount);
    if (!value || value <= 0) {
      setError("Enter a valid amount.");
      return;
    }
    setError(null);
    setLoading(true);
    try {
      const body = await simulatePayment(value, instrument);
      if (body.error) {
        setError(body.error);
      } else {
        setReceipt(body);
      }
    } catch (err) {
      setError("Request failed: " + err);
    } finally {
      setLoading(false);
    }
  }

  function fileComplaint() {
    const text = claimText(receipt);
    navigate("/verify-page?prefill=" + encodeURIComponent(text));
  }

  function payAgain() {
    setReceipt(null);
    setError(null);
  }

  return (
    <>
      <Mesh />
      <div className="vheader">
        <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
          <a className="back" href="/">← Landing</a>
          <a className="brand" href="/">
            <span className="mark">◆</span>
            <span className="word">Ledger Oracle</span>
          </a>
        </div>
        <ThemeToggle />
      </div>

      <div className="vwrap">
        <div className="layout">
          <div className="panel">
            <div className="inner">
              {!receipt ? (
                <>
                  <h2>Make a payment</h2>
                  <p className="pay-lede">
                    This is a real write into the ledger — the same <code>captures</code> table the
                    investigation agent reads from. What happens next is the exact failure mode this
                    product exists to catch: the ledger will show your payment as settled, but your
                    own screen won't confirm it.
                  </p>

                  <label className="pay-label">Amount (₹)</label>
                  <input
                    className="pay-input"
                    type="number"
                    min="1"
                    step="0.01"
                    value={amount}
                    onChange={(e) => setAmount(e.target.value)}
                  />

                  <label className="pay-label">Instrument</label>
                  <select className="pay-select" value={instrument} onChange={(e) => setInstrument(e.target.value)}>
                    <option value="upi">UPI</option>
                    <option value="card">Card</option>
                    <option value="netbanking">Netbanking</option>
                  </select>

                  {error && <div className="pay-error">{error}</div>}

                  <button className="verify-btn" disabled={loading} onClick={pay}>
                    {loading && <span className="spinner"></span>}
                    {loading ? "PROCESSING…" : "PAY NOW"}
                  </button>
                </>
              ) : (
                <>
                  <div className="verdict-box verdict-block">
                    <div className="verdict-label">Payment failed</div>
                    <div className="verdict-summary">
                      We couldn't confirm this transaction on your account. If money was deducted,
                      file a complaint below — support can check it against our records.
                    </div>
                  </div>

                  <h2>Transaction details</h2>
                  <div className="field-row"><span className="k">amount</span><span className="v">{rupees(receipt.amount_paise)}</span></div>
                  <div className="field-row"><span className="k">order id</span><span className="v">{receipt.order_id}</span></div>
                  <div className="field-row"><span className="k">utr</span><span className="v">{receipt.utr}</span></div>
                  <div className="field-row"><span className="k">instrument</span><span className="v">{receipt.instrument}</span></div>
                  {receipt.payee_vpa && (
                    <div className="field-row"><span className="k">payee vpa</span><span className="v">{receipt.payee_vpa}</span></div>
                  )}
                  <div className="field-row"><span className="k">captured at</span><span className="v">{receipt.captured_at}</span></div>

                  <div className="pay-actions">
                    <button className="verify-btn" onClick={fileComplaint}>FILE A COMPLAINT →</button>
                    <button className="btn ghost pay-again" onClick={payAgain}>Make another payment</button>
                  </div>
                </>
              )}
            </div>
          </div>

          <div className="panel">
            <div className="inner">
              <h2>What happens next</h2>
              <ol className="pay-steps">
                <li>
                  <strong>Your payment settles for real.</strong> A genuine row lands in the
                  ledger's <code>captures</code> table — same table, same schema, as every other
                  payment in this system.
                </li>
                <li>
                  <strong>Your screen says it failed anyway.</strong> This is the exact gap Ledger
                  Oracle exists to catch — a payment the ledger confirms, but the customer's own
                  client never did.
                </li>
                <li>
                  <strong>You file a complaint with the real UTR.</strong> No fabricated demo
                  data — the investigation agent looks this reference up against the same row you
                  just created.
                </li>
                <li>
                  <strong>One refund, not infinite.</strong> The moment a claim on this reference
                  passes, it's consumed. Try filing the same complaint twice — the second attempt
                  blocks with <code>REF_ALREADY_CONSUMED</code>.
                </li>
              </ol>
              <span className="term">app.py — POST /pay/simulate, capped at ₹50,000/request</span>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
