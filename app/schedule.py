"""Local schedules for a flow. Weekly days use 0=Sunday … 6=Saturday."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone

from app.carry import FlowError

FREQUENCIES = ("daily", "weekly", "weekdays", "once")


def _our_weekday(day) -> int:
    """Python Monday=0 … Sunday=6 → Sunday=0 … Saturday=6."""

    return (day.weekday() + 1) % 7


def _clock(value: str) -> tuple[int, int]:
    if not re.match(r"^\d{2}:\d{2}$", value or ""):
        raise FlowError("时间要写成 09:00 这样")
    hour, minute = int(value[:2]), int(value[3:])
    if hour > 23 or minute > 59:
        raise FlowError("时间不对")
    return hour, minute


def normalize_schedule(schedule: dict | None, now: datetime | None = None) -> dict:
    raw = dict(schedule or {})
    frequency = raw.get("frequency") or "daily"
    if frequency not in FREQUENCIES:
        raise FlowError("不认识的执行频率")
    hour, minute = _clock(raw.get("time") or "09:00")
    enabled = bool(raw.get("enabled"))
    date_text = (raw.get("date") or "").strip()
    weekdays: list[int] = []
    if frequency == "weekly":
        seen = set()
        for item in raw.get("weekdays") or []:
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            if 0 <= number <= 6:
                seen.add(number)
        weekdays = sorted(seen)
        if enabled and not weekdays:
            raise FlowError("每周执行要至少选一天")
    if frequency == "once" and enabled:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_text):
            raise FlowError("单次执行要选一个日期")
    normalized = {
        "enabled": enabled,
        "frequency": frequency,
        "time": f"{hour:02d}:{minute:02d}",
        "weekdays": weekdays,
        "date": date_text,
        "nextRunAt": None,
    }
    if enabled:
        moment = now or datetime.now().astimezone()
        nxt = compute_next(normalized, moment)
        if nxt is None:
            raise FlowError("这个时间已经过了，不会再执行")
        normalized["nextRunAt"] = nxt.astimezone(timezone.utc).isoformat(timespec="seconds")
    return normalized


def compute_next(schedule: dict, now: datetime) -> datetime | None:
    if now.tzinfo is None:
        now = now.astimezone()
    hour, minute = _clock(schedule.get("time") or "09:00")
    frequency = schedule.get("frequency") or "daily"
    if frequency == "once":
        date_text = schedule.get("date") or ""
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_text):
            return None
        year, month, day = (int(part) for part in date_text.split("-"))
        candidate = datetime(year, month, day, hour, minute, tzinfo=now.tzinfo)
        if candidate <= now:
            return None
        return candidate
    allowed = set(schedule.get("weekdays") or [])
    for offset in range(0, 15):
        day = (now + timedelta(days=offset)).date()
        candidate = datetime.combine(day, time(hour, minute), tzinfo=now.tzinfo)
        if candidate <= now:
            continue
        weekday = _our_weekday(day)
        if frequency == "daily":
            return candidate
        if frequency == "weekdays" and weekday in (1, 2, 3, 4, 5):
            return candidate
        if frequency == "weekly" and weekday in allowed:
            return candidate
    return None


def mark_after_run(schedule: dict, now: datetime) -> dict:
    """Move a schedule forward after a timed run, including a failed one."""

    current = dict(schedule or {})
    if current.get("frequency") == "once":
        current["enabled"] = False
        current["nextRunAt"] = None
        return current
    nxt = compute_next(current, now)
    if nxt is None:
        current["enabled"] = False
        current["nextRunAt"] = None
        return current
    current["nextRunAt"] = nxt.astimezone(timezone.utc).isoformat(timespec="seconds")
    return current
