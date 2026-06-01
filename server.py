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
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from agent import run_review
from github_client import GitHubClient

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


def _job_id(owner: str, repo: str, pr_number: int) -> str:
    return f"{owner}/{repo}/pull/{pr_number}"


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Open Reviewer starting up")
    yield
    log.info("Open Reviewer shutting down")


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


async def process_review(owner: str, repo: str, pr_number: int) -> dict[str, Any]:
    """Run the full review pipeline for a PR.

    Steps
    -----
    1. Fetch the unified diff via GitHub API.
    2. Run the Claude Code agent with the oss-pr-reviewer skill.
    3. Parse the structured JSON output.
    4. Post review comments and summary back to the PR.
    """
    jid = _job_id(owner, repo, pr_number)
    _jobs[jid] = {"status": "running", "result": None}
    log.info("Starting review for %s", jid)

    try:
        async with GitHubClient(token=GITHUB_TOKEN) as gh:
            # 1. Fetch diff
            log.info("Fetching diff for %s", jid)
            diff_text = await gh.fetch_diff(owner, repo, pr_number)
            if not diff_text.strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="PR diff is empty — nothing to review",
                )

            # 2. Run review via Claude Code agent
            log.info("Running Claude Code review for %s (diff size: %d bytes)", jid, len(diff_text))
            review_result = await run_review(
                diff_text=diff_text,
                skill_path=SKILL_PATH,
                owner=owner,
                repo=repo,
                pr_number=pr_number,
            )

            # 3. Validate structured output
            _validate_review_result(review_result)

            # 4. Post comments
            log.info("Posting review results for %s", jid)
            for finding in review_result.get("findings", []):
                try:
                    await gh.post_review_comment(
                        owner=owner,
                        repo=repo,
                        pr_number=pr_number,
                        body=f"**{finding.get('severity', 'info').upper()}**: {finding.get('message', '')}",
                        commit_id=finding.get("commit_id"),
                        path=finding.get("path"),
                        line=finding.get("line"),
                    )
                except Exception as exc:
                    log.warning("Failed to post individual finding: %s", exc)

            summary_body = _build_summary_body(review_result)
            await gh.post_pr_comment(
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                body=summary_body,
            )

        _jobs[jid] = {"status": "completed", "result": review_result}
        log.info("Review completed for %s", jid)
        return review_result

    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Review failed for %s", jid)
        _jobs[jid] = {"status": "failed", "error": str(exc)}
        raise


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


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("server:app", host=host, port=port, reload=False, log_level="info")
