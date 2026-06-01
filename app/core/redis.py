import json
import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from core.config import settings

logger = logging.getLogger("core.redis")


class RedisStore:
    def __init__(self) -> None:
        self.client: aioredis.Redis | None = None

    async def connect(self) -> None:
        try:
            self.client = aioredis.from_url(
                settings.redis_url, decode_responses=True
            )
            await self.client.ping()
            logger.info("Redis connection verified with PING")
        except RedisError as e:
            logger.critical(f"Cannot connect to Redis: {type(e).__name__} - {e}")
            raise
        except Exception as e:
            logger.critical(f"Unexpected error connecting to Redis: {type(e).__name__} - {e}")
            raise

    async def close(self) -> None:
        if self.client:
            try:
                await self.client.aclose()
                logger.info("Redis connection closed")
            except RedisError as e:
                logger.warning(f"Error closing Redis connection: {type(e).__name__} - {e}")

    def _key(self, request_id: str) -> str:
        return f"request:{request_id}"

    async def create_request(self, request_id: str, user_input: str) -> None:
        key = self._key(request_id)
        data = {
            "id": request_id,
            "user_input": user_input,
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "result": "",
            "error": "",
        }
        try:
            await self.client.hset(key, mapping=data)
            await self.client.expire(key, settings.request_ttl)
        except RedisError as e:
            logger.error(f"[create_request] Failed to write request {request_id} to Redis: {type(e).__name__} - {e}")
            raise

    async def get_request(self, request_id: str) -> dict | None:
        key = self._key(request_id)
        try:
            data = await self.client.hgetall(key)
            if not data:
                return None
            return data
        except RedisError as e:
            logger.error(f"[get_request] Failed to read request {request_id} from Redis: {type(e).__name__} - {e}")
            raise

    async def update_status(
        self,
        request_id: str,
        status: str,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        key = self._key(request_id)
        updates = {"status": status}
        if result is not None:
            updates["result"] = json.dumps(result)
        if error is not None:
            updates["error"] = error
        try:
            await self.client.hset(key, mapping=updates)
            logger.debug(f"[update_status] Request {request_id} status updated to '{status}'")
        except RedisError as e:
            logger.error(
                f"[update_status] Failed to update request {request_id} to '{status}': {type(e).__name__} - {e}"
            )
            raise


store = RedisStore()
