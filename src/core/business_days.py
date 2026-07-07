from __future__ import annotations

from datetime import datetime, timedelta, timezone


def business_days_since(last_updated: datetime, as_of: datetime) -> int:
    resolved_last_updated = _normalize_datetime(last_updated)
    resolved_as_of = _normalize_datetime(as_of)
    if resolved_as_of <= resolved_last_updated:
        return 0

    business_days = 0
    current = resolved_last_updated.date() + timedelta(days=1)
    end_date = resolved_as_of.date()
    while current <= end_date:
        if current.weekday() < 5:
            business_days += 1
        current += timedelta(days=1)
    return business_days


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)