"""Central configuration (12-factor: everything comes from env vars / .env)."""

from __future__ import annotations

import os
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_PACK_DIR = ROOT_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    env: str = "dev"
    service_name: str = "concierge"

    # ---- models -----------------------------------------------------------
    llm_mode: Literal["auto", "anthropic", "offline"] = "auto"
    agent_model: str = "anthropic:claude-opus-5-5"
    agent_fallback_model: str = "anthropic:claude-sonnet-5-5"
    agent_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    agent_max_tokens: int = 8000
    router_model: str = "anthropic:claude-haiku-5-5"
    judge_model: str = "anthropic:claude-sonnet-5-5"
    ollama_base_url: str = "http://localhost:11434"
    llm_timeout_s: float = 30.0

    # ---- topology ---------------------------------------------------------
    inventory_url: str = "inprocess"
    pricing_url: str = "inprocess"
    reservations_url: str = "inprocess"
    mcp_url: str = "inprocess"

    # ---- state ------------------------------------------------------------
    data_dir: Path = ROOT_DIR / "var"
    domain_pack: Path = DATA_PACK_DIR / "domain" / "hotel.yaml"
    policies_dir: Path = DATA_PACK_DIR / "policies"
    inventory_db_url: str | None = None
    pricing_db_url: str | None = None
    reservations_db_url: str | None = None
    memory_db_url: str | None = None
    redis_url: str | None = None
    checkpointer: Literal["memory", "sqlite", "redis", "postgres"] = "sqlite"
    checkpoint_url: str | None = None
    qdrant_url: str | None = None
    qdrant_collection: str = "policies"
    embedding_provider: Literal["hashing", "fastembed"] = "hashing"
    reranker: Literal["heuristic", "cross-encoder"] = "heuristic"
    retrieval_min_score: float = 0.30
    session_ttl_s: int = 60 * 60 * 24 * 7

    # ---- human in the loop ------------------------------------------------
    approval_secret: str = "dev-approval-secret-change-me"
    approval_ttl_s: int = 60 * 60 * 24
    operator_api_key: str = "operator-dev-key"
    cancel_refund_approval_threshold: float = 1000.0
    large_booking_total_threshold: float = 3000.0
    large_booking_rooms: int = 4

    # ---- budgets ------------------------------------------------------------
    turn_latency_budget_ms: int = 5000
    turn_cost_budget_usd: float = 0.05
    max_tool_steps: int = 6

    # ---- channels -----------------------------------------------------------
    public_base_url: str = "http://localhost:8000"
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_whatsapp_from: str = "whatsapp:+14155238886"
    twilio_validate_signature: bool = True
    meta_verify_token: str | None = None
    meta_app_secret: str | None = None
    meta_access_token: str | None = None
    meta_phone_number_id: str | None = None
    imap_host: str | None = None
    imap_port: int = 993
    imap_user: str | None = None
    imap_password: str | None = None
    imap_poll_s: int = 30
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "concierge@example.com"
    stt_provider: Literal["none", "faster-whisper"] = "none"
    whisper_model: str = "base.en"

    # ---- observability ----------------------------------------------------
    otel_exporter_otlp_endpoint: str | None = None
    log_level: str = "INFO"
    log_json: bool = False

    # ---- determinism --------------------------------------------------------
    concierge_today: date | None = Field(default=None)

    @model_validator(mode="after")
    def _derive_defaults(self) -> Settings:
        self.data_dir = Path(self.data_dir)
        for attr, name in (
            ("inventory_db_url", "inventory"),
            ("pricing_db_url", "pricing"),
            ("reservations_db_url", "reservations"),
            ("memory_db_url", "memory"),
        ):
            if not getattr(self, attr):
                setattr(self, attr, f"sqlite+aiosqlite:///{(self.data_dir / f'{name}.db').as_posix()}")
        if self.checkpointer == "sqlite" and not self.checkpoint_url:
            self.checkpoint_url = (self.data_dir / "checkpoints.db").as_posix()
        return self

    @property
    def anthropic_available(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    @property
    def use_llm(self) -> bool:
        if self.llm_mode == "offline":
            return False
        if self.llm_mode == "anthropic":
            return True
        return self.anthropic_available

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings


def reset_settings() -> None:
    """Drop the cached settings (tests change env vars between cases)."""
    get_settings.cache_clear()
