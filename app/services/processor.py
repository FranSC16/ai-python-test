import logging

from clients.provider import provider_client, RateLimitError, ServerError
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
        # Step 1: Retrieve request from store
        req = await store.get_request(request_id)
        if not req:
            logger.error(f"[processor] Request {request_id} not found in store - may have expired or never existed")
            return

        await store.update_status(request_id, "processing")
        logger.info(f"[processor] Starting processing for request {request_id}")

        user_input = req["user_input"]

        # Step 2: Call AI extraction
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_input},
        ]
        raw_response = await provider_client.extract(messages)

        if not raw_response:
            logger.warning(f"[processor] AI extraction returned empty response for request {request_id}")
            await store.update_status(
                request_id, "failed", error="AI extraction returned no response"
            )
            return

        # Step 3: Parse AI response through guardrails pipeline
        extracted = parse_ai_response(raw_response)

        if not extracted:
            logger.warning(
                f"[processor] Failed to parse AI response for request {request_id} - "
                f"response length: {len(raw_response)} chars, "
                f"preview: '{raw_response[:80]}...'"
            )
            await store.update_status(
                request_id,
                "failed",
                error="Could not extract structured data from AI response",
            )
            return

        logger.info(
            f"[processor] Successfully extracted data for request {request_id}: "
            f"type={extracted.type}"
        )

        # Step 4: Send notification
        notify_request = NotifyRequest(
            to=extracted.to, message=extracted.message, type=extracted.type
        )

        try:
            success = await provider_client.notify(notify_request)
        except RateLimitError:
            logger.error(f"[processor] Notification for {request_id} failed: rate limit exceeded after all retries")
            success = False
        except ServerError:
            logger.error(f"[processor] Notification for {request_id} failed: provider server errors after all retries")
            success = False
        except Exception as e:
            logger.error(
                f"[processor] Unexpected error sending notification for {request_id}: "
                f"{type(e).__name__} - {e}"
            )
            success = False

        # Step 5: Update final status
        if success:
            await store.update_status(
                request_id,
                "sent",
                result=extracted.model_dump(),
            )
            logger.info(f"[processor] Request {request_id} completed successfully - notification delivered")
        else:
            await store.update_status(
                request_id, "failed", error="Notification delivery failed after retries"
            )
            logger.warning(f"[processor] Request {request_id} failed - notification could not be delivered")

    except Exception as e:
        logger.error(
            f"[processor] Unhandled exception while processing request {request_id}: "
            f"{type(e).__name__} - {e}",
            exc_info=True,
        )
        try:
            await store.update_status(
                request_id, "failed", error=f"Internal processing error: {type(e).__name__}"
            )
        except Exception as store_err:
            logger.error(
                f"[processor] Additionally failed to update error status for {request_id}: "
                f"{type(store_err).__name__} - {store_err}"
            )
