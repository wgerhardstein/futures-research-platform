"""Unit tests for the SPY RSI(2) rule (no network)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from signals import spy_rsi2 as r


def series(vals) -> pd.Series:
    return pd.Series(vals, index=pd.bdate_range("2020-01-01", periods=len(vals)), dtype=float)


def test_rsi2_extremes():
    assert r.rsi2(series(np.arange(1, 30))).iloc[-1] == 100
    down = r.rsi2(series(list(np.arange(1, 30)) + [26, 22, 18])).iloc[-1]
    assert down < 10


def test_buy_needs_uptrend_and_oversold():
    up = list(np.linspace(100, 200, 220))
    assert r.decide(series(up + [196, 192]), False)["action"] == "BUY"           # above 200-day, two down days
    assert r.decide(series(up + [201]), False)["action"] == "WAIT"               # not oversold
    downtrend = list(np.linspace(200, 100, 220))
    assert r.decide(series(downtrend + [96, 92]), False)["action"] == "WAIT"     # oversold but below the 200-day


def test_sell_on_close_above_5_day():
    up = list(np.linspace(100, 200, 220))
    assert r.decide(series(up + [190, 185, 199]), True)["action"] == "SELL"
    assert r.decide(series(up + [190, 185, 186]), True)["action"] == "HOLD"


def test_backtest_round_trip():
    up = list(np.linspace(100, 200, 220))
    t = r.backtest(series(up + [196, 192, 199, 200]), cost=0)
    assert len(t) == 1 and t.days.iloc[0] == 1 and t.ret.iloc[0] > 0
