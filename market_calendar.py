import os
from datetime import datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

# MOEX 2026 official calendar exceptions for the STOCK and FUTURES markets.
#
# Source:
# https://www.moex.com/n96571
#
# The exchange states that in 2026:
# - 1-2 Jan, 7 Jan, 8 Mar, 9 May, 31 Dec: NO trading.
# - 23 Feb, 1 May, 12 Jun, 4 Nov: trading is held as an additional
#   weekend session for stock and derivatives markets.
# - Weekend sessions are held on all other calendar weekends EXCEPT:
#   3-4 Jan, 10-11 Jan, 14-15 Feb, 7-8 Mar, 21-22 Mar,
#   9-10 May, 20-21 Jun, 1-2 Aug, 15-16 Aug, 12-13 Sep,
#   24-25 Oct, 5-6 Dec.
#
# A later exchange notice can override this list. If a newer official
# calendar is added, update this table before the new year starts.

YEAR = 2026

# Full-day closures for both stock and futures.
CLOSED_DAYS_2026 = {
    "2026-01-01",
    "2026-01-02",
    "2026-01-07",
    "2026-03-08",
    "2026-05-09",
    "2026-12-31",
}

# State holidays that are explicitly traded as additional weekend sessions.
TRADED_HOLIDAYS_2026 = {
    "2026-02-23",
    "2026-05-01",
    "2026-06-12",
    "2026-11-04",
}

# Weekend dates with NO additional stock/futures session.
NO_WEEKEND_SESSION_2026 = {
    "2026-01-03",
    "2026-01-04",
    "2026-01-10",
    "2026-01-11",
    "2026-02-14",
    "2026-02-15",
    "2026-03-07",
    "2026-03-08",
    "2026-03-21",
    "2026-03-22",
    "2026-05-09",
    "2026-05-10",
    "2026-06-20",
    "2026-06-21",
    "2026-08-01",
    "2026-08-02",
    "2026-08-15",
    "2026-08-16",
    "2026-09-12",
    "2026-09-13",
    "2026-10-24",
    "2026-10-25",
    "2026-12-05",
    "2026-12-06",
}

# MOEX additional weekend sessions are 09:50-19:00 MSK.
WEEKEND_SESSION_START = (9, 50)
WEEKEND_SESSION_END = (19, 0)

# Regular trading day: scanner may use its own market-data/session logic.
# We intentionally do not invent a single "open/close" time for all
# instruments because stock and futures have different sessions.
#
# The calendar layer answers the user's key question:
# "Is this a MOEX trading date for the stock/futures markets?"
# Scanner execution remains responsible for its indicator/data freshness.

# Optional safety override. Set CALENDAR_ALLOW_WEEKDAY_FALLBACK=1 only if
# you explicitly want unknown future years to be treated as Mon-Fri.
ALLOW_WEEKDAY_FALLBACK = os.getenv(
    "CALENDAR_ALLOW_WEEKDAY_FALLBACK", "0"
).strip() == "1"


def _date_string(when=None):
    when = when or datetime.now(MSK)
    return when.astimezone(MSK).date().isoformat()


def _weekend_session(date_string):
    if date_string in NO_WEEKEND_SESSION_2026:
        return False

    # Saturday/Sunday sessions are normally traded in 2026 except the
    # explicitly excluded weekends above.
    weekday = datetime.fromisoformat(date_string).weekday()
    return weekday in (5, 6)


def _market_day_2026(date_string):
    if date_string in CLOSED_DAYS_2026:
        return {
            "open": False,
            "known": True,
            "date": date_string,
            "reason": "H",
            "reason_text": "праздник / нет торгов",
            "source": "MOEX official 2026 calendar",
        }

    if date_string in TRADED_HOLIDAYS_2026:
        return {
            "open": True,
            "known": True,
            "date": date_string,
            "reason": "W",
            "reason_text": "дополнительная сессия выходного дня",
            "source": "MOEX official 2026 calendar",
        }

    if _weekend_session(date_string):
        return {
            "open": True,
            "known": True,
            "date": date_string,
            "reason": "W",
            "reason_text": "дополнительная сессия выходного дня",
            "source": "MOEX official 2026 calendar",
        }

    return {
        "open": True,
        "known": True,
        "date": date_string,
        "reason": "N",
        "reason_text": "обычный торговый день",
        "source": "MOEX official 2026 calendar",
    }


def market_day(kind, when=None):
    """
    Return whether the selected MOEX market has a trading day.

    kind is kept as 'stock' or 'futures' for compatibility with the
    existing scanner. Both use the same 2026 stock/futures calendar
    published by MOEX.
    """
    when = (when or datetime.now(MSK)).astimezone(MSK)
    date_string = when.date().isoformat()

    if when.year == YEAR:
        result = _market_day_2026(date_string)

        # Preserve the useful session information for weekend sessions.
        if result["reason"] == "W":
            result["session_start_msk"] = "09:50"
            result["session_end_msk"] = "19:00"

        result["kind"] = kind
        return result

    if ALLOW_WEEKDAY_FALLBACK:
        open_day = when.weekday() < 5
        return {
            "open": open_day,
            "known": True,
            "date": date_string,
            "reason": "N" if open_day else "H",
            "reason_text": "weekday fallback",
            "source": "weekday fallback — NOT official MOEX calendar",
            "kind": kind,
        }

    return {
        "open": False,
        "known": False,
        "date": date_string,
        "reason": "calendar_year_unknown",
        "reason_text": f"нет подтверждённого календаря MOEX для {when.year}",
        "source": "fail-closed",
        "kind": kind,
    }


def markets_status(when=None):
    when = (when or datetime.now(MSK)).astimezone(MSK)

    stock = market_day("stock", when)
    futures = market_day("futures", when)

    calendar_ok = bool(
        stock.get("known") and futures.get("known")
    )

    full_scan_allowed = bool(
        calendar_ok
        and stock.get("open")
        and futures.get("open")
    )

    return {
        "date": when.date().isoformat(),
        "time_msk": when.strftime("%H:%M:%S"),
        "stock": stock,
        "futures": futures,
        "calendar_ok": calendar_ok,
        "full_scan_allowed": full_scan_allowed,
    }


def scan_allowed(when=None):
    return markets_status(when)["full_scan_allowed"]
