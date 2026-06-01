"""
Rate limiting: per-repo cooldown and global concurrency control.

Prevents:
- Webhook floods from overwhelming the agent (multiple commits pushed at once)
- Duplicate reviews for the same PR within the cooldown window
- Cost overruns from runaway webhook triggers

Interview talking point:
"Rate limiting matters more for AI services than traditional APIs because
each request costs actual money (LLM tokens). I implemented per-repo cooldown
and global concurrency caps — when a user pushes 5 commits in 2 minutes, only
the last one triggers a review."
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any

log = logging.getLogger("open-reviewer.ratelimit")

# ---- Configuration -----------------------------------------------------------

# Minimum seconds between reviews for the same PR
PER_PR_COOLDOWN_SECONDS = 120  # 2 min

# Maximum concurrent reviews across all repos
MAX_CONCURRENT_REVIEWS = 3

# ---- State -------------------------------------------------------------------

# Last review time per PR: key = "owner/repo/pr"
_last_review_times: dict[str, float] = {}

# Active review count
_active_count = 0
_active_lock = asyncio.Lock()


# ---- Rate limit checking -----------------------------------------------------


def check_pr_cooldown(owner: str, repo: str, pr_number: int) -> float | None:
    """Check if this PR is within the cooldown window.

    Returns:
        Seconds remaining in cooldown, or None if clear to review.
    """
    key = f"{owner}/{repo}/{pr_number}"
    last_time = _last_review_times.get(key, 0)
    elapsed = time.time() - last_time
    remaining = PER_PR_COOLDOWN_SECONDS - elapsed
    if remaining > 0:
        return remaining
    return None


def record_pr_review(owner: str, repo: str, pr_number: int) -> None:
    """Record that a review was started for this PR."""
    key = f"{owner}/{repo}/{pr_number}"
    _last_review_times[key] = time.time()


async def acquire_review_slot() -> bool:
    """Try to acquire a concurrent review slot.

    Returns True if a slot was acquired, False if at capacity.
    """
    global _active_count
    async with _active_lock:
        if _active_count >= MAX_CONCURRENT_REVIEWS:
            return False
        _active_count += 1
        return True


async def release_review_slot() -> None:
    """Release a concurrent review slot."""
    global _active_count
    async with _active_lock:
        _active_count = max(0, _active_count - 1)


class RateLimitExceeded(Exception):
    """Raised when a review request exceeds rate limits."""
    pass


async def check_and_acquire(
    owner: str, repo: str, pr_number: int,
) -> None:
    """Combined check: cooldown + concurrency. Raises RateLimitExceeded if blocked."""
    cooldown = check_pr_cooldown(owner, repo, pr_number)
    if cooldown is not None:
        raise RateLimitExceeded(
            f"PR #{pr_number} in cooldown ({cooldown:.0f}s remaining)"
        )

    acquired = await acquire_review_slot()
    if not acquired:
        raise RateLimitExceeded(
            f"At concurrency limit ({MAX_CONCURRENT_REVIEWS}). Try again later."
        )

    record_pr_review(owner, repo, pr_number)


def get_status() -> dict[str, Any]:
    """Get current rate limit status."""
    return {
        "active_reviews": _active_count,
        "max_concurrent": MAX_CONCURRENT_REVIEWS,
        "per_pr_cooldown_seconds": PER_PR_COOLDOWN_SECONDS,
        "tracked_prs": len(_last_review_times),
    }
