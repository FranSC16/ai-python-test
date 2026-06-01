# Notification Service Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement an intelligent notification service that processes natural language inputs, extracts structured data via AI mock, and sends notifications with robust error handling.

**Architecture:** Layered service with FastAPI endpoints delegating to a processor service that orchestrates AI extraction (with a 5-step parser pipeline for noisy LLM responses), notification delivery (with adaptive retries), and Redis-backed state management. Background tasks handle processing asynchronously.

**Tech Stack:** FastAPI, httpx, Pydantic v2, tenacity, redis[hiredis], Docker Compose

**Design doc:** `docs/plans/2026-06-01-notification-service-design.md`

---

### Task 1: Infrastructure Setup (Docker + Dependencies)

**Files:**
- Modify: `app/requirements.txt`
- Modify: `docker-compose.yaml`

**Step 1: Update requirements.txt**

```txt
fastapi==0.110.0
uvicorn[standard]==0.27.1
httpx==0.27.0
pydantic==2.6.3
tenacity==8.2.3
redis[hiredis]==5.0.1
```

**Step 2: Add Redis service to docker-compose.yaml**

Add `redis` service before `app`, update `app` to depend on it:

```yaml
  redis:
    image: redis:7-alpine
    container_name: ia-redis
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5
```

Update `app` service:
```yaml
  app:
    build:
      context: ./app
      dockerfile: Dockerfile
    container_name: ia-app
    network_mode: "service:provider"
    depends_on:
      provider:
        condition: service_healthy
      redis:
        condition: service_healthy
```

Note: since app uses `network_mode: "service:provider"`, it shares provider's network. Redis needs to be accessible. Add redis to provider's network or use container name. Since app shares provider network, redis must be reachable. Add `redis` to the provider network by making provider depend on redis, or reference redis by container hostname. The simplest approach: app accesses redis via `ia-redis:6379` since Docker Compose puts all services on the same default network (network_mode only affects the app container's network stack — it shares provider's, which is on the default network where redis also lives).

**Step 3: Commit**

```bash
git add app/requirements.txt docker-compose.yaml
git commit -m "feat: add Redis service and dependency to infrastructure"
```

---

### Task 2: Config Module

**Files:**
- Create: `app/core/__init__.py`
- Create: `app/core/config.py`

**Step 1: Create core package**

`app/core/__init__.py` — empty file.

**Step 2: Write config.py**

```python
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
```

Note: `pydantic_settings` is included with `pydantic==2.6.3` but may need explicit install. Check — if needed, add `pydantic-settings==2.2.1` to requirements.txt.

**Step 3: Commit**

```bash
git add app/core/
git commit -m "feat: add config module with settings"
```

---

### Task 3: Pydantic Models

**Files:**
- Create: `app/models/__init__.py`
- Create: `app/models/schemas.py`

**Step 1: Create models package**

`app/models/__init__.py` — empty file.

**Step 2: Write schemas.py**

```python
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class CreateRequest(BaseModel):
    user_input: str = Field(..., min_length=1)


class CreateResponse(BaseModel):
    id: str


class StatusResponse(BaseModel):
    id: str
    status: Literal["queued", "processing", "sent", "failed"]


class ExtractedData(BaseModel):
    to: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    type: Literal["email", "sms"]


class AIMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class AIExtractRequest(BaseModel):
    messages: list[AIMessage]


class NotifyRequest(BaseModel):
    to: str
    message: str
    type: Literal["email", "sms"]
```

**Step 3: Commit**

```bash
git add app/models/
git commit -m "feat: add Pydantic data models"
```

---

### Task 4: Redis Storage Module

**Files:**
- Create: `app/core/redis.py`

**Step 1: Write redis.py**

```python
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
```

**Step 2: Commit**

```bash
git add app/core/redis.py
git commit -m "feat: add Redis storage module with async operations"
```

---

### Task 5: AI Response Parser

This is the most critical module. Build the 5-step pipeline.

**Files:**
- Create: `app/parsers/__init__.py`
- Create: `app/parsers/ai_parser.py`
- Create: `app/tests/__init__.py`
- Create: `app/tests/test_parser.py`

**Step 1: Write the failing tests**

`app/tests/test_parser.py`:

```python
import pytest
from parsers.ai_parser import parse_ai_response


class TestDirectJSON:
    def test_clean_json(self):
        raw = '{"to": "test@test.com", "message": "hello", "type": "email"}'
        result = parse_ai_response(raw)
        assert result is not None
        assert result.to == "test@test.com"
        assert result.message == "hello"
        assert result.type == "email"

    def test_clean_json_sms(self):
        raw = '{"to": "600111222", "message": "confirmed", "type": "sms"}'
        result = parse_ai_response(raw)
        assert result is not None
        assert result.type == "sms"


class TestAlternativeKeys:
    def test_recipient_body_channel(self):
        raw = '{"Recipient": "a@b.com", "body": "hi", "channel": "email"}'
        result = parse_ai_response(raw)
        assert result is not None
        assert result.to == "a@b.com"
        assert result.message == "hi"
        assert result.type == "email"

    def test_destination_text_method(self):
        raw = '{"destination": "a@b.com", "text": "hi", "method": "email"}'
        result = parse_ai_response(raw)
        assert result is not None
        assert result.to == "a@b.com"

    def test_capitalized_keys(self):
        raw = '{"To": "a@b.com", "Message": "hi", "Type": "email"}'
        result = parse_ai_response(raw)
        assert result is not None
        assert result.to == "a@b.com"


class TestExtraAndMissingFields:
    def test_extra_fields_ignored(self):
        raw = '{"to": "a@b.com", "message": "hi", "type": "email", "confidence": 0.99, "latency_ms": 120}'
        result = parse_ai_response(raw)
        assert result is not None
        assert result.to == "a@b.com"

    def test_missing_type_infer_email(self):
        raw = '{"to": "a@b.com", "message": "hi"}'
        result = parse_ai_response(raw)
        assert result is not None
        assert result.type == "email"

    def test_missing_type_infer_sms(self):
        raw = '{"to": "600111222", "message": "hi"}'
        result = parse_ai_response(raw)
        assert result is not None
        assert result.type == "sms"

    def test_missing_destination(self):
        raw = '{"message": "hi", "type": "email"}'
        result = parse_ai_response(raw)
        assert result is None


class TestMarkdownWrapped:
    def test_json_code_block(self):
        raw = 'He extraido:\n```json\n{"to": "a@b.com", "message": "hi", "type": "email"}\n```'
        result = parse_ai_response(raw)
        assert result is not None
        assert result.to == "a@b.com"

    def test_generic_code_block(self):
        raw = 'Output:\n```\n{"to": "a@b.com", "message": "hi", "type": "email"}\n```'
        result = parse_ai_response(raw)
        assert result is not None

    def test_embedded_in_text(self):
        raw = 'Claro, el destino es a@b.com. En formato JSON: {"to": "a@b.com", "message": "hi", "type": "email"}'
        result = parse_ai_response(raw)
        assert result is not None
        assert result.to == "a@b.com"


class TestBrokenJSON:
    def test_single_quotes(self):
        raw = "{'to': 'a@b.com', 'message': 'hi', 'type': 'email'}"
        result = parse_ai_response(raw)
        assert result is not None
        assert result.to == "a@b.com"

    def test_unquoted_keys(self):
        raw = '{to: "a@b.com", message: "hi", type: "email"}'
        result = parse_ai_response(raw)
        assert result is not None
        assert result.to == "a@b.com"

    def test_truncated_json(self):
        raw = '{"to": "a@b.com", "message": "hi", "type": "email" ...'
        result = parse_ai_response(raw)
        assert result is not None
        assert result.to == "a@b.com"


class TestRefusal:
    def test_refusal_spanish(self):
        raw = "Lo siento, como IA no tengo permitido procesar datos de contacto personales."
        result = parse_ai_response(raw)
        assert result is None

    def test_refusal_english(self):
        raw = "Refused: Content analysis flagged sensitive information."
        result = parse_ai_response(raw)
        assert result is None

    def test_error_message(self):
        raw = "Error: El contenido del mensaje viola las politicas de seguridad (Potential Spam)."
        result = parse_ai_response(raw)
        assert result is None
```

**Step 2: Run tests to verify they fail**

```bash
cd app && python -m pytest tests/test_parser.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'parsers'`

**Step 3: Write the parser implementation**

`app/parsers/__init__.py` — empty file.

`app/parsers/ai_parser.py`:

```python
import json
import re
from models.schemas import ExtractedData

# Compiled regex patterns (once, at module level)
_MARKDOWN_JSON_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
_EMBEDDED_JSON_RE = re.compile(r"\{[^{}]*\}")
_SINGLE_QUOTES_RE = re.compile(r"'")
_UNQUOTED_KEYS_RE = re.compile(r"(\{|,)\s*(\w+)\s*:")
_TRAILING_NOISE_RE = re.compile(r"\.\.\.\s*$")

# Key normalization map
_KEY_MAP = {
    "to": "to",
    "recipient": "to",
    "destination": "to",
    "message": "message",
    "body": "message",
    "text": "message",
    "type": "type",
    "channel": "type",
    "method": "type",
}

_EMAIL_RE = re.compile(r"[\w.\-]+@[\w.\-]+\.\w+")
_PHONE_RE = re.compile(r"\b\d{3}-?\d{3}-?\d{3,4}\b")


def parse_ai_response(raw: str) -> ExtractedData | None:
    if not raw or not raw.strip():
        return None

    content = raw.strip()

    # Step 1: Try direct JSON parse
    data = _try_json(content)
    if data is not None:
        return _normalize(data)

    # Step 2: Extract from markdown code blocks
    data = _try_markdown(content)
    if data is not None:
        return _normalize(data)

    # Step 3: Extract embedded JSON from text
    data = _try_embedded(content)
    if data is not None:
        return _normalize(data)

    # Step 4: Repair broken JSON
    data = _try_repair(content)
    if data is not None:
        return _normalize(data)

    # Step 5: Could not extract — refusal or unrecoverable
    return None


def _try_json(content: str) -> dict | None:
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _try_markdown(content: str) -> dict | None:
    match = _MARKDOWN_JSON_RE.search(content)
    if match:
        return _try_json(match.group(1).strip())
    return None


def _try_embedded(content: str) -> dict | None:
    matches = _EMBEDDED_JSON_RE.findall(content)
    for candidate in matches:
        result = _try_json(candidate)
        if result is not None:
            return result
    return None


def _try_repair(content: str) -> dict | None:
    # Try to find any JSON-like structure first
    for candidate in _EMBEDDED_JSON_RE.findall(content):
        repaired = _repair_string(candidate)
        result = _try_json(repaired)
        if result is not None:
            return result

    # Try repairing the full content
    repaired = _repair_string(content)
    result = _try_json(repaired)
    if result is not None:
        return result

    # Handle truncated JSON: find opening { and try to close it
    brace_start = content.find("{")
    if brace_start != -1:
        fragment = content[brace_start:]
        fragment = _TRAILING_NOISE_RE.sub("", fragment)
        # Try to close truncated JSON
        if fragment.count("{") > fragment.count("}"):
            fragment = fragment.rstrip().rstrip(",") + "}"
        repaired = _repair_string(fragment)
        result = _try_json(repaired)
        if result is not None:
            return result

    return None


def _repair_string(s: str) -> str:
    # Remove trailing noise like ...
    s = _TRAILING_NOISE_RE.sub("", s).strip()
    # Close unclosed braces
    if s.count("{") > s.count("}"):
        s = s.rstrip().rstrip(",") + "}"
    # Replace single quotes with double quotes
    s = _SINGLE_QUOTES_RE.sub('"', s)
    # Add quotes to unquoted keys
    s = _UNQUOTED_KEYS_RE.sub(r'\1 "\2":', s)
    return s


def _normalize(data: dict) -> ExtractedData | None:
    normalized = {}
    for key, value in data.items():
        canonical = _KEY_MAP.get(key.lower())
        if canonical and canonical not in normalized:
            normalized[canonical] = value

    to = normalized.get("to")
    message = normalized.get("message")
    notif_type = normalized.get("type")

    if not to or not message:
        return None

    # Infer type if missing
    if not notif_type:
        if _EMAIL_RE.match(str(to)):
            notif_type = "email"
        elif _PHONE_RE.match(str(to)):
            notif_type = "sms"
        else:
            return None

    # Validate type value
    if notif_type not in ("email", "sms"):
        return None

    try:
        return ExtractedData(to=str(to), message=str(message), type=notif_type)
    except Exception:
        return None
```

**Step 4: Run tests to verify they pass**

```bash
cd app && python -m pytest tests/test_parser.py -v
```

Expected: ALL PASS

**Step 5: Commit**

```bash
git add app/parsers/ app/tests/
git commit -m "feat: add AI response parser with 5-step pipeline and tests"
```

---

### Task 6: HTTP Client with Retries

**Files:**
- Create: `app/clients/__init__.py`
- Create: `app/clients/provider.py`

**Step 1: Create clients package**

`app/clients/__init__.py` — empty file.

**Step 2: Write provider.py**

```python
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
from models.schemas import AIExtractRequest, NotifyRequest

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
            logger.error(f"Notify failed: {e}")
            return False


provider_client = ProviderClient()
```

**Step 3: Commit**

```bash
git add app/clients/
git commit -m "feat: add HTTP client with adaptive retry strategy"
```

---

### Task 7: Processor Service

**Files:**
- Create: `app/services/__init__.py`
- Create: `app/services/processor.py`

**Step 1: Create services package**

`app/services/__init__.py` — empty file.

**Step 2: Write processor.py**

```python
import logging

from clients.provider import provider_client
from core.redis import store
from models.schemas import NotifyRequest
from parsers.ai_parser import parse_ai_response

logger = logging.getLogger("services.processor")

SYSTEM_PROMPT = (
    "You are a structured data extractor. Given a user message, extract exactly "
    "these fields and respond ONLY with a JSON object, no markdown, no explanation:\n"
    '{"to": "<email or phone>", "message": "<content>", "type": "email|sms"}\n'
    "Rules:\n"
    '- "to" must be an email address or phone number found in the user message\n'
    '- "type" must be "email" if destination is an email, "sms" if it is a phone number\n'
    '- "message" is the content the user wants to send\n'
    "- Respond with ONLY the JSON object, nothing else"
)


async def process_request(request_id: str) -> None:
    try:
        req = await store.get_request(request_id)
        if not req:
            logger.error(f"Request {request_id} not found")
            return

        await store.update_status(request_id, "processing")

        user_input = req["user_input"]

        # Step 1: Call AI extraction
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_input},
        ]
        raw_response = await provider_client.extract(messages)

        if not raw_response:
            await store.update_status(
                request_id, "failed", error="AI extraction returned no response"
            )
            return

        # Step 2: Parse AI response
        extracted = parse_ai_response(raw_response)

        if not extracted:
            await store.update_status(
                request_id,
                "failed",
                error=f"Could not parse AI response: {raw_response[:200]}",
            )
            return

        # Step 3: Send notification
        notify_request = NotifyRequest(
            to=extracted.to, message=extracted.message, type=extracted.type
        )

        try:
            success = await provider_client.notify(notify_request)
        except Exception as e:
            logger.error(f"Notification failed after retries: {e}")
            success = False

        if success:
            await store.update_status(
                request_id,
                "sent",
                result=extracted.model_dump(),
            )
        else:
            await store.update_status(
                request_id, "failed", error="Notification delivery failed"
            )

    except Exception as e:
        logger.error(f"Unexpected error processing {request_id}: {e}")
        try:
            await store.update_status(
                request_id, "failed", error=f"Unexpected error: {str(e)}"
            )
        except Exception:
            pass
```

**Step 3: Commit**

```bash
git add app/services/
git commit -m "feat: add processor service orchestrating extract-parse-notify pipeline"
```

---

### Task 8: FastAPI Endpoints (main.py)

**Files:**
- Modify: `app/main.py`

**Step 1: Write main.py with all endpoints**

```python
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from clients.provider import provider_client
from core.redis import store
from models.schemas import CreateRequest, CreateResponse, StatusResponse
from services.processor import process_request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.connect()
    await provider_client.connect()
    logger.info("Startup complete: Redis and HTTP client connected")
    yield
    await provider_client.close()
    await store.close()
    logger.info("Shutdown complete")


app = FastAPI(title="Notification Service", lifespan=lifespan)


@app.post("/v1/requests", status_code=201, response_model=CreateResponse)
async def create_request(body: CreateRequest):
    request_id = str(uuid.uuid4())
    await store.create_request(request_id, body.user_input)
    return CreateResponse(id=request_id)


@app.post("/v1/requests/{request_id}/process", status_code=202)
async def process(request_id: str):
    req = await store.get_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    asyncio.create_task(process_request(request_id))
    return {"id": request_id, "status": "processing"}


@app.get("/v1/requests/{request_id}", response_model=StatusResponse)
async def get_status(request_id: str):
    req = await store.get_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    return StatusResponse(id=req["id"], status=req["status"])
```

**Step 2: Commit**

```bash
git add app/main.py
git commit -m "feat: implement FastAPI endpoints with async processing"
```

---

### Task 9: Build and Run Full Stack

**Step 1: Build and start all services**

```bash
docker-compose up -d --build
```

**Step 2: Verify services are healthy**

```bash
docker-compose ps
```

Expected: `ia-app`, `ia-provider`, `ia-redis`, `ia-influxdb`, `ia-grafana` all running/healthy.

**Step 3: Manual smoke test**

```bash
# Create request
curl -s -X POST http://localhost:5000/v1/requests \
  -H "Content-Type: application/json" \
  -d '{"user_input": "Enviar email a juan@example.com diciendo hola"}' | jq .

# Process it (use the id from above)
curl -s -X POST http://localhost:5000/v1/requests/{ID}/process | jq .

# Check status (wait 3-4 seconds for AI processing)
sleep 4
curl -s http://localhost:5000/v1/requests/{ID} | jq .
```

Expected: status should be `"sent"` or `"failed"` (depends on the stochastic mock response).

**Step 4: Check logs if anything fails**

```bash
docker-compose logs app --tail 50
```

**Step 5: Commit any fixes if needed**

---

### Task 10: Run k6 Load Tests

**Step 1: Run the k6 validation suite**

```bash
docker-compose run --rm load-test
```

**Step 2: Analyze results**

Check k6 output for check pass rates. Target: high pass rate on all checks:
- `create status is 201 or 200`
- `id is present in response`
- `process status is 202 or 200`
- `status request is 200`
- `status is valid string`

**Step 3: View Grafana dashboard**

Open http://localhost:3000/d/ia-performance-scorecard/ in browser.

**Step 4: Fix any issues discovered and re-run**

Common issues:
- Redis connection errors → check network_mode and hostname
- Timeouts → adjust timeout settings in config
- Parser failures → check logs, add missing cases

**Step 5: Final commit**

```bash
git add -A
git commit -m "feat: complete notification service implementation"
```

---

## Execution Notes

- Tasks 1-4 are infrastructure/foundational — do them sequentially
- Task 5 (parser) is the most complex — TDD with full test coverage
- Tasks 6-8 build on each other sequentially
- Tasks 9-10 are integration/validation
- If `pydantic-settings` is not bundled with pydantic 2.6.3, add it to requirements.txt
- The `network_mode: "service:provider"` means the app container shares the provider's network stack. Redis hostname `ia-redis` should be resolvable via Docker's default network. If not, consider using the provider's network explicitly or adjusting the Redis URL.
