export interface RunState {
  thread_id: string;
  status: string;
  run_id?: string;
  invoice_id?: string;
  vendor?: string;
  amount?: number;
  policy_notes?: string;
  policy_tier?: string;
  approval?: string;
  payment_attempts?: number;
  spend_usd: number;
  spend_calls: { tier: string; input_tokens: number; output_tokens: number; cost_usd: number }[];
  degraded: boolean;
  halt_reason?: string;
  log: string[];
  awaiting_approval?: {
    prompt: string;
    invoice_id?: string;
    vendor?: string;
    amount?: number;
    policy_notes?: string;
  };
  checkpoint_backend?: string;
}

export interface Preset {
  id: string;
  label: string;
  invoice_id: string;
  vendor: string;
  amount: number;
  budget_usd?: number;
  blurb: string;
}

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

export const api = {
  presets: () => fetch("/api/presets").then((r) => j<{ presets: Preset[] }>(r)),
  start: (p: Preset) =>
    fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        invoice_id: p.invoice_id,
        vendor: p.vendor,
        amount: p.amount,
        budget_usd: p.budget_usd ?? 0.5,
      }),
    }).then((r) => j<RunState>(r)),
  get: (thread_id: string) =>
    fetch(`/api/runs/${thread_id}`).then((r) => j<RunState>(r)),
  resume: (thread_id: string, decision: "approved" | "rejected") =>
    fetch(`/api/runs/${thread_id}/resume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision }),
    }).then((r) => j<RunState>(r)),
  health: () => fetch("/api/health").then((r) => j<{ checkpoint_backend: string; demo_mode: boolean }>(r)),
};
