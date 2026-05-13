"""Time utilities locked to UTC+8 / Beijing time."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


def beijing_today() -> date:
    return beijing_now().date()


def beijing_strftime(fmt: str) -> str:
    return beijing_now().strftime(fmt)


def beijing_iso() -> str:
    return beijing_now().isoformat(timespec="seconds")
