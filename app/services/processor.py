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

        extracted = parse_ai_response(raw_response)

        if not extracted:
            await store.update_status(
                request_id,
                "failed",
                error=f"Could not parse AI response: {raw_response[:200]}",
            )
            return

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
