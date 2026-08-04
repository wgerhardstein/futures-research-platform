#!/usr/bin/env python3
"""One scoring path for prop-firm evaluation accounts.

WHAT THIS IS. A trader on a funded-account evaluation must reach a profit
target before breaching a trailing drawdown floor, within a fixed number of
calendar days. Given a series of daily P&L and daily adverse excursion, this
answers: starting on day X, would that account have passed, busted, or run
out of time?

NO RANDOM SAMPLING IN THE MEASUREMENT PATH. `run_eval()` walks one real day
sequence. `census()` walks it from EVERY possible start day and counts the
outcomes. The pass rate is literally passes / possible starts -- not an
estimate of it. Identical output on every run, no seed, nothing to tune.

This replaced a Monte Carlo sampler, and the reason is worth stating: the
same book once scored two very different numbers because two code paths had
silently drifted apart, and random variation between runs is excellent
camouflage for that kind of bug. Determinism makes divergence obvious
immediately.

ONE SCORING PATH, ENFORCED. Every variation -- trailing floor on/off, daily
loss limit on/off, dynamic sizing, a no-clock account, a consistency rule --
is a FLAG or an INPUT to this one function, never a second implementation.
Two implementations of the same rule will eventually disagree, and you will
not find out from the code; you will find out from a number you cannot
reproduce.

THE CALENDAR BUG THIS FIXES, which is the most instructive thing here. An
earlier version treated 30 ROWS of a trading-day series as a 30-day window.
Trading days exclude weekends, so 30 rows actually spans about 45 CALENDAR
days -- roughly 50% more runway than the account really gets. The effect was
large and entirely in the flattering direction: a materially higher apparent
pass rate and far fewer timeouts. Here the series carries EVERY calendar day,
with weekends and holidays present as zero rows, so a 30-day window is 30
real days and a Friday start genuinely differs from a Monday start.

The lesson generalises past trading: an index is not a date, and any deadline
measured in rows rather than in time will silently grant extra runway.

RULES MODELLED (50K evaluation tier; these are the firm's published terms)
  target       +$3,000
  max drawdown  $2,000, trailing the peak balance
  daily loss    $1,000 SOFT -- intraday unrealised touching it caps the day
                at exactly -$1,000 and halts trading; the account survives
  window        30 CALENDAR days, hard deadline
  breach        HARD and INTRADAY: balance minus the day's adverse excursion
                touching the floor fails the account, even though the floor
                itself only steps at the session boundary

SLIPPAGE is charged per contract and grows with size, because filling ten
contracts is worse than filling one. It is deliberately explicit rather than
buried inside the P&L, since it biases the position-sizing decision
specifically and that is exactly where you want it visible.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
FROZEN = HERE.parent / "data" / "trades"

TARGET = 3000.0
MAX_DD = 2000.0
DLL = 1000.0
LOCK = 100.0            # floor stops trailing at start + $100
WINDOW_CAL_DAYS = 30

# headroom_guard: cushion below which NO position exists to take. The
# idealized guard scales a day by a fraction, which lets size shrink
# without limit; real positions come in whole contracts and the smallest
# tick we trade is $0.50 (MNQ). Without an absolute floor the cushion
# decays geometrically toward zero and float rounding eventually lands
# it exactly ON the barrier, which is how test_invariants.py found a
# bust inside a guard whose own docstring promised none.
MIN_ROOM = 1.0

# contract point values, stated here rather than imported. The frozen
# CSVs already carry P&L in dollars; these are needed only to convert
# bar prices into dollars when reconstructing an exact intraday path.
POINT_VALUE = {"MNQ": 2.0, "MES": 5.0, "MGC": 10.0, "MCL": 100.0}

# extra $ of slippage per contract, per trade, beyond the base friction
# already inside pnl_1x. 0 for size 1 (the base case the CSVs were
# built at) and growing with size.
SLIP_PER_EXTRA_CONTRACT = 0.25


def slip_cost(size: int, n_trades: int) -> float:
    """Total extra slippage $ for holding `size` contracts across
    `n_trades` fills. Zero at size 1 by construction."""
    return max(0, size - 1) * SLIP_PER_EXTRA_CONTRACT * n_trades * 2  # in+out


# ────────────────────────────────────────────────────────── day series
def load_leg(name: str) -> pd.DataFrame:
    df = pd.read_csv(FROZEN / f"{name}.csv", parse_dates=["date"])
    df["date"] = df["date"].dt.date
    return df


# NOTE (public release): the exact-MAE path is omitted here.
#
# The private version also offers build_day_series_exact(), which
# reconstructs the true minute-level trough of the combined account
# instead of summing each leg's individual worst excursion. Summing
# assumes every leg bottoms out in the same minute, which overstates
# the real trough and penalises wider books systematically.
#
# It is omitted for one reason only: it requires licensed 1-minute bar
# data that cannot be redistributed. The summed path below is the
# conservative one, so every number this public engine produces is if
# anything harsher than the private engine's.

def build_day_series(book: dict, legs: dict | None = None,
                     apply_slippage: bool = True) -> pd.DataFrame:
    """book: {leg_name: size}. Returns one row per CALENDAR day between
    the first and last trade, with pnl/mae summed across legs at their
    sizes. Non-trading days are present with zeros -- that is what makes
    a 30-row window equal 30 calendar days.

    MAE is summed across legs on the same day (conservative: assumes
    they trough together). The exact minute-path treatment is a separate
    refinement; this engine deliberately errs pessimistic."""
    legs = legs or {}
    frames = []
    for name, size in book.items():
        if size <= 0:
            continue
        d = legs.get(name)
        if d is None:
            d = load_leg(name)
            legs[name] = d
        g = d.groupby("date").agg(pnl=("pnl_1x", "sum"), mae=("mae_1x", "sum"),
                                  n=("pnl_1x", "size"))
        g = g * [size, size, 1]
        if apply_slippage:
            g["pnl"] = g["pnl"] - g["n"] * slip_cost(size, 1)
        frames.append(g[["pnl", "mae"]])
    if not frames:
        raise ValueError("empty book")
    tot = frames[0]
    for f in frames[1:]:
        tot = tot.add(f, fill_value=0.0)
    full = pd.date_range(min(tot.index), max(tot.index), freq="D")
    out = pd.DataFrame(index=[d.date() for d in full])
    out["pnl"] = tot["pnl"].reindex(out.index).fillna(0.0)
    out["mae"] = tot["mae"].reindex(out.index).fillna(0.0)
    return out


# ────────────────────────────────────────────────────────── simulator
@dataclass
class Result:
    outcome: str            # PASS / FAIL_DD / TIMEOUT / NO_DATA
    day: int                # calendar day the outcome occurred on
    balance: float
    dll_hits: int = 0
    peak: float = 0.0


def run_eval(days: np.ndarray, maes: np.ndarray, start: int,
             *, trailing_floor: bool = True, use_dll: bool = True,
             policy=None, window: int | None = WINDOW_CAL_DAYS,
             consistency: float | None = None,
             headroom_guard: bool = False,
             lock: float | None = None) -> Result:
    """Walk one eval from `start`. days/maes are per CALENDAR day.

    policy(day_idx, balance, cushion, days_left) -> size multiplier
    (1.0 = as sized). This is the ONLY hook for dynamic behaviour --
    coast-to-pass, defend-near-floor, hail-mary. Default None = static.

    window=None models a NO-CLOCK evaluation (some firms' never expire); the
    walk runs to the end of data and an unresolved outcome is TIMEOUT,
    which the caller should read as CENSORED, not failed.

    consistency=0.5 models a CONSISTENCY rule: passing requires the
    best single profitable day <= 50% of total profit at the time of
    passing, so a big payday forces grinding until total >= 2x that day.

    headroom_guard=True models the runner's _headroom_guard (day-level,
    IDEALIZED): the day is scaled down so its adverse excursion cannot
    exceed the cushion above yesterday's floor, and trading halts at
    zero cushion. Idealized because the live guard sizes per-entry off
    planned full-stop risk and real stops can slip/gap past their
    levels -- this flag is the guard's BEST case (busts become
    impossible by construction; what remains is pass vs timeout).
    """
    n = len(days)
    bal = 0.0
    peak = 0.0
    floor = -MAX_DD
    dll_hits = 0
    best_day = 0.0
    if window is None:
        window = n - start
    avail = min(window, n - start)
    if avail < window or window <= 0:
        return Result("NO_DATA", 0, 0.0)

    for k in range(1, window + 1):
        i = start + k - 1
        pnl, mae = days[i], maes[i]

        if policy is not None:
            mult = policy(k, bal, bal - floor, window - k)
            pnl, mae = pnl * mult, mae * mult

        if headroom_guard and mae > 0:
            room = (bal - floor) * 0.999
            if room <= MIN_ROOM:
                pnl = mae = 0.0          # entry blocked: one stop from breach
            elif mae > room:
                g = room / mae           # resize so full excursion fits
                pnl, mae = pnl * g, mae * g

        if use_dll and mae >= DLL:
            # FOUND BY A CONTROL TEST, and it had been wrong in an
            # earlier engine too: capping the day's P&L at -DLL while
            # leaving the excursion uncapped is incoherent. If the broker
            # liquidates the moment unrealised P&L touches the daily
            # limit, then the day's adverse excursion IS that limit -- the
            # account never reached the larger figure. Checking the floor
            # against an uncapped excursion reports busts that physically
            # cannot happen, biasing every result pessimistic on exactly
            # the days that matter most.
            pnl = -DLL
            mae = DLL
            dll_hits += 1

        # HARD intraday breach against yesterday's floor
        if bal - mae <= floor:
            return Result("FAIL_DD", k, bal, dll_hits, peak)

        bal += pnl
        best_day = max(best_day, pnl)
        peak = max(peak, bal)
        if trailing_floor:
            # An EVALUATION floor trails peak - MAX_DD forever, with NO
            # ceiling. Some account types instead LOCK the floor once it
            # reaches a threshold, and conflating the two makes results far
            # too generous in exactly the endgame that decides the outcome:
            # at peak +$2,900 a locked floor leaves a $2,800 cushion where
            # the real one leaves $2,000. Verified against the broker
            # platform directly rather than assumed. Pass `lock=` for
            # account types that do lock.
            floor = peak - MAX_DD if lock is None else min(lock, peak - MAX_DD)
        if bal <= floor:
            return Result("FAIL_DD", k, bal, dll_hits, peak)
        if bal >= TARGET and (consistency is None
                              or best_day <= consistency * bal):
            return Result("PASS", k, bal, dll_hits, peak)

    return Result("TIMEOUT", window, bal, dll_hits, peak)


def census(series: pd.DataFrame, **kw) -> dict:
    """EXHAUSTIVE walk: run one eval from EVERY calendar start day that
    has a full 30-day window ahead of it. Counting, not sampling -- no
    RNG, no seed, identical output every run. 'pass' is literally
    passes / possible_starts over the whole history.

    Replaced a random-start sampler: random starts only ever approximated
    this number, and the sampling noise made a real divergence between two
    code paths hard to see. The census IS the number.

    Overlap caveat, stated rather than hidden: consecutive starts share
    29 of 30 days, so the denominator that bounds statistical precision
    is independent_windows, not n_starts."""
    days = series["pnl"].to_numpy(float)
    maes = series["mae"].to_numpy(float)
    n = len(days)
    last_start = n - WINDOW_CAL_DAYS          # inclusive: full window fits
    if last_start < 0:
        return {"error": "series shorter than one window", "n_days": n}

    res = {"PASS": 0, "FAIL_DD": 0, "TIMEOUT": 0, "NO_DATA": 0}
    days_to_pass, days_to_fail, dll = [], [], 0
    for start in range(last_start + 1):
        r = run_eval(days, maes, start, **kw)
        res[r.outcome] += 1
        dll += r.dll_hits
        if r.outcome == "PASS":
            days_to_pass.append(r.day)
        elif r.outcome != "NO_DATA":
            days_to_fail.append(r.day)

    resolved = (last_start + 1) - res["NO_DATA"]
    p = res["PASS"] / resolved if resolved else 0.0
    out = {
        "pass": p,
        "fail_dd": res["FAIL_DD"] / resolved if resolved else 0.0,
        "timeout": res["TIMEOUT"] / resolved if resolved else 0.0,
        # the raw counts -- the actual arithmetic behind the rates
        "n_starts": resolved,
        "n_pass": res["PASS"],
        "n_fail_dd": res["FAIL_DD"],
        "n_timeout": res["TIMEOUT"],
        "median_days_to_pass": float(np.median(days_to_pass)) if days_to_pass else None,
        "median_days_to_fail": float(np.median(days_to_fail)) if days_to_fail else None,
        "dll_hits_per_start": dll / resolved if resolved else 0.0,
        "n_calendar_days": n,
        # the honest denominator: independent non-overlapping windows
        "independent_windows": n // WINDOW_CAL_DAYS,
    }
    # business metrics -- the real objective
    if p > 0:
        out["evals_per_funded"] = 1.0 / p
        out["days_per_funded"] = (
            (1 - p) / p * (out["median_days_to_fail"] or WINDOW_CAL_DAYS)
            + (out["median_days_to_pass"] or WINDOW_CAL_DAYS))
    return out


def simulate(series: pd.DataFrame, n_sims: int | None = None,
             seed: int | None = None, **kw) -> dict:
    """Compatibility shim so older scripts keep running. n_sims and seed
    are IGNORED -- every caller now gets the exhaustive census."""
    return census(series, **kw)
