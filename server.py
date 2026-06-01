"""
Open Reviewer — AI code review service.

Receives GitHub webhooks (PR opened / synchronize), delegates review to a
Claude Code agent running the oss-pr-reviewer skill, and posts the structured
results back as PR comments.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from agent import run_review
from auth import require_auth
from context_engine import prioritize_diff
from cost import ReviewCost, start_tracking, stop_tracking, estimate_monthly_cost
from github_client import GitHubClient
from observability import metrics, tracer
from pipeline import run_pipeline
from ratelimit import RateLimitExceeded, check_and_acquire, release_review_slot, get_status as rate_limit_status
from store import JobStore, JobRecord, store as job_store

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("open-reviewer")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
"""GitHub webhook secret used to validate HMAC-SHA256 signatures."""

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
"""Personal access token (classic or fine-grained) with repo / pull_requests
write scope."""

SKILL_PATH = os.environ.get(
    "SKILL_PATH",
    "/app/oss-pr-reviewer",
)
"""Path to the mounted oss-pr-reviewer skill repo."""

# ---------------------------------------------------------------------------
# In-memory job log (lightweight; replace with Redis in production)
# ---------------------------------------------------------------------------

_jobs: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Open Reviewer starting up")
    job_store.init()
    yield
    log.info("Open Reviewer shutting down")
    job_store.close()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Open Reviewer",
    description="AI code review service powered by Claude Code + oss-pr-reviewer",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def verify_webhook_signature(payload_body: bytes, signature_header: str | None) -> bool:
    """Validate x-hub-signature-256 against the webhook secret."""
    if not WEBHOOK_SECRET:
        log.warning("WEBHOOK_SECRET not set — skipping signature verification")
        return True
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(),
        payload_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@dataclass
class PREvent:
    action: str
    owner: str
    repo: str
    pr_number: int
    diff_url: str


def parse_payload(payload: dict[str, Any]) -> PREvent | None:
    """Extract PR metadata from a GitHub webhook payload."""
    action = payload.get("action", "")
    if action not in ("opened", "synchronize", "ready_for_review", "reopened"):
        return None

    pr = payload.get("pull_request")
    if not pr:
        return None

    repo_info = payload.get("repository", {})
    full_name = repo_info.get("full_name", "")
    owner, repo = full_name.split("/", 1) if "/" in full_name else ("", "")

    return PREvent(
        action=action,
        owner=owner,
        repo=repo,
        pr_number=pr["number"],
        diff_url=pr.get("diff_url", f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr['number']}"),
    )


async def process_review(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    head_sha: str = "",
    use_pipeline: bool = True,
) -> dict[str, Any]:
    """Run the full review pipeline for a PR.

    Production flow:
    1. Rate limit check (cooldown + concurrency)
    2. Idempotency check via SQLite (same SHA already reviewed?)
    3. Fetch diff, apply context engineering
    4. Multi-agent pipeline: Review → Verify
    5. Cost tracking per review
    6. Persist to SQLite job store
    7. Post comments, release rate limit slot
    """
    jid = f"{owner}/{repo}/pull/{pr_number}"

    # Rate limit check
    try:
        await check_and_acquire(owner, repo, pr_number)
    except RateLimitExceeded as e:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(e))

    # Start cost tracking
    cost_tracker = start_tracking(jid)

    # Persist job
    try:
        job_store.create_job(owner, repo, pr_number, head_sha=head_sha)
    except ValueError:
        release_review_slot()
        return {"status": "skipped", "reason": "SHA already reviewed (idempotency)"}

    job_store.update_job(jid, status="running")
    trace_id = str(uuid.uuid4())

    try:
        with tracer.trace(trace_id):
            async with GitHubClient(token=GITHUB_TOKEN) as gh:
                cost_tracker.github_api_calls += 1

                # 1. Fetch diff
                with tracer.span("fetch_diff") as span:
                    diff_text = await gh.fetch_diff(owner, repo, pr_number)
                    span.set_attribute("diff.chars", len(diff_text))
                    if not diff_text.strip():
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="PR diff is empty — nothing to review",
                        )

                # 2. Context engineering + review pipeline
                pipeline_result = await run_pipeline(
                    diff_text=diff_text,
                    skill_path=SKILL_PATH,
                    owner=owner,
                    repo=repo,
                    pr_number=pr_number,
                    stages=["review", "verify"] if use_pipeline else ["review"],
                )

                review_result = {
                    "summary": pipeline_result.summary,
                    "findings": pipeline_result.findings,
                    "mergeability_score": pipeline_result.mergeability_score,
                    "pipeline_metadata": pipeline_result.pipeline_metadata,
                }

                # Estimate tokens (SDK doesn't expose exact counts yet)
                estimated_tokens = len(diff_text) // 4 + len(str(review_result)) // 4
                cost_tracker.add_tokens(
                    input_tokens=estimated_tokens,
                    output_tokens=len(str(review_result)) // 4,
                    duration_ms=pipeline_result.pipeline_metadata.get("total_duration", 0) * 1000,
                )

                _validate_review_result(review_result)

                # 3. Post comments
                with tracer.span("post_comments") as span:
                    for finding in review_result.get("findings", []):
                        try:
                            await gh.post_review_comment(
                                owner=owner, repo=repo, pr_number=pr_number,
                                body=f"**{finding.get('severity', 'info').upper()}**: {finding.get('message', '')}",
                                commit_id=finding.get("commit_id"),
                                path=finding.get("path"), line=finding.get("line"),
                            )
                        except Exception as exc:
                            log.warning("Failed to post finding: %s", exc)

                    summary_body = _build_summary_body(review_result)
                    await gh.post_pr_comment(
                        owner=owner, repo=repo, pr_number=pr_number, body=summary_body,
                    )
                    cost_tracker.github_api_calls += len(review_result.get("findings", [])) + 1
                    span.set_attribute("comments.posted", len(review_result.get("findings", [])) + 1)

                # 4. Persist
                job_store.update_job(
                    jid, status="completed", result=review_result,
                    cost_tokens=cost_tracker.total_tokens,
                    cost_dollars=cost_tracker.total_cost,
                )
                if head_sha:
                    job_store.save_review_history(
                        owner, repo, pr_number, head_sha, review_result,
                    )

                # 5. Record metrics
                duration_ms = (time.time() - cost_tracker.started_at) * 1000
                metrics.record_review(
                    duration_ms=duration_ms,
                    stage_timings=pipeline_result.pipeline_metadata.get("stage_timings", {}),
                    finding_count=len(pipeline_result.findings),
                    success=True,
                )

        return review_result

    except HTTPException:
        job_store.update_job(jid, status="failed", error="HTTP error")
        metrics.record_review(0, {}, 0, success=False)
        raise
    except Exception as exc:
        job_store.update_job(jid, status="failed", error=str(exc))
        metrics.record_review(0, {}, 0, success=False)
        raise
    finally:
        stop_tracking(jid)
        await release_review_slot()


def _validate_review_result(result: dict[str, Any]) -> None:
    """Ensure the review result has the expected structure."""
    for key in ("summary", "findings", "mergeability_score"):
        if key not in result:
            log.warning("Review result missing key: %s", key)
    if not isinstance(result.get("findings"), list):
        log.warning("Review result 'findings' is not a list; coercing")
        result["findings"] = []
    try:
        score = float(result.get("mergeability_score", 0))
        result["mergeability_score"] = max(0.0, min(1.0, score))
    except (TypeError, ValueError):
        result["mergeability_score"] = 0.0


def _build_summary_body(result: dict[str, Any]) -> str:
    """Build a Markdown summary comment from the review result."""
    score = result.get("mergeability_score", 0)
    findings = result.get("findings", [])
    critical = sum(1 for f in findings if f.get("severity") == "critical")
    warn = sum(1 for f in findings if f.get("severity") == "warning")
    info = sum(1 for f in findings if f.get("severity") in ("info", "suggestion"))

    lines = [
        "## Open Review — AI Code Review",
        "",
        f"**Mergeability Score**: {score:.0%}",
        "",
        "### Findings Summary",
        f"- Critical: {critical}",
        f"- Warnings: {warn}",
        f"- Suggestions: {info}",
        "",
    ]

    summary_text = result.get("summary", "")
    if summary_text:
        lines.append("### Summary")
        lines.append("")
        lines.append(summary_text)
        lines.append("")

    if findings:
        lines.append("### Detailed Findings")
        lines.append("")
        lines.append("| Severity | File | Message |")
        lines.append("| --- | --- | --- |")
        for f in findings:
            path = f.get("path", "N/A")
            msg = f.get("message", "")
            sev = f.get("severity", "info").upper()
            lines.append(f"| {sev} | {path} | {msg} |")
        lines.append("")

    lines.append("---")
    lines.append("_Powered by [Open Reviewer](https://github.com/Oxygen56/open-reviewer)_")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """Health-check endpoint."""
    return {"status": "ok", "version": "0.1.0"}


@app.post("/webhook")
async def webhook(request: Request):
    """Receive GitHub webhook events for pull_request actions."""
    payload_body = await request.body()
    sig = request.headers.get("x-hub-signature-256")
    if not verify_webhook_signature(payload_body, sig):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    event = request.headers.get("x-github-event", "")
    if event != "pull_request":
        return {"status": "ignored", "reason": f"unsupported event: {event}"}

    payload = json.loads(payload_body)
    pr_event = parse_payload(payload)
    if pr_event is None:
        return {"status": "ignored", "reason": f"uninteresting action: {payload.get('action')}"}

    log.info(
        "Webhook received: %s/%s PR #%d (%s)",
        pr_event.owner,
        pr_event.repo,
        pr_event.pr_number,
        pr_event.action,
    )

    # Fire-and-forget: return 202 immediately, process in background.
    # For production, use a proper task queue (Celery / Redis Queue /…).
    import asyncio

    asyncio.ensure_future(
        process_review(pr_event.owner, pr_event.repo, pr_event.pr_number)
    )

    return {
        "status": "accepted",
        "job_id": _job_id(pr_event.owner, pr_event.repo, pr_event.pr_number),
    }


@app.post("/review/{owner}/{repo}/{pr_number}")
async def manual_review(owner: str, repo: str, pr_number: int):
    """Manually trigger a review for a given PR.

    This is useful for testing or re-reviewing a PR without triggering a new
    webhook event.
    """
    log.info("Manual review triggered for %s/%s PR #%d", owner, repo, pr_number)
    result = await process_review(owner, repo, pr_number)
    return {"status": "completed", "result": result}


@app.get("/review/{owner}/{repo}/{pr_number}")
async def review_status(owner: str, repo: str, pr_number: int):
    """Check the status of a review job."""
    jid = _job_id(owner, repo, pr_number)
    job = _jobs.get(jid)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No review found")
    return {"job_id": jid, **job}


@app.get("/reviews")
async def list_reviews():
    """List all tracked review jobs (in-memory; limited to this process)."""
    return {"reviews": _jobs}


@app.get("/metrics")
async def get_metrics():
    """Observability: pipeline performance metrics."""
    return metrics.summary()


@app.get("/admin/stats")
@require_auth
async def admin_stats():
    """Protected: aggregate job statistics from SQLite store."""
    return job_store.get_stats()


@app.get("/admin/jobs")
@require_auth
async def admin_jobs(status: str | None = None):
    """Protected: list recent jobs."""
    return {"jobs": [r.__dict__ for r in job_store.list_jobs(status=status)]}


@app.get("/admin/ratelimits")
@require_auth
async def admin_ratelimits():
    """Protected: current rate limit status."""
    return rate_limit_status()


@app.get("/admin/costs")
@require_auth
async def admin_costs():
    """Protected: cost summary."""
    stats = job_store.get_stats()
    return {
        "total_cost_dollars": stats["total_cost_dollars"],
        "total_tokens": stats["total_tokens"],
        "estimated_monthly": estimate_monthly_cost(),
    }


@app.get("/admin/jobs/{jid}")
@require_auth
async def admin_job_detail(jid: str):
    """Protected: single job detail."""
    try:
        job = job_store.get_job(jid)
        return job.__dict__
    except KeyError:
        raise HTTPException(status_code=404, detail="Job not found")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("server:app", host=host, port=port, reload=False, log_level="info")
