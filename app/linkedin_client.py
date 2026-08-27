"""Thin client around LinkedIn's internal "Voyager" API.

LinkedIn's public profile pages are rendered client-side; the same data is
fetched by the browser from undocumented internal REST endpoints
(`/voyager/api/...`). This module talks to those endpoints directly using an
authenticated session cookie, which is what "reverse engineering the
LinkedIn API" means in practice — there is no official public API for this.

This is unofficial and unsupported by LinkedIn. It relies on cookie-based
session auth (li_at + JSESSIONID) obtained by the operator logging into
linkedin.com in a real browser — see README for how to grab them. Automated
username/password login is deliberately NOT implemented: it reliably
triggers LinkedIn's checkpoint/CAPTCHA flow when done from a server, and
repeated failed attempts risk the account being restricted.
"""

import asyncio
import re
import time
from typing import Any, Dict

import httpx

from app.config import Settings

VOYAGER_BASE = "https://www.linkedin.com/voyager/api"

_PUBLIC_ID_RE = re.compile(
    r"linkedin\.com/in/([^/?#]+)", re.IGNORECASE
)


class LinkedInAuthError(Exception):
    """Raised when the configured session cookies are missing/expired."""


class LinkedInProfileNotFound(Exception):
    """Raised when LinkedIn returns 404 for the requested profile."""


class LinkedInRateLimited(Exception):
    """Raised when LinkedIn responds with 429 or a checkpoint challenge."""


def extract_public_identifier(profile_url: str) -> str:
    """Pull the `in/<public-id>` slug out of any LinkedIn profile URL shape.

    Accepts www.linkedin.com, linkedin.com, m.linkedin.com, with or without
    trailing slash / query string / locale prefix.
    """
    match = _PUBLIC_ID_RE.search(profile_url.strip())
    if not match:
        raise ValueError(
            f"Could not find a /in/<public-id> segment in URL: {profile_url!r}"
        )
    return match.group(1).rstrip("/")


class _GlobalRateLimiter:
    """Process-wide throttle so we never hammer LinkedIn back-to-back,
    regardless of how many API requests arrive concurrently."""

    def __init__(self, min_interval_seconds: float):
        self._min_interval = min_interval_seconds
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def wait_turn(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_at
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            self._last_request_at = time.monotonic()


class LinkedInClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._rate_limiter = _GlobalRateLimiter(settings.min_request_interval_seconds)

    def _headers(self) -> Dict[str, str]:
        # csrf-token must match the JSESSIONID cookie value (LinkedIn quotes
        # it as "ajax:1234567890" in the cookie; the header wants it as-is).
        csrf = self._settings.jsessionid_cookie
        return {
            "User-Agent": self._settings.user_agent,
            "Accept": "application/vnd.linkedin.normalized+json+2.1",
            "csrf-token": csrf,
            "x-restli-protocol-version": "2.0.0",
            "x-li-lang": "en_US",
        }

    def _cookies(self) -> Dict[str, str]:
        return {
            "li_at": self._settings.li_at_cookie,
            "JSESSIONID": self._settings.jsessionid_cookie,
        }

    async def fetch_profile_view(self, public_identifier: str) -> Dict[str, Any]:
        """Calls the `profileView` aggregate endpoint, which returns
        summary, positions, education, skills, certifications and
        languages in a single normalized JSON payload."""
        if not self._settings.is_configured:
            raise LinkedInAuthError(
                "LI_AT_COOKIE / JSESSIONID_COOKIE are not configured on the server."
            )

        await self._rate_limiter.wait_turn()

        url = f"{VOYAGER_BASE}/identity/profiles/{public_identifier}/profileView"
        async with httpx.AsyncClient(
            headers=self._headers(),
            cookies=self._cookies(),
            timeout=self._settings.request_timeout_seconds,
            follow_redirects=True,
        ) as client:
            response = await client.get(url)

        if response.status_code == 404:
            raise LinkedInProfileNotFound(public_identifier)
        if response.status_code in (401, 403):
            raise LinkedInAuthError(
                "LinkedIn rejected the session cookies (expired or invalid)."
            )
        if response.status_code == 429:
            raise LinkedInRateLimited("LinkedIn responded with 429 Too Many Requests.")

        response.raise_for_status()
        return response.json()
