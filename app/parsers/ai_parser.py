import json
import re

from models.schemas import ExtractedData

_MARKDOWN_JSON_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
_EMBEDDED_JSON_RE = re.compile(r"\{[^{}]*\}")
_SINGLE_QUOTES_RE = re.compile(r"'")
_UNQUOTED_KEYS_RE = re.compile(r"(\{|,)\s*(\w+)\s*:")
_TRAILING_NOISE_RE = re.compile(r"\.\.\.\s*$")

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

    data = _try_json(content)
    if data is not None:
        return _normalize(data)

    data = _try_markdown(content)
    if data is not None:
        return _normalize(data)

    data = _try_embedded(content)
    if data is not None:
        return _normalize(data)

    data = _try_repair(content)
    if data is not None:
        return _normalize(data)

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
    for candidate in _EMBEDDED_JSON_RE.findall(content):
        repaired = _repair_string(candidate)
        result = _try_json(repaired)
        if result is not None:
            return result

    repaired = _repair_string(content)
    result = _try_json(repaired)
    if result is not None:
        return result

    brace_start = content.find("{")
    if brace_start != -1:
        fragment = content[brace_start:]
        fragment = _TRAILING_NOISE_RE.sub("", fragment)
        if fragment.count("{") > fragment.count("}"):
            fragment = fragment.rstrip().rstrip(",") + "}"
        repaired = _repair_string(fragment)
        result = _try_json(repaired)
        if result is not None:
            return result

    return None


def _repair_string(s: str) -> str:
    s = _TRAILING_NOISE_RE.sub("", s).strip()
    if s.count("{") > s.count("}"):
        s = s.rstrip().rstrip(",") + "}"
    s = _SINGLE_QUOTES_RE.sub('"', s)
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

    if not notif_type:
        if _EMAIL_RE.match(str(to)):
            notif_type = "email"
        elif _PHONE_RE.match(str(to)):
            notif_type = "sms"
        else:
            return None

    if notif_type not in ("email", "sms"):
        return None

    try:
        return ExtractedData(to=str(to), message=str(message), type=notif_type)
    except Exception:
        return None
