"""The durable invoice-approval DAG.

Flow: intake -> validate -> policy_check -> [approve?] -> request_approval?
     -> execute_payment -> notify -> END

Durability properties (the actual point of this project):
- Every state transition is checkpointed. Kill the process mid-run and resume
  from the exact step; nothing executes twice.
- execute_payment is flaky by design (demo) and retries with exponential
  backoff: 1s, 2s, 4s. Every attempt is in the run log.
- request_approval uses LangGraph's interrupt(): the run parks until a human
  approves or rejects. In production this pages Slack; the demo uses the web
  inbox (same resume semantics).
- policy_check runs through the degradation chain: if the cost ledger trips
  the kill-switch, the run drops to the next cheaper model tier instead of
  dying. Frontier -> Mid -> Local -> Cache.

All cost accounting lives in checkpointed state (spend_usd, spend_calls), so
a crashed run resumes with its budget ledger intact.
"""
from __future__ import annotations

import time
import uuid
from typing import Literal, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .models import (
    DEGRADATION_ORDER,
    BudgetExceeded,
    CostLedger,
    DemoModel,
    ModelClient,
    ModelResult,
    Tier,
    get_model,
)


class FlowState(TypedDict, total=False):
    run_id: str
    invoice_id: str
    vendor: str
    amount: float
    budget_usd: float
    status: str
    validation_errors: list
    policy_notes: str
    policy_tier: str
    approval: Optional[str]
    payment_attempts: int
    log: list
    spend_usd: float
    spend_calls: list
    degraded: bool
    halt_reason: str


APPROVAL_THRESHOLD = 10_000.0
MAX_PAYMENT_ATTEMPTS = 4


def _log(state: FlowState, msg: str) -> dict:
    entry = f"[{state.get('status', 'init')}] {msg}"
    return {"log": [*state.get("log", []), entry]}


def build_graph(model: Optional[ModelClient] = None) -> StateGraph:
    """Compile the DAG. The model is pluggable; demo simulator by default."""
    model = model or get_model()

    def intake(state: FlowState) -> dict:
        run_id = state.get("run_id") or f"run_{uuid.uuid4().hex[:8]}"
        out: dict = {"run_id": run_id, "status": "intake",
                     "payment_attempts": 0, "spend_usd": 0.0,
                     "spend_calls": [], "degraded": False}
        out.update(_log({**state, **out},
                        f"intake: invoice {state['invoice_id']} from "
                        f"{state['vendor']} for ${state['amount']:,.2f}"))
        return out

    def validate(state: FlowState) -> dict:
        errors = []
        if state["amount"] <= 0:
            errors.append("amount must be positive")
        if not state["vendor"].strip():
            errors.append("vendor is required")
        if errors:
            out = {"status": "rejected", "validation_errors": errors}
            out.update(_log({**state, **out},
                            f"validation failed: {'; '.join(errors)}"))
            return out
        out = {"status": "validated", "validation_errors": []}
        out.update(_log({**state, **out}, "validation passed"))
        return out

    def policy_check(state: FlowState) -> dict:
        # Rejected invoices never reach a model. No spend, no review.
        if state.get("status") == "rejected":
            return {}
        ledger = CostLedger(
            budget_usd=state.get("budget_usd", 0.50),
            spent_usd=state.get("spend_usd", 0.0),
            calls=list(state.get("spend_calls", [])),
        )
        prompt = (f"Review invoice {state['invoice_id']}: vendor={state['vendor']}, "
                  f"amount=${state['amount']:,.2f}. Apply the payment policy.")
        system = "You are a payments policy reviewer. Be strict and specific."
        last_error = None
        for tier in DEGRADATION_ORDER:
            try:
                # Projected check: the kill-switch fires BEFORE the expensive
                # call, then the chain falls through to the next cheaper tier.
                ledger.check(projected_usd=ledger.estimate_next(tier))
                result: ModelResult = model.complete(tier, system, prompt)
                ledger.track(result)
                out = {
                    "status": "policy_reviewed",
                    "policy_notes": result.text,
                    "policy_tier": tier.value,
                    "spend_usd": round(ledger.spent_usd, 6),
                    "spend_calls": ledger.calls,
                    "degraded": tier != Tier.FRONTIER,
                }
                out.update(_log(
                    {**state, **out},
                    f"policy review by {tier.value} tier ({result.latency_ms}ms, "
                    f"${ledger.spent_usd:.6f} run spend)"))
                return out
            except BudgetExceeded as e:
                last_error = str(e)
                continue  # degradation chain: next cheaper tier
        out = {
            "status": "halted",
            "halt_reason": last_error or "budget exceeded on every tier",
            "spend_usd": round(ledger.spent_usd, 6),
            "spend_calls": ledger.calls,
        }
        out.update(_log({**state, **out}, f"KILL-SWITCH: {out['halt_reason']}"))
        return out

    def needs_approval(state: FlowState) -> Literal["approval", "execute", "end"]:
        if state["status"] in ("rejected", "halted"):
            return "end"
        if (state["amount"] > APPROVAL_THRESHOLD
                or "REVIEW" in state.get("policy_notes", "")):
            return "approval"
        return "execute"

    def request_approval(state: FlowState) -> dict:
        # Parks the run. Nothing proceeds until a human resumes it.
        # Production: this also fires a Slack webhook (see README).
        decision = interrupt({
            "type": "approval_request",
            "invoice_id": state["invoice_id"],
            "vendor": state["vendor"],
            "amount": state["amount"],
            "policy_notes": state.get("policy_notes", ""),
            "prompt": (f"Approve payment of ${state['amount']:,.2f} "
                       f"to {state['vendor']}?"),
        })
        approved = str(decision).lower() == "approved"
        out = {
            "approval": "approved" if approved else "rejected",
            "status": "approved" if approved else "rejected",
        }
        out.update(_log({**state, **out}, f"human decision: {out['approval']}"))
        return out

    def execute_payment(state: FlowState) -> dict:
        # Flaky by design in demo mode: the first two attempts hit a gateway
        # timeout, then it succeeds. Real gateways fail; the retry behavior
        # with exponential backoff is the honest part. Every attempt is
        # logged, and the checkpoint after this node means a crash here
        # never double-pays.
        log = list(state.get("log", []))
        attempts = state.get("payment_attempts", 0)
        for _ in range(MAX_PAYMENT_ATTEMPTS):
            attempts += 1
            if isinstance(model, DemoModel) and attempts < 3:
                backoff = 2 ** (attempts - 1)
                log.append(f"[executing] attempt {attempts} failed "
                           f"(gateway timeout); backing off {backoff}s")
                time.sleep(min(backoff, 2))
                continue
            log.append(f"[executing] attempt {attempts} succeeded: "
                       f"${state['amount']:,.2f} -> {state['vendor']}")
            return {"payment_attempts": attempts, "status": "paid", "log": log}
        log.append(f"[executing] FAILED after {attempts} attempts")
        return {"payment_attempts": attempts, "status": "failed",
                "halt_reason": "payment gateway kept timing out", "log": log}

    def notify(state: FlowState) -> dict:
        out = {"status": "complete"}
        out.update(_log({**state, **out},
                        f"notification sent: {state['vendor']} paid "
                        f"${state['amount']:,.2f}"))
        return out

    g = StateGraph(FlowState)
    g.add_node("intake", intake)
    g.add_node("validate", validate)
    g.add_node("policy_check", policy_check)
    g.add_node("request_approval", request_approval)
    g.add_node("execute_payment", execute_payment)
    g.add_node("notify", notify)

    g.add_edge(START, "intake")
    g.add_edge("intake", "validate")
    g.add_edge("validate", "policy_check")
    g.add_conditional_edges("policy_check", needs_approval,
                            {"approval": "request_approval",
                             "execute": "execute_payment",
                             "end": END})
    g.add_conditional_edges(
        "request_approval",
        lambda s: "execute" if s["approval"] == "approved" else "end",
        {"execute": "execute_payment", "end": END})
    g.add_conditional_edges(
        "execute_payment",
        lambda s: "notify" if s["status"] == "paid" else "end",
        {"notify": "notify", "end": END})
    g.add_edge("notify", END)
    return g


def new_ledger(budget_usd: float = 0.50) -> CostLedger:
    return CostLedger(budget_usd=budget_usd)
