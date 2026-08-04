#!/usr/bin/env python3
"""Exam for a third-party evaluation simulator.

The candidate is PropAlphaEvalSolver
(github.com/Prop-Alpha/PropAlphaEvalSolver, or any fork -- pass --src).

VERDICT, recorded here so nobody re-runs this hoping for a different one:
the tool FAILS this exam as a scorer of our results, and PASSES as an
independent check of the trailing-drawdown mechanic. Both halves matter,
so this script runs the half that is valid and refuses the half that is
not.

WHY IT CANNOT SCORE OUR BOOK (exam step 1, semantic mapping):

  1. NO CLOCK. `run_eval_only` is `while not passed and not failed` with
     a 700-day escape hatch. The evaluation modelled here has a
     hard 30-day access period, and TIMEOUT is roughly a third of our
     census outcomes. A model that cannot time out cannot produce a
     number comparable to ours -- it answers "would this trader EVER
     pass", which is a different question with a much higher answer.

  2. FLOOR CHECKED ON REALIZED BALANCE ONLY. `if self.balance <=
     self.minimum_balance` (account_models.py:66) runs after each trade
     books. There is no intraday excursion term. Our engine busts on
     `bal - mae <= floor` because a real account is liquidated intraday,
     not at the close. Theirs is the more forgiving model and would
     understate busts.

  3. TRADES ARE A TWO-OUTCOME COIN. Every trade returns exactly +tp or
     -stop. An independent fill audit measured that 769 of
     1,715 of our audited trades -- 45% -- exit at NEITHER level, they
     run to EOD. The generator cannot express our most common trade.

  4. IID, NO CLUSTERING. Draws are independent. We measured that our
     losing days cluster by magnitude, which is what drives busts, and
     the noise-floor tool takes block length from |pnl| for that reason.

  (1) alone is disqualifying. This is a limitation of the whole
  parametric-MC category, not of this tool specifically: every
  win-rate/avg-win/avg-loss calculator shares (3) and (4).

WHAT IT DOES GET RIGHT, and this is the useful half: the EOD-stepping
trail, including the evaluation-vs-funded asymmetry -- a distinction we
got wrong once ourselves and later verified against the broker platform.
Their account model raises the evaluation floor by the gain over the
high-water mark with NO cap, and caps only in the funded branch. An
implementation written by a stranger, for a different firm, landed on
exactly the mechanic we verified.

So the valid experiment is a mechanics cross-check on the narrow case
BOTH engines can express: a driftless coin against an EOD-stepping
trail with no clock, where a closed form also exists. Three-way
agreement there tests our trail code from outside.

    python -m validation.eval_mc_audit [--n 20000] [--src PATH]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

HERE = Path(__file__).resolve().parent

from engine import engine as E                                     # noqa: E402

BIG = 10 ** 9
TARGET, MAXDD, START = 3000.0, 2000.0, 50000.0
STEP = 100.0            # symmetric +/- per trade, one trade per day
MAXD = 5000             # per-path day cap; neither engine should reach it

RULES = {
    'Initial Balance (Eval)': START, 'Max Loss (Eval)': MAXDD,
    'Funding Target Balance': START + TARGET,
    # neutralised so only the trail and the target can decide the path
    'Maximum Daily Win': BIG, 'Maximum Daily Loss': BIG,
    'Winning Day PnL Minimum': BIG, 'Minimum Winning Days for Payout': BIG,
    'Minimum Winning Balance': BIG,
    'Initial Balance (Funded)': START, 'Max Loss (Funded)': MAXDD,
    'Unshared Winning Balance (Funded)': START, 'Profit Share Fraction': 1.0,
}


def theirs(TopstepAccount, n: int, seed: int) -> tuple[int, int]:
    rng = np.random.default_rng(seed)
    npass = nfail = 0
    for _ in range(n):
        a = TopstepAccount(RULES)
        for _ in range(MAXD):
            r = STEP if rng.random() < 0.5 else -STEP
            out = a.trade(r, 0)
            if out == 'PASS EVAL':
                npass += 1
                break
            if out == 'FAIL':
                nfail += 1
                break
            a.end_of_day_update()       # one trade per day
    return npass, nfail


def ours(n: int, seed: int) -> tuple[int, int]:
    rng = np.random.default_rng(seed)
    npass = nfail = 0
    zeros = np.zeros(MAXD)
    for _ in range(n):
        d = np.where(rng.random(MAXD) < 0.5, STEP, -STEP)
        # window=None -> no clock, matching their unbounded loop.
        # use_dll=False -> their daily limits are neutralised above.
        # lock=None -> uncapped eval trail, which is what both model.
        r = E.run_eval(d, zeros, 0, window=None, use_dll=False,
                       trailing_floor=True, lock=None)
        if r.outcome == "PASS":
            npass += 1
        elif r.outcome == "FAIL_DD":
            nfail += 1
    return npass, nfail


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--src", default=str(Path.home() / "src" / "PropAlphaEvalSolver"),
                    help="clone of PropAlphaEvalSolver")
    a = ap.parse_args()

    src = Path(a.src)
    if not (src / "account_models.py").exists():
        print(f"PropAlphaEvalSolver not found at {src}\n"
              f"  git clone https://github.com/Prop-Alpha/PropAlphaEvalSolver {src}")
        sys.exit(2)
    sys.path.insert(0, str(src))
    from account_models import TopstepAccount          # noqa: E402

    tp, tf = theirs(TopstepAccount, a.n, a.seed)
    op, of = ours(a.n, a.seed)
    pt, po = tp / (tp + tf), op / (op + of)
    # driftless walk vs a TRAILING floor: P(pass) = exp(-target/maxdd).
    # (a LOCKED floor would instead give maxdd/(target+maxdd) = 40%.)
    theory = float(np.exp(-TARGET / MAXDD))
    band = 1.96 * np.sqrt(2 * 0.25 / a.n)

    print(f"MECHANICS CROSS-CHECK -- driftless +/-${STEP:.0f} coin, one trade/day,")
    print(f"EOD-stepping ${MAXDD:,.0f} trail, no lock, no clock, target +${TARGET:,.0f}.")
    print(f"{a.n:,} paths per engine.\n")
    print(f"  closed form  exp(-T/F)   : {theory*100:6.2f}%")
    print(f"  our engine.run_eval      : {po*100:6.2f}%   ({op:,} / {op+of:,})")
    print(f"  PropAlphaEvalSolver      : {pt*100:6.2f}%   ({tp:,} / {tp+tf:,})")
    print(f"\n  engine-vs-engine gap     : {abs(pt-po)*100:6.2f} pts"
          f"   (95% MC noise +/-{band*100:.2f})")
    print(f"  widest gap to theory     : {max(abs(pt-theory), abs(po-theory))*100:6.2f} pts"
          f"   (both should sit slightly ABOVE:")
    print(f"                              ${STEP:.0f} steps overshoot the barriers)")
    verdict = "AGREE" if abs(pt - po) < band else "DISAGREE -- investigate"
    print(f"\n  VERDICT: {verdict}")
    print("\nThis validates the TRAIL MECHANIC only. It is not, and cannot be,")
    print("a check of our book's pass rate -- see the module docstring.")


if __name__ == "__main__":
    main()
