#!/usr/bin/env python3
"""Synthetic 1-minute bar generator.

The real bar data behind this platform is licensed and cannot be
redistributed, and the validation suite needs bars: the fill auditor
re-resolves exits against them, and the indicator auditor recomputes ATR,
EMA and VWAP from them.

So this produces bars with the right *statistical texture* rather than the
right prices. What matters for the audits is not realism of level but
realism of structure:

  * a U-shaped intraday volatility profile -- active open, quiet midday,
    active close -- because a flat profile would make every session's ATR
    identical and hide bugs that only appear when volatility moves
  * overnight gaps between sessions, so the true-range calculation is
    actually exercised (TR reduces to high-low if the previous close always
    sits inside today's range)
  * volume correlated with volatility, so VWAP is not just a mean of prices
  * a tick grid, so prices are representable instrument values

None of it is calibrated to a real instrument, and it is not meant to be.

    python -m synthetic.bars [--out data/bars] [--days 260] [--seed 11]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

SPEC = {                    # tick, point value, start price, daily vol (pts)
    "MNQ": (0.25, 2.0, 18000.0, 220.0),
    "MES": (0.25, 5.0, 5300.0, 55.0),
}
RTH_START, RTH_END = 9 * 60 + 30, 15 * 60 + 59   # minute-of-day, inclusive


def _vol_profile(minutes: np.ndarray) -> np.ndarray:
    """U-shape across the session, normalised to mean 1.0."""
    x = (minutes - RTH_START) / (RTH_END - RTH_START)      # 0..1
    shape = 0.55 + 1.5 * (x - 0.5) ** 2 * 4                # high at both ends
    return shape / shape.mean()


def make_bars(inst: str, n_days: int, rng: np.random.Generator) -> pd.DataFrame:
    tick, _pv, px0, daily_vol = SPEC[inst]
    days = pd.bdate_range("2025-01-02", periods=n_days)
    mins = np.arange(RTH_START, RTH_END + 1)
    prof = _vol_profile(mins)
    per_min = daily_vol / np.sqrt(len(mins))

    rows = []
    px = px0
    for d in days:
        # overnight gap -- without this, TR collapses to high-low
        px += rng.normal(0, daily_vol * 0.35)
        steps = rng.normal(0, per_min, len(mins)) * prof
        # mild intraday trend so sessions are not pure noise
        steps += rng.normal(0, per_min * 0.25)
        closes = px + np.cumsum(steps)
        opens = np.concatenate([[px], closes[:-1]])
        wick = np.abs(rng.normal(0, per_min * 0.8, len(mins))) * prof
        highs = np.maximum(opens, closes) + wick
        lows = np.minimum(opens, closes) - wick
        vol = np.maximum(1, (rng.gamma(2.0, 300.0, len(mins)) * prof)).astype(int)

        idx = [d + pd.Timedelta(minutes=int(m)) for m in mins]
        rows.append(pd.DataFrame({
            "ts": idx,
            "open": np.round(opens / tick) * tick,
            "high": np.round(highs / tick) * tick,
            "low": np.round(lows / tick) * tick,
            "close": np.round(closes / tick) * tick,
            "volume": vol,
        }))
        px = closes[-1]

    df = pd.concat(rows, ignore_index=True)
    # enforce OHLC coherence after tick rounding
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)
    return df


def verify(df: pd.DataFrame, inst: str) -> None:
    if (df.high < df.low).any():
        raise AssertionError(f"{inst}: high < low")
    if (df.high < df[["open", "close"]].max(axis=1) - 1e-9).any():
        raise AssertionError(f"{inst}: high below open/close")
    if (df.low > df[["open", "close"]].min(axis=1) + 1e-9).any():
        raise AssertionError(f"{inst}: low above open/close")
    if (df.volume <= 0).any():
        raise AssertionError(f"{inst}: non-positive volume")


def load(inst: str, root: str | Path = "data/bars") -> pd.DataFrame:
    p = Path(root) / f"{inst}_1min.parquet"
    df = pd.read_parquet(p)
    return df.set_index("ts")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/bars")
    ap.add_argument("--days", type=int, default=260)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(a.seed)
    for inst in SPEC:
        df = make_bars(inst, a.days, rng)
        verify(df, inst)
        df.to_parquet(out / f"{inst}_1min.parquet", index=False)
        print(f"  {inst}  {len(df):>7,} bars over {a.days} sessions  "
              f"({df.ts.min().date()} .. {df.ts.max().date()})")
    print(f"\nwrote to {out}/  (seed {a.seed}, deterministic)")
    print("OHLC coherence verified on every bar")


if __name__ == "__main__":
    main()
