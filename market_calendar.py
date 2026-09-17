import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
YEAR = 2026

# MOEX 2026 calendar for STOCK and DERIVATIVES markets.
# Official MOEX sources:
# https://www.moex.com/n96571
# https://www.moex.com/n95564
# https://www.moex.com/n103931

CLOSED_DAYS_2026 = {
    "2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04",
    "2026-01-07", "2026-03-08", "2026-05-09", "2026-05-10",
    "2026-12-31",
}

# Holidays explicitly opened as regular trading days.
REGULAR_HOLIDAY_SESSIONS_2026 = {
    "2026-01-05", "2026-01-06", "2026-01-08", "2026-01-09",
    "2026-03-09", "2026-05-11",
}

# Additional weekend-style sessions: 09:50-19:00 MSK.
WEEKEND_HOLIDAY_SESSIONS_2026 = {
    "2026-02-23", "2026-05-01", "2026-06-12", "2026-11-04",
}

# Weekend dates with NO additional stock/futures session.
# 5-6 Dec were moved to a trading weekend; 28-29 Nov are now closed.
NO_WEEKEND_SESSION_2026 = {
    "2026-01-03", "2026-01-04",
    "2026-01-10", "2026-01-11",
    "2026-02-14", "2026-02-15",
    "2026-03-07", "2026-03-08",
    "2026-03-21", "2026-03-22",
    "2026-05-09", "2026-05-10",
    "2026-06-20", "2026-06-21",
    "2026-08-01", "2026-08-02",
    "2026-08-15", "2026-08-16",
    "2026-09-12", "2026-09-13",
    "2026-10-24", "2026-10-25",
    "2026-11-28", "2026-11-29",
}

REGULAR_SESSION_START = time(6, 50)
REGULAR_SESSION_END = time(23, 50)
WEEKEND_SESSION_START = time(9, 50)
WEEKEND_SESSION_END = time(19, 0)

ALLOW_WEEKDAY_FALLBACK = os.getenv(
    "CALENDAR_ALLOW_WEEKDAY_FALLBACK", "0"
).strip() == "1"


def _is_weekend(date_string):
    return datetime.fromisoformat(date_string).weekday() >= 5


def _market_day_2026(date_string):
    if date_string in CLOSED_DAYS_2026:
        return {
            "open": False, "known": True, "date": date_string,
            "reason": "H", "reason_text": "праздник / нет торгов",
            "source": "MOEX official 2026 calendar",
        }

    if date_string in REGULAR_HOLIDAY_SESSIONS_2026:
        return {
            "open": True, "known": True, "date": date_string,
            "reason": "N", "reason_text": "перенесённый торговый день",
            "source": "MOEX official 2026 calendar",
            "session_start_msk": "06:50", "session_end_msk": "23:50",
        }

    if date_string in WEEKEND_HOLIDAY_SESSIONS_2026:
        return {
            "open": True, "known": True, "date": date_string,
            "reason": "W", "reason_text": "дополнительная сессия выходного дня",
            "source": "MOEX official 2026 calendar",
            "session_start_msk": "09:50", "session_end_msk": "19:00",
        }

    if _is_weekend(date_string):
        if date_string in NO_WEEKEND_SESSION_2026:
            return {
                "open": False, "known": True, "date": date_string,
                "reason": "W0",
                "reason_text": "выходной без дополнительной сессии",
                "source": "MOEX official 2026 calendar",
            }
        return {
            "open": True, "known": True, "date": date_string,
            "reason": "W", "reason_text": "дополнительная сессия выходного дня",
            "source": "MOEX official 2026 calendar",
            "session_start_msk": "09:50", "session_end_msk": "19:00",
        }

    return {
        "open": True, "known": True, "date": date_string,
        "reason": "N", "reason_text": "обычный торговый день",
        "source": "MOEX official 2026 calendar",
        "session_start_msk": "06:50", "session_end_msk": "23:50",
    }


def market_day(kind, when=None):
    when = (when or datetime.now(MSK)).astimezone(MSK)
    date_string = when.date().isoformat()

    if when.year == YEAR:
        result = _market_day_2026(date_string)
        result["kind"] = kind
        return result

    if ALLOW_WEEKDAY_FALLBACK:
        open_day = when.weekday() < 5
        return {
            "open": open_day, "known": True, "date": date_string,
            "reason": "N" if open_day else "H",
            "reason_text": "weekday fallback",
            "source": "weekday fallback — NOT official MOEX calendar",
            "kind": kind,
            "session_start_msk": "06:50" if open_day else None,
            "session_end_msk": "23:50" if open_day else None,
        }

    return {
        "open": False, "known": False, "date": date_string,
        "reason": "calendar_year_unknown",
        "reason_text": f"нет подтверждённого календаря MOEX для {when.year}",
        "source": "fail-closed", "kind": kind,
    }


def _in_session(day, when):
    if not day.get("open") or not day.get("known"):
        return False
    start = day.get("session_start_msk")
    end = day.get("session_end_msk")
    if not start or not end:
        return False
    return start <= when.strftime("%H:%M") <= end


def markets_status(when=None):
    when = (when or datetime.now(MSK)).astimezone(MSK)
    stock = market_day("stock", when)
    futures = market_day("futures", when)

    calendar_ok = bool(stock.get("known") and futures.get("known"))
    stock_session = _in_session(stock, when)
    futures_session = _in_session(futures, when)

    return {
        "date": when.date().isoformat(),
        "time_msk": when.strftime("%H:%M:%S"),
        "stock": stock,
        "futures": futures,
        "calendar_ok": calendar_ok,
        "full_scan_allowed": bool(
            calendar_ok and stock_session and futures_session
        ),
    }


def scan_allowed(when=None):
    return markets_status(when)["full_scan_allowed"]


def next_scan_time(when=None, step_minutes=1, max_days=370):
    """Find the next real MOEX trading minute for the full scanner."""
    when = (when or datetime.now(MSK)).astimezone(MSK)
    probe = when.replace(second=0, microsecond=0)

    if scan_allowed(probe):
        return probe

    for _ in range(max_days * 24 * 60):
        probe += timedelta(minutes=max(1, step_minutes))
        if scan_allowed(probe):
            return probe
    return None
