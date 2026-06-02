from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any


def to_json(value: Any) -> Any:
    if is_dataclass(value):
        return {key: to_json(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [to_json(item) for item in value]
    if isinstance(value, dict):
        return {key: to_json(item) for key, item in value.items()}
    if hasattr(value, "value"):
        return value.value
    return value

