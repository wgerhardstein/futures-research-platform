#!/usr/bin/env python3
"""How big does a difference have to be before it means anything?

THE PROBLEM THIS SOLVES, and it is the most useful thing in this repository.

The census enumerates every possible start day, so its pass rate is not an
estimate -- it is exactly the fraction of start days that passed. It is very
tempting to treat that as certain, and to act on small differences between
configurations: this book beat that one by three points, so ship it.

That reasoning is wrong, and the reason is subtle. The number is exact for
THIS history, but the history itself is one sample of a process that could
have gone differently. Change the sample and the pass rate moves. So before
comparing two configurations you need to know how much the number moves for
reasons that have nothing to do with the configurations.

Answering that requires resampling the day series, and resampling a
DRAWDOWN-driven statistic has a trap in it.

THE BLOCK-LENGTH TRAP. An ordinary bootstrap draws days independently, which
destroys the ordering -- and ordering is everything here, because a drawdown
is a property of a SEQUENCE. Two losing days back to back can breach a floor
that the same two days separated by a winner never would. So a block
bootstrap is required, and the block length has to come from the right
series:

  * block length from SIGNED daily P&L comes out near zero -- direction is
    close to a coin flip day to day, so the sampler concludes there is
    nothing to preserve and shuffles almost freely.
  * block length from ABSOLUTE daily P&L comes out much longer, because
    VOLATILITY clusters strongly even when direction does not.

Magnitude clustering is exactly what drives busts. Taking the block length
from signed P&L would produce confidence intervals that are far too narrow
and would license precisely the false confidence this tool exists to remove.
So it is taken from |pnl|.

WHAT TO DO WITH THE ANSWER. Compare the gap between two configurations to
the width here. In the private system this measurement re-graded a week of
apparently-meaningful comparisons as noise, and the correct response was to
stop deciding on them and use mechanism, tail risk, or operational
simplicity instead.

    python -m validation.noise_floor [--reps 300]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from arch.bootstrap import StationaryBootstrap, optimal_block_length

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import engine as E                                       # noqa: E402

# Two configurations of the same synthetic legs, differing only in size.
# The question is whether the census can tell them apart at all.
BOOKS = {
    "config A (6 contracts)": {"alpha_MNQ": 2, "beta_MNQ": 2,
                               "gamma_MES": 1, "delta_MES": 1},
    "config B (9 contracts)": {"alpha_MNQ": 3, "beta_MNQ": 3,
                               "gamma_MES": 2, "delta_MES": 1},
}


def census_rate(days: np.ndarray, maes: np.ndarray) -> float:
    """Pass rate over every start day with a full window ahead of it."""
    last = len(days) - E.WINDOW_CAL_DAYS
    if last < 0:
        return np.nan
    n = p = 0
    for s in range(last + 1):
        r = E.run_eval(days, maes, s)
        if r.outcome == "NO_DATA":
            continue
        n += 1
        p += (r.outcome == "PASS")
    return p / n if n else np.nan


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=300)
    a = ap.parse_args()

    series = {k: E.build_day_series(b) for k, b in BOOKS.items()}
    names = list(BOOKS)

    # align the two books on a common calendar so pairs stay comparable
    n = min(len(s) for s in series.values())
    pnl = {k: s["pnl"].to_numpy(float)[:n] for k, s in series.items()}
    mae = {k: s["mae"].to_numpy(float)[:n] for k, s in series.items()}

    ref = pnl[names[0]]
    bl_signed = float(optimal_block_length(ref)["stationary"].iloc[0])
    bl_abs = float(optimal_block_length(np.abs(ref))["stationary"].iloc[0])
    block = max(1.0, bl_abs)
    print("arch optimal_block_length (Politis-White):")
    print(f"    signed pnl   {bl_signed:6.1f} d   (direction is near a coin flip)")
    print(f"    |pnl|        {bl_abs:6.1f} d   (volatility clusters)  <- USING")
    print(f"    -> ~{n/block:.0f} independent blocks in {n} calendar days\n")

    point = {k: census_rate(pnl[k], mae[k]) for k in names}

    # Resample INDEX positions once per replication and apply the SAME
    # resampling to both books, so the paired difference is not contaminated
    # by the two books being drawn on different days.
    idx = np.arange(n)
    bs = StationaryBootstrap(block, idx, seed=17)
    rows = []
    print(f"running {a.reps} stationary-bootstrap replications ...")
    for i, (data, _kw) in enumerate(bs.bootstrap(a.reps)):
        take = data[0].astype(int)
        rows.append({k: census_rate(pnl[k][take], mae[k][take]) for k in names})
    rep = pd.DataFrame(rows).dropna()

    print("\n95% CONFIDENCE INTERVALS (percentile, stationary block bootstrap)")
    for k in names:
        lo, hi = np.percentile(rep[k], [2.5, 97.5])
        print(f"  {k:<26} {point[k]*100:5.1f}%   "
              f"[{lo*100:5.1f}%, {hi*100:5.1f}%]   width {(hi-lo)*100:5.1f}")

    d = rep[names[0]] - rep[names[1]]
    lo, hi = np.percentile(d, [2.5, 97.5])
    obs = (point[names[0]] - point[names[1]]) * 100
    print(f"\n  observed difference        {obs:+5.1f} pts")
    print(f"  95% CI on the difference   [{lo*100:+5.1f}, {hi*100:+5.1f}]")
    print(f"  sd of the paired diff      {d.std()*100:5.2f} pts")
    print(f"  -> a gap must exceed ~{1.96*d.std()*100:.1f} pts to be "
          f"distinguishable at 95%")
    verdict = ("NOT distinguishable from zero" if lo < 0 < hi
               else "distinguishable")
    print(f"\n  VERDICT: the difference between these two configs is {verdict}.")
    print("\n  Read this as a floor on every comparison you make: a gap")
    print("  smaller than the width above is not evidence, however tempting.")


if __name__ == "__main__":
    main()
