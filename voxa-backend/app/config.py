"""
Centralised, environment-driven configuration for the gateway and all workers.

Every field can be overridden with an environment variable of the same name
(or an entry in a local ``.env`` file). Defaults are tuned for local development
with ``docker compose``; production deployments are expected to override at
least ``JWT_SECRET_KEY``, ``DATABASE_URL`` and ``REDIS_URL``.
"""
import logging
import secrets
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("voxa.config")

DEFAULT_CORS_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:[0-9]+)?$"


class Settings(BaseSettings):
    # Database Configuration
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/voxa_db"

    # Redis Configuration
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT & Auth
    # Intentionally has no hard-coded default: a secret committed to a public repository
    # lets anyone mint admin tokens. When unset, an ephemeral random secret is generated
    # (see validator below) so local runs still work, but tokens do not survive restarts
    # and are not shared across replicas.
    JWT_SECRET_KEY: Optional[str] = Field(default=None, validate_default=True)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24 hours

    # Password for the seeded demo admin account (admin@voxa.ai). Override in any
    # internet-facing deployment.
    SEED_ADMIN_PASSWORD: str = "adminpassword123"

    # CORS: regex of browser origins allowed to call the REST API.
    # Default allows any localhost / 127.0.0.1 port (local docker compose / next dev).
    CORS_ALLOW_ORIGIN_REGEX: str = DEFAULT_CORS_ORIGIN_REGEX

    # Rate Limiter (sliding-window log, per authenticated user or client IP)
    RATE_LIMIT_REQUESTS_PER_MINUTE: int = 30
    # Only honour X-Forwarded-For when the gateway sits behind a trusted reverse proxy;
    # otherwise any client can spoof the header and dodge per-IP limits.
    TRUST_FORWARDED_FOR: bool = False

    # WebSocket ingress bounds (memory-exhaustion protection)
    WS_MAX_TEXT_QUERY_CHARS: int = 2000
    WS_MAX_AUDIO_CHUNK_B64_CHARS: int = 4_000_000  # ~3 MB of raw audio per utterance

    # AI Models (answers are produced by a deterministic router; no generative model is used)
    STT_MODEL_SIZE: str = "base"
    TTS_VOICE: str = "en-US-AriaNeural"
    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"

    # Redis Streams Topic Names
    STREAM_STT_AUDIO: str = "stream:audio:transcribe"
    STREAM_LLM_INFERENCE: str = "stream:llm:inference"
    STREAM_TTS_SYNTHESIS: str = "stream:tts:synthesize"
    STREAM_DLQ: str = "stream:dead:letter:queue"
    # Approximate cap (XADD MAXLEN ~) so acknowledged entries do not accumulate forever.
    STREAM_MAXLEN: int = 10_000

    model_config = SettingsConfigDict(env_file=".env", extra="allow")

    @field_validator("JWT_SECRET_KEY", mode="after")
    @classmethod
    def _ensure_jwt_secret(cls, value: Optional[str]) -> str:
        if value:
            return value
        logger.warning(
            "JWT_SECRET_KEY is not set; generated an ephemeral secret. Tokens will be "
            "invalidated on restart and are not valid across replicas. Set JWT_SECRET_KEY "
            "(e.g. `openssl rand -hex 32`) for any shared deployment."
        )
        return secrets.token_hex(32)

    @field_validator("CORS_ALLOW_ORIGIN_REGEX", mode="after")
    @classmethod
    def _default_cors_regex(cls, value: str) -> str:
        # docker compose passes unset variables through as "", which would block every origin.
        return value or DEFAULT_CORS_ORIGIN_REGEX


settings = Settings()
