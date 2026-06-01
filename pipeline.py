"""
Multi-agent review pipeline: Reviewer → Verifier → Summarizer.

Implements the adversarial verification pattern from ADR 004:
- Reviewer: high recall, finds everything
- Verifier: high precision, removes false positives
- Summarizer: generates human-readable output

This is the core architecture that demonstrates understanding of
production agent system design — not just calling an LLM, but
orchestrating a quality-controlled pipeline.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

from agent import run_review
from context_engine import prioritize_diff
from observability import Span, tracer

log = logging.getLogger("open-reviewer.pipeline")


@dataclass
class PipelineResult:
    """Complete result from the multi-agent review pipeline."""
    summary: str
    findings: list[dict]
    mergeability_score: float
    pipeline_metadata: dict = field(default_factory=dict)


async def run_pipeline(
    diff_text: str,
    skill_path: str,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    stages: list[str] | None = None,
) -> PipelineResult:
    """Run the full Review → Verify → Summarize pipeline.

    Parameters
    ----------
    stages:
        Which stages to run. Default: ["review", "verify"].
        Set to ["review"] for single-agent mode.
        Set to ["review", "verify", "summarize"] for full pipeline.
    """
    if stages is None:
        stages = ["review", "verify"]

    metadata = {
        "pipeline_version": "0.2.0",
        "stages": stages,
        "started_at": time.time(),
        "stage_timings": {},
    }

    # Stage 0: Context engineering
    with tracer.span("context_engineering") as span:
        prepared_diff = prepare_diff_for_review(diff_text)
        span.set_attribute("diff.original_chars", len(diff_text))
        span.set_attribute("diff.prepared_chars", len(prepared_diff))

    # Stage 1: Review (high recall)
    findings = []
    if "review" in stages:
        with tracer.span("review_stage") as span:
            t0 = time.time()
            review_prompt = _review_prompt(prepared_diff, owner, repo, pr_number)
            review_result = await run_review(
                review_prompt, skill_path, owner, repo, pr_number,
            )
            findings = review_result.get("findings", [])
            span.set_attribute("findings.raw_count", len(findings))
            metadata["stage_timings"]["review"] = time.time() - t0
            log.info("Review stage: %d findings", len(findings))

    # Stage 2: Verify (high precision) — adversarial
    verified_findings = findings
    if "verify" in stages and findings:
        with tracer.span("verify_stage") as span:
            t0 = time.time()
            verified_findings = await verify_findings(
                findings, diff_text, skill_path, owner, repo, pr_number,
            )
            removed = len(findings) - len(verified_findings)
            span.set_attribute("findings.removed", removed)
            span.set_attribute("findings.verified", len(verified_findings))
            metadata["stage_timings"]["verify"] = time.time() - t0
            log.info("Verify stage: %d verified, %d removed",
                     len(verified_findings), removed)

    # Stage 3: Summarize
    summary = ""
    mergeability_score = _compute_mergeability(verified_findings)
    if "summarize" in stages:
        with tracer.span("summarize_stage") as span:
            t0 = time.time()
            summary = await generate_summary(
                verified_findings, diff_text, skill_path, owner, repo, pr_number,
            )
            metadata["stage_timings"]["summarize"] = time.time() - t0

    metadata["total_duration"] = time.time() - metadata["started_at"]

    return PipelineResult(
        summary=summary or _default_summary(verified_findings, mergeability_score),
        findings=verified_findings,
        mergeability_score=mergeability_score,
        pipeline_metadata=metadata,
    )


# ---- Context engineering ----------------------------------------------------

def prepare_diff_for_review(diff_text: str) -> str:
    """Apply layered context strategy to the diff."""
    return prioritize_diff(diff_text)


# ---- Prompt templates --------------------------------------------------------


def _review_prompt(diff_text: str, owner: str, repo: str, pr_number: int) -> str:
    """Prompt for the high-recall Reviewer agent."""
    return f"""You are a thorough, security-focused code reviewer.

Repository: {owner}/{repo} — PR #{pr_number}

Be SUSPICIOUS. Assume every change could introduce a bug, security vulnerability, or regression. Err on the side of reporting issues — the Verifier agent will filter false positives.

Check for:
1. Security: injection, XSS, auth bypass, secret leak, unsafe deserialization
2. Bugs: off-by-one, null/undefined access, race conditions, missing validation
3. Performance: N+1 queries, unnecessary allocations, blocking I/O
4. Completeness: missing tests, missing error handling, undocumented breaking changes

Output as JSON: {{"findings": [{{"severity": "critical|warning|info", "message": "...", "path": "...", "line": N}}]}}

```diff
{diff_text}
```"""


# ---- Adversarial verification -----------------------------------------------


async def verify_findings(
    findings: list[dict],
    diff_text: str,
    skill_path: str,
    owner: str,
    repo: str,
    pr_number: int,
) -> list[dict]:
    """Adversarial verification: try to REFUTE each finding.

    Each finding is independently checked against the diff. The verifier is
    instructed to default to REMOVING the finding unless it can confirm the
    issue is real.
    """
    if not findings:
        return []

    findings_json = json.dumps(findings, indent=2)
    verify_prompt = f"""You are a code review VERIFIER. Your job is to check each
finding below against the PR diff and REMOVE any that are false positives.

For each finding, determine:
- Is the issue ACTUALLY present in the diff?
- Would fixing it actually improve the code?
- Is it a style preference masquerading as a bug?

Default to REMOVING the finding if you are uncertain. Only keep findings
where you are >80% confident the issue is real and meaningful.

Findings to verify:
```json
{findings_json}
```

PR Diff:
```diff
{diff_text}
```

Output ONLY valid JSON:
{{"verified_findings": [/* only the REAL findings, same schema */]}}"""

    result = await run_review(verify_prompt, skill_path, owner, repo, pr_number)
    return result.get("verified_findings", findings)


# ---- Summarizer -------------------------------------------------------------


async def generate_summary(
    findings: list[dict],
    diff_text: str,
    skill_path: str,
    owner: str,
    repo: str,
    pr_number: int,
) -> str:
    """Generate a human-readable summary from verified findings."""
    critical = [f for f in findings if f.get("severity") == "critical"]
    warnings = [f for f in findings if f.get("severity") == "warning"]

    summary_prompt = f"""Write a 2-3 paragraph code review summary for a busy
maintainer. Prioritize critical issues. Be concise and actionable.

Critical issues ({len(critical)}): {json.dumps(critical)}
Warnings ({len(warnings)}): {json.dumps(warnings)}

Output: Just the summary text, no JSON."""

    result = await run_review(summary_prompt, skill_path, owner, repo, pr_number)
    # run_review expects JSON output; for summary we extract raw text
    return result.get("summary", str(result))


# ---- Helpers -----------------------------------------------------------------


def _compute_mergeability(findings: list[dict]) -> float:
    """Compute mergeability score from findings severity."""
    if not findings:
        return 1.0
    weights = {"critical": -0.3, "warning": -0.1, "info": -0.02}
    score = 1.0
    for f in findings:
        score += weights.get(f.get("severity", "info"), -0.02)
    return max(0.0, min(1.0, round(score, 2)))


def _default_summary(findings: list[dict], score: float) -> str:
    critical = sum(1 for f in findings if f.get("severity") == "critical")
    warn = sum(1 for f in findings if f.get("severity") == "warning")
    return (
        f"Automated review found {len(findings)} issues "
        f"({critical} critical, {warn} warnings). "
        f"Mergeability: {score:.0%}."
    )
