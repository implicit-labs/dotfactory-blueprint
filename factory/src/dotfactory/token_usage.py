"""Normalize provider token usage without retaining provider payloads."""

from __future__ import annotations

from typing import Any, Mapping


ALIASES = {
    "input_tokens": ("input_tokens", "inputTokens", "input"),
    "output_tokens": ("output_tokens", "outputTokens", "output"),
    "cache_read_tokens": (
        "cache_read_input_tokens", "cacheReadInputTokens", "cacheRead",
        "cached_input_tokens", "cachedInputTokens", "cache_read",
    ),
    "cache_write_tokens": (
        "cache_creation_input_tokens", "cacheCreationInputTokens", "cacheWrite",
        "cache_write",
    ),
    "total_tokens": ("total_tokens", "totalTokens", "total"),
}


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or int(value) != value:
        return None
    return int(value)


def normalize_token_usage(value: Any) -> dict[str, int] | None:
    """Return stable token fields from Codex, Claude, or OMP usage."""
    if not isinstance(value, Mapping):
        return None
    normalized: dict[str, int] = {}
    matched_keys: set[str] = set()
    for target, aliases in ALIASES.items():
        for alias in aliases:
            count = _count(value.get(alias))
            if count is not None:
                normalized[target] = count
                matched_keys.add(alias)
                break
    if not normalized:
        return None
    if "total_tokens" not in normalized:
        total_fields = ("input_tokens", "output_tokens")
        if not ({"cached_input_tokens", "cachedInputTokens"} & matched_keys):
            total_fields += ("cache_read_tokens", "cache_write_tokens")
        normalized["total_tokens"] = sum(
            normalized.get(field, 0) for field in total_fields
        )
    return normalized
