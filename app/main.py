import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.cache import ProfileCache
from app.config import Settings, get_settings
from app.linkedin_client import (
    LinkedInAuthError,
    LinkedInBotDetected,
    LinkedInRequestTimeout,
    LinkedInClient,
    LinkedInDailyLimitExceeded,
    LinkedInProfileNotFound,
    LinkedInRateLimited,
    LinkedInSearchNoMatch,
    extract_public_identifier,
)
from app.parser import parse_profile_view
from app.rate_limit import SlidingWindowRateLimiter
from app.schemas import LinkedInProfile, ProfileUrlMatch

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("linkedin_profile_api")

_settings = get_settings()
_client = LinkedInClient(_settings)
_cache = ProfileCache(max_size=_settings.cache_max_size, ttl_seconds=_settings.cache_ttl_seconds)
_url_cache = ProfileCache(max_size=_settings.cache_max_size, ttl_seconds=_settings.cache_ttl_seconds)
# Per-caller ceiling independent of the global LinkedIn throttle, so one
# noisy API key can't starve everyone else's request budget.
_caller_rate_limiter = SlidingWindowRateLimiter(max_requests=20, window_seconds=60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await _client.close()


app = FastAPI(
    title="LinkedIn Profile API",
    description=(
        "Accepts a LinkedIn profile URL and returns structured profile data "
        "scraped via LinkedIn's internal Voyager API. Unofficial; see README "
        "for setup and limitations."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def require_api_key(x_api_key: str = Header(default="")) -> str:
    if not _settings.api_key:
        # Operator hasn't set one — fail closed rather than run wide open.
        raise HTTPException(
            status_code=500,
            detail="Server misconfiguration: API_KEY is not set.",
        )
    if x_api_key != _settings.api_key:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header.")
    return x_api_key


def enforce_caller_rate_limit(api_key: str = Depends(require_api_key)) -> None:
    if not _caller_rate_limiter.allow(api_key):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded for this API key. Try again shortly.",
        )


_INDEX_HTML = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> str:
    return _INDEX_HTML


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "linkedin_session_configured": _settings.is_configured}


@app.get(
    "/api/v1/profile",
    response_model=LinkedInProfile,
    dependencies=[Depends(enforce_caller_rate_limit)],
)
async def get_profile(
    url: str = Query(..., description="Full LinkedIn profile URL, e.g. https://www.linkedin.com/in/someone/"),
    settings: Settings = Depends(get_settings),
) -> LinkedInProfile:
    try:
        public_id = extract_public_identifier(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    cached = _cache.get(public_id)
    if cached is not None:
        return cached.model_copy(update={"cache_hit": True})

    try:
        raw = await _client.fetch_profile_view(public_id)
    except LinkedInProfileNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Profile not found: {exc}") from exc
    except (
        LinkedInBotDetected,
        LinkedInRequestTimeout,
        LinkedInAuthError,
        LinkedInRateLimited,
        LinkedInDailyLimitExceeded,
    ) as exc:
        logger.warning("Known LinkedIn limitation fetching %s: %s", public_id, exc)
        raise _known_limitation_http_exception(exc) from exc
    except Exception as exc:  # noqa: BLE001 - surface upstream failures as 502
        logger.exception("Unexpected error fetching profile %s", public_id)
        raise HTTPException(status_code=502, detail=f"Failed to fetch profile from LinkedIn: {exc}") from exc

    profile = parse_profile_view(
        raw,
        public_identifier=public_id,
        profile_url=f"https://www.linkedin.com/in/{public_id}/",
    )
    _cache.set(public_id, profile)
    return profile


@app.get(
    "/api/v1/find-profile-url",
    response_model=ProfileUrlMatch,
    dependencies=[Depends(enforce_caller_rate_limit)],
)
async def find_profile_url(
    name: str = Query(..., description="Full name to search for, e.g. 'Jane Doe'."),
    company: Optional[str] = Query(None, description="Current or past company, to disambiguate the name."),
    email: Optional[str] = Query(None, description="Professional email, to disambiguate the name."),
) -> ProfileUrlMatch:
    """Finds a LinkedIn profile URL from a name plus a company or email —
    mirrors PhantomBuster's "LinkedIn Profile URL Finder". Requires company
    or email since a name alone matches too many people to be useful (the
    same constraint that tool documents)."""
    if not company and not email:
        raise HTTPException(
            status_code=400,
            detail="Provide 'company' or 'email' along with 'name' to disambiguate the search.",
        )

    cache_key = f"{name.strip().lower()}|{(company or '').strip().lower()}|{(email or '').strip().lower()}"
    cached = _url_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        profile_url = await _client.search_profile_url(name, company=company, email=email)
    except LinkedInSearchNoMatch as exc:
        raise HTTPException(status_code=404, detail=f"No LinkedIn profile matched: {exc}") from exc
    except (
        LinkedInBotDetected,
        LinkedInRequestTimeout,
        LinkedInAuthError,
        LinkedInRateLimited,
        LinkedInDailyLimitExceeded,
    ) as exc:
        logger.warning("Known LinkedIn limitation searching for %r: %s", name, exc)
        raise _known_limitation_http_exception(exc) from exc
    except Exception as exc:  # noqa: BLE001 - surface upstream failures as 502
        logger.exception("Unexpected error searching for %r", name)
        raise HTTPException(status_code=502, detail=f"Failed to search LinkedIn: {exc}") from exc

    result = ProfileUrlMatch(
        public_identifier=extract_public_identifier(profile_url),
        profile_url=profile_url,
        matched_query=f"{name} · {company or email}",
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    _url_cache.set(cache_key, result)
    return result


def _known_limitation_http_exception(exc: Exception) -> HTTPException:
    """Translates the LinkedIn exceptions shared across endpoints into an
    HTTPException whose body includes a caller-facing `alert` explaining
    the known, documented limitation — see README 'Known limitations'."""
    if isinstance(exc, LinkedInBotDetected):
        return HTTPException(
            status_code=502,
            detail={
                "error": f"LinkedIn blocked the automated request: {exc}",
                "alert": (
                    "Known limitation: LinkedIn responded with a self-redirect loop "
                    "instead of serving the request — observed live even on a plain "
                    "authenticated page load, consistent with an account/session-level "
                    "challenge rather than a flaw in this request specifically. This is "
                    "not a bug in this service — see README 'Known limitations' for what "
                    "was verified during live testing."
                ),
            },
        )
    if isinstance(exc, LinkedInRequestTimeout):
        return HTTPException(
            status_code=502,
            detail={
                "error": f"The request to LinkedIn itself timed out or failed: {exc}",
                "alert": (
                    "Known limitation: this is a network-level failure talking to "
                    "LinkedIn (slow connection, DNS, TLS handshake), not a rejection "
                    "from LinkedIn — retrying, or raising REQUEST_TIMEOUT_SECONDS, may "
                    "help. See README 'Known limitations'."
                ),
            },
        )
    if isinstance(exc, LinkedInAuthError):
        return HTTPException(
            status_code=502,
            detail={
                "error": f"LinkedIn session invalid: {exc}",
                "alert": (
                    "Known limitation: the configured li_at cookie may be expired, or "
                    "LinkedIn challenged the session. See README 'Known limitations'."
                ),
            },
        )
    if isinstance(exc, LinkedInDailyLimitExceeded):
        return HTTPException(
            status_code=429,
            detail={
                "error": str(exc),
                "alert": (
                    "Known limitation: this is a self-imposed daily safety cap "
                    "(DAILY_REQUEST_LIMIT), not LinkedIn itself rate-limiting — it exists "
                    "to keep this instance's footprint against the LinkedIn account "
                    "predictable. Resets at 00:00 UTC. See README 'Known limitations'."
                ),
            },
        )
    # LinkedInRateLimited
    return HTTPException(status_code=429, detail=str(exc))


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    # Some raise sites pass a dict detail (error + alert); others pass a
    # plain string. Normalize both into a flat {"error": ..., ...} body.
    content = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
    return JSONResponse(status_code=exc.status_code, content=content)
