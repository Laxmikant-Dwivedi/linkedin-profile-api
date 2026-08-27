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

    # Minimum seconds between outbound requests to LinkedIn, enforced
    # globally, to stay well under anything that looks like scripted abuse.
    min_request_interval_seconds: float = 3.0

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
