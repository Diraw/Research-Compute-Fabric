"""Secret redaction for logs, audit records and error messages.

RCF never stores credentials, but users paste command lines and remote output
that may contain tokens.  Everything that leaves the platform (CLI --json, MCP
responses, audit rows, persisted error strings) passes through :func:`redact`.
"""

from __future__ import annotations

import re
from typing import Any

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(password|passwd|pwd)\s*[=:]\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"(?i)\b(token|api[_-]?key|secret|access[_-]?key)\s*[=:]\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer <redacted>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted-aws-key>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "<redacted-github-token>"),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "<redacted-private-key>",
    ),
    (re.compile(r"(?i)(-i|--identity(?:-file)?)\s+\S+"), r"\1 <redacted-path>"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+:[^@\s]+@[A-Za-z0-9.-]+\b"), "<redacted-credentials>@host"),
)

MAX_TEXT = 4000


def redact(text: str | None) -> str:
    if not text:
        return ""
    result = text
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    if len(result) > MAX_TEXT:
        result = result[:MAX_TEXT] + "...<truncated>"
    return result


def redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: (redact_obj(v) if not _secret_key(str(k)) else "<redacted>") for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v) for v in value]
    return value


def _secret_key(key: str) -> bool:
    key_l = key.lower()
    return any(word in key_l for word in ("password", "secret", "token", "private_key", "passphrase"))
