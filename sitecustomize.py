"""AA Analitik score precision patch.

Loaded automatically by Python before bot.py. It keeps the v1.3 quality gate
unchanged, but makes the displayed score continuous instead of a pile of
identical integer values such as 76.0.
"""

from __future__ import annotations

import math


def _clamp(value, lo=0.0, hi=1.0):
    return max(lo, min(hi, value))


def _continuous_score(item):
    raw = item.get("score")
    try:
        base = float(raw)
    except (TypeError, ValueError):
        return raw

    if base <= 0:
        return base

    adx = float(item.get("adx") or 0.0)
    volume_ratio = float(item.get("volume_ratio") or 1.0)
    trigger_atr = float(item.get("trigger_distance_atr") or 0.0)
    rr = float(item.get("rr") or 0.0)
    rsi = float(item.get("rsi") or 50.0)
    side = item.get("side")
    plus_di = float(item.get("plus_di") or 0.0)
    minus_di = float(item.get("minus_di") or 0.0)

    adx_q = _clamp((adx - 18.0) / 22.0)
    volume_q = _clamp((volume_ratio - 1.0) / 1.0)
    trigger_q = _clamp(1.0 - trigger_atr / 1.5)
    rr_q = _clamp((rr - 2.0) / 2.0)

    if side == "LONG":
        rsi_q = _clamp(1.0 - abs(rsi - 58.0) / 20.0)
        di_q = _clamp((plus_di - minus_di) / 20.0)
    elif side == "SHORT":
        rsi_q = _clamp(1.0 - abs(rsi - 42.0) / 20.0)
        di_q = _clamp((minus_di - plus_di) / 20.0)
    else:
        rsi_q = _clamp(1.0 - abs(rsi - 50.0) / 30.0)
        di_q = _clamp(abs(plus_di - minus_di) / 20.0)

    micro = (
        0.24 * adx_q
        + 0.20 * volume_q
        + 0.22 * trigger_q
        + 0.16 * rr_q
        + 0.10 * rsi_q
        + 0.08 * di_q
    )

    fraction = 0.01 + 0.98 * _clamp(micro)
    return round(math.floor(base) + fraction, 2)


def _patch_result(result):
    if not isinstance(result, dict):
        return result

    seen = set()
    for key in ("longs", "shorts", "watch", "confirmed", "stocks", "futures"):
        items = result.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            marker = id(item)
            if marker in seen:
                continue
            seen.add(marker)
            item["score"] = _continuous_score(item)

    meta = result.get("meta")
    if isinstance(meta, dict):
        diagnostics = meta.get("diagnostics")
        if isinstance(diagnostics, dict):
            rows = diagnostics.get("top_waits")
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        row["score"] = _continuous_score(row)

    return result


try:
    import scanner as _scanner

    _original_run_scan = _scanner.run_scan

    def run_scan_with_precise_scores(*args, **kwargs):
        return _patch_result(_original_run_scan(*args, **kwargs))

    _scanner.run_scan = run_scan_with_precise_scores
except Exception:
    pass
