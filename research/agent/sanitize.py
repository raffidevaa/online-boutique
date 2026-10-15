"""Redaction applied before telemetry is sent to an external LLM provider."""

from __future__ import annotations

import re


_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|password|secret)\b\s*[:=]\s*"
    r"[\"']?[^,\s}\"']+"
)
_URL = re.compile(r"https?://[^\s\"'<>]+")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_UUID = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
_LONG_HEX_ID = re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{32}|[0-9a-f]{64})(?![0-9a-f])")


def sanitize_text(value: str) -> str:
    """Remove common credentials and network identifiers while preserving RCA labels."""
    sanitized = _BEARER.sub("Bearer <redacted>", value)
    sanitized = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=<redacted>", sanitized
    )
    sanitized = _URL.sub("<redacted-url>", sanitized)
    sanitized = _IPV4.sub("<redacted-ip>", sanitized)
    sanitized = _EMAIL.sub("<redacted-email>", sanitized)
    sanitized = _UUID.sub("<redacted-id>", sanitized)
    return _LONG_HEX_ID.sub("<redacted-id>", sanitized)


def sanitize_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Copy and sanitize provider messages without mutating the local session trace."""
    sanitized: list[dict[str, str]] = []
    for message in messages:
        sanitized.append(
            {
                key: sanitize_text(value) if isinstance(value, str) else value
                for key, value in message.items()
            }
        )
    return sanitized
