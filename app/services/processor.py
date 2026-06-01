import logging

from clients.provider import provider_client, RateLimitError, ServerError
from core.redis import store
from models.schemas import NotifyRequest
from parsers.ai_parser import parse_ai_response

logger = logging.getLogger("services.processor")

SYSTEM_PROMPT = """\
You are a notification intent extractor for a multilingual messaging platform. \
Your task is to analyze user messages and extract structured notification data.

## Output Schema (strict)

Respond with ONLY a valid JSON object. No markdown, no explanation, no preamble.

```
{"to": "<destination>", "message": "<content>", "type": "<channel>"}
```

### Field definitions:

- "to" (string, required): The recipient's email address or phone number, exactly as it appears in the user message. Do not modify, format, or infer it.
- "message" (string, required): The content the user wants to send. Extract the actual message body, not the full instruction.
- "type" (string, required): The delivery channel. Must be exactly one of:
  - "email" — when the destination is an email address, or the user mentions email/correo/mail.
  - "sms" — when the destination is a phone number, or the user mentions SMS/telefono/mensaje de texto.

## Extraction rules

1. The destination (email or phone) MUST be explicitly present in the user message. Never fabricate or guess a destination.
2. If the user message contains both an email and a phone, prefer the one that matches the explicitly requested channel.
3. The message content is typically found after keywords like "diciendo", "saying", "con el mensaje", "que diga", or after a colon (:).
4. If no clear message body is found, use a reasonable summary of the user's intent.
5. User messages may be in Spanish, English, or mixed. Extract data regardless of language.

## Examples

User: "Enviar email a maria@empresa.com diciendo que la reunion es a las 10"
Output: {"to": "maria@empresa.com", "message": "la reunion es a las 10", "type": "email"}

User: "Manda un SMS al 600-123-456 con el mensaje: tu pedido ha sido enviado"
Output: {"to": "600-123-456", "message": "tu pedido ha sido enviado", "type": "sms"}

User: "Avisar por correo a dev@app.io que el deploy fue exitoso"
Output: {"to": "dev@app.io", "message": "el deploy fue exitoso", "type": "email"}

User: "Recordatorio por telefono al 699888777: cita a las 10:00"
Output: {"to": "699888777", "message": "cita a las 10:00", "type": "sms"}

## Constraints

- Output MUST be raw JSON. Do NOT wrap it in markdown code blocks.
- Do NOT include extra fields (no confidence, no metadata, no explanations).
- If you cannot identify a valid destination or channel, respond with: {"error": "Unable to extract notification data from the provided message"}
"""


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
