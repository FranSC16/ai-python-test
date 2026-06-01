import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
