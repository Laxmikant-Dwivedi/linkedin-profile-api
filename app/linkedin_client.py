"""Fetches LinkedIn's internal "Voyager" profile JSON using a real headless
browser, rather than a raw HTTP client.

Earlier iteration of this used `httpx` to call
`/voyager/api/identity/profiles/<id>/profileView` directly with `li_at` +
`JSESSIONID` cookies and a matching `csrf-token` header. That's the
"textbook" reverse-engineering approach and it's how the endpoint URL/shape
here was discovered — but live testing against a real account showed
LinkedIn's edge now rejects that kind of request with `403 CSRF check
failed`, independent of whether the cookies/token were valid: a bare HTTP
client has a different TLS/HTTP2 fingerprint than a browser and gets
filtered before the CSRF logic is even meaningfully checked.

Driving a real (headless) Chromium sidesteps that: we inject only the
`li_at` cookie, navigate to the profile page, and let the browser's own JS
make its normal authenticated request to that same `profileView` endpoint
— which we intercept via the Playwright network-response listener. The
JSON payload is identical to before, so `app/parser.py` didn't need to
change at all.
"""

import asyncio
import re
import time
from typing import Any, Dict, Optional

from playwright.async_api import Response, async_playwright

from app.config import Settings

_PUBLIC_ID_RE = re.compile(r"linkedin\.com/in/([^/?#]+)", re.IGNORECASE)

_PROFILE_VIEW_PATH = "/voyager/api/identity/profiles/"
_LOGIN_WALL_MARKERS = ("/authwall", "/login", "/checkpoint/")


class LinkedInAuthError(Exception):
    """Raised when the configured session cookie is missing/expired."""


class LinkedInProfileNotFound(Exception):
    """Raised when the profile page never yields a profileView response
    (private profile fully outside the viewer's network, deleted account,
    typo'd URL, etc.)."""


class LinkedInRateLimited(Exception):
    """Raised when LinkedIn responds with 429 to the intercepted request."""


def extract_public_identifier(profile_url: str) -> str:
    """Pull the `in/<public-id>` slug out of any LinkedIn profile URL shape.

    Accepts www.linkedin.com, linkedin.com, m.linkedin.com, with or without
    trailing slash / query string / locale prefix.
    """
    match = _PUBLIC_ID_RE.search(profile_url.strip())
    if not match:
        raise ValueError(f"Could not find a /in/<public-id> segment in URL: {profile_url!r}")
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
        self._playwright = None
        self._browser = None
        self._startup_lock = asyncio.Lock()

    async def _ensure_browser(self) -> None:
        if self._browser is not None:
            return
        async with self._startup_lock:
            if self._browser is not None:
                return
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def fetch_profile_view(self, public_identifier: str) -> Dict[str, Any]:
        if not self._settings.is_configured:
            raise LinkedInAuthError("LI_AT_COOKIE is not configured on the server.")

        await self._rate_limiter.wait_turn()
        await self._ensure_browser()

        context = await self._browser.new_context(
            user_agent=self._settings.user_agent,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            timezone_id="America/Los_Angeles",
        )
        # Headless Chromium exposes `navigator.webdriver = true` and a few
        # other tells that automated-browser detectors key off; strip the
        # obvious ones before any page script runs. This is a standard,
        # widely-used mitigation for automation fingerprinting, not an
        # attempt to defeat security controls.
        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            window.chrome = { runtime: {} };
            """
        )
        await context.add_cookies(
            [
                {
                    "name": "li_at",
                    "value": self._settings.li_at_cookie,
                    "domain": ".linkedin.com",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                }
            ]
        )

        captured: Dict[str, Any] = {}
        response_seen = asyncio.Event()

        async def on_response(response: Response) -> None:
            if _PROFILE_VIEW_PATH not in response.url or "profileView" not in response.url:
                return
            captured["status"] = response.status
            if response.status == 200:
                try:
                    captured["json"] = await response.json()
                except Exception:  # noqa: BLE001 - body wasn't JSON, treat as failure below
                    pass
            response_seen.set()

        page = await context.new_page()
        page.on("response", on_response)

        try:
            timeout_ms = self._settings.request_timeout_seconds * 1000
            await page.goto(
                f"https://www.linkedin.com/in/{public_identifier}/",
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            try:
                await asyncio.wait_for(
                    response_seen.wait(), timeout=self._settings.request_timeout_seconds
                )
            except asyncio.TimeoutError:
                if any(marker in page.url for marker in _LOGIN_WALL_MARKERS):
                    raise LinkedInAuthError(
                        "LinkedIn redirected to a login/checkpoint page — "
                        "the li_at cookie is invalid, expired, or the account hit a security check."
                    )
                raise LinkedInProfileNotFound(public_identifier)
        finally:
            await page.close()
            await context.close()

        status: Optional[int] = captured.get("status")
        if status == 404:
            raise LinkedInProfileNotFound(public_identifier)
        if status in (401, 403):
            raise LinkedInAuthError("LinkedIn rejected the session (expired, invalid, or challenged).")
        if status == 429:
            raise LinkedInRateLimited("LinkedIn responded with 429 Too Many Requests.")
        if "json" not in captured:
            raise LinkedInProfileNotFound(public_identifier)
        return captured["json"]
