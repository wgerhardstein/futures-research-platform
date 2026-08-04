"""Scoring engine for prop-firm evaluation accounts.

Re-exports the public surface so `from engine import run_eval, census` works
as well as `from engine import engine as E`.
"""
from .engine import (           # noqa: F401
    TARGET, MAX_DD, DLL, LOCK, WINDOW_CAL_DAYS, MIN_ROOM, POINT_VALUE,
    Result, run_eval, census, simulate, build_day_series, load_leg,
    slip_cost,
)

__all__ = [
    "TARGET", "MAX_DD", "DLL", "LOCK", "WINDOW_CAL_DAYS", "MIN_ROOM",
    "POINT_VALUE", "Result", "run_eval", "census", "simulate",
    "build_day_series", "load_leg", "slip_cost",
]
