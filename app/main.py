import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.cache import ProfileCache
from app.config import Settings, get_settings
from app.linkedin_client import (
    LinkedInAuthError,
    LinkedInClient,
    LinkedInProfileNotFound,
    LinkedInRateLimited,
    extract_public_identifier,
)
from app.parser import parse_profile_view
from app.rate_limit import SlidingWindowRateLimiter
from app.schemas import LinkedInProfile

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("linkedin_profile_api")

_settings = get_settings()
_client = LinkedInClient(_settings)
_cache = ProfileCache(max_size=_settings.cache_max_size, ttl_seconds=_settings.cache_ttl_seconds)
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
    except LinkedInAuthError as exc:
        logger.error("LinkedIn auth error: %s", exc)
        raise HTTPException(status_code=502, detail=f"LinkedIn session invalid: {exc}") from exc
    except LinkedInProfileNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Profile not found: {exc}") from exc
    except LinkedInRateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
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


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
