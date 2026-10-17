"""Shared API Gateway response and input-validation helpers."""
import json
import os
from typing import Any, Dict

ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")


def response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
            "Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
        },
        "body": json.dumps(body),
    }


def server_error(context: Any) -> Dict[str, Any]:
    """Generic 500: details go to CloudWatch, never to the caller."""
    request_id = getattr(context, "aws_request_id", None)
    return response(500, {"error": "Internal server error", "request_id": request_id})


class BadRequest(ValueError):
    """Raised for invalid client input; becomes a 400."""


def parse_body(event: Dict[str, Any]) -> Dict[str, Any]:
    raw = (event or {}).get("body") or "{}"
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise BadRequest("Request body must be valid JSON")
    if not isinstance(body, dict):
        raise BadRequest("Request body must be a JSON object")
    return body


def clamp_int(value: Any, default: int, low: int, high: int, name: str) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise BadRequest(f"'{name}' must be an integer")
    return max(low, min(high, number))


def require_text(value: Any, name: str, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BadRequest(f"'{name}' is required")
    value = value.strip()
    if len(value) > max_len:
        raise BadRequest(f"'{name}' must be at most {max_len} characters")
    return value
