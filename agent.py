"""
Claude Agent SDK integration for code review.

Uses the `claude_agent_sdk` package to spawn a stateless Claude Code agent
that runs the oss-pr-reviewer skill against a supplied PR diff and returns
structured JSON output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from typing import Any

from claude_agent_sdk import AgentConfig, AgentSession, ClaudeAgentSDK

log = logging.getLogger("open-reviewer.agent")

# The prompt we send to the agent.  The skill is loaded from the local
# repository path mounted in the container (SKILL_PATH).
_REVIEW_PROMPT_TEMPLATE = """\
Run the oss-pr-reviewer skill against the PR diff below.

Repository: {owner}/{repo}
PR Number: {pr_number}

```diff
{diff_text}
```

Output your review as **valid JSON only** (no markdown fences, no extra text)
with this exact schema:
{{
  "summary": "One or two paragraphs summarising the overall quality of the changes.",
  "findings": [
    {{
      "severity": "critical | warning | info",
      "message": "Human-readable description of the issue.",
      "path": "relative/file/path.py",
      "line": 42,
      "commit_id": null
    }}
  ],
  "mergeability_score": 0.85
}}

- "findings" may be empty if no issues are found.
- "mergeability_score" is a float between 0.0 (block) and 1.0 (merge cleanly).
- Only include "commit_id" and "line" if the finding relates to a specific
  location in a specific file.
"""


def _extract_json(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from *text*.

    Handles the case where the agent wraps the JSON in markdown fences or
    includes extra prose.
    """
    # Try to find a ```json … ``` block first.
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if m:
        candidate = m.group(1).strip()
    else:
        candidate = text.strip()

    # Walk backward from the end looking for a balanced JSON object.
    for i in range(len(candidate), 0, -1):
        snippet = candidate[:i]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            continue
    return None


async def run_review(
    diff_text: str,
    skill_path: str,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Run a Claude Code review via the Agent SDK.

    Parameters
    ----------
    diff_text:
        The unified diff of the PR to review.
    skill_path:
        Absolute path to the oss-pr-reviewer skill repository on disk.
    owner, repo, pr_number:
        PR metadata, included in the prompt for context.
    timeout_seconds:
        Maximum wall-clock time to wait for the agent to finish.

    Returns
    -------
    A dictionary with keys ``summary``, ``findings``, and
    ``mergeability_score``.
    """
    # Build the prompt
    prompt = _REVIEW_PROMPT_TEMPLATE.format(
        diff_text=_truncate_diff(diff_text, max_chars=50_000),
        owner=owner,
        repo=repo,
        pr_number=pr_number,
    )

    # Configure the SDK
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")

    sdk = ClaudeAgentSDK(api_key=api_key)

    config = AgentConfig(
        name="oss-pr-reviewer",
        skills=[skill_path],
        max_tokens=4096,
        timeout_seconds=timeout_seconds,
    )

    session: AgentSession | None = None
    try:
        session = await sdk.create_session(config=config)
        log.info("Agent session created: %s", session.id)

        result_text = await session.query(prompt)
        log.info(
            "Agent response received (%d chars)",
            len(result_text),
        )

        parsed = _extract_json(result_text)
        if parsed is None:
            log.warning(
                "Could not parse JSON from agent response.  Raw text: %.500s",
                result_text,
            )
            return {
                "summary": "Review agent returned unparseable output.",
                "findings": [
                    {
                        "severity": "warning",
                        "message": "Agent output could not be parsed as JSON.  "
                        "Falling back to raw text.  Please review the agent output manually.",
                        "path": None,
                        "line": None,
                        "commit_id": None,
                    }
                ],
                "mergeability_score": 0.0,
            }

        return parsed

    except asyncio.TimeoutError:
        log.error("Agent review timed out after %ds", timeout_seconds)
        return {
            "summary": "Review timed out.",
            "findings": [
                {
                    "severity": "warning",
                    "message": f"The review agent did not complete within {timeout_seconds}s.",
                    "path": None,
                    "line": None,
                    "commit_id": None,
                }
            ],
            "mergeability_score": 0.0,
        }

    except Exception:
        log.exception("Agent review failed")
        raise

    finally:
        if session is not None:
            try:
                await sdk.delete_session(session.id)
            except Exception:
                log.warning("Failed to clean up session %s", session.id)


def _truncate_diff(diff: str, max_chars: int = 50_000) -> str:
    """Truncate a diff to *max_chars* while keeping whole lines."""
    if len(diff) <= max_chars:
        return diff
    truncated = diff[:max_chars]
    # Cut at the last newline to avoid breaking a line
    last_nl = truncated.rfind("\n")
    if last_nl != -1:
        truncated = truncated[:last_nl]
    return truncated + "\n… (diff truncated due to size)"
