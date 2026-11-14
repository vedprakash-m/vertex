from __future__ import annotations

from src.core.exceptions import ConfigError


def parse_chart_blocks_publish(value: object) -> bool:
    if not isinstance(value, bool):
        raise ConfigError("chart_blocks_publish must be a boolean.")
    return value


def parse_chart_cache_ttl(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ConfigError("chart_cache_ttl_hours must be a positive integer.")
    return value
