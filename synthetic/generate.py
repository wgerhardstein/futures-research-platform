#!/usr/bin/env python3
"""Synthetic trade generator -- derives trades from the synthetic bars.

WHY THIS EXISTS. The real trade files behind this platform are the output of
private strategy logic, so they are not in this repository. A repository
nobody can run is not worth reading, so this produces trade series with the
identical schema, letting the whole validation suite execute end to end on
data that discloses nothing.

WHY IT RUNS A STRATEGY RATHER THAN DRAWING RANDOM NUMBERS. The fill auditor
re-resolves each trade's exit against 1-minute bars and compares to what we
recorded. If trades were drawn from a distribution they would not correspond
to any bar sequence, the auditor would have nothing coherent to check, and
the most interesting test in the repository would be a no-op. So trades are
produced by running `strategy.placeholder` over `synthetic.bars` and
resolving each bracket honestly against those bars, stop checked first.

The placeholder has no edge and is not meant to. Several generated legs lose
money. That is fine and somewhat the point: the validation layer exists to
tell you when something does not work.

THE DAY-SERIES CONTRACT. Every row satisfies `mae >= max(0, -pnl)` -- a trade
cannot lose more than its own worst adverse excursion. The engine's risk
arithmetic depends on it, and it is asserted before anything is written.

    python -m synthetic.generate [--out data/trades] [--bars data/bars]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from strategy.placeholder import PlaceholderRuntime
from synthetic.bars import SPEC, load

EOD_MIN = 15 * 60 + 54          # flat-by minute; matches the live convention

# leg -> (instrument, entry minute, stop pts, target pts, side)
# Deliberately varied so the suite sees wins, losses, and EOD exits.
LEGS = {
    "alpha_MNQ": ("MNQ", 10 * 60 + 30, 40.0, 100.0, +1),
    "beta_MNQ":  ("MNQ", 11 * 60 + 30, 30.0,  60.0, -1),
    "gamma_MES": ("MES", 10 * 60 +  0, 10.0,  50.0, +1),
    "delta_MES": ("MES", 13 * 60 +  0, 25.0,  75.0, -1),
}


def resolve(day: pd.DataFrame, i: int, side: int, sl_pts: float,
            tp_pts: float, pv: float) -> dict | None:
    """Walk forward from the entry bar, STOP CHECKED FIRST within each bar.

    Checking the stop first is the conservative convention: when a single bar
    spans both levels we cannot know from 1-minute data which was touched
    first, so we book the loss. The alternative flatters every result.
    """
    if i + 1 >= len(day):
        return None
    ep = float(day.open.iloc[i + 1])            # fill at next bar's open
    sl = ep - side * sl_pts
    tp = ep + side * tp_pts

    mae = mfe = 0.0
    for j in range(i + 1, len(day)):
        b = day.iloc[j]
        adverse = (ep - float(b.low)) if side > 0 else (float(b.high) - ep)
        favour = (float(b.high) - ep) if side > 0 else (ep - float(b.low))
        mae = max(mae, adverse * pv)
        mfe = max(mfe, favour * pv)
        hit_stop = (float(b.low) <= sl) if side > 0 else (float(b.high) >= sl)
        hit_tgt = (float(b.high) >= tp) if side > 0 else (float(b.low) <= tp)
        if hit_stop:
            return dict(xt=day.index[j], xpx=sl, pnl=-sl_pts * pv,
                        mae=max(mae, sl_pts * pv), mfe=mfe)
        if hit_tgt:
            return dict(xt=day.index[j], xpx=tp, pnl=tp_pts * pv,
                        mae=mae, mfe=max(mfe, tp_pts * pv))
    last = day.iloc[-1]
    pnl = (float(last.close) - ep) * side * pv
    return dict(xt=day.index[-1], xpx=float(last.close), pnl=pnl,
                mae=max(mae, max(0.0, -pnl)), mfe=mfe)


def build_leg(name: str, spec: tuple, bars_root: str) -> pd.DataFrame:
    inst, minute, sl, tp, side = spec
    _tick, pv, _p0, _v = SPEC[inst]
    d1 = load(inst, bars_root)
    rt = PlaceholderRuntime(entry_minute=minute, stop_points=sl,
                            target_points=tp, side=side)
    rt.prime(None)

    rows = []
    for d, day in d1.groupby(d1.index.date):
        day = day[(day.index.hour * 60 + day.index.minute) <= EOD_MIN]
        if len(day) < 3:
            continue
        mins = day.index.hour * 60 + day.index.minute
        hit = np.flatnonzero(mins.to_numpy() == minute)
        if not len(hit):
            continue
        i = int(hit[0])
        intent = rt.on_bar(day.index[i], minute)
        if intent is None:
            continue
        r = resolve(day, i, intent.side, intent.stop_points,
                    intent.target_points, pv)
        if r is None:
            continue
        rows.append(dict(
            date=d, entry_ts=day.index[i + 1], exit_ts=r["xt"], inst=inst,
            side=intent.side, ep=float(day.open.iloc[i + 1]), xpx=r["xpx"],
            pnl_1x=round(r["pnl"], 2), mae_1x=round(r["mae"], 2),
            mfe_1x=round(r["mfe"], 2)))
    return pd.DataFrame(rows)


def verify(df: pd.DataFrame, name: str) -> None:
    """The generator is not exempt from the contract it exists to satisfy."""
    bad = df[df.mae_1x < np.maximum(0.0, -df.pnl_1x) - 1e-6]
    if len(bad):
        raise AssertionError(f"{name}: {len(bad)} rows violate mae >= max(0,-pnl)")
    if (df.mae_1x < 0).any() or (df.mfe_1x < 0).any():
        raise AssertionError(f"{name}: negative excursion")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/trades")
    ap.add_argument("--bars", default="data/bars")
    a = ap.parse_args()

    if not (Path(a.bars) / "MNQ_1min.parquet").exists():
        raise SystemExit(f"no bars at {a.bars}/ -- run `python -m synthetic.bars` first")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    summary = []
    for name, spec in LEGS.items():
        df = build_leg(name, spec, a.bars)
        verify(df, name)
        df.to_csv(out / f"{name}.csv", index=False)
        wr = (df.pnl_1x > 0).mean() * 100 if len(df) else 0.0
        summary.append(dict(leg=name, n=len(df), total=round(df.pnl_1x.sum(), 2),
                            wr=round(wr, 2), max_mae=round(df.mae_1x.max(), 2)))
        print(f"  {name:<12} {len(df):>4} trades  WR {wr:5.1f}%  "
              f"net {df.pnl_1x.sum():>9,.0f}")

    pd.DataFrame(summary).to_csv(out / "_summary.csv", index=False)
    print(f"\nwrote {len(LEGS)} legs to {out}/  (deterministic -- derived from the bars)")
    print("contract verified: mae >= max(0, -pnl) on every row")


if __name__ == "__main__":
    main()
