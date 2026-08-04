#!/usr/bin/env python3
"""PLACEHOLDER strategy runtime.

**Production signal logic is private. This placeholder demonstrates the
interface the engine consumes, and nothing else.**

The execution engine drives a strategy through a small, stable contract:

    1. construct    Runtime(**params)
    2. prime        runtime.prime(historical_bars)   seed indicator state
    3. per bar      intent = runtime.on_bar(bar)     Optional[Intent]
    4. on fill      runtime.confirm_entry(...) / confirm_exit()
    5. persistence  runtime.save_state() / load_state(state)

This stub implements that contract exactly. What it does NOT contain is any
of the work that makes a strategy worth running: the indicator combinations,
the regime and session filters, the trend confirmation, the entry and exit
thresholds, the parameter-and-timeframe pairings, or the pre-placement logic.
Those are the product.

What it does instead is emit one long entry per session at a fixed minute
with a fixed bracket. That is deliberately not a strategy -- it has no edge
and is not intended to -- but it produces a realistic *shape* of trades so
the validation suite has something to chew on. Swap in a private module
implementing the same interface to run the engine for real.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Intent:
    """What a runtime asks the execution layer to do."""
    side: int                 # +1 long, -1 short
    stop_points: float
    target_points: float
    reason: str = ""


@dataclass
class PlaceholderRuntime:
    entry_minute: int = 10 * 60 + 30      # minute-of-day for the one entry
    stop_points: float = 30.0
    target_points: float = 60.0
    side: int = 1

    _fired_today: object = None
    _primed: bool = False

    # -- lifecycle ---------------------------------------------------------
    def prime(self, bars) -> None:
        """Real runtimes seed indicator state from history here."""
        self._primed = True

    @property
    def is_primed(self) -> bool:
        return self._primed

    def on_bar(self, ts, minute_of_day: int) -> Optional[Intent]:
        day = ts.date()
        if self._fired_today == day:
            return None
        if minute_of_day != self.entry_minute:
            return None
        self._fired_today = day
        return Intent(side=self.side,
                      stop_points=self.stop_points,
                      target_points=self.target_points,
                      reason="placeholder: fixed-time entry, no edge")

    # -- persistence -------------------------------------------------------
    def save_state(self) -> dict:
        return {"fired_today": str(self._fired_today),
                "primed": self._primed}

    def load_state(self, s: dict) -> None:
        self._primed = bool(s.get("primed", False))
