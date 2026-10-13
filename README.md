# flowforge

Durable invoice-approval workflows on LangGraph. I built this because most
agent demos die the moment something real happens: the process crashes, the
payment API flakes, a $25k invoice needs a human to sign off, or the model
bill runs away. This handles all four.

Live demo: _(deploying)_

## The demo

Submit an invoice and watch it move through the graph: intake, validation,
policy review, payment, notification. The interesting part is breaking it:

* **$25k invoice** parks at a human approval gate (`interrupt()`). Approve or
  reject it from the inbox tab.
* **Flaky gateway** fails the payment twice, retries with backoff, succeeds on
  attempt 3. Every attempt shows up in the run log.
* **Simulate crash** drops the client mid-run and re-attaches by thread id.
  The run picks up from its checkpoint. Nothing runs twice.
* **Tiny budget ($0.000001)** trips the cost kill-switch before the expensive
  call. The run degrades Frontier to Mid to Local to Cache instead of dying.
* **Bad invoice** gets rejected at validation. Zero model calls, zero spend.

## How it is built

Checkpoints: every state transition persists. Set `REDIS_URL` (Upstash) and a
fresh serverless invocation resumes by `thread_id`. Without it, an in-memory
store keeps the demo working in one process.

Retries: a plain retry loop with exponential backoff inside the payment node,
so the backoff shows up in the run log instead of hiding in framework config.

Human in the loop: LangGraph `interrupt()` parks the run. The demo inbox
approves or rejects; the same hook fires a Slack webhook in production
(`SLACK_WEBHOOK_URL`).

Cost kill-switch: a ledger tracks projected spend per run. Before each model
call it checks spend plus projected cost of the next tier against the budget
(default $0.50). On breach it walks down the degradation chain. Cache tier is
free, so the chain always terminates.

Demo mode: no `LLM_API_KEY` means a deterministic policy engine stands in for
the model, with simulated token counts so cost accounting behaves the same.
Set `LLM_PROVIDER` and `LLM_API_KEY` for real models. The workflow code does
not change.

## Run it

```bash
pip install -r requirements.txt
# optional: REDIS_URL=rediss://...      Upstash, enables crash-resume across processes
# optional: SLACK_WEBHOOK_URL=https://...  approval notifications
# optional: LLM_PROVIDER=openai LLM_API_KEY=sk-...  real models, demo mode otherwise
uvicorn api.index:app --port 8000

npm install && npm run dev   # UI on :5173, proxies /api to :8000
```

Deploy: push to `main`. Vercel builds the UI and serves `api/index.py` as a
serverless function. Set `REDIS_URL` in the Vercel dashboard for durable
checkpoints in production.

## Numbers (demo mode, local)

* Small invoice, clean run: completes in ~4s, 3 payment attempts (2 forced
  retries), $0.000264 spend
* $25k invoice: parks at approval in ~1s, resumes after the human decides
* Tiny budget: degrades to cache tier, $0.00 spend, run completes
* Crash mid-run plus resume: resumes from checkpoint, payment executes once

## Layout

```
flowforge/        graph.py (the DAG), models.py (pluggable models, demo
                  simulator, cost ledger), checkpoints.py (Redis or memory)
api/index.py      FastAPI app, also the Vercel serverless entry
src/              React UI: DAG view, run log, approval inbox, crash button
docs/ADR-001.md   why LangGraph instead of Temporal
INTERVIEW.md      how I explain this in 60 seconds
```

Python, FastAPI, LangGraph, Redis (Upstash), React + Vite. No LangChain agent
abstractions. The DAG is explicit so every step is inspectable.
