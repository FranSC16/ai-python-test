import logging

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random,
    before_sleep_log,
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
        logger.info(f"Provider client initialized targeting {settings.provider_base_url}")

    async def close(self) -> None:
        if self.client:
            await self.client.aclose()
            logger.info("Provider client connection closed")

    async def extract(self, messages: list[dict]) -> str | None:
        try:
            response = await self.client.post(
                "/v1/ai/extract",
                json={"messages": messages},
                timeout=settings.extract_timeout,
            )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            logger.debug(f"[extract] AI extraction completed, response length: {len(content)} chars")
            return content
        except httpx.TimeoutException:
            logger.error(f"[extract] Request timed out after {settings.extract_timeout}s waiting for AI provider")
            return None
        except httpx.HTTPStatusError as e:
            logger.error(f"[extract] AI provider returned HTTP {e.response.status_code}")
            return None
        except httpx.RequestError as e:
            logger.error(f"[extract] Network error connecting to AI provider: {type(e).__name__} - {e}")
            return None
        except (KeyError, IndexError) as e:
            logger.error(f"[extract] Unexpected response structure from AI provider: missing {e}")
            return None
        except Exception as e:
            logger.error(f"[extract] Unexpected error during AI extraction: {type(e).__name__} - {e}")
            return None

    @retry(
        retry=retry_if_exception_type(RateLimitError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=8) + wait_random(0, 1),
        reraise=True,
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    @retry(
        retry=retry_if_exception_type(ServerError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=2) + wait_random(0, 0.5),
        reraise=True,
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    async def notify(self, request: NotifyRequest) -> bool:
        try:
            response = await self.client.post(
                "/v1/notify",
                json=request.model_dump(),
                timeout=settings.notify_timeout,
            )
            if response.status_code == 429:
                logger.warning("[notify] Rate limited by provider (429), will retry")
                raise RateLimitError("Rate limit exceeded")
            if response.status_code >= 500:
                logger.warning(f"[notify] Provider server error ({response.status_code}), will retry")
                raise ServerError(f"Server error: {response.status_code}")
            if response.status_code == 401:
                logger.error("[notify] Authentication failed (401) - check API key configuration")
                return False
            if response.status_code == 422:
                logger.error(f"[notify] Validation error (422) - provider rejected the payload")
                return False
            response.raise_for_status()
            return True
        except (RateLimitError, ServerError):
            raise
        except httpx.TimeoutException:
            logger.error(f"[notify] Request timed out after {settings.notify_timeout}s")
            return False
        except httpx.RequestError as e:
            logger.error(f"[notify] Network error sending notification: {type(e).__name__} - {e}")
            return False
        except Exception as e:
            logger.error(f"[notify] Unexpected error: {type(e).__name__} - {e}")
            return False


provider_client = ProviderClient()
