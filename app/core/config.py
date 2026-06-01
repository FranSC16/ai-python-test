from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    provider_base_url: str = "http://localhost:3001"
    api_key: str = "test-dev-2026"

    redis_url: str = "redis://ia-redis:6379/0"

    extract_timeout: float = 10.0
    notify_timeout: float = 5.0

    notify_max_retries: int = 3

    request_ttl: int = 3600

    model_config = {"env_prefix": "APP_"}


settings = Settings()
