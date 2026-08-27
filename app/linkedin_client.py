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
import random
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional

from playwright.async_api import BrowserContext, Error as PlaywrightError
from playwright.async_api import Response, async_playwright

from app.config import Settings

_PUBLIC_ID_RE = re.compile(r"linkedin\.com/in/([^/?#]+)", re.IGNORECASE)

_PROFILE_VIEW_PATH = "/voyager/api/identity/profiles/"
_LOGIN_WALL_MARKERS = ("/authwall", "/login", "/checkpoint/")

_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = { runtime: {} };
"""


class LinkedInAuthError(Exception):
    """Raised when the configured session cookie is missing/expired."""


class LinkedInProfileNotFound(Exception):
    """Raised when LinkedIn explicitly told us the profile doesn't exist
    (a real 404 from the intercepted response)."""


class LinkedInRateLimited(Exception):
    """Raised when LinkedIn responds with 429 to the intercepted request."""


class LinkedInBotDetected(Exception):
    """Raised when LinkedIn's automation detection visibly intervened —
    e.g. bounced the navigation into a redirect loop — rather than serving
    the page. A known, documented limitation; see README."""


class LinkedInCaptureTimeout(Exception):
    """Raised when the page loaded without any bot-detection signature, but
    no `profileView` response ever arrived before our timeout. Ambiguous by
    nature: could be a genuinely wrong public-id, or a CPU-starved host
    that's simply too slow to finish rendering the page in time. See
    README Known Limitations."""


class LinkedInSearchNoMatch(Exception):
    """Raised when a people-search returned no profile links at all —
    genuinely no match, or LinkedIn served a blurred/limited results page
    (its "commercial use limit" for non-Premium accounts) instead of real
    results. See README Known Limitations."""


class LinkedInDailyLimitExceeded(Exception):
    """Raised when this process has already made `daily_request_limit`
    LinkedIn-bound requests today. A self-imposed safety ceiling — distinct
    from LinkedInRateLimited, which reflects LinkedIn itself pushing back —
    meant to keep this instance's total footprint against the account
    predictable regardless of how many callers are hitting the API."""


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
    regardless of how many API requests arrive concurrently. Spaces
    requests by a randomized interval rather than a fixed delay —
    perfectly even spacing is itself a machine-like tell that's easy to
    fingerprint; jittered pacing looks closer to a human browsing."""

    def __init__(self, min_interval_seconds: float, max_interval_seconds: float):
        self._min_interval = min_interval_seconds
        self._max_interval = max(max_interval_seconds, min_interval_seconds)
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def wait_turn(self) -> None:
        async with self._lock:
            now = time.monotonic()
            target_interval = random.uniform(self._min_interval, self._max_interval)
            elapsed = now - self._last_request_at
            if elapsed < target_interval:
                await asyncio.sleep(target_interval - elapsed)
            self._last_request_at = time.monotonic()


class _DailyRequestBudget:
    """Caps LinkedIn-bound requests to `limit` per rolling UTC calendar day.
    `limit <= 0` disables the cap. This is a self-imposed safety ceiling on
    top of (not a replacement for) LinkedIn's own rate limiting — it bounds
    this process's total footprint regardless of how many API callers or
    distinct profiles/searches are driving it."""

    def __init__(self, limit: int):
        self._limit = limit
        self._lock = asyncio.Lock()
        self._day: Optional[str] = None
        self._count = 0

    async def consume(self) -> None:
        if self._limit <= 0:
            return
        async with self._lock:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            if today != self._day:
                self._day = today
                self._count = 0
            if self._count >= self._limit:
                raise LinkedInDailyLimitExceeded(
                    f"This instance has already made {self._count} LinkedIn requests "
                    f"today (limit {self._limit}). Resets at 00:00 UTC."
                )
            self._count += 1


class LinkedInClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._rate_limiter = _GlobalRateLimiter(
            settings.min_request_interval_seconds, settings.max_request_interval_seconds
        )
        self._daily_budget = _DailyRequestBudget(settings.daily_request_limit)
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

    async def _new_stealth_context(self) -> BrowserContext:
        """A fresh browser context with the li_at session and the standard
        automation-fingerprint mitigations applied — shared by every
        LinkedIn-facing operation (profile fetch, people search, ...)."""
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
        await context.add_init_script(_STEALTH_INIT_SCRIPT)
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
        return context

    async def fetch_profile_view(self, public_identifier: str) -> Dict[str, Any]:
        if not self._settings.is_configured:
            raise LinkedInAuthError("LI_AT_COOKIE is not configured on the server.")

        await self._daily_budget.consume()
        await self._rate_limiter.wait_turn()
        await self._ensure_browser()

        context = await self._new_stealth_context()

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
            await self._goto_or_raise_bot_detected(
                page, f"https://www.linkedin.com/in/{public_identifier}/", timeout_ms
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
                raise LinkedInCaptureTimeout(public_identifier)
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

    async def _goto_or_raise_bot_detected(self, page, url: str, timeout_ms: float) -> None:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightError as exc:
            if "ERR_TOO_MANY_REDIRECTS" in str(exc):
                raise LinkedInBotDetected(
                    "LinkedIn's automation detection bounced this request into a "
                    "security-challenge redirect loop before the page loaded."
                ) from exc
            raise

    async def search_profile_url(
        self, full_name: str, company: Optional[str] = None, email: Optional[str] = None
    ) -> str:
        """Searches LinkedIn's people search for `full_name` disambiguated by
        `company` or `email`, and returns the first matching profile URL.

        Mirrors PhantomBuster's "LinkedIn Profile URL Finder": too many
        people share a name for name-only search to be useful, so a company
        or email is required to narrow it down (same constraint that tool
        documents). Implemented via DOM extraction (`a[href*="/in/"]`)
        rather than intercepting LinkedIn's internal search API response,
        since the search results DOM's href attributes are far more stable
        across LinkedIn frontend changes than its internal search endpoint
        schema — the same tradeoff documented for `fetch_profile_view`,
        just resolved the other way here.
        """
        if not company and not email:
            raise ValueError("Provide company or email to disambiguate the search.")
        if not self._settings.is_configured:
            raise LinkedInAuthError("LI_AT_COOKIE is not configured on the server.")

        query = " ".join(part for part in [full_name, company or email] if part)
        search_url = "https://www.linkedin.com/search/results/people/?keywords=" + urllib.parse.quote(query)

        await self._daily_budget.consume()
        await self._rate_limiter.wait_turn()
        await self._ensure_browser()

        context = await self._new_stealth_context()
        page = await context.new_page()

        try:
            timeout_ms = self._settings.request_timeout_seconds * 1000
            await self._goto_or_raise_bot_detected(page, search_url, timeout_ms)

            try:
                await page.wait_for_selector('a[href*="/in/"]', timeout=timeout_ms)
            except PlaywrightError:
                if any(marker in page.url for marker in _LOGIN_WALL_MARKERS):
                    raise LinkedInAuthError(
                        "LinkedIn redirected to a login/checkpoint page — "
                        "the li_at cookie is invalid, expired, or the account hit a security check."
                    )
                raise LinkedInSearchNoMatch(query)

            hrefs: List[str] = await page.eval_on_selector_all(
                'a[href*="/in/"]', "els => els.map(e => e.href)"
            )
        finally:
            await page.close()
            await context.close()

        for href in hrefs:
            try:
                public_id = extract_public_identifier(href)
            except ValueError:
                continue
            return f"https://www.linkedin.com/in/{public_id}/"

        raise LinkedInSearchNoMatch(query)
