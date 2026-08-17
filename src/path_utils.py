"""Tiny shared helper for resolving dotted config paths against nested dicts."""
from __future__ import annotations

from typing import Any


def get_path(obj: Any, dotted_path: str) -> Any:
    """Resolve a dotted path against a nested dict. Missing -> None, never raises."""
    if dotted_path == "":
        return obj
    cur = obj
    for part in dotted_path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur
