"""
MCP GitHub integration client.

Handles all GitHub API interactions:
- Fetching PR diffs
- Posting line-specific review comments (pull request review threads)
- Posting a summary PR comment (issue comment)

Uses either the GitHub MCP server (when available) or falls back to direct
HTTPS calls against the GitHub REST API.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger("open-reviewer.github")

GITHUB_API_BASE = "https://api.github.com"


class GitHubClient:
    """Thin async HTTP client for the GitHub REST API.

    Usage as an async context manager::

        async with GitHubClient(token="ghp_…") as gh:
            diff = await gh.fetch_diff("owner", "repo", 42)
    """

    def __init__(self, token: str) -> None:
        self._token = token
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "GitHubClient":
        headers = {
            "Accept": "application/vnd.github.v3.diff",
            "User-Agent": "open-reviewer/0.1.0",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._client = httpx.AsyncClient(headers=headers, follow_redirects=True)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # PR diff
    # ------------------------------------------------------------------

    async def fetch_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return the unified diff for *pr_number* as a raw string."""
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
        response = await self._request("GET", url)
        return response.text

    # ------------------------------------------------------------------
    # Review comments (line-specific, create a review thread)
    # ------------------------------------------------------------------

    async def post_review_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        commit_id: str | None = None,
        path: str | None = None,
        line: int | None = None,
    ) -> dict[str, Any]:
        """Post a pull request review comment.

        If *commit_id*, *path*, and *line* are all provided the comment is
        attached to a specific line in the diff.  Otherwise it is posted as a
        general PR comment.
        """
        # Re-create the client with the JSON media type for write operations
        async with self._write_client() as client:
            url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/comments"
            payload: dict[str, Any] = {"body": body}
            if commit_id:
                payload["commit_id"] = commit_id
            if path:
                payload["path"] = path
            if line is not None:
                payload["line"] = line
                payload["side"] = "RIGHT"

            response = await client.post(url, json=payload)
            if response.status_code not in (200, 201):
                log.warning(
                    "Failed to post review comment to %s/%s PR #%s: "
                    "HTTP %s %s",
                    owner,
                    repo,
                    pr_number,
                    response.status_code,
                    response.text[:300],
                )
            return response.json() if response.text else {}

    # ------------------------------------------------------------------
    # PR summary comment
    # ------------------------------------------------------------------

    async def post_pr_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> dict[str, Any]:
        """Post an issue-style (non-review) comment on the PR."""
        async with self._write_client() as client:
            url = (
                f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues"
                f"/{pr_number}/comments"
            )
            response = await client.post(url, json={"body": body})
            if response.status_code not in (200, 201):
                log.warning(
                    "Failed to post PR comment to %s/%s PR #%s: "
                    "HTTP %s %s",
                    owner,
                    repo,
                    pr_number,
                    response.status_code,
                    response.text[:300],
                )
            return response.json() if response.text else {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("GitHubClient not opened; use 'async with'")
        response = await self._client.request(method, url, **kwargs)
        if response.status_code == 401:
            raise PermissionError(
                "GitHub API returned 401 — check your GITHUB_TOKEN"
            )
        if response.status_code == 403:
            raise PermissionError(
                "GitHub API returned 403 — token may lack access or rate limit "
                "exceeded"
            )
        if response.status_code == 404:
            log.warning("GitHub 404 for %s %s", method, url)
        response.raise_for_status()
        return response

    def _write_client(self) -> httpx.AsyncClient:
        """Return an AsyncClient pre-configured for JSON write operations."""
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "open-reviewer/0.1.0",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return httpx.AsyncClient(headers=headers, follow_redirects=True)
