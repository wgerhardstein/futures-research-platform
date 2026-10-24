"""Reproduce the SPY RSI(2) result with free data (Yahoo Finance, no keys): python -m research.spy_rsi2_backtest

Uses the same rule code as the live signal (signals/spy_rsi2.py). Prints every statistic quoted in research/RESULTS.md.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import yfinance as yf

from signals.spy_rsi2 import backtest

START, SPLIT, END = "1994-01-01", "2010-01-01", "2026-10-24"


def main() -> None:
    px = yf.download("SPY", start="1993-01-01", end=END, progress=False, auto_adjust=False)["Adj Close"].squeeze().dropna()
    px.index = pd.DatetimeIndex(px.index).tz_localize(None)
    t = backtest(px)
    t = t[t.entry >= START].reset_index(drop=True)
    yrs = (px.index[-1] - pd.Timestamp(START)).days / 365.25
    eq = (1 + t.ret).cumprod()
    h1, h2 = t.ret[t.entry < SPLIT], t.ret[t.entry >= SPLIT]
    # random-entry control: same number of trades and holding periods, entries on random days above the 200-day average
    sma = px.rolling(200).mean()
    ok = np.where((px > sma).values & (px.index >= START))[0]
    ok = ok[ok < len(px) - 40]
    rng = np.random.default_rng(0)
    ctrl = [np.mean(px.values[(e := rng.choice(ok, len(t))) + (h := rng.permutation(t.days.values))] / px.values[e] - 1 - 0.0004)
            for _ in range(2000)]
    spy = px[px.index >= START]
    print(f"SPY RSI(2) dip-buy, {t.entry.min():%Y-%m-%d} -> {t.exit.max():%Y-%m-%d}, 0.02% cost per side")
    print(f"  trades {len(t)}, winners {100 * (t.ret > 0).mean():.0f}%, mean {100 * t.ret.mean():+.2f}% per trade, avg hold {t.days.mean():.1f} days")
    print(f"  1994-2009 mean {100 * h1.mean():+.2f}% (n {len(h1)}) | 2010-2026 mean {100 * h2.mean():+.2f}% (n {len(h2)})")
    print(f"  random-entry control: mean {100 * np.mean(ctrl):+.2f}%, 95th percentile {100 * np.percentile(ctrl, 95):+.2f}%")
    print(f"  shares only: {100 * (eq.iloc[-1] ** (1 / yrs) - 1):+.1f}%/yr, max drawdown {100 * (eq / eq.cummax() - 1).min():.0f}%, "
          f"invested {100 * t.days.sum() / len(spy):.0f}% of days")
    print(f"  SPY buy and hold: {100 * ((spy.iloc[-1] / spy.iloc[0]) ** (1 / yrs) - 1):+.1f}%/yr, max drawdown {100 * (spy / spy.cummax() - 1).min():.0f}%")


if __name__ == "__main__":
    main()
