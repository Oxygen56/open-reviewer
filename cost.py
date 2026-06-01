"""
Cost tracking and token budget management.

Tracks per-review token usage and estimated cost across:
- Claude Agent SDK (model inference)
- GitHub API calls

Interview talking point:
"I instrumented token-level cost tracking. Each review records input/output
tokens, and the /costs endpoint shows per-repo spend over time. This lets
you answer 'how much does code review cost us per month?' — which is the
first question any production team asks about an AI service."
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("open-reviewer.cost")

# Claude model pricing per 1M tokens (as of mid-2026)
MODEL_PRICING = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "default": {"input": 3.00, "output": 15.00},
}

# GitHub API cost: $0 (free tier), but track calls for rate limit awareness
GITHUB_API_COST_PER_CALL = 0.0  # Free for public repos


@dataclass
class TokenUsage:
    """Token consumption for one model call."""
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = "claude-sonnet-4-6"
    duration_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def estimated_cost(self) -> float:
        pricing = MODEL_PRICING.get(self.model, MODEL_PRICING["default"])
        input_cost = (self.input_tokens / 1_000_000) * pricing["input"]
        output_cost = (self.output_tokens / 1_000_000) * pricing["output"]
        return round(input_cost + output_cost, 6)


@dataclass
class ReviewCost:
    """Aggregated cost for one review."""
    review_id: str
    started_at: float = field(default_factory=time.time)
    token_usages: list[TokenUsage] = field(default_factory=list)
    github_api_calls: int = 0
    pipeline_stage: str = "unknown"

    @property
    def total_tokens(self) -> int:
        return sum(u.total_tokens for u in self.token_usages)

    @property
    def total_cost(self) -> float:
        llm_cost = sum(u.estimated_cost for u in self.token_usages)
        api_cost = self.github_api_calls * GITHUB_API_COST_PER_CALL
        return round(llm_cost + api_cost, 6)

    @property
    def duration_ms(self) -> float:
        return (time.time() - self.started_at) * 1000

    def add_tokens(
        self,
        input_tokens: int,
        output_tokens: int,
        model: str = "claude-sonnet-4-6",
        duration_ms: float = 0.0,
    ) -> None:
        self.token_usages.append(TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            duration_ms=duration_ms,
        ))

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "total_tokens": self.total_tokens,
            "total_cost": self.total_cost,
            "token_usages": [
                {
                    "input": u.input_tokens,
                    "output": u.output_tokens,
                    "model": u.model,
                    "cost": u.estimated_cost,
                    "duration_ms": u.duration_ms,
                }
                for u in self.token_usages
            ],
            "github_api_calls": self.github_api_calls,
            "duration_ms": round(self.duration_ms, 0),
        }


# ---- Cost tracker for active reviews ----------------------------------------

_active_costs: dict[str, ReviewCost] = {}


def start_tracking(review_id: str, stage: str = "reviewer") -> ReviewCost:
    """Start tracking cost for a review job."""
    rc = ReviewCost(review_id=review_id, pipeline_stage=stage)
    _active_costs[review_id] = rc
    return rc


def stop_tracking(review_id: str) -> ReviewCost | None:
    """Stop tracking and return the final cost."""
    return _active_costs.pop(review_id, None)


def get_active_cost(review_id: str) -> ReviewCost | None:
    return _active_costs.get(review_id)


# ---- Projection --------------------------------------------------------------


def estimate_monthly_cost(
    reviews_per_day: int = 10,
    avg_tokens_per_review: int = 8_000,
    model: str = "claude-sonnet-4-6",
) -> dict[str, Any]:
    """Estimate monthly cost based on expected review volume."""
    daily_tokens = reviews_per_day * avg_tokens_per_review
    monthly_tokens = daily_tokens * 30
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["default"])
    # Assume 80% input, 20% output token split
    input_tokens = int(monthly_tokens * 0.8)
    output_tokens = int(monthly_tokens * 0.2)
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]

    return {
        "reviews_per_day": reviews_per_day,
        "avg_tokens_per_review": avg_tokens_per_review,
        "model": model,
        "estimated_monthly_cost": round(input_cost + output_cost, 2),
        "monthly_tokens": monthly_tokens,
        "input_cost": round(input_cost, 2),
        "output_cost": round(output_cost, 2),
    }
