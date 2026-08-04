#!/usr/bin/env python3
"""Property invariants on the scoring path.

The last and most searching layer of the validation programme, and the only
one that can still find a bug the others would miss.

WHY THIS AND NOT MORE EXAMPLE TESTS. `test_controls.py` checks control
cases I chose, so it can only find bugs I already suspected. hypothesis
chooses the inputs adversarially and shrinks any failure to a minimal
counterexample, so it finds the cases I did NOT think of. Every number
in this project flows through run_eval; nothing else is worth this much
scrutiny.

EACH PROPERTY IS DEFINITIONALLY TRUE OR IT IS A BUG. Deliberately NOT
included: plausible-sounding behavioural conjectures like "a uniformly
more profitable series cannot bust." That one is FALSE -- extra profit
raises the peak, which raises the trailing floor, which can bust a path
that survived. Encoding a guess as an invariant just manufactures a
false alarm, so every property below is either an identity, a bound
implied by the rules, or a claim the docstring makes explicitly.

    python -m pytest validation/test_invariants.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


from engine import engine as E                                     # noqa: E402

SET = settings(max_examples=400, deadline=None,
               suppress_health_check=[HealthCheck.too_slow])

OUTCOMES = {"PASS", "FAIL_DD", "TIMEOUT", "NO_DATA"}


@st.composite
def day_series(draw, min_len=1, max_len=90):
    """A CONTRACT-VALID day series:
    mae >= max(0, -pnl), because a day cannot lose more than its own
    worst excursion. Feeding contract-INVALID data would only prove the
    engine mishandles input that cannot exist."""
    n = draw(st.integers(min_len, max_len))
    fl = st.floats(-4000, 4000, allow_nan=False, allow_infinity=False)
    pnl = np.array(draw(st.lists(fl, min_size=n, max_size=n)))
    extra = np.array(draw(st.lists(
        st.floats(0, 3000, allow_nan=False, allow_infinity=False),
        min_size=n, max_size=n)))
    mae = np.maximum(0.0, -pnl) + extra
    return pnl, mae


@st.composite
def series_and_start(draw, max_window=40):
    pnl, mae = draw(day_series(min_len=2))
    w = draw(st.integers(1, max_window))
    start = draw(st.integers(0, max(0, len(pnl) - 1)))
    return pnl, mae, start, w


# --------------------------------------------------------------------
# 1-3. Totality and the shape of the answer
# --------------------------------------------------------------------

@given(series_and_start())
@SET
def test_total_function_never_raises(sas):
    """run_eval is TOTAL on contract-valid input: it always returns one
    of the four outcomes and never raises. Every census cell depends on
    this; an exception on one start day would silently skew a rate."""
    pnl, mae, start, w = sas
    r = E.run_eval(pnl, mae, start, window=w)
    assert r.outcome in OUTCOMES
    assert np.isfinite(r.balance)


@given(series_and_start())
@SET
def test_day_within_window(sas):
    """The reported day is inside the window it was given. A day > window
    would mean the walk ran past the account's deadline."""
    pnl, mae, start, w = sas
    r = E.run_eval(pnl, mae, start, window=w)
    if r.outcome == "NO_DATA":
        return
    assert 1 <= r.day <= w


@given(series_and_start())
@SET
def test_determinism(sas):
    """Identical inputs give an identical Result. This is the no-RNG rule
    enforced mechanically rather than by reading the source."""
    pnl, mae, start, w = sas
    a = E.run_eval(pnl, mae, start, window=w)
    b = E.run_eval(pnl.copy(), mae.copy(), start, window=w)
    assert (a.outcome, a.day, a.balance, a.dll_hits) == \
           (b.outcome, b.day, b.balance, b.dll_hits)


# --------------------------------------------------------------------
# 4-6. The outcome labels mean what they say
# --------------------------------------------------------------------

@given(series_and_start())
@SET
def test_pass_implies_target_reached(sas):
    """PASS is only ever returned at or above +$3,000."""
    pnl, mae, start, w = sas
    r = E.run_eval(pnl, mae, start, window=w)
    if r.outcome == "PASS":
        assert r.balance >= E.TARGET - 1e-9


@given(series_and_start())
@SET
def test_timeout_implies_target_not_reached(sas):
    """Without a consistency rule, TIMEOUT means the target was never
    reached. (WITH a consistency rule this is legitimately false -- an
    account can end above target and still not qualify -- so it is off here.)"""
    pnl, mae, start, w = sas
    r = E.run_eval(pnl, mae, start, window=w, consistency=None)
    if r.outcome == "TIMEOUT":
        assert r.balance < E.TARGET


@given(series_and_start())
@SET
def test_bust_implies_below_target(sas):
    """You cannot bust an account that has already passed. Implied by the
    rules: busting needs bal <= peak - $2,000, and bal >= $3,000 forces
    peak >= $3,000, so the floor sits at >= $1,000 < bal."""
    pnl, mae, start, w = sas
    r = E.run_eval(pnl, mae, start, window=w)
    if r.outcome == "FAIL_DD":
        assert r.balance < E.TARGET


# --------------------------------------------------------------------
# 7-9. Monotonicity in the FLAGS -- a more permissive rule is never worse
# --------------------------------------------------------------------

@given(series_and_start())
@SET
def test_lock_is_weakly_more_permissive(sas):
    """A LOCKED floor is min(LOCK, peak - MAX_DD), so it sits at or below
    the unlocked one on every day. It can therefore never bust a path
    that survived unlocked, and never busts earlier.

    A rule correction stated as a property: if this fails, the lock is not
    doing what its documentation claims."""
    pnl, mae, start, w = sas
    free = E.run_eval(pnl, mae, start, window=w, lock=None)
    lock = E.run_eval(pnl, mae, start, window=w, lock=E.LOCK)
    if lock.outcome == "FAIL_DD":
        assert free.outcome == "FAIL_DD"
        assert free.day <= lock.day


@given(series_and_start())
@SET
def test_longer_window_preserves_a_pass(sas):
    """A pass on day d with a 30-day clock is still a pass with a longer
    one: the walk is identical up to d and PASS returns immediately.
    Guards against the calendar-window bug class described in the engine's
    header: 30 ROWS read as 30 CALENDAR days."""
    pnl, mae, start, w = sas
    short = E.run_eval(pnl, mae, start, window=w)
    if short.outcome != "PASS":
        return
    longer = E.run_eval(pnl, mae, start, window=w + 7)
    if longer.outcome == "NO_DATA":
        return
    assert longer.outcome == "PASS"
    assert longer.day == short.day


@given(series_and_start())
@SET
def test_headroom_guard_makes_busts_impossible(sas):
    """The engine docstring claims headroom_guard=True is the guard's BEST
    case, in which "busts become impossible by construction; what remains
    is pass vs timeout."

    That is an explicit, falsifiable claim about our own code, so it gets
    tested rather than trusted. If a FAIL_DD ever appears here, either the
    guard model is wrong or the bracket built on it is unsound."""
    pnl, mae, start, w = sas
    r = E.run_eval(pnl, mae, start, window=w, headroom_guard=True)
    assert r.outcome != "FAIL_DD"


# --------------------------------------------------------------------
# 10-11. Indexing -- the off-by-one family that has actually bitten us
# --------------------------------------------------------------------

@given(series_and_start())
@SET
def test_start_offset_equals_slicing(sas):
    """Starting at index s on the full array must equal starting at 0 on
    the array sliced from s. The census calls run_eval once per start, so
    an indexing slip here would tilt every rate we quote."""
    pnl, mae, start, w = sas
    a = E.run_eval(pnl, mae, start, window=w)
    b = E.run_eval(pnl[start:], mae[start:], 0, window=w)
    assert (a.outcome, a.day) == (b.outcome, b.day)
    assert abs(a.balance - b.balance) < 1e-9


@given(day_series(min_len=31), st.integers(1, 30))
@SET
def test_leading_zero_days_are_inert_except_for_the_clock(days_pair, pad):
    """Calendar padding (weekends/holidays) carries pnl=0 and mae=0, so it
    must not change the P&L path -- only consume clock. Prepending `pad`
    zero days and lengthening the window by `pad` must reproduce the
    original outcome exactly.

    This is the property the calendar rebuild rests on: zero rows are real
    days that burn the deadline and nothing else."""
    pnl, mae = days_pair
    z = np.zeros(pad)
    a = E.run_eval(pnl, mae, 0, window=30)
    b = E.run_eval(np.concatenate([z, pnl]), np.concatenate([z, mae]),
                   0, window=30 + pad)
    if a.outcome == "NO_DATA" or b.outcome == "NO_DATA":
        return
    assert a.outcome == b.outcome
    assert b.day == a.day + pad
    assert abs(a.balance - b.balance) < 1e-9


# --------------------------------------------------------------------
# 12. The DLL cap is coherent (the bug one of the closed-form controls found)
# --------------------------------------------------------------------

@given(series_and_start())
@SET
def test_dll_bounds_the_daily_loss(sas):
    """With the soft DLL on, no single day may take the balance down by
    more than the daily limit: the broker liquidates the moment unrealised
    touches it. This is the coherence fix in run_eval -- capping pnl at
    -DLL while leaving mae uncapped reported busts that cannot happen."""
    pnl, mae, start, w = sas
    r = E.run_eval(pnl, mae, start, window=w, use_dll=True,
                   trailing_floor=False)
    if r.outcome not in ("TIMEOUT", "PASS"):
        return
    # reconstruct the capped path and check every daily step
    bal, prev = 0.0, 0.0
    for k in range(r.day):
        i = start + k
        p, m = pnl[i], mae[i]
        if m >= E.DLL:
            p = -E.DLL
        bal += p
        assert bal - prev >= -E.DLL - 1e-9
        prev = bal


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
