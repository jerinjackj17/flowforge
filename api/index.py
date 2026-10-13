"""flowforge API (FastAPI, served as a Vercel Python serverless function).

Endpoints:
  POST /api/runs                 start a run {invoice_id, vendor, amount, budget_usd?}
  GET  /api/runs/{thread_id}     poll full state; includes approval prompt when parked
  POST /api/runs/{thread_id}/resume  {decision: "approved"|"rejected"}
  GET  /api/presets              demo invoices (small, big, invalid, tiny-budget)

Durability note: every invoke is checkpointed. On Vercel each request is a
fresh process; with REDIS_URL set the run resumes from Redis by thread_id.
The "simulate crash" button in the UI just drops the client and re-attaches:
if the run continues from its checkpoint, durability is proven.
"""
from __future__ import annotations

import os
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from flowforge.checkpoints import get_checkpointer
from flowforge.graph import build_graph
from flowforge.models import get_model

app = FastAPI(title="flowforge")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PRESETS = [
    {"id": "small", "label": "Routine invoice · $480",
     "invoice_id": "INV-2041", "vendor": "Acme Cloud", "amount": 480.0,
     "blurb": "Under the approval threshold. Should sail through untouched."},
    {"id": "big", "label": "Big invoice · $25,000",
     "invoice_id": "INV-2042", "vendor": "Acme Cloud", "amount": 25000.0,
     "blurb": "Over $10k: parks at a human approval gate."},
    {"id": "sketchy", "label": "Sketchy invoice · $64,000, new vendor",
     "invoice_id": "INV-2043", "vendor": "New vendor LLC", "amount": 64000.0,
     "blurb": "Policy flags fire. Watch the review notes."},
    {"id": "invalid", "label": "Invalid invoice",
     "invoice_id": "INV-2044", "vendor": "", "amount": -50.0,
     "blurb": "Fails validation. Rejected before any model is called ($0 spend)."},
    {"id": "tiny-budget", "label": "Tiny budget · $0.000001",
     "invoice_id": "INV-2045", "vendor": "Acme Cloud", "amount": 480.0,
     "budget_usd": 0.000001,
     "blurb": "Kill-switch chaos: the run degrades Frontier → Mid → Local → Cache."},
]


class StartRun(BaseModel):
    invoice_id: str = Field(min_length=1)
    vendor: str = ""
    amount: float
    budget_usd: float = 0.50


class ResumeRun(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")


def _compile():
    checkpointer, backend = get_checkpointer()
    graph = build_graph(get_model()).compile(checkpointer=checkpointer)
    return graph, backend


def _serialize(result: dict) -> dict:
    """Strip interrupts into a clean API shape."""
    interrupts = result.pop("__interrupt__", None)
    out = {
        "status": result.get("status", "unknown"),
        "run_id": result.get("run_id"),
        "invoice_id": result.get("invoice_id"),
        "vendor": result.get("vendor"),
        "amount": result.get("amount"),
        "policy_notes": result.get("policy_notes"),
        "policy_tier": result.get("policy_tier"),
        "approval": result.get("approval"),
        "payment_attempts": result.get("payment_attempts"),
        "spend_usd": result.get("spend_usd", 0.0),
        "spend_calls": result.get("spend_calls", []),
        "degraded": result.get("degraded", False),
        "halt_reason": result.get("halt_reason"),
        "log": result.get("log", []),
        "awaiting_approval": None,
    }
    if interrupts:
        payload = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
        out["awaiting_approval"] = payload
        out["status"] = "awaiting_approval"
    return out


@app.get("/api/presets")
def presets():
    return {"presets": PRESETS}


@app.post("/api/runs")
def start_run(body: StartRun):
    graph, backend = _compile()
    thread_id = f"th_{uuid.uuid4().hex[:10]}"
    cfg = {"configurable": {"thread_id": thread_id}}
    result = graph.invoke(
        {"invoice_id": body.invoice_id, "vendor": body.vendor,
         "amount": body.amount, "budget_usd": body.budget_usd},
        cfg,
    )
    out = _serialize(dict(result))
    out["thread_id"] = thread_id
    out["checkpoint_backend"] = backend
    return out


@app.get("/api/runs/{thread_id}")
def get_run(thread_id: str):
    graph, backend = _compile()
    cfg = {"configurable": {"thread_id": thread_id}}
    try:
        snap = graph.get_state(cfg)
    except Exception:
        raise HTTPException(404, "unknown run")
    values = dict(snap.values) if snap.values else {}
    if not values:
        raise HTTPException(404, "unknown run")
    out = _serialize(values)
    # Parked at the approval gate?
    if snap.next and "request_approval" in snap.next:
        out["status"] = "awaiting_approval"
        out["awaiting_approval"] = {
            "type": "approval_request",
            "invoice_id": values.get("invoice_id"),
            "vendor": values.get("vendor"),
            "amount": values.get("amount"),
            "policy_notes": values.get("policy_notes"),
            "prompt": (f"Approve payment of ${values.get('amount', 0):,.2f} "
                       f"to {values.get('vendor')}?"),
        }
    out["thread_id"] = thread_id
    out["checkpoint_backend"] = backend
    return out


@app.post("/api/runs/{thread_id}/resume")
def resume_run(thread_id: str, body: ResumeRun):
    from langgraph.types import Command

    graph, backend = _compile()
    cfg = {"configurable": {"thread_id": thread_id}}
    try:
        result = graph.invoke(Command(resume=body.decision), cfg)
    except Exception as e:
        raise HTTPException(409, f"cannot resume: {e}")
    out = _serialize(dict(result))
    out["thread_id"] = thread_id
    out["checkpoint_backend"] = backend
    return out


@app.get("/api/health")
def health():
    _, backend = get_checkpointer()
    return {"ok": True, "checkpoint_backend": backend,
            "demo_mode": not bool(os.environ.get("LLM_API_KEY"))}
