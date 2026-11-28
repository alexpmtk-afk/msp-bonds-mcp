"""Pure helpers, suitable for synthetic unit tests."""
from __future__ import annotations
import base64
import hashlib
import re
from pathlib import Path


class InputError(ValueError):
    pass


def bounded_number(value: int, low: int = 1, high: int = 100) -> int:
    if not isinstance(value, int) or value < low or value > high:
        raise InputError(f"Number must be between {low} and {high}")
    return value


def drive_q_literal(value: str) -> str:
    if len(value) > 1000:
        raise InputError("Drive filter too long")
    return value.replace("\\", "\\\\").replace("'", "\\'")


def safe_filename(value: str) -> str:
    # Gmail header filenames are untrusted and must never become paths.
    base = Path(value.replace("\\", "/")).name.strip()
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).rstrip(". ")
    if not base or base in (".", ".."):
        base = "attachment.bin"
    return base[:150]


def decode_base64url(value: str) -> bytes:
    if not isinstance(value, str):
        raise InputError("Expected base64url-encoded attachment")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise InputError("Invalid base64url attachment") from exc


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def attachment_parts(payload: dict) -> list[dict]:
    """Return safe metadata for named attachments without exposing message body bytes."""
    result = []
    stack = [payload]
    while stack:
        part = stack.pop()
        stack.extend(reversed(part.get("parts", []) or []))
        body = part.get("body") or {}
        name = part.get("filename", "")
        attachment_id = body.get("attachmentId")
        if name or attachment_id:
            result.append({"filename": safe_filename(name) if name else "attachment.bin",
                           "mime_type": part.get("mimeType", ""),
                           "part_id": part.get("partId", ""),
                           "attachment_id": attachment_id,
                           "size": body.get("size")})
    return result


def message_headers(payload: dict) -> dict:
    headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", [])}
    return {key: headers.get(key, "")[:800] for key in ("from", "to", "date", "subject")}
