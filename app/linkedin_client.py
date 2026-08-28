"""Fetches LinkedIn's internal "Voyager" profile JSON via direct HTTP
requests — no browser, no JavaScript execution, exactly the assignment's
"purely reverse-engineered ... does not use a browser" requirement.

This went through three iterations, and the current one is a direct result
of what live testing against a real account showed at each step:

1. Plain `httpx` calling `/voyager/api/identity/profiles/<id>/profileView`
   with `li_at` + `JSESSIONID` cookies and a matching `csrf-token` header
   (the "textbook" reverse-engineering approach, and how this endpoint was
   found in the first place) got a clean `403 CSRF check failed` on every
   attempt, even with fresh, valid cookies. A bare HTTP client's TLS/HTTP2
   handshake has a different fingerprint than a real browser's, and
   LinkedIn's edge filters on that before the CSRF token is meaningfully
   checked at all.
2. A headless-Chromium version (driving a real browser to make the same
   request) got past that specific check, confirming the TLS/HTTP2
   fingerprint theory — but a browser is explicitly disallowed for this
   assignment, so that path was abandoned rather than pursued further.
3. Current approach: `curl_cffi`, a library that reproduces a real Chrome
   build's TLS/JA3 fingerprint (and matching header set — `Sec-Ch-Ua`,
   `Accept`, etc.) at the HTTP-client level, with no browser or JS involved.
   Verified live that this alone clears the CSRF check `httpx` couldn't.
   The remaining piece: LinkedIn only issues `bcookie`/`bscookie`/`lidc`/
   `JSESSIONID` to a session that has actually visited the site — a fresh
   client that presents `li_at` cold, with none of that history, doesn't
   look like a real returning session. `_ensure_session()` does one
   anonymous GET to linkedin.com first (exactly what a real browser's very
   first request of the day looks like) to pick up those cookies *before*
   ever presenting `li_at`, then reuses that one session for every
   subsequent request — a real browser only "warms up" once, not before
   every single page view either.

Live end-to-end verification of this final version was inconclusive: after
extensive testing throughout the day (the abandoned browser version
included), the test account itself started getting a self-redirecting
`302` loop back to the exact URL requested — on a plain `/feed/` page load
with nothing but `li_at`, no API call involved — which looks like LinkedIn
challenging the account/session itself rather than rejecting anything
about this specific request. See README "Known limitations" for the full
account of what was and wasn't confirmed live.
"""

import asyncio
import random
import re
import time
import urllib.parse
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlsplit

from curl_cffi.requests import AsyncSession, RequestsError

from app.config import Settings

_PUBLIC_ID_RE = re.compile(r"linkedin\.com/in/([^/?#]+)", re.IGNORECASE)
_SEARCH_RESULT_PUBLIC_ID_RE = re.compile(r"/in/([A-Za-z0-9\-_%]+)")

_PROFILE_VIEW_URL = "https://www.linkedin.com/voyager/api/identity/profiles/{public_id}/profileView"
_SEARCH_URL = "https://www.linkedin.com/voyager/api/search/dash/clusters"
_LOGIN_WALL_MARKERS = ("/authwall", "/login", "/checkpoint/")
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


class LinkedInAuthError(Exception):
    """Raised when the configured session cookie is missing/expired, or
    LinkedIn redirected to an explicit login/checkpoint page."""


class LinkedInProfileNotFound(Exception):
    """Raised when LinkedIn explicitly told us the profile doesn't exist
    (a real 404, or a 200 whose body wasn't the JSON we expected)."""


class LinkedInRateLimited(Exception):
    """Raised when LinkedIn responds with 429."""


class LinkedInBotDetected(Exception):
    """Raised when LinkedIn responded with a redirect back to the exact
    URL that was requested — a self-redirect loop rather than a real
    login-wall destination. Observed live even on a plain authenticated
    page load with a valid `li_at` cookie and no API call involved,
    consistent with an account/session-level challenge rather than
    anything specific to this request. See README Known Limitations."""


class LinkedInRequestTimeout(Exception):
    """Raised when the HTTP request to LinkedIn itself timed out or failed
    at the network level (not a LinkedIn-issued response at all)."""


class LinkedInSearchNoMatch(Exception):
    """Raised when a people-search response contained no `/in/<public-id>`
    reference at all — genuinely no match, or LinkedIn served a
    blurred/limited results payload (its "commercial use limit" for
    non-Premium accounts) instead of real results."""


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


def _is_self_redirect(request_url: str, location_header: str) -> bool:
    """True if `location_header` resolves to the same URL that was
    requested (ignoring a trailing slash) — LinkedIn's observed behavior
    for a challenged session/account, rather than a real redirect
    somewhere else (e.g. an actual login page)."""
    if not location_header:
        return False
    resolved = urljoin(request_url, location_header)
    a, b = urlsplit(resolved), urlsplit(request_url)
    return (a.netloc, a.path.rstrip("/"), a.query) == (b.netloc, b.path.rstrip("/"), b.query)


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
        self._session: Optional[AsyncSession] = None
        self._startup_lock = asyncio.Lock()

    async def _ensure_session(self) -> AsyncSession:
        if self._session is not None:
            return self._session
        async with self._startup_lock:
            if self._session is not None:
                return self._session
            session = AsyncSession(
                impersonate=self._settings.impersonate_browser,
                timeout=self._settings.request_timeout_seconds,
            )
            # Anonymous warm-up BEFORE presenting li_at — see module
            # docstring for why. Deliberately not using our own rate
            # limiter/daily budget for this one bootstrap request: it's
            # infrastructure setup, not a profile/search lookup.
            await session.get("https://www.linkedin.com/", allow_redirects=True)
            session.cookies.set("li_at", self._settings.li_at_cookie, domain=".linkedin.com")
            self._session = session
            return session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @staticmethod
    def _current_jsessionid(session: AsyncSession) -> Optional[str]:
        for cookie in session.cookies.jar:
            if cookie.name == "JSESSIONID":
                return cookie.value
        return None

    def _voyager_headers(self, session: AsyncSession, referer: str) -> Dict[str, str]:
        jsessionid = self._current_jsessionid(session)
        return {
            "Accept": "application/vnd.linkedin.normalized+json+2.1",
            "csrf-token": f'"{jsessionid}"' if jsessionid else "",
            "x-restli-protocol-version": "2.0.0",
            "x-li-lang": "en_US",
            "Referer": referer,
        }

    async def fetch_profile_view(self, public_identifier: str) -> Dict[str, Any]:
        if not self._settings.is_configured:
            raise LinkedInAuthError("LI_AT_COOKIE is not configured on the server.")

        await self._daily_budget.consume()
        await self._rate_limiter.wait_turn()
        session = await self._ensure_session()

        url = _PROFILE_VIEW_URL.format(public_id=public_identifier)
        referer = f"https://www.linkedin.com/in/{public_identifier}/"

        try:
            response = await session.get(
                url, headers=self._voyager_headers(session, referer), allow_redirects=False
            )
        except RequestsError as exc:
            raise LinkedInRequestTimeout(str(exc)) from exc

        if response.status_code in _REDIRECT_STATUSES:
            self._raise_for_redirect(url, response.headers.get("location", ""))

        if response.status_code == 404:
            raise LinkedInProfileNotFound(public_identifier)
        if response.status_code in (401, 403):
            raise LinkedInAuthError("LinkedIn rejected the session (expired, invalid, or challenged).")
        if response.status_code == 429:
            raise LinkedInRateLimited("LinkedIn responded with 429 Too Many Requests.")
        if response.status_code != 200:
            raise LinkedInProfileNotFound(public_identifier)

        try:
            return response.json()
        except Exception as exc:  # noqa: BLE001 - body wasn't the JSON we expected
            raise LinkedInProfileNotFound(public_identifier) from exc

    def _raise_for_redirect(self, request_url: str, location: str) -> None:
        if _is_self_redirect(request_url, location):
            raise LinkedInBotDetected(
                "LinkedIn responded with a redirect back to the exact URL requested, "
                "instead of serving it — a challenge response observed even on a plain "
                "authenticated page load during testing, not specific to this endpoint."
            )
        if any(marker in location for marker in _LOGIN_WALL_MARKERS):
            raise LinkedInAuthError(
                "LinkedIn redirected to a login/checkpoint page — "
                "the li_at cookie is invalid, expired, or the account hit a security check."
            )
        raise LinkedInAuthError(f"LinkedIn redirected unexpectedly to {location!r}.")

    async def search_profile_url(
        self, full_name: str, company: Optional[str] = None, email: Optional[str] = None
    ) -> str:
        """Searches LinkedIn's people-search API for `full_name`
        disambiguated by `company` or `email`, and returns the first
        matching profile URL — mirrors PhantomBuster's "LinkedIn Profile
        URL Finder". Requires `company` or `email` since name-only search
        matches too many people to be useful (the same constraint that
        tool documents).

        The search endpoint/query-parameter shape below is the highest-
        uncertainty part of this codebase: unlike `profileView` (verified
        against a real response during development), LinkedIn's internal
        search API was not confirmed live — see README Known Limitations.
        To reduce that fragility, the result is extracted by regex-scanning
        the raw response text for any `/in/<public-id>` occurrence rather
        than parsing a specific JSON shape — a public identifier tends to
        appear as a string value somewhere in the payload even if the
        surrounding structure has drifted from what's assumed here.
        """
        if not company and not email:
            raise ValueError("Provide company or email to disambiguate the search.")
        if not self._settings.is_configured:
            raise LinkedInAuthError("LI_AT_COOKIE is not configured on the server.")

        await self._daily_budget.consume()
        await self._rate_limiter.wait_turn()
        session = await self._ensure_session()

        keywords = " ".join(part for part in [full_name, company or email] if part)
        query = (
            f"(keywords:{urllib.parse.quote(keywords)},"
            "flagshipSearchIntent:SEARCH_SRP,"
            "queryParameters:(resultType:List(PEOPLE)),"
            "includeFiltersInResponse:false)"
        )
        url = f"{_SEARCH_URL}?q=all&query={urllib.parse.quote(query, safe='(),:')}"
        referer = "https://www.linkedin.com/search/results/people/?keywords=" + urllib.parse.quote(keywords)

        try:
            response = await session.get(
                url, headers=self._voyager_headers(session, referer), allow_redirects=False
            )
        except RequestsError as exc:
            raise LinkedInRequestTimeout(str(exc)) from exc

        if response.status_code in _REDIRECT_STATUSES:
            self._raise_for_redirect(url, response.headers.get("location", ""))
        if response.status_code in (401, 403):
            raise LinkedInAuthError("LinkedIn rejected the session (expired, invalid, or challenged).")
        if response.status_code == 429:
            raise LinkedInRateLimited("LinkedIn responded with 429 Too Many Requests.")
        if response.status_code != 200:
            raise LinkedInSearchNoMatch(keywords)

        for public_id in _SEARCH_RESULT_PUBLIC_ID_RE.findall(response.text):
            return f"https://www.linkedin.com/in/{public_id}/"

        raise LinkedInSearchNoMatch(keywords)
