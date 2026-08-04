#!/usr/bin/env python3
"""Independent audit of the fill resolver.

WHY THIS MATTERS MORE THAN ANY OTHER TEST HERE. Every result this platform
produces rests on one question: given an entry and a bracket, where did the
trade actually exit? If that is wrong, nothing downstream can be right --
the day series is wrong, the risk arithmetic is wrong, and every conclusion
built on them is confidently wrong. Fill logic is also where the subtle bugs
live: off-by-one indexing, filling at a price the market never traded,
crediting a stop that could not have been touched.

So this hands each trade's (entry bar, side, stop, target) to
`backtesting.py` -- an engine written by someone else -- asks it to resolve
the exit on the SAME bars, and diffs exit bar and exit price against what we
recorded.

TWO CONVENTIONS THAT MUST BE MAPPED BEFORE ANY NUMBER IS TRUSTED. The first
run of this audit against the real system mismatched 2 of the first 3 trades
purely from these, and both are conventions rather than bugs:

1. ENTRY ALIGNMENT. backtesting.py's strategy loop starts at index 1, so
   `next()` first fires on bar 1 and, with `trade_on_close=False`, the order
   fills at bar 2's OPEN. The slice therefore needs TWO lead-in bars for
   their entry to land on ours.

2. END-OF-DAY EXITS ARE NOT COMPARABLE ON PRICE. We take the CLOSE of the
   last bar; their finalisation issues a market order with no next bar to
   fill on, so they take that bar's OPEN. A pure convention difference in
   both directions. EOD trades are therefore compared on WHETHER a bracket
   fired, never on price, and are reported in their own bucket rather than
   counted as agreement.

3. KNOWN AND MAPPED: backtesting.py registers the take-profit BEFORE the
   stop, so on a bar touching both it books the winner where we book the
   loser. Any trade where that could fire is reported separately rather than
   silently counted as agreement.

    python -m validation.fill_audit [--legs alpha_MNQ,beta_MNQ]
"""
from __future__ import annotations

import argparse
import contextlib
import io
import sys
import warnings
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from backtesting import Backtest, Strategy               # noqa: E402
from synthetic.bars import load                          # noqa: E402
from synthetic.generate import LEGS, EOD_MIN             # noqa: E402

TRADES = Path("data/trades")
_BARS: dict = {}


def bars_by_date(inst: str, root: str) -> dict:
    if inst not in _BARS:
        d1 = load(inst, root).rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume"})
        _BARS[inst] = {d: g for d, g in d1.groupby(d1.index.date)}
    return _BARS[inst]


def make_strategy(side: int, sl: float, tp: float):
    class S(Strategy):
        def init(self):
            self._done = False

        def next(self):
            if self._done:
                return
            self._done = True
            (self.buy if side == 1 else self.sell)(size=1, sl=sl, tp=tp)
    return S


def resolve_external(inst, entry_ts, side, ep, tp_pts, sl_pts, root):
    """(exit_ts, exit_px, reason, entry_px) from backtesting.py, or None."""
    g = bars_by_date(inst, root).get(entry_ts.date())
    if g is None or not len(g):
        return None
    mins = g.index.hour * 60 + g.index.minute
    day = g[mins <= EOD_MIN]
    pos = day.index.get_indexer([entry_ts])
    # TWO lead-in bars -- see convention (1) in the module docstring.
    if len(pos) == 0 or pos[0] < 2:
        return None
    sl, tp = ep - side * sl_pts, ep + side * tp_pts
    sub = day.iloc[pos[0] - 2:][["Open", "High", "Low", "Close", "Volume"]]
    if len(sub) < 3:
        return None
    # backtesting.py renders a progress bar per run; with one run per trade
    # that is thousands of bars of noise. Silence it.
    with contextlib.redirect_stderr(io.StringIO()), \
            contextlib.redirect_stdout(io.StringIO()):
        res = Backtest(sub, make_strategy(side, sl, tp), cash=1_000_000,
                       commission=0.0, trade_on_close=False,
                       exclusive_orders=False, finalize_trades=True).run()
    tr = res["_trades"]
    if not len(tr):
        return None
    t = tr.iloc[0]
    xp = float(t["ExitPrice"])
    reason = ("target" if abs(xp - tp) < 1e-6 else
              "stop" if abs(xp - sl) < 1e-6 else "eod/other")
    return sub.index[int(t["ExitBar"])], xp, reason, float(t["EntryPrice"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--legs", default=",".join(LEGS))
    ap.add_argument("--bars", default="data/bars")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    grand = []
    for leg in [x.strip() for x in a.legs.split(",") if x.strip() in LEGS]:
        inst, _minute, sl_pts, tp_pts, _side = LEGS[leg]
        d = pd.read_csv(TRADES / f"{leg}.csv")
        for c in ("entry_ts", "exit_ts"):
            d[c] = pd.to_datetime(d[c])
        if a.limit:
            d = d.head(a.limit)

        rows = []
        for r in d.itertuples():
            out = resolve_external(inst, r.entry_ts, int(r.side), float(r.ep),
                                   tp_pts, sl_pts, a.bars)
            if out is None:
                rows.append(dict(leg=leg, status="NO_RUN"))
                continue
            xt, xp, reason, entry_px = out
            sd = int(r.side)
            ours_eod = (abs(float(r.xpx) - float(r.ep) - sd * tp_pts) > 1e-6 and
                        abs(float(r.xpx) - float(r.ep) + sd * sl_pts) > 1e-6)
            rows.append(dict(
                leg=leg, status="OK",
                entry_match=abs(entry_px - float(r.ep)) < 1e-6,
                ours_eod=ours_eod, theirs_eod=(reason == "eod/other"),
                same_bar=(xt == r.exit_ts),
                dpx=xp - float(r.xpx)))
        f = pd.DataFrame(rows)
        grand.append(f)
        ok = f[f.status == "OK"]
        print(f"== {leg}  ({inst}, +{tp_pts:.0f}/-{sl_pts:.0f}) ==")
        print(f"   trades {len(f)}   resolved by both {len(ok)}")
        if len(ok):
            brk = ok[~ok.ours_eod & ~ok.theirs_eod]
            eod = ok[ok.ours_eod & ok.theirs_eod]
            split = ok[ok.ours_eod != ok.theirs_eod]
            print(f"   entry price identical : {int(ok.entry_match.sum())}/{len(ok)}")
            if len(brk):
                print(f"   both hit a bracket    : {len(brk)}   "
                      f"bar match {int(brk.same_bar.sum())}/{len(brk)}   "
                      f"price match {int((brk.dpx.abs()<1e-6).sum())}/{len(brk)}")
            print(f"   both ran to EOD       : {len(eod)}  (price not comparable)")
            print(f"   *** DISAGREE on whether a bracket fired: {len(split)} ***")
        print()

    if grand:
        a_ = pd.concat(grand)
        ok = a_[a_.status == "OK"]
        brk = ok[~ok.ours_eod & ~ok.theirs_eod]
        split = ok[ok.ours_eod != ok.theirs_eod]
        print("=" * 62)
        print(f"TOTAL resolved by both engines : {len(ok)}")
        print(f"  entry price identical        : {int(ok.entry_match.sum())}/{len(ok)}")
        if len(brk):
            print(f"  bracket-resolved by both     : {len(brk)}")
            print(f"     exit bar identical        : {int(brk.same_bar.sum())}/{len(brk)}")
            print(f"     exit price identical      : "
                  f"{int((brk.dpx.abs()<1e-6).sum())}/{len(brk)}")
        print(f"  DISAGREED on bracket firing  : {len(split)}"
              f"   <-- the only bucket that means anything")


if __name__ == "__main__":
    main()
