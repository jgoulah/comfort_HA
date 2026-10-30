"""Kumo Cloud command normalization and safe error formatting."""

from __future__ import annotations

import json
from math import isfinite
import re
from typing import Any

from .const import OPERATION_MODE_AUTO

REPORTED_AUTO_MODES = frozenset({OPERATION_MODE_AUTO, "autoCool", "autoHeat"})
CLOUD_SETPOINT_PRECISION = 1
_SETPOINT_COMMANDS = ("spCool", "spHeat", "spAuto")

_SENSITIVE_KEY_PARTS = (
    "access",
    "authorization",
    "cookie",
    "credential",
    "email",
    "password",
    "refresh",
    "secret",
    "token",
    "username",
)
_MAX_ERROR_DETAIL_LENGTH = 2048


def normalize_cloud_commands(commands: dict[str, Any]) -> dict[str, Any]:
    """Return commands valid for the cloud API without mutating the caller."""
    normalized = dict(commands)
    if normalized.get("operationMode") in {"autoCool", "autoHeat"}:
        normalized["operationMode"] = OPERATION_MODE_AUTO

    # Home Assistant converts whole Fahrenheit values to repeating Celsius
    # floats (75 F -> 23.888...). The cloud accepts tenths of a degree and
    # rejects extra precision with invalidSpCoolIncrement/invalidSpHeatIncrement.
    for key in _SETPOINT_COMMANDS:
        value = normalized.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and isfinite(value)
        ):
            normalized[key] = round(value, CLOUD_SETPOINT_PRECISION)
    return normalized


def _redact_json(value: Any) -> Any:
    """Recursively redact credential-like JSON fields."""
    if isinstance(value, dict):
        return {
            key: (
                "<redacted>"
                if any(part in str(key).lower() for part in _SENSITIVE_KEY_PARTS)
                else _redact_json(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(detail: str) -> str:
    """Redact credential-like values from unstructured response text."""
    detail = re.sub(r"(?i)\bBearer\s+\S+", "Bearer <redacted>", detail)
    detail = re.sub(
        r"\b[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
        "<redacted-token>",
        detail,
    )
    sensitive_names = "|".join(_SENSITIVE_KEY_PARTS)
    return re.sub(
        rf"(?i)\b({sensitive_names})\w*\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|\S+)",
        lambda match: f"{match.group(1)}=<redacted>",
        detail,
    )


def sanitize_error_body(body: str) -> str:
    """Format an API error body for logs while redacting sensitive values."""
    detail = body.strip()
    if not detail:
        return "<empty>"

    try:
        parsed = json.loads(detail)
    except (TypeError, ValueError):
        detail = _redact_text(detail)
    else:
        detail = json.dumps(
            _redact_json(parsed), ensure_ascii=False, separators=(",", ":")
        )

    if len(detail) > _MAX_ERROR_DETAIL_LENGTH:
        return f"{detail[:_MAX_ERROR_DETAIL_LENGTH]}...<truncated>"
    return detail
