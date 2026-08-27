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
    # browser and copying the `li_at` cookie. See README. A headless
    # browser drives the actual scrape, so no CSRF/JSESSIONID handling is
    # needed here — the browser establishes its own session.
    li_at_cookie: str = ""

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

    # Outbound request timeout to LinkedIn.
    request_timeout_seconds: float = 20.0

    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    @property
    def is_configured(self) -> bool:
        return bool(self.li_at_cookie)


@lru_cache
def get_settings() -> Settings:
    return Settings()
