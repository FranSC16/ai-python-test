# Notification Service - Design Document

## Overview

Servicio inteligente de notificaciones que procesa entradas de lenguaje natural, extrae datos estructurados via IA mock, y coordina el envio de notificaciones (email/sms). Construido con FastAPI, Redis, y httpx.

## Architecture

Layered Service architecture con separacion clara de responsabilidades:

```
POST /v1/requests ──────────────────────────────► Redis (queued)
POST /v1/requests/{id}/process ─► 202 Accepted
                                    │
                            Background Task
                                    │
                    ┌───────────────▼──────────────────┐
                    │        processor.py               │
                    │  1. Redis: status → processing    │
                    │  2. client → /v1/ai/extract       │
                    │  3. parser → ExtractedData        │
                    │  4. client → /v1/notify (retries) │
                    │  5. Redis: status → sent|failed   │
                    └──────────────────────────────────┘

GET /v1/requests/{id} ──────────────────────────► Redis (read status)
```

## File Structure

```
app/
├── main.py                  # FastAPI app + endpoints + lifespan
├── requirements.txt         # Dependencies
├── Dockerfile               # Container config
├── models/
│   └── schemas.py           # Pydantic models
├── services/
│   └── processor.py         # Orchestration: extract → parse → notify
├── clients/
│   └── provider.py          # Async HTTP client with retries (tenacity)
├── parsers/
│   └── ai_parser.py         # AI response cleaning pipeline
└── core/
    ├── config.py             # Settings (URLs, API key, retry config)
    └── redis.py              # Redis connection + CRUD operations
```

## Data Models

### CreateRequest (input)
- `user_input: str` — Natural language text from user

### NotificationRequest (stored in Redis)
- `id: str` — UUID4
- `user_input: str` — Original input
- `status: queued|processing|sent|failed` — Current state
- `created_at: datetime` — ISO timestamp
- `result: dict | None` — Extracted data (to, message, type)
- `error: str | None` — Failure reason if failed

### ExtractedData (parser output)
- `to: str` — Email or phone number
- `message: str` — Notification content
- `type: email|sms` — Delivery channel

### AIExtractRequest (to provider)
- `messages: list[AIMessage]` — system + user messages

### NotifyRequest (to provider)
- `to: str`, `message: str`, `type: email|sms`

### Data Flow
```
CreateRequest → NotificationRequest(queued)
  → AIExtractRequest → raw response → ExtractedData (parser)
    → NotifyRequest → NotificationRequest(sent|failed)
```

## AI Response Parser Pipeline

The core challenge. A 5-step pipeline where each step attempts to extract valid data. First success wins (early return for performance):

### Step 1: Direct JSON (handles ~50% of responses)
- `json.loads(content)` directly
- If it parses and has required fields → done

### Step 2: Extract from Markdown (handles ~10%)
- Regex to find ```json ... ``` or ``` ... ``` blocks
- Extract content and `json.loads()`

### Step 3: Extract Embedded JSON (handles ~10%)
- Regex to find `{...}` pattern in surrounding text
- `json.loads()` on the match

### Step 4: Repair Broken JSON (handles ~10%)
- Replace single quotes → double quotes
- Add quotes to unquoted keys
- Remove trailing `...` or truncation artifacts
- Attempt `json.loads()` on repaired string

### Step 5: Failure (handles ~10%)
- Total refusal or unrecoverable text
- Return None → status becomes `failed`

### Key Normalization (after any successful parse)

```python
KEY_MAP = {
    "recipient": "to", "to": "to", "destination": "to",
    "body": "message", "message": "message", "text": "message",
    "channel": "type", "type": "type", "method": "type",
}
```

- Lowercase all keys, map to canonical names
- If `type` missing but `to` looks like email → infer "email"
- If `type` missing but `to` looks like phone → infer "sms"
- Ignore extra fields (confidence, latency_ms, etc.)

### Performance
- All regex patterns compiled once at module level
- Pipeline short-circuits on first successful step

## HTTP Client (provider.py)

### Singleton httpx.AsyncClient
- Initialized in FastAPI lifespan, closed on shutdown
- Connection pooling (no per-request client creation)

### Extract endpoint
- `POST localhost:3001/v1/ai/extract`
- Header: `X-API-Key: test-dev-2026`
- Timeout: 10s (IA mock takes 1.5-3s)
- No retries (IA doesn't return 429/500)

### Notify endpoint
- `POST localhost:3001/v1/notify`
- Header: `X-API-Key: test-dev-2026`
- Timeout: 5s
- Adaptive retry strategy with tenacity:
  - **429 (rate limit):** wait 2s, 4s, 8s + jitter
  - **500 (server error):** wait 0.5s, 1s, 2s + jitter
  - Max 3 attempts
  - Random jitter +/-0.5s

## Redis Storage

### Key Pattern
```
request:{uuid}  →  Redis Hash
```

### Fields
- `user_input`, `status`, `created_at`, `result` (JSON string), `error`

### TTL
- 1 hour per key (auto-cleanup)

### Operations
- `create_request(id, user_input)` → HSET + EXPIRE
- `get_request(id)` → HGETALL
- `update_status(id, status, result?, error?)` → HSET

### Dependency
- `redis[hiredis]` for protocol parsing performance
- `redis.asyncio` for async operations

## Endpoints

### POST /v1/requests
1. Validate input (Pydantic)
2. Generate UUID4
3. Save to Redis with status "queued"
4. Return `201 Created` with `{"id": uuid}`

### POST /v1/requests/{id}/process
1. Verify ID exists in Redis
2. Launch background task (asyncio.create_task)
3. Return `202 Accepted` with `{"id": id, "status": "processing"}`

### GET /v1/requests/{id}
1. Read from Redis
2. If not found → 404
3. Return `200 OK` with `{"id": id, "status": status}`

## System Prompt

```
You are a structured data extractor. Given a user message, extract exactly
these fields and respond ONLY with a JSON object, no markdown, no explanation:
{"to": "<email or phone>", "message": "<content>", "type": "email|sms"}
```

## Infrastructure Changes

### docker-compose.yaml additions
- `redis` service: `redis:7-alpine`, port 6379, healthcheck
- `app` depends_on redis (service_healthy)

### requirements.txt additions
- `redis[hiredis]`

## Concurrency Model

- Endpoints are async (FastAPI + uvicorn)
- Processing runs as background tasks (asyncio.create_task)
- 202 Accepted returns immediately, state updates async
- Supports 200 concurrent VUs from k6 load test

## Error Handling

- Every step in processor has try/catch
- Failed state always written to Redis with error reason
- No request stays in "processing" forever
- HTTP timeouts prevent hanging on provider failures
