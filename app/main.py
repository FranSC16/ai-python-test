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
    try:
        await store.connect()
        logger.info("Redis connection established")
    except Exception as e:
        logger.critical(f"Failed to connect to Redis: {type(e).__name__} - {e}")
        raise

    try:
        await provider_client.connect()
        logger.info("HTTP provider client initialized")
    except Exception as e:
        logger.critical(f"Failed to initialize provider client: {type(e).__name__} - {e}")
        raise

    logger.info("Application startup complete")
    yield

    await provider_client.close()
    await store.close()
    logger.info("Application shutdown complete: all connections closed")


app = FastAPI(title="Notification Service", lifespan=lifespan)


@app.post("/v1/requests", status_code=201, response_model=CreateResponse)
async def create_request(body: CreateRequest):
    request_id = str(uuid.uuid4())
    try:
        await store.create_request(request_id, body.user_input)
    except Exception as e:
        logger.error(f"[create_request] Failed to store request {request_id}: {type(e).__name__} - {e}")
        raise HTTPException(status_code=500, detail="Internal error while creating request")
    logger.info(f"[create_request] Request {request_id} created successfully")
    return CreateResponse(id=request_id)


@app.post("/v1/requests/{request_id}/process", status_code=202)
async def process(request_id: str):
    try:
        req = await store.get_request(request_id)
    except Exception as e:
        logger.error(f"[process] Failed to read request {request_id} from store: {type(e).__name__} - {e}")
        raise HTTPException(status_code=500, detail="Internal error while reading request")
    if not req:
        logger.warning(f"[process] Request {request_id} not found in store")
        raise HTTPException(status_code=404, detail="Request not found")
    asyncio.create_task(process_request(request_id))
    logger.info(f"[process] Background processing started for request {request_id}")
    return {"id": request_id, "status": "processing"}


@app.get("/v1/requests/{request_id}", response_model=StatusResponse)
async def get_status(request_id: str):
    try:
        req = await store.get_request(request_id)
    except Exception as e:
        logger.error(f"[get_status] Failed to read request {request_id} from store: {type(e).__name__} - {e}")
        raise HTTPException(status_code=500, detail="Internal error while reading request")
    if not req:
        logger.warning(f"[get_status] Request {request_id} not found in store")
        raise HTTPException(status_code=404, detail="Request not found")
    return StatusResponse(id=req["id"], status=req["status"])
