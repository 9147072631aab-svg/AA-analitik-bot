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
_session.headers.update({"User-Agent": "AA-Analitik/Calendar/1.1"})


def _fetch(kind, year):
    """Fetch the official MOEX machine-readable calendar."""
    params = {"show_all_days": 1, "iss.only": "off_days", "start": 0}
    last_error = None

    for scheme in ("https", "http"):
        url = f"{scheme}://iss.moex.com/iss/calendars/{kind}"
        try:
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
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"MOEX calendar unavailable: {last_error}")


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
    when = (when or datetime.now(MSK)).astimezone(MSK)
    d = when.date().isoformat()

    try:
        row = calendar(kind, when.year).get(d)
        if row is None:
            return {
                "open": False,
                "known": False,
                "date": d,
                "reason": "calendar_missing_date",
                "source": "MOEX ISS",
            }

        is_traded = row.get("is_traded")
        if is_traded is None:
            return {
                "open": False,
                "known": False,
                "date": d,
                "reason": "calendar_unknown",
                "trade_session_date": row.get("trade_session_date"),
                "source": "MOEX ISS",
            }

        return {
            "open": is_traded == 1,
            "known": True,
            "date": d,
            "reason": row.get("reason") or "N",
            "trade_session_date": row.get("trade_session_date"),
            "source": "MOEX ISS",
        }
    except Exception as exc:
        # Fail closed: calendar failure must never become a false OPEN state.
        return {
            "open": False,
            "known": False,
            "date": d,
            "reason": "calendar_error",
            "source": "MOEX ISS unavailable",
            "error": str(exc)[:300],
        }


def markets_status(when=None):
    when = (when or datetime.now(MSK)).astimezone(MSK)
    stock = market_day("stock", when)
    futures = market_day("futures", when)
    calendar_ok = bool(stock.get("known") and futures.get("known"))
    full_scan_allowed = bool(
        calendar_ok and stock.get("open") and futures.get("open")
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
