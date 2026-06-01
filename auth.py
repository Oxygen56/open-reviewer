"""
API authentication and authorization.

Simple token-based auth for admin endpoints (/metrics, /reviews, /costs).
Production path: swap for OAuth2/OIDC (Google, GitHub App).

Interview talking point:
"The admin endpoints are protected by API key auth with constant-time
comparison to prevent timing attacks. For production, I'd swap this for
OAuth2 with GitHub App identity — that way, only repo admins can see
review stats for their repos. The current implementation is explicitly
documented as 'MVP auth' with a clear upgrade path."
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from functools import wraps
from typing import Callable

from fastapi import Request, HTTPException, status

log = logging.getLogger("open-reviewer.auth")

ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")


def verify_api_key(request: Request) -> bool:
    """Constant-time API key verification."""
    if not ADMIN_API_KEY:
        # No auth configured — allow all (development mode)
        log.warning("ADMIN_API_KEY not set — admin endpoints are unprotected")
        return True

    provided = request.headers.get("X-API-Key", "")
    if not provided:
        return False

    # Constant-time comparison to prevent timing attacks
    return hmac.compare_digest(provided, ADMIN_API_KEY)


def require_auth(func: Callable):
    """Decorator to require API key authentication on admin endpoints."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        # Extract request from args or kwargs
        request = None
        for arg in args:
            if isinstance(arg, Request):
                request = arg
                break
        if request is None:
            request = kwargs.get("request")

        if request and not verify_api_key(request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key. Set X-API-Key header.",
            )
        return await func(*args, **kwargs)
    return wrapper


# ---- Webhook IP allowlist (optional) ----------------------------------------

# GitHub webhook IP ranges (from https://api.github.com/meta)
# These change periodically — update from the API
GITHUB_HOOK_IPS = {
    "192.30.252.0/22", "185.199.108.0/22",
    "140.82.112.0/20", "143.55.64.0/20",
    "2a0a:a440::/29", "2606:50c0::/32",
}


def verify_github_ip(request: Request) -> bool:
    """Verify that the request comes from a GitHub webhook IP range."""
    # This is a simplified check — production should use proper CIDR matching
    forwarded = request.headers.get("X-Forwarded-For", "")
    if not forwarded:
        return True  # Can't verify, allow through
    # In production: match against GITHUB_HOOK_IPS with ipaddress module
    return True  # Placeholder — implement CIDR matching for production
