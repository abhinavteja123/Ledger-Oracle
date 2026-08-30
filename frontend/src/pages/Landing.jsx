import { useEffect, useRef, useState } from "react";
import { Reveal, useReveal, ThemeToggle, Mesh } from "../components/Shared.jsx";
import "./Landing.css";

function Nav() {
  return (
    <nav className="top">
      <div className="wrap row">
        <a className="brand" href="/">
          <span className="mark">◆</span>
          <span className="word">Ledger Oracle</span>
        </a>
        <div className="navlinks">
          <a href="#problem">The Gap</a>
          <a href="#how">How It Decides</a>
          <a href="#abuse">Repeat-Abuse Guard</a>
          <a href="#money">On Money</a>
          <a href="#proof">Proof</a>
        </div>
        <ThemeToggle />
      </div>
    </nav>
  );
}

function Hero() {
  return (
    <header className="hero">
      <div className="wrap">
        <div className="kicker-row">
          <span className="kicker">
            <span className="dot"></span> Razorpay AI Buildathon 2026 — Track: AI Risk Manager
          </span>
        </div>
        <div className="hero-grid">
          <div>
            <h1 className="headline">
              The claim isn't <span className="strike">the fact</span>
              <br />
              the <span className="grad">ledger</span> is.
            </h1>
            <p className="sub">
              A customer says a payment happened. Every refund guardrail on the market checks
              whether that claim is <strong>allowed</strong> — the right amount, the right scope,
              the right approvals. Almost none of them check whether it's <strong>true</strong>.
              Ledger Oracle is the check that sits in front of that gap.
            </p>
            <div className="cta-row">
              <a className="btn primary" href="/verify-page">
                Submit a claim <span className="arrow-wrap">→</span>
              </a>
              <a className="btn ghost" href="/admin">
                Open the admin console <span className="arrow-wrap">→</span>
              </a>
            </div>
          </div>

          <Reveal className="dbz-outer exhibit">
            <div className="dbz-inner">
              <div className="bar">
                <span>Exhibit — passes every config guardrail</span>
                <span>real failure mode</span>
              </div>
              <div className="body">
                <div className="bubble customer">
                  <div className="role">Customer</div>
                  "Hi, I was charged twice for order #4471. Please refund one of them."
                </div>
                <div className="bubble verdict">
                  <div className="role">What a config-only guardrail sees</div>
                  Amount <code>₹2,499</code> is inside the refund limit. Scope, consent, PII — all
                  clear. <strong>Approved.</strong> There was one charge, not two — the refund pays
                  out anyway, because nothing upstream ever looked at the ledger.
                </div>
              </div>
            </div>
          </Reveal>
        </div>
      </div>
    </header>
  );
}

function Problem() {
  return (
    <section id="problem">
      <div className="wrap">
        <Reveal>
          <span className="eyebrow">
            <span className="dot"></span>01 — The Gap
          </span>
        </Reveal>
        <Reveal as="h2" className="title">
          Existing guardrails validate the action. Nobody validates the story.
        </Reveal>
        <Reveal as="p" className="lede">
          Agent guardrails today — amount ceilings, scope, PII handling, audit logging — all check
          the refund <strong>against the merchant's configuration</strong>. When the requested
          amount sits inside the limit and the underlying fact is false, every one of those
          guardrails passes it, because none of them ever looks at the payments ledger. Four
          major Indian PSPs have publicly written about this exact scam and shipped no detector
          for it — their own advice is "verify the UTR against your bank statement," precisely
          the check nothing in the stack automates.
        </Reveal>
        <Reveal className="gap-diagram">
          <span className="gap-node">Customer claim</span>
          <span className="gap-arrow">→</span>
          <span className="gap-node hole">nothing checks it against reality</span>
          <span className="gap-arrow">→</span>
          <span className="gap-node">Agent decides</span>
          <span className="gap-arrow">→</span>
          <span className="gap-node">Config guardrails</span>
          <span className="gap-arrow">→</span>
          <span className="gap-node danger">Money moves</span>
        </Reveal>
        <Reveal as="p" className="lede" style={{ marginBottom: 0 }}>
          Ledger Oracle sits in the hole in that diagram. It doesn't replace config guardrails or
          fraud-scoring vendors watching for serial abusers — it answers a narrower, upstream
          question those systems never ask: <strong>is this specific claim, about this specific
          order, actually reflected in the ledger?</strong>
        </Reveal>
      </div>
    </section>
  );
}

function How() {
  return (
    <section id="how">
      <div className="wrap">
        <Reveal>
          <span className="eyebrow">
            <span className="dot"></span>02 — How It Decides
          </span>
        </Reveal>
        <Reveal as="h2" className="title">
          The agent investigates. It never decides.
        </Reveal>
        <Reveal as="p" className="lede">
          Every claim goes through two halves with a hard wall between them. The first half is a
          bounded LLM agent that gathers evidence by calling read-only tools against the payments
          ledger. The second half is a plain deterministic function that looks at that evidence
          and returns a verdict. The model is never in the room when the decision gets made —
          structurally, not by convention.
        </Reveal>

        <Reveal className="flow">
          <div className="fnode agent">
            <div className="tag">Step 1 — Bounded Investigation Agent</div>
            <div className="name">agent.py</div>
            <div className="desc">
              Max 5 tool calls, max 2 retries per tool, 30s wall-clock budget — then it stops and
              escalates instead of hanging. It can only call the five tools below; nothing in its
              output types can express a decision.
            </div>
          </div>
          <div className="connector">↓ calls</div>
          <div className="flow-row tools">
            <div className="fnode tool">
              <div className="name">get_payment_by_utr</div>
              <div className="desc">look up a payment by UTR</div>
            </div>
            <div className="fnode tool">
              <div className="name">get_order_payments</div>
              <div className="desc">every payment tied to an order</div>
            </div>
            <div className="fnode tool">
              <div className="name">find_duplicate_captures</div>
              <div className="desc">was there really a second charge</div>
            </div>
            <div className="fnode tool">
              <div className="name">check_refund_history</div>
              <div className="desc">has this already been refunded</div>
            </div>
            <div className="fnode tool">
              <div className="name">check_payment_status</div>
              <div className="desc">captured, failed, or pending</div>
            </div>
          </div>
          <div className="connector">↓ evidence, typed and append-only</div>
          <div className="fnode engine">
            <div className="tag">Step 2 — Deterministic Policy Engine</div>
            <div className="name">policy.py — decide()</div>
            <div className="desc">
              Takes the evidence the agent collected and returns a verdict by plain comparison
              against the ledger — no LLM call, no network call, no randomness, in this function
              or anywhere it imports from. Its own docstring states the contract in four words:
              "DECIDES. No network. No model. No randomness."
            </div>
          </div>
        </Reveal>

        <Reveal className="verdicts">
          <div className="verdict-chip pass">
            <div className="v">Pass</div>
            <div className="d">Claim matches ledger evidence within tolerance.</div>
          </div>
          <div className="verdict-chip block">
            <div className="v">Block</div>
            <div className="d">Claim contradicts what the ledger shows.</div>
          </div>
          <div className="verdict-chip escalate">
            <div className="v">Escalate</div>
            <div className="d">Evidence is insufficient or in-flight — a human decides, not a guess.</div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

function AbuseGuard() {
  return (
    <section id="abuse">
      <div className="wrap">
        <Reveal>
          <span className="eyebrow">
            <span className="dot"></span>03 — Repeat-Claim Abuse Guard
          </span>
        </Reveal>
        <Reveal as="h2" className="title">
          Retrying is fine. Farming denials for a lucky verdict isn't.
        </Reveal>
        <Reveal as="p" className="lede">
          A determined bad actor doesn't need a clever exploit — they can just keep resubmitting
          the same denied order, hoping an ambiguous phrasing eventually slips a non-deterministic
          model into an approval. The engine now counts an order's own history: three prior claims
          that ended in a genuine terminal denial — <code>REF_NOT_IN_LEDGER</code>,{" "}
          <code>AMOUNT_MISMATCH</code>, <code>EXCEEDS_CAPTURED_TOTAL</code>, and similar — and the
          next automatic attempt on that order blocks outright, before a single tool call runs.
        </Reveal>
        <Reveal className="dbz-outer">
          <div className="dbz-inner callout">
            <h3>It only counts denials the customer wasn't invited to retry.</h3>
            <p>
              An in-flight settlement, a temporarily unavailable model, or a fat-fingered typo are
              all cases where <em>resubmitting is the correct, expected behavior</em> — the engine
              says so in its own reason text. None of those count against a customer. Only a real,
              terminal "no" — repeated three times on the same order — trips the guard.
            </p>
            <ul>
              <li>
                <strong>Deterministic, not a model call.</strong> The count is computed once in{" "}
                <code>app.py</code> and handed to <code>policy.decide()</code> as a plain integer —
                same pattern this codebase already uses for replay protection, so the policy engine
                itself still makes zero network calls.
              </li>
              <li>
                <strong>Keyed on order_id</strong>, not the claimed reference — duplicate-charge
                claims never carry one, and an attacker can vary the reference across attempts far
                more easily than the order they're actually targeting.
              </li>
              <li>
                <strong>Blocks, it doesn't merely escalate</strong> — three genuine denials on one
                order is a strong enough signal that a fourth automatic attempt shouldn't get a
                fresh roll of the dice.
              </li>
            </ul>
            <span className="term">policy.py — REPEATED_CLAIM_ABUSE, gated on config.ABUSE_REPEAT_THRESHOLD</span>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

function Money() {
  return (
    <section id="money">
      <div className="wrap">
        <Reveal>
          <span className="eyebrow">
            <span className="dot"></span>04 — On Money
          </span>
        </Reveal>
        <Reveal as="h2" className="title">
          The system that investigates refunds cannot move a rupee.
        </Reveal>
        <Reveal className="dbz-outer">
          <div className="dbz-inner callout">
            <h3>All five tools are read-only. There is no write path.</h3>
            <p>
              Every tool the agent can call is registered with <code>mutates=False</code>, and the
              connection they run against opens the ledger in <code>mode=ro</code> — a mutation
              isn't blocked by convention, it raises a database error. There's no{" "}
              <code>issue_refund</code> tool in the agent's allowlist at all; it was left out on
              purpose. This isn't a limitation of the demo — it's the safety argument.
            </p>
            <ul>
              <li>
                <strong>The agent gathers evidence.</strong> It cannot act on it.
              </li>
              <li>
                <strong>
                  The verdict carries <code>max_refundable_paise</code>
                </strong>{" "}
                — a bounded ceiling the engine authorizes for a human reviewer or a downstream
                payout system to act on, not a disbursement. Nothing on this page, or in this
                system, ever credits money.
              </li>
              <li>
                <strong>Escalate routes to a human queue</strong> — <code>/review</code> — and only
                an explicit human <code>approve</code> ever marks a claim's reference as consumed,
                closing the loop against replay.
              </li>
            </ul>
            <span className="term">
              models.py · Verdict.max_refundable_paise — "bounded money action; 0 on block/escalate"
            </span>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

function CountUp({ to, suffix = "", decimals = 0 }) {
  const [n, setN] = useState(0);
  const ref = useReveal();
  const done = useRef(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting && !done.current) {
            done.current = true;
            const start = performance.now();
            const dur = 1100;
            const step = (t) => {
              const p = Math.min(1, (t - start) / dur);
              const eased = 1 - Math.pow(1 - p, 3);
              setN(to * eased);
              if (p < 1) requestAnimationFrame(step);
            };
            requestAnimationFrame(step);
            io.unobserve(el);
          }
        });
      },
      { threshold: 0.4 }
    );
    io.observe(el);
    return () => io.disconnect();
  }, [to]);
  return (
    <span ref={ref}>
      {n.toFixed(decimals)}
      {suffix}
    </span>
  );
}

function Proof() {
  return (
    <section id="proof">
      <div className="wrap">
        <Reveal>
          <span className="eyebrow">
            <span className="dot"></span>05 — Proof, Not Promises
          </span>
        </Reveal>
        <Reveal as="h2" className="title">
          Numbers we checked before writing them down.
        </Reveal>
        <Reveal as="p" className="lede">
          A safety claim on a page like this is worthless if it's aspirational. These are current,
          from this session's test runs against this codebase — not a stale figure pulled from an
          earlier draft.
        </Reveal>
        <Reveal className="dbz-outer stats">
          <div className="stats" style={{ margin: 0 }}>
            <div className="dbz-inner stat">
              <div className="n">
                <CountUp to={0} />/1
              </div>
              <div className="l">
                <code>unsafe_pass_count</code> on the dev-set and a dedicated adversarial corpus —
                prompt injection, homoglyph &amp; zero-width UTR disguises, contradictory-amount
                claims — every case built to trick the engine into an unsafe pass.
              </div>
            </div>
            <div className="dbz-inner stat">
              <div className="n">
                <CountUp to={100} suffix="%" />
              </div>
              <div className="l">
                of tools in the agent's allowlist are read-only (<code>mutates=False</code>); the
                ledger connection itself opens read-only underneath them.
              </div>
            </div>
            <div className="dbz-inner stat">
              <div className="n plain">
                <CountUp to={136} />
              </div>
              <div className="l">
                automated tests in this repo, several asserting the architecture itself — e.g. that{" "}
                <code>policy.decide()</code> makes zero network calls across every pass/block/escalate
                path, including the repeat-abuse gate.
              </div>
            </div>
            <div className="dbz-inner stat">
              <div className="n plain">⛓</div>
              <div className="l">
                Every decision writes to a hash-chained, append-only audit log. Tamper with one
                field and <code>audit/verify.py</code> reports failure at the exact broken event —
                checkable live in the admin console.
              </div>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

function Demo() {
  return (
    <section id="demo" style={{ paddingBottom: 24 }}>
      <div className="wrap">
        <Reveal>
          <span className="eyebrow">
            <span className="dot"></span>06 — See It Run
          </span>
        </Reveal>
        <Reveal as="h2" className="title">
          No login. Pick a seat at the table.
        </Reveal>
        <Reveal className="cards">
          <a className="card user dbz-outer" href="/verify-page">
            <div className="dbz-inner" style={{ padding: 28 }}>
              <div className="idx">as the claimant</div>
              <div className="t">Submit a claim</div>
              <div className="d">
                Write a refund claim the way a customer would. Watch the agent investigate and the
                engine return pass, block, or escalate — with the full evidence trail behind the
                verdict.
              </div>
              <div className="go">
                Open /verify-page <span className="arrow-wrap">→</span>
              </div>
            </div>
          </a>
          <a className="card admin dbz-outer" href="/admin">
            <div className="dbz-inner" style={{ padding: 28 }}>
              <div className="idx">as the reviewer</div>
              <div className="t">Open the admin console</div>
              <div className="d">
                Browse every claim ever processed, inspect the raw ledger tables, check the audit
                chain's integrity, and compare the engine's verdicts against a human reviewer's.
              </div>
              <div className="go">
                Open /admin <span className="arrow-wrap">→</span>
              </div>
            </div>
          </a>
        </Reveal>
      </div>
    </section>
  );
}

function Footer() {
  return (
    <footer>
      <div className="wrap">
        <div className="footer-row">
          <span>Ledger Oracle — Razorpay AI Buildathon 2026</span>
          <span>agent investigates · engine decides · nothing moves money</span>
        </div>
        <p className="fine">
          Not a fraud score, not a chargeback responder, not an image-forensics tool, not an
          autonomous refunder. One narrow job: check a specific claim against the ledger before
          anything downstream is authorized to act on it.
        </p>
      </div>
    </footer>
  );
}

export default function Landing() {
  return (
    <>
      <Mesh />
      <Nav />
      <Hero />
      <Problem />
      <How />
      <AbuseGuard />
      <Money />
      <Proof />
      <Demo />
      <Footer />
    </>
  );
}
