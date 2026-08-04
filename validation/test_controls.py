#!/usr/bin/env python3
"""The five analytic controls for the scoring engine.

Every one has an answer derivable BY HAND, so a broken engine cannot
hide behind a plausible-looking number. Nothing downstream runs until
all five pass.

    python validation/test_controls.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent

from engine import engine as E                                   # noqa: E402


def series_from(pnl_list, mae_list=None):
    """Synthetic series. Index is positional -- the engine reads the
    pnl/mae columns only, and one row is one calendar day by
    construction, so no real dates are needed here."""
    n = len(pnl_list)
    return pd.DataFrame({"pnl": pnl_list,
                         "mae": mae_list if mae_list is not None else [0.0] * n})


def t1_driftless():
    """A zero-drift walk hits +$3,000 before -$2,000 with probability
    F/(T+F) = 2000/5000 = 40%. Pure math.

    TWO CONDITIONS the first version of this test got wrong:
      * trailing_floor MUST be off. F/(T+F) is the fixed-barrier result;
        a floor that ratchets up behind the peak makes the true answer
        strictly lower, so testing against 40% with trailing on would
        fail a CORRECT engine.
      * the window must be long enough that paths actually RESOLVE.
        Unresolved paths are timeouts, not barrier outcomes, and would
        swamp the statistic inside 30 days.
    Small steps ($100) keep barrier overshoot negligible.

    THIRD CONDITION: an INDEPENDENT walk per trial. Resampling windows
    out of one shared 60k-step path gives overlapping, correlated draws
    -- that produced 35.4% and looked like an engine error when it was
    just sampling noise on a single realisation."""
    from engine import run_eval
    rng = np.random.default_rng(7)
    n_trial, steps = 4000, 2500
    npass = nfail = nunres = 0
    for _ in range(n_trial):
        pnl = rng.choice([100.0, -100.0], size=steps)
        mae = np.zeros(steps)
        r = run_eval(pnl, mae, 0, use_dll=False, trailing_floor=False,
                     window=steps)
        if r.outcome == "PASS":
            npass += 1
        elif r.outcome == "FAIL_DD":
            nfail += 1
        else:
            nunres += 1
    resolved = npass + nfail
    cond = npass / resolved if resolved else 0.0
    ok = 0.375 <= cond <= 0.425
    return ok, (f"P(pass | resolved)={cond*100:.1f}% of {resolved} independent "
                f"walks (theory 40.0%, unresolved {nunres})")


def t2_constant_gain():
    """+$150/day, zero MAE -> balance crosses $3,000 during day 20.
    20 x 150 = 3000. Must PASS on exactly day 20, every time."""
    s = series_from([150.0] * 400)
    r = E.simulate(s, n_sims=200, seed=5)
    ok = abs(r["pass"] - 1.0) < 1e-9 and r["median_days_to_pass"] == 20
    return ok, f"pass={r['pass']*100:.0f}% median_day={r['median_days_to_pass']}"


def t3_constant_loss():
    """-$100/day, zero MAE -> balance hits the -$2,000 floor during
    day 20. Must FAIL_DD on exactly day 20, every time."""
    s = series_from([-100.0] * 400)
    r = E.simulate(s, n_sims=200, seed=5, use_dll=False)
    ok = abs(r["fail_dd"] - 1.0) < 1e-9 and r["median_days_to_fail"] == 20
    return ok, f"fail_dd={r['fail_dd']*100:.0f}% median_day={r['median_days_to_fail']}"


def t4_flat_times_out_at_30_calendar_days():
    """THE CALENDAR CONTROL -- the bug that inflated every number in this
    project. A book that never trades must TIMEOUT at exactly calendar
    day 30, not at 30 trading days (~45 calendar days)."""
    s = series_from([0.0] * 400)
    r = E.simulate(s, n_sims=100, seed=5)
    ok = abs(r["timeout"] - 1.0) < 1e-9 and r["n_calendar_days"] == 400
    from engine import run_eval
    rr = run_eval(s["pnl"].to_numpy(float), s["mae"].to_numpy(float), 0)
    ok = ok and rr.outcome == "TIMEOUT" and rr.day == 30
    return ok, f"timeout={r['timeout']*100:.0f}% resolved on day {rr.day} (must be 30)"


def t5_dll_caps_the_day():
    """A day whose intraday MAE exceeds $1,000 must be capped at exactly
    -$1,000 rather than taking its full loss -- and the account must
    SURVIVE it. Here the raw day is -$2,500 with $2,500 MAE."""
    from engine import run_eval
    # -$2,500 with $2,500 intraday excursion. Without the DLL the
    # intraday check (bal - mae = -2500) breaches the -$2,000 floor and
    # the account dies. With it, the day is capped at -$1,000 and the
    # account survives. The first version of this test used -$1,500,
    # which never reaches the floor at all -- so "without DLL" correctly
    # survived and the test was wrong, not the engine.
    pnl = np.array([-2500.0] + [0.0] * 60)
    mae = np.array([2500.0] + [0.0] * 60)
    with_dll = run_eval(pnl, mae, 0, use_dll=True)
    without = run_eval(pnl, mae, 0, use_dll=False)
    ok = (with_dll.outcome == "TIMEOUT" and abs(with_dll.balance + 1000.0) < 1e-9
          and with_dll.dll_hits == 1 and without.outcome == "FAIL_DD")
    return ok, (f"with DLL: {with_dll.outcome} bal={with_dll.balance:+.0f} "
                f"hits={with_dll.dll_hits} | without: {without.outcome}")


if __name__ == "__main__":
    tests = [("1 driftless -> 40%", t1_driftless),
             ("2 constant +150 -> pass day 20", t2_constant_gain),
             ("3 constant -100 -> bust day 20", t3_constant_loss),
             ("4 flat -> timeout day 30 CALENDAR", t4_flat_times_out_at_30_calendar_days),
             ("5 DLL caps day at -1000, survives", t5_dll_caps_the_day)]
    n_ok = 0
    print("=== ANALYTIC CONTROLS (hand-derivable answers) ===\n")
    for name, fn in tests:
        try:
            ok, msg = fn()
        except Exception as exc:
            ok, msg = False, f"EXCEPTION {type(exc).__name__}: {exc}"
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:38} {msg}")
        n_ok += ok
    print(f"\n{n_ok}/{len(tests)} controls pass")
    sys.exit(0 if n_ok == len(tests) else 1)
