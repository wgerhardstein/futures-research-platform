#!/usr/bin/env python3
"""Independent check of the indicator math.

THE BLIND SPOT THIS CLOSES. The live runtime and the research backtest are
verified against each other, and `execution/session_core.py` feeds BOTH. So a
wrong ATR would be wrong IDENTICALLY on both sides and the parity check would
report success forever. Parity verification manufactures that blind spot by
construction -- it can only ever detect divergence between the two things it
compares, and a shared dependency cannot diverge from itself.

Only a third implementation can see in. So: drive the real `SessionCore` over
bars, and compare its ATR14, EMA9 and session VWAP against `pandas_ta`,
written by someone else.

TWO THINGS THAT HAD TO BE RIGHT OR THIS GRADES THE WRONG EXAM

1. OUR ATR IS AN SMA OF TRUE RANGE, NOT WILDER'S. `pandas_ta.atr()` defaults
   to RMA (Wilder smoothing) and would disagree BY DESIGN. The correct
   reference is `mamode='sma'`. The Wilder value is also computed and
   reported as information, because "we use SMA where most charts use
   Wilder" is worth knowing even though it is a deliberate choice.

2. SEEDING DIFFERS AND MUST NOT BE GRADED. We seed ema9 from the first bucket
   close; pandas_ta seeds from an SMA of the first `length` values. With
   alpha 0.2 that difference decays below 1e-10 within ~100 buckets, so the
   comparison skips a burn-in and asserts CONVERGENCE rather than equality at
   bar zero. Grading the seed would be a guaranteed false alarm.

A NOTE ON HOW THIS IS HOOKED, because the first version of this audit failed
for the wrong reason. `_roll_day` flushes a straggler bucket by calling
`_close_bucket()` directly and discards the return value, so that bucket's
true range enters the ATR ring and moves ema9 while never appearing in
`update()`'s output. Building the reference from `update()` alone compares
two different bucket sequences, and the ring carries the difference forward
for exactly the lookback length. The symptom -- mismatches in runs of ~14 --
looks precisely like a real indicator bug. Hooking `_close_bucket` fixes it.

That is why this reports WHERE mismatches land, not just that they exist.

    python -m validation.indicator_audit [--inst MNQ]
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import pandas_ta as ta                                   # noqa: E402
from execution.session_core import SessionCore           # noqa: E402
from synthetic.bars import load                          # noqa: E402

BURN_IN = 120          # buckets skipped so ema9 seeding is not graded
TOL = 1e-6             # these are prices, not ratios


def drive_core(d1: pd.DataFrame) -> pd.DataFrame:
    """Feed bars through the real SessionCore, recording every bucket it
    closes -- including stragglers flushed at a day roll (see module note)."""
    core = SessionCore()
    rows = []
    orig = core._close_bucket

    def hooked():
        key = core._bkt_key
        out = orig()
        if out:
            rows.append(dict(ts=key, atr=core.atr_now, ema9=core.ema9_sig,
                             vwap=core.vwap, h=out["h"], l=out["l"],
                             c=out["c"], v=out["v"]))
        return out

    core._close_bucket = hooked
    for ts, r in d1.iterrows():
        core.update(ts, float(r.open), float(r.high), float(r.low),
                    float(r.close), float(r.volume))
    return pd.DataFrame(rows)


def reference(b: pd.DataFrame) -> pd.DataFrame:
    """The same three numbers, computed independently by pandas_ta on the
    bucket series SessionCore itself produced. Bucket construction is not
    under audit here -- the indicator math is."""
    ref = pd.DataFrame(index=b.index)
    ref["atr_sma"] = ta.atr(b.h, b.l, b.c, length=14, mamode="sma").shift(1)
    ref["atr_wilder"] = ta.atr(b.h, b.l, b.c, length=14, mamode="rma").shift(1)
    ref["ema9"] = ta.ema(b.c, length=9).shift(1)
    day = pd.to_datetime(b.ts).dt.date
    tp = (b.h + b.l + b.c) / 3.0
    pv = (tp * b.v).groupby(day).cumsum()
    vv = b.v.groupby(day).cumsum()
    ref["vwap"] = np.where(vv > 0, pv / vv, np.nan)
    return ref


def report(name: str, ours: pd.Series, theirs: pd.Series) -> bool:
    both = ours.notna() & theirs.notna()
    n = int(both.sum())
    if not n:
        print(f"  {name:<8} NO OVERLAP")
        return False
    d = (ours[both] - theirs[both]).abs()
    ok = float(d.max()) < TOL
    print(f"  {name:<8} n={n:<6} max|diff|={d.max():.3e}  "
          f"mean={d.mean():.3e}  {'MATCH' if ok else '*** MISMATCH ***'}")
    if not ok:                     # localise it before anyone draws conclusions
        bad = d[d > TOL]
        sub = ours.index[bad.index]
        print(f"           {len(bad)} bad of {n}; first at position {bad.index[0]}")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inst", default="MNQ")
    ap.add_argument("--bars", default="data/bars")
    a = ap.parse_args()

    d1 = load(a.inst, a.bars)
    print(f"INDICATOR AUDIT -- SessionCore vs pandas_ta {ta.version}")
    print(f"{a.inst}: {len(d1):,} 1-minute bars\n")

    ours = drive_core(d1)
    if ours.empty:
        raise SystemExit("SessionCore produced no buckets")
    ref = reference(ours)
    print(f"{len(ours):,} 5m buckets; first {BURN_IN} skipped so ema9 "
          f"seeding is not graded\n")
    o = ours.iloc[BURN_IN:].reset_index(drop=True)
    r = ref.iloc[BURN_IN:].reset_index(drop=True)

    results = [report("ATR14", o.atr, r.atr_sma),
               report("EMA9", o.ema9, r.ema9),
               report("VWAP", o.vwap, r.vwap)]

    both = o.atr.notna() & r.atr_wilder.notna()
    if both.any():
        w = (o.atr[both] - r.atr_wilder[both]).abs()
        rel = (w / o.atr[both].replace(0, np.nan)).mean() * 100
        print(f"\n  [info] our SMA-ATR vs Wilder RMA-ATR: mean |diff| "
              f"{w.mean():.3f} pts ({rel:.1f}% of ATR).")
        print(f"         Deliberate -- the spec is rolling(14).mean(), not "
              f"Wilder smoothing.")

    print()
    if not all(results):
        print("RESULT: MISMATCH. The indicator feeding BOTH the backtest and\n"
              "production disagrees with an independent implementation.")
        sys.exit(1)
    print(f"RESULT: all three match an independent implementation to {TOL:g}.\n"
          "The parity blind spot is closed for ATR14, EMA9 and session VWAP.")


if __name__ == "__main__":
    main()
