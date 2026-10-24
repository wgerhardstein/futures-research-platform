"""Unit tests for the 0DTE breakout rule pieces (no network)."""
from __future__ import annotations

import pandas as pd

from signals import orb

ET = "America/New_York"


def bar(t: str, c: float) -> dict:
    return {"t": pd.Timestamp(f"2026-10-23 {t}", tz=ET).tz_convert("UTC").isoformat(), "c": c}


def test_opening_range_long_uses_high():
    f5 = pd.DataFrame({"o": [10, 10.2, 10.1, 10.3, 10.4], "h": [10.3, 10.5, 10.2, 10.6, 10.5],
                       "l": [9.9, 10.1, 10.0, 10.2, 10.3], "c": [10.2, 10.1, 10.3, 10.4, 10.45]})
    assert orb.opening_range(f5) == (True, 10.6)


def test_opening_range_short_uses_low_and_doji_is_none():
    f5 = pd.DataFrame({"o": [10, 9.9], "h": [10.1, 10.0], "l": [9.7, 9.8], "c": [9.9, 9.8]})
    assert orb.opening_range(f5) == (False, 9.7)
    assert orb.opening_range(pd.DataFrame({"o": [10, 10], "h": [11, 11], "l": [9, 9], "c": [10.5, 10]})) is None


def test_stop_needs_a_completed_close_not_a_wick():
    entry = pd.Timestamp("2026-10-23 09:42", tz=ET)
    now = pd.Timestamp("2026-10-23 09:45", tz=ET)
    bars = [bar("09:41", 50.0), bar("09:42", 50.40), bar("09:43", 50.34)]   # 09:41 is before entry -> ignored
    assert not orb.stop_closed(bars, entry, 50.33, True, now)
    assert orb.stop_closed(bars + [bar("09:44", 50.33)], entry, 50.33, True, now)
    # 09:45 bar is still forming at 09:45:xx -> not counted
    assert not orb.stop_closed([bar("09:45", 49.0)], entry, 50.33, True, now)


def test_short_stop_is_a_close_above():
    entry = pd.Timestamp("2026-10-23 09:46", tz=ET)
    now = pd.Timestamp("2026-10-23 09:50", tz=ET)
    assert orb.stop_closed([bar("09:47", 163.81)], entry, 163.80, False, now)
    assert not orb.stop_closed([bar("09:47", 163.79)], entry, 163.80, False, now)


def test_nearest_contract_takes_nearest_expiry_then_strike():
    cs = [{"symbol": "A1", "expiration_date": "2026-10-23", "strike_price": "50"},
          {"symbol": "A2", "expiration_date": "2026-10-23", "strike_price": "51"},
          {"symbol": "B1", "expiration_date": "2026-10-30", "strike_price": "50.5"}]
    assert orb.nearest_contract(cs, 50.47) == "A1"
    assert orb.nearest_contract([], 50) is None


def test_daily_stats_and_filters():
    bars = [{"h": 11, "l": 9, "c": 10, "v": 2e6} for _ in range(20)]
    st = orb.daily_stats(bars)
    assert st["atr14"] == 2.0 and st["prev_c"] == 10 and orb.eligible(st)
    assert not orb.eligible(dict(st, prev_c=4.0))
    assert orb.daily_stats(bars[:10]) is None
