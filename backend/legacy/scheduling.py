"""Send-window and pacing helpers for campaign queues."""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import config

_UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(_UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(_UTC).isoformat()


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_UTC)
    return parsed.astimezone(_UTC)


def get_timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo((name or config.CAMPAIGN_TIMEZONE or "UTC").strip())
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


def parse_hhmm(value: str, default: str) -> time:
    raw = (value or default).strip()
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour=hour, minute=minute)
    except (ValueError, TypeError):
        pass
    hour_text, minute_text = default.split(":", 1)
    return time(hour=int(hour_text), minute=int(minute_text))


def _window_bounds(campaign: dict[str, Any], day: date) -> tuple[datetime, datetime]:
    tz = get_timezone(campaign.get("timezone"))
    start = parse_hhmm(campaign.get("send_window_start", "09:00"), "09:00")
    end = parse_hhmm(campaign.get("send_window_end", "18:00"), "18:00")
    return (
        datetime.combine(day, start, tzinfo=tz),
        datetime.combine(day, end, tzinfo=tz),
    )


def send_days(campaign: dict[str, Any]) -> set[int]:
    raw = campaign.get("send_days")
    if not isinstance(raw, list) or not raw:
        return {0, 1, 2, 3, 4}
    days: set[int] = set()
    for value in raw:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= number <= 6:
            days.add(number)
    return days or {0, 1, 2, 3, 4}


def is_within_window(campaign: dict[str, Any], moment: datetime | None = None) -> bool:
    if campaign.get("send_window_enabled") is False:
        return True
    local = (moment or utc_now()).astimezone(get_timezone(campaign.get("timezone")))
    if local.weekday() not in send_days(campaign):
        return False
    start, end = _window_bounds(campaign, local.date())
    return start <= local < end


def next_window_start(campaign: dict[str, Any], moment: datetime | None = None) -> datetime:
    """Returns the next allowed send time (or ``moment`` when already inside)."""
    moment = moment or utc_now()
    if campaign.get("send_window_enabled") is False:
        return moment
    local = moment.astimezone(get_timezone(campaign.get("timezone")))
    allowed = send_days(campaign)
    for offset in range(0, 15):
        day = local.date() + timedelta(days=offset)
        if day.weekday() not in allowed:
            continue
        start, end = _window_bounds(campaign, day)
        if offset == 0 and start <= local < end:
            return moment.astimezone(_UTC)
        if start > local:
            return start.astimezone(_UTC)
    # Fallback: tomorrow at the configured start.
    start, _ = _window_bounds(campaign, local.date() + timedelta(days=1))
    return start.astimezone(_UTC)


def local_day_start(campaign: dict[str, Any], moment: datetime | None = None) -> datetime:
    local = (moment or utc_now()).astimezone(get_timezone(campaign.get("timezone")))
    start = datetime.combine(local.date(), time(0, 0), tzinfo=get_timezone(campaign.get("timezone")))
    return start.astimezone(_UTC)


def hour_ago(moment: datetime | None = None) -> datetime:
    return (moment or utc_now()) - timedelta(hours=1)


def random_delay_seconds(campaign: dict[str, Any]) -> int:
    """Non-uniform wait between sends (default 4-12 minutes)."""
    try:
        minimum = int(campaign.get("delay_min_seconds", 240))
    except (TypeError, ValueError):
        minimum = 240
    try:
        maximum = int(campaign.get("delay_max_seconds", 720))
    except (TypeError, ValueError):
        maximum = 720
    minimum = max(1, min(minimum, 21600))
    maximum = max(minimum, min(maximum, 21600))
    # SystemRandom avoids the predictable pattern of the module-level RNG.
    return random.SystemRandom().randint(minimum, maximum)


def next_cap_reset(campaign: dict[str, Any], moment: datetime | None = None) -> datetime:
    """When a capped campaign may try again (next local midnight)."""
    moment = moment or utc_now()
    tz = get_timezone(campaign.get("timezone"))
    local = moment.astimezone(tz)
    tomorrow = datetime.combine(local.date() + timedelta(days=1), time(0, 5), tzinfo=tz)
    return tomorrow.astimezone(_UTC)


__all__ = [
    "get_timezone",
    "hour_ago",
    "is_within_window",
    "iso",
    "local_day_start",
    "next_cap_reset",
    "next_window_start",
    "parse_hhmm",
    "parse_iso",
    "random_delay_seconds",
    "send_days",
    "utc_now",
]
