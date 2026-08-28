from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, loaded from environment variables / .env.

    None of these values ever get committed to the repo — see .env.example
    for the list of variables a deployer needs to set.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LinkedIn session — obtained by logging into linkedin.com in a real
    # browser and copying the `li_at` cookie. See README. JSESSIONID is
    # deliberately NOT configured here: it's issued fresh per HTTP session
    # by LinkedIn itself and must come from this client's own anonymous
    # warm-up request, not be copied from a browser — see
    # `LinkedInClient._ensure_session()`.
    li_at_cookie: str = ""

    # Which browser's TLS/JA3 + header fingerprint curl_cffi impersonates.
    # See https://github.com/lexiforest/curl_cffi for the current list of
    # supported values (e.g. "chrome124", "chrome131", or the bare "chrome"
    # alias for curl_cffi's current default) — bump this if the pinned
    # version stops matching what LinkedIn's edge currently accepts.
    impersonate_browser: str = "chrome"

    # API key clients must send in the `X-API-Key` header. Required so this
    # publicly hosted service can't be used by strangers to drain the
    # operator's LinkedIn session / trigger rate limiting or a ban.
    api_key: str = ""

    # Outbound requests to LinkedIn are spaced by a random interval drawn
    # from [min, max] rather than a fixed delay — evenly-spaced requests are
    # themselves a machine-like tell; jittered pacing is closer to how a
    # human actually browses.
    min_request_interval_seconds: float = 3.0
    max_request_interval_seconds: float = 8.0

    # Safety ceiling on how many LinkedIn-bound requests (profile fetch +
    # people search combined) this process will make in a rolling UTC day,
    # independent of the per-caller API rate limit — caps this instance's
    # total footprint against the LinkedIn account regardless of how many
    # different API keys are calling it. 0 disables the cap.
    daily_request_limit: int = 300

    # How long a scraped profile is cached before being re-fetched.
    cache_ttl_seconds: int = 3600
    cache_max_size: int = 512

    # Timeout for each direct HTTP request to LinkedIn. Lightweight (no
    # browser rendering involved), so mainly a safety net against a hung
    # connection rather than something to tune around a hosting platform's
    # proxy timeout.
    request_timeout_seconds: float = 15.0

    @property
    def is_configured(self) -> bool:
        return bool(self.li_at_cookie)


@lru_cache
def get_settings() -> Settings:
    return Settings()
