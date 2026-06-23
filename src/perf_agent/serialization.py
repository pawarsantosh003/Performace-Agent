from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from .governance import redact_sensitive

SECRET_FIELDS = {
    "api_key",
    "connection_string",
    "password",
    "token",
    "secret",
    "secret_string",
    "client_secret",
    "aws_secret_access_key",
    "azure_client_secret",
}


def _redact_key(key: str, value: Any) -> Any:
    if key.lower() in SECRET_FIELDS:
        return "<redacted>"
    return to_json(value)


def to_json(value: Any) -> Any:
    if is_dataclass(value):
        return redact_sensitive({key: _redact_key(key, item) for key, item in asdict(value).items()})
    if isinstance(value, list):
        return [to_json(item) for item in value]
    if isinstance(value, dict):
        return redact_sensitive({key: _redact_key(key, item) for key, item in value.items()})
    if hasattr(value, "value"):
        return value.value
    return value
