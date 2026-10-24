"""Paper-log summary: how the live paper trades compare with the backtest."""
from __future__ import annotations

import pandas as pd

from signals import orb


def run() -> None:
    if not orb.LOG.exists():
        print("No paper trades logged yet (data/signals/orb_paper.csv).")
        return
    x = pd.read_csv(orb.LOG)
    p = x[x.primary == True]  # noqa: E712
    print(f"0DTE breakout paper log: {len(x)} logged trades on {x.date.nunique()} days; {len(p)} were the rule's trade (PRIMARY).")
    if len(p):
        f = p[p.filled_at_mid == True]  # noqa: E712
        print(f"  PRIMARY: filled at mid {len(f)}/{len(p)}, mean {100 * p.ret_mid.mean():+.0f}% mid-to-mid, {100 * p.ret_conservative.mean():+.0f}% "
              f"ask-in/bid-out, total ${p.usd_mid_1lot.sum():+,.0f} on 1 contract each.")
    nf = x[x.filled_at_mid == False]  # noqa: E712
    if len(nf):
        print(f"  Signals whose mid limit did NOT fill: {len(nf)}, mean {100 * nf.ret_mid.mean():+.0f}% (if this is much better than the "
              f"filled ones, mid fills are adverse selection).")
    print("  Backtest reference: about +2% to +9% per trade at good fills, about 0 at poor fills. Judge after ~30 trades, not before.")
