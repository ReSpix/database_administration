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

    # ---- Database ----
    db_scheme: str = "postgresql"
    db_host: str = "postgres"
    db_port: int = 5432
    db_user: str = "postgres"
    db_password: str = "postgres"
    db_name: str = "database"

    # ---- Simulation ----
    sim_seed:         int   = 42
    sim_duration:     float = 3600.0
    sim_order_rate:   float = 0.2
    sim_courier_tick: float = 2.0
    sim_preflight:    bool  = True
    sim_offer_timeout: float = 25.0     # сим-секунды, сколько курьер «думает»
    
    # ---- Quick test mode (10 sec, slow for observation) ----
    sim_quick:        bool = False       # если true — запускает быстрый тест на 10 сек
    sim_quick_duration: float = 10.0
    sim_quick_order_rate: float = 0.5   # 1 заказ в 2 сек
    sim_quick_courier_tick: float = 1.0 # тик курьера каждую сек
    sim_quick_log_level: str = "DEBUG"  # подробные логи

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

    @property
    def database_url(self) -> str:
        return f"{self.db_scheme}://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def effective_duration(self) -> float:
        return self.sim_quick_duration if self.sim_quick else self.sim_duration

    @property
    def effective_order_rate(self) -> float:
        return self.sim_quick_order_rate if self.sim_quick else self.sim_order_rate

    @property
    def effective_courier_tick(self) -> float:
        return self.sim_quick_courier_tick if self.sim_quick else self.sim_courier_tick


settings = Settings()