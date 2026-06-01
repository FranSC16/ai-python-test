import json
from datetime import datetime, timezone

import redis.asyncio as aioredis

from core.config import settings


class RedisStore:
    def __init__(self) -> None:
        self.client: aioredis.Redis | None = None

    async def connect(self) -> None:
        self.client = aioredis.from_url(
            settings.redis_url, decode_responses=True
        )

    async def close(self) -> None:
        if self.client:
            await self.client.aclose()

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
        await self.client.hset(key, mapping=data)
        await self.client.expire(key, settings.request_ttl)

    async def get_request(self, request_id: str) -> dict | None:
        key = self._key(request_id)
        data = await self.client.hgetall(key)
        if not data:
            return None
        return data

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
        await self.client.hset(key, mapping=updates)


store = RedisStore()
