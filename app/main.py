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
