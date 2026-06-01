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
