"""
Tests for the open-reviewer pipeline.

Tests are organised into three groups:
1. Unit tests for server helpers (signature verification, payload parsing)
2. Unit tests for the GitHub client
3. Integration-style test for the review agent (mocked SDK)
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from server import (
    _build_summary_body,
    _validate_review_result,
    app,
    parse_payload,
    verify_webhook_signature,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """FastAPI TestClient."""
    return TestClient(app)


@pytest.fixture
def sample_pr_payload():
    """A realistic GitHub pull_request webhook payload (opened)."""
    return {
        "action": "opened",
        "number": 42,
        "pull_request": {
            "number": 42,
            "diff_url": "https://api.github.com/repos/octocat/Hello-World/pulls/42",
            "head": {"sha": "abc123"},
        },
        "repository": {
            "full_name": "octocat/Hello-World",
        },
    }


@pytest.fixture
def sample_diff():
    """A minimal unified diff."""
    return """\
diff --git a/hello.py b/hello.py
index abc..def 100644
--- a/hello.py
+++ b/hello.py
@@ -1,3 +1,4 @@
 def greet(name):
-    print("Hello, " + name)
+    """Greet the user."""
+    print(f"Hello, {name}")
"""


# ---------------------------------------------------------------------------
# server.py — Webhook signature verification
# ---------------------------------------------------------------------------


class TestSignatureVerification:
    def test_valid_signature(self):
        secret = "my-secret"
        body = b'{"key": "value"}'
        expected = (
            "sha256="
            + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        )
        assert verify_webhook_signature(body, expected) is True

    def test_invalid_signature(self):
        assert verify_webhook_signature(b"{}", "sha256:deadbeef") is False

    def test_missing_signature(self):
        assert verify_webhook_signature(b"{}", None) is False

    def test_no_secret_configured(self, monkeypatch):
        monkeypatch.setattr("server.WEBHOOK_SECRET", "")
        assert verify_webhook_signature(b"{}", None) is True


# ---------------------------------------------------------------------------
# server.py — Payload parsing
# ---------------------------------------------------------------------------


class TestParsePayload:
    def test_opened_pr(self, sample_pr_payload):
        event = parse_payload(sample_pr_payload)
        assert event is not None
        assert event.action == "opened"
        assert event.owner == "octocat"
        assert event.repo == "Hello-World"
        assert event.pr_number == 42

    def test_synchronize(self, sample_pr_payload):
        sample_pr_payload["action"] = "synchronize"
        event = parse_payload(sample_pr_payload)
        assert event is not None
        assert event.action == "synchronize"

    def test_ignored_action(self, sample_pr_payload):
        sample_pr_payload["action"] = "closed"
        event = parse_payload(sample_pr_payload)
        assert event is None

    def test_missing_pull_request(self, sample_pr_payload):
        del sample_pr_payload["pull_request"]
        event = parse_payload(sample_pr_payload)
        assert event is None


# ---------------------------------------------------------------------------
# server.py — Validation & summary helpers
# ---------------------------------------------------------------------------


class TestValidation:
    def test_valid_result(self):
        result = {
            "summary": "Looks good.",
            "findings": [],
            "mergeability_score": 0.9,
        }
        _validate_review_result(result)  # should not raise
        assert result["mergeability_score"] == 0.9

    def test_coerce_score(self):
        result = {"mergeability_score": "0.75"}
        _validate_review_result(result)
        assert result["mergeability_score"] == 0.75

    def test_clamp_score_high(self):
        result = {"mergeability_score": 1.5}
        _validate_review_result(result)
        assert result["mergeability_score"] == 1.0

    def test_clamp_score_low(self):
        result = {"mergeability_score": -0.5}
        _validate_review_result(result)
        assert result["mergeability_score"] == 0.0

    def test_findings_coerced_to_list(self):
        result = {"findings": None, "mergeability_score": 0}
        _validate_review_result(result)
        assert result["findings"] == []


class TestSummaryBuilder:
    def test_empty_findings(self):
        result = {"summary": "", "findings": [], "mergeability_score": 1.0}
        body = _build_summary_body(result)
        assert "100%" in body
        assert "0" in body  # zero findings
        assert "Open Review" in body

    def test_with_findings(self):
        result = {
            "summary": "Some issues found.",
            "findings": [
                {"severity": "critical", "message": "Security issue", "path": "auth.py"},
                {"severity": "warning", "message": "Style nit", "path": "main.py"},
            ],
            "mergeability_score": 0.3,
        }
        body = _build_summary_body(result)
        assert "30%" in body
        assert "CRITICAL" in body
        assert "auth.py" in body
        assert "main.py" in body

    def test_score_formatted(self):
        result = {"summary": "", "findings": [], "mergeability_score": 0.33333}
        body = _build_summary_body(result)
        assert "33%" in body


# ---------------------------------------------------------------------------
# GitHub client tests
# ---------------------------------------------------------------------------


class TestGitHubClient:
    @pytest.mark.asyncio
    async def test_fetch_diff_success(self):
        from github_client import GitHubClient

        async with GitHubClient(token="test-token") as gh:
            gh._client = httpx.AsyncClient()  # override for test
            # Mock the internal _request method
            with patch.object(gh, "_request", new=AsyncMock()) as mock_req:
                mock_response = httpx.Response(200, text="diff content")
                mock_req.return_value = mock_response

                diff = await gh.fetch_diff("owner", "repo", 1)
                assert diff == "diff content"
                mock_req.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_post_review_comment(self):
        from github_client import GitHubClient

        gh = GitHubClient(token="test-token")
        # Use the write client directly with httpx mock
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_instance
            mock_response = httpx.Response(201, json={"id": 1})
            mock_instance.post.return_value = mock_response

            async with gh:
                result = await gh.post_review_comment(
                    owner="o", repo="r", pr_number=1, body="test"
                )
            assert result["id"] == 1


# ---------------------------------------------------------------------------
# Agent tests (mocked)
# ---------------------------------------------------------------------------


class TestAgent:
    @pytest.mark.asyncio
    async def test_run_review_success(self, sample_diff):
        from agent import run_review

        with (
            patch("agent.ClaudeAgentSDK") as MockSDK,
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
        ):
            mock_session = AsyncMock()
            mock_session.id = "sess-1"
            mock_session.query = AsyncMock(
                return_value=json.dumps(
                    {
                        "summary": "Good PR.",
                        "findings": [
                            {
                                "severity": "info",
                                "message": "Consider adding types.",
                                "path": "hello.py",
                                "line": 3,
                                "commit_id": None,
                            }
                        ],
                        "mergeability_score": 0.9,
                    }
                )
            )

            mock_sdk_instance = MockSDK.return_value
            mock_sdk_instance.create_session = AsyncMock(return_value=mock_session)

            result = await run_review(
                diff_text=sample_diff,
                skill_path="/fake/path",
                owner="octocat",
                repo="Hello-World",
                pr_number=42,
            )

            assert result["summary"] == "Good PR."
            assert len(result["findings"]) == 1
            assert result["mergeability_score"] == 0.9

    @pytest.mark.asyncio
    async def test_run_review_unparseable_output(self, sample_diff):
        from agent import run_review

        with (
            patch("agent.ClaudeAgentSDK") as MockSDK,
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
        ):
            mock_session = AsyncMock()
            mock_session.id = "sess-1"
            mock_session.query = AsyncMock(return_value="Just some text, no JSON")

            mock_sdk_instance = MockSDK.return_value
            mock_sdk_instance.create_session = AsyncMock(return_value=mock_session)

            result = await run_review(
                diff_text=sample_diff,
                skill_path="/fake/path",
                owner="octocat",
                repo="Hello-World",
                pr_number=42,
            )

            assert "unparseable" in result["summary"].lower()
            assert result["mergeability_score"] == 0.0


# ---------------------------------------------------------------------------
# Integration: webhook endpoint
# ---------------------------------------------------------------------------


class TestWebhookEndpoint:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_webhook_accepted(self, client, sample_pr_payload, monkeypatch):
        monkeypatch.setattr("server.WEBHOOK_SECRET", "")
        monkeypatch.setattr("server.process_review", AsyncMock())

        resp = client.post(
            "/webhook",
            json=sample_pr_payload,
            headers={"x-github-event": "pull_request"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "accepted"
        assert "job_id" in data

    def test_webhook_ignored_event(self, client, sample_pr_payload):
        resp = client.post(
            "/webhook",
            json=sample_pr_payload,
            headers={"x-github-event": "push"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_webhook_bad_signature(self, client, sample_pr_payload, monkeypatch):
        monkeypatch.setattr("server.WEBHOOK_SECRET", "real-secret")
        resp = client.post(
            "/webhook",
            json=sample_pr_payload,
            headers={
                "x-github-event": "pull_request",
                "x-hub-signature-256": "sha256:bad",
            },
        )
        assert resp.status_code == 401

    def test_review_status_not_found(self, client):
        resp = client.get("/review/owner/repo/99999")
        assert resp.status_code == 404
