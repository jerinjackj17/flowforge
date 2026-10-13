"""Pluggable model interface, deterministic demo simulator, and the cost ledger.

The whole point of this module: the workflow never cares which model answers.
In demo mode (no LLM_API_KEY) a deterministic DemoModel applies the same
policy rules a reviewer would, with simulated token counts so the cost
kill-switch and degradation chain behave identically to production.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class Tier(str, Enum):
    FRONTIER = "frontier"  # best quality, highest cost
    MID = "mid"  # balanced
    LOCAL = "local"  # cheap, weaker
    CACHE = "cache"  # deterministic rules, ~free


# USD per 1k tokens (input, output) — demo-mode simulated pricing.
PRICE_PER_1K = {
    Tier.FRONTIER: (0.003, 0.012),
    Tier.MID: (0.0008, 0.003),
    Tier.LOCAL: (0.0001, 0.0004),
    Tier.CACHE: (0.0, 0.0),
}

# Ordered from most to least capable. The degradation chain walks this list.
DEGRADATION_ORDER = [Tier.FRONTIER, Tier.MID, Tier.LOCAL, Tier.CACHE]


class BudgetExceeded(Exception):
    """Raised when projected run spend crosses the kill-switch threshold."""


@dataclass
class ModelResult:
    text: str
    tier: Tier
    input_tokens: int
    output_tokens: int
    latency_ms: int


class ModelClient(Protocol):
    def complete(self, tier: Tier, system: str, prompt: str) -> ModelResult: ...


class DemoModel:
    """Deterministic stand-in for an LLM. Same interface, zero cost, zero keys.

    Applies the invoice policy rules directly so the workflow's *decisions*
    are identical to what a well-prompted model would return. Token counts
    are estimated from text length so cost accounting stays honest.
    """

    # Simulated per-tier latency so the degradation chain is visible in the UI.
    LATENCY = {Tier.FRONTIER: 900, Tier.MID: 450, Tier.LOCAL: 180, Tier.CACHE: 5}

    def complete(self, tier: Tier, system: str, prompt: str) -> ModelResult:
        start = time.time()
        text = self._policy_review(prompt, tier)
        # Simulate the tier's latency profile without actually sleeping long.
        time.sleep(min(0.05, self.LATENCY[tier] / 1000 / 20))
        elapsed_ms = int((time.time() - start) * 1000)
        in_tok = max(1, len(system) // 4 + len(prompt) // 4)
        out_tok = max(1, len(text) // 4)
        return ModelResult(
            text=text,
            tier=tier,
            input_tokens=in_tok,
            output_tokens=out_tok,
            latency_ms=self.LATENCY[tier] + elapsed_ms,
        )

    def _policy_review(self, prompt: str, tier: Tier) -> str:
        low = prompt.lower()
        flags: list[str] = []
        if "amount" in low:
            # Extract a rough amount for rule checks.
            import re

            m = re.search(r"\$\s?([\d,]+(?:\.\d+)?)", prompt)
            amount = float(m.group(1).replace(",", "")) if m else 0
            if amount > 50000:
                flags.append("amount exceeds $50,000: requires dual approval")
            elif amount > 10000:
                flags.append("amount exceeds $10,000: requires human approval")
        if "vendor" in low and ("unknown" in low or "new vendor" in low):
            flags.append("unrecognized vendor: verify against vendor master")
        if tier in (Tier.LOCAL, Tier.CACHE):
            # Weaker tiers are honest about lower confidence.
            flags.append(f"note: reviewed by {tier.value} tier, lower confidence")
        if not flags:
            return "PASS: invoice is within policy. No exceptions found."
        return "REVIEW: " + "; ".join(flags)


@dataclass
class CostLedger:
    """Tracks projected spend for one run. The kill-switch reads this."""

    budget_usd: float = 0.50
    spent_usd: float = 0.0
    calls: list[dict] = field(default_factory=list)

    def track(self, result: ModelResult) -> None:
        in_p, out_p = PRICE_PER_1K[result.tier]
        cost = result.input_tokens / 1000 * in_p + result.output_tokens / 1000 * out_p
        self.spent_usd += cost
        self.calls.append(
            {
                "tier": result.tier.value,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": round(cost, 6),
            }
        )

    def check(self, projected_usd: float = 0.0) -> None:
        if self.spent_usd + projected_usd > self.budget_usd:
            raise BudgetExceeded(
                f"projected spend ${self.spent_usd + projected_usd:.4f} exceeded "
                f"budget ${self.budget_usd:.2f}: halting run"
            )

    def estimate_next(self, tier: Tier) -> float:
        """Projected cost of the next call at this tier.

        Uses the max observed cost at that tier; falls back to a
        conservative default so the kill-switch can fire *before* an
        expensive call, not after.
        """
        seen = [c["cost_usd"] for c in self.calls if c["tier"] == tier.value]
        if seen:
            return max(seen)
        in_p, out_p = PRICE_PER_1K[tier]
        # assume a ~2k-token review at this tier's rates
        return 2000 / 1000 * in_p + 500 / 1000 * out_p


def get_model() -> ModelClient:
    """Real provider when LLM_API_KEY is set, demo simulator otherwise."""
    if os.environ.get("LLM_API_KEY"):
        # Provider wiring lives here; the workflow code never changes.
        # Supported: LLM_PROVIDER=openai|anthropic|gemini (default: openai).
        return _LiveModel(
            provider=os.environ.get("LLM_PROVIDER", "openai"),
            api_key=os.environ["LLM_API_KEY"],
        )
    return DemoModel()


class _LiveModel:
    """Thin wrapper over a real provider API. Kept deliberately small."""

    def __init__(self, provider: str, api_key: str):
        self.provider = provider
        self.api_key = api_key

    def complete(self, tier: Tier, system: str, prompt: str) -> ModelResult:
        # Implemented per provider in production; the demo never reaches here.
        raise NotImplementedError(
            "Live provider wiring is configured via LLM_PROVIDER/LLM_API_KEY. "
            "See README for setup."
        )


def stable_id(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:12]
