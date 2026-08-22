from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    # App Settings
    APP_NAME: str = "Voxa 2.0"
    ENVIRONMENT: str = "production"
    DEBUG: bool = False
    
    # Database Configuration
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/voxa_db"
    
    # Redis Configuration
    REDIS_URL: str = "redis://localhost:6379/0"
    
    # JWT & Auth
    JWT_SECRET_KEY: str = "voxa_super_secret_production_key_2026_jwt_token_auth"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440 # 24 hours
    
    # Rate Limiter
    RATE_LIMIT_REQUESTS_PER_MINUTE: int = 30
    
    # AI Models
    LLM_MODEL_NAME: str = "Qwen/Qwen2.5-0.5B-Instruct"
    STT_MODEL_SIZE: str = "base"
    TTS_VOICE: str = "en-US-AriaNeural"
    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
    
    # Redis Streams Topic Names
    STREAM_STT_AUDIO: str = "stream:audio:transcribe"
    STREAM_LLM_INFERENCE: str = "stream:llm:inference"
    STREAM_DLQ: str = "stream:dead:letter:queue"
    
    class Config:
        env_file = ".env"
        extra = "allow"

settings = Settings()
