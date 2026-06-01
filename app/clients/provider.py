import logging

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

from core.config import settings
from models.schemas import NotifyRequest

logger = logging.getLogger("clients.provider")


class RateLimitError(Exception):
    pass


class ServerError(Exception):
    pass


class ProviderClient:
    def __init__(self) -> None:
        self.client: httpx.AsyncClient | None = None

    async def connect(self) -> None:
        self.client = httpx.AsyncClient(
            base_url=settings.provider_base_url,
            headers={
                "X-API-Key": settings.api_key,
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        if self.client:
            await self.client.aclose()

    async def extract(self, messages: list[dict]) -> str | None:
        try:
            response = await self.client.post(
                "/v1/ai/extract",
                json={"messages": messages},
                timeout=settings.extract_timeout,
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"Extract failed: {e}")
            return None

    @retry(
        retry=retry_if_exception_type(RateLimitError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=8) + wait_random(0, 1),
        reraise=True,
    )
    @retry(
        retry=retry_if_exception_type(ServerError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=2) + wait_random(0, 0.5),
        reraise=True,
    )
    async def notify(self, request: NotifyRequest) -> bool:
        try:
            response = await self.client.post(
                "/v1/notify",
                json=request.model_dump(),
                timeout=settings.notify_timeout,
            )
            if response.status_code == 429:
                logger.warning("Rate limited by provider, retrying...")
                raise RateLimitError("Rate limit exceeded")
            if response.status_code >= 500:
                logger.warning(f"Server error {response.status_code}, retrying...")
                raise ServerError(f"Server error: {response.status_code}")
            response.raise_for_status()
            return True
        except (RateLimitError, ServerError):
            raise
        except Exception as e:
            logger.error(f"Notify failed [{type(e).__name__}]: {e}")
            return False


provider_client = ProviderClient()
