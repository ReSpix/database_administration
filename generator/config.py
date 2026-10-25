from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8",
        extra="ignore", case_sensitive=False,
    )

    # ---- API ----
    api_scheme: str = "http"
    api_host:   str = "localhost"
    api_port:   int = 8080
    api_timeout: float = 5.0
    api_health_path: str = "/health"
    api_max_retries: int = 3
    api_retry_backoff: float = 0.5

    # ---- Simulation ----
    sim_seed:         int   = 42
    sim_duration:     float = 3600.0
    sim_order_rate:   float = 0.2
    sim_courier_tick: float = 2.0
    sim_preflight:    bool  = True
    sim_offer_timeout: float = 25.0     # сим-секунды, сколько курьер «думает»

    @field_validator("api_scheme")
    @classmethod
    def _check_scheme(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in ("http", "https"):
            raise ValueError("API_SCHEME must be 'http' or 'https'")
        return v

    @field_validator("api_port")
    @classmethod
    def _check_port(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError("API_PORT must be in [1, 65535]")
        return v

    @property
    def api_base_url(self) -> str:
        return f"{self.api_scheme}://{self.api_host}:{self.api_port}"


settings = Settings()