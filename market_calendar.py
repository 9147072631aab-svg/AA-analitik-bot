import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

MSK = ZoneInfo("Europe/Moscow")
BASE = "https://iss.moex.com/iss"
TIMEOUT = int(__import__("os").getenv("MOEX_CALENDAR_TIMEOUT", "10"))
CACHE_TTL = int(__import__("os").getenv("MOEX_CALENDAR_CACHE_TTL", "21600"))

_cache = {}
_session = requests.Session()
_session.headers.update({"User-Agent": "AA-Analitik/Calendar/1.0"})


def _fetch(kind, year):
    # MOEX official machine-readable trading calendar.
    # is_traded=0: non-trading day / holiday
    # is_traded=1: trading day, including weekend sessions and transferred workdays.
    url = f"{BASE}/calendars/{kind}.json"
    params = {
        "show_all_days": 1,
        "iss.only": "off_days",
        "start": 0,
    }
    r = _session.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    block = payload.get("off_days", {})
    columns = block.get("columns", [])
    data = block.get("data", [])
    rows = [dict(zip(columns, row)) for row in data]

    result = {}
    for row in rows:
        d = str(row.get("tradedate") or "")[:10]
        if not d.startswith(str(year)):
            continue
        value = row.get("is_traded")
        result[d] = {
            "is_traded": None if value in (None, "") else int(value),
            "reason": row.get("reason"),
            "trade_session_date": row.get("trade_session_date"),
        }
    return result


def calendar(kind, year=None):
    year = year or datetime.now(MSK).year
    key = (kind, year)
    now = time.time()
    cached = _cache.get(key)
    if cached and now - cached["ts"] < CACHE_TTL:
        return cached["data"]

    data = _fetch(kind, year)
    _cache[key] = {"ts": now, "data": data}
    return data


def market_day(kind, when=None):
    when = when or datetime.now(MSK)
    d = when.astimezone(MSK).date().isoformat()
    try:
        row = calendar(kind, when.year).get(d)
        if row is None:
            # Conservative fallback: Mon-Fri only when MOEX does not return a row.
            return {
                "open": when.weekday() < 5,
                "date": d,
                "reason": "fallback_weekday",
                "source": "fallback",
            }
        return {
            "open": row["is_traded"] == 1,
            "date": d,
            "reason": row.get("reason") or "N",
            "trade_session_date": row.get("trade_session_date"),
            "source": "MOEX ISS",
        }
    except Exception as exc:
        # Fail safe: never invent a holiday, but do not block ordinary weekdays
        # if the public calendar endpoint is temporarily unavailable.
        return {
            "open": when.weekday() < 5,
            "date": d,
            "reason": "calendar_error",
            "source": "fallback",
            "error": str(exc)[:300],
        }


def markets_status(when=None):
    when = when or datetime.now(MSK)
    stock = market_day("stock", when)
    futures = market_day("futures", when)
    return {
        "date": when.astimezone(MSK).date().isoformat(),
        "time_msk": when.astimezone(MSK).strftime("%H:%M:%S"),
        "stock": stock,
        "futures": futures,
        "full_scan_allowed": bool(stock["open"] and futures["open"]),
    }


def scan_allowed(when=None):
    return markets_status(when)["full_scan_allowed"]
