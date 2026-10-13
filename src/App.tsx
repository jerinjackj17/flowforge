import { useEffect, useRef, useState } from "react";
import { api, type Preset, type RunState } from "./api";
import "./styles.css";

const DAG: { id: string; label: string }[] = [
  { id: "intake", label: "intake" },
  { id: "validate", label: "validate" },
  { id: "policy_check", label: "policy review" },
  { id: "request_approval", label: "human approval" },
  { id: "execute_payment", label: "execute payment" },
  { id: "notify", label: "notify" },
];

const TERMINAL = new Set(["complete", "rejected", "failed", "halted"]);

function nodeState(id: string, run: RunState): "done" | "active" | "idle" | "skipped" {
  const log = run.log.join("\n");
  const saw = (s: string) => log.includes(s);
  switch (id) {
    case "intake":
      return "done";
    case "validate":
      return saw("[validated]") || saw("[rejected]") ? "done" : run.status === "intake" ? "active" : "idle";
    case "policy_check":
      if (saw("policy review by")) return "done";
      return run.status === "validated" ? "active" : "idle";
    case "request_approval":
      if (run.awaiting_approval) return "active";
      if (run.approval) return "done";
      return "skipped";
    case "execute_payment":
      if (run.status === "paid" || run.status === "complete" || saw("attempt") ) return saw("attempt") && run.status !== "complete" && run.status !== "paid" ? "active" : "done";
      return run.status === "approved" ? "active" : "idle";
    case "notify":
      return run.status === "complete" ? "done" : "idle";
    default:
      return "idle";
  }
}

function DagView({ run }: { run: RunState }) {
  return (
    <div className="dag">
      {DAG.map((n, i) => {
        const st = nodeState(n.id, run);
        return (
          <div key={n.id} className="dag-row">
            <div className={`dag-node ${st}`}>
              <span className="dag-dot" />
              {n.label}
              {n.id === "request_approval" && st === "skipped" && <em> (skipped)</em>}
            </div>
            {i < DAG.length - 1 && <div className="dag-link" />}
          </div>
        );
      })}
    </div>
  );
}

export default function App() {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [run, setRun] = useState<RunState | null>(null);
  const [busy, setBusy] = useState(false);
  const [crashed, setCrashed] = useState(false);
  const [health, setHealth] = useState<{ checkpoint_backend: string; demo_mode: boolean } | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    api.presets().then((p) => setPresets(p.presets)).catch(() => {});
    api.health().then(setHealth).catch(() => {});
  }, []);

  const poll = (thread_id: string) => {
    if (timer.current) window.clearInterval(timer.current);
    timer.current = window.setInterval(async () => {
      try {
        const s = await api.get(thread_id);
        setRun(s);
        if (TERMINAL.has(s.status)) {
          if (timer.current) window.clearInterval(timer.current);
        }
      } catch {
        /* run may not exist yet on a fresh backend */
      }
    }, 1200);
  };

  const start = async (p: Preset) => {
    setBusy(true);
    setCrashed(false);
    try {
      const s = await api.start(p);
      setRun(s);
      if (!TERMINAL.has(s.status)) poll(s.thread_id);
    } finally {
      setBusy(false);
    }
  };

  const resume = async (decision: "approved" | "rejected") => {
    if (!run) return;
    setBusy(true);
    try {
      const s = await api.resume(run.thread_id, decision);
      setRun(s);
      if (!TERMINAL.has(s.status)) poll(s.thread_id);
    } finally {
      setBusy(false);
    }
  };

  // "Crash" the client: drop everything, re-attach by thread_id only.
  // If the run continues from its checkpoint, durability is proven.
  const simulateCrash = async () => {
    if (!run) return;
    if (timer.current) window.clearInterval(timer.current);
    setCrashed(true);
    setRun(null);
    await new Promise((r) => setTimeout(r, 1500));
    const s = await api.get(run.thread_id);
    setRun(s);
    setCrashed(false);
    if (!TERMINAL.has(s.status)) poll(s.thread_id);
  };

  return (
    <div className="page">
      <header>
        <div>
          <h1>flowforge</h1>
          <p className="sub">durable agent workflows · checkpoints · retries · human approval · cost kill-switch</p>
        </div>
        {health && (
          <div className="health">
            <span className={`pill ${health.demo_mode ? "demo" : "live"}`}>
              {health.demo_mode ? "demo mode · no api key" : "live model"}
            </span>
            <span className="pill">checkpoints: {health.checkpoint_backend}</span>
          </div>
        )}
      </header>

      <section className="presets">
        <h2>1 · pick a scenario</h2>
        <div className="preset-grid">
          {presets.map((p) => (
            <button key={p.id} className="preset" disabled={busy} onClick={() => start(p)}>
              <strong>{p.label}</strong>
              <span>{p.blurb}</span>
            </button>
          ))}
        </div>
      </section>

      {crashed && <div className="crash">client crashed — re-attaching to the run by thread id…</div>}

      {run && (
        <>
          <section className="cols">
            <div>
              <h2>2 · the DAG, live</h2>
              <DagView run={run} />
            </div>
            <div>
              <h2>3 · run state</h2>
              <dl className="state">
                <div><dt>status</dt><dd><code>{run.status}</code></dd></div>
                <div><dt>invoice</dt><dd>{run.invoice_id} · {run.vendor} · ${Number(run.amount).toLocaleString()}</dd></div>
                <div><dt>policy tier</dt><dd>{run.policy_tier ?? "—"}{run.degraded && " · degraded"}</dd></div>
                <div><dt>spend</dt><dd>${run.spend_usd.toFixed(6)} of $0.50 budget</dd></div>
                <div><dt>payment attempts</dt><dd>{run.payment_attempts ?? "—"}</dd></div>
                {run.halt_reason && <div><dt>halt reason</dt><dd>{run.halt_reason}</dd></div>}
              </dl>
              {run.spend_calls.length > 0 && (
                <table className="calls">
                  <thead><tr><th>tier</th><th>in tok</th><th>out tok</th><th>cost</th></tr></thead>
                  <tbody>
                    {run.spend_calls.map((c, i) => (
                      <tr key={i}><td>{c.tier}</td><td>{c.input_tokens}</td><td>{c.output_tokens}</td><td>${c.cost_usd.toFixed(6)}</td></tr>
                    ))}
                  </tbody>
                </table>
              )}
              <div className="actions">
                <button onClick={simulateCrash} disabled={busy || TERMINAL.has(run.status)}>
                  simulate crash &amp; resume
                </button>
              </div>
            </div>
          </section>

          {run.awaiting_approval && (
            <section className="approval">
              <h2>human approval required</h2>
              <p>{run.awaiting_approval.prompt}</p>
              {run.awaiting_approval.policy_notes && (
                <p className="notes">{run.awaiting_approval.policy_notes}</p>
              )}
              <div className="actions">
                <button className="approve" disabled={busy} onClick={() => resume("approved")}>approve</button>
                <button className="reject" disabled={busy} onClick={() => resume("rejected")}>reject</button>
              </div>
            </section>
          )}

          <section>
            <h2>run log</h2>
            <pre className="log">{run.log.join("\n")}</pre>
          </section>
        </>
      )}

      <footer>
        <p>flowforge · langgraph + redis checkpoints · every transition persisted · <a href="https://github.com/jerinjackj17/flowforge">github</a></p>
      </footer>
    </div>
  );
}
