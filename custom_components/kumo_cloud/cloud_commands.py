"""Kumo Cloud command normalization and safe error formatting."""

from __future__ import annotations

import json
from math import isclose, isfinite
import re
from typing import Any

from .const import OPERATION_MODE_AUTO

REPORTED_AUTO_MODES = frozenset({OPERATION_MODE_AUTO, "autoCool", "autoHeat"})
_SETPOINT_COMMANDS = ("spCool", "spHeat", "spAuto")
_MITSUBISHI_F_TO_C = {
    61: 16.0,
    62: 16.5,
    63: 17.0,
    64: 17.5,
    65: 18.0,
    66: 18.5,
    67: 19.5,
    68: 20.0,
    69: 21.0,
    70: 21.5,
    71: 22.0,
    72: 22.5,
    73: 23.0,
    74: 23.5,
    75: 24.0,
    76: 24.5,
    77: 25.0,
    78: 25.5,
    79: 26.0,
    80: 26.5,
}
_MITSUBISHI_C_TO_F = {celsius: fahrenheit for fahrenheit, celsius in _MITSUBISHI_F_TO_C.items()}

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
    # floats (75 F -> 23.888...). Kumo accepts 0.5 C setpoint increments and
    # rejects extra precision with invalidSpCoolIncrement/invalidSpHeatIncrement.
    # For whole-F inputs, use Mitsubishi's proprietary mapping, which differs
    # from ordinary rounding at 64-66 F and 69-72 F.
    for key in _SETPOINT_COMMANDS:
        value = normalized.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and isfinite(value)
        ):
            fahrenheit = value * 9.0 / 5.0 + 32.0
            whole_fahrenheit = round(fahrenheit)
            if isclose(fahrenheit, whole_fahrenheit, abs_tol=1e-5):
                normalized[key] = _MITSUBISHI_F_TO_C.get(
                    whole_fahrenheit, round(value * 2.0) / 2.0
                )
            else:
                normalized[key] = round(value * 2.0) / 2.0
    return normalized


def celsius_to_display_fahrenheit(celsius: Any) -> Any:
    """Convert a cloud Celsius value to the Fahrenheit Mitsubishi displays.

    Values on Mitsubishi's table map exactly (21.0 C -> 69 F, where plain
    conversion gives 69.8 and Home Assistant would show 70). Anything else
    falls back to ordinary conversion.
    """
    if (
        isinstance(celsius, bool)
        or not isinstance(celsius, (int, float))
        or not isfinite(celsius)
    ):
        return celsius
    half_step = round(celsius * 2.0) / 2.0
    if isclose(celsius, half_step, abs_tol=1e-5) and half_step in _MITSUBISHI_C_TO_F:
        return float(_MITSUBISHI_C_TO_F[half_step])
    return round(celsius * 9.0 / 5.0 + 32.0, 1)


def fahrenheit_to_cloud_celsius(fahrenheit: Any) -> Any:
    """Convert a Home Assistant Fahrenheit value to cloud Celsius.

    Whole-degree values on Mitsubishi's table use it (69 F -> 21.0 C);
    others convert normally and are quantized by normalize_cloud_commands.
    """
    if (
        isinstance(fahrenheit, bool)
        or not isinstance(fahrenheit, (int, float))
        or not isfinite(fahrenheit)
    ):
        return fahrenheit
    whole = round(fahrenheit)
    if isclose(fahrenheit, whole, abs_tol=1e-5) and whole in _MITSUBISHI_F_TO_C:
        return _MITSUBISHI_F_TO_C[whole]
    return (fahrenheit - 32.0) * 5.0 / 9.0


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
