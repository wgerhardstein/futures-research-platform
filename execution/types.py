#!/usr/bin/env python3
"""Neutral data types crossing the broker boundary.

Everything above the adapter layer speaks these, so swapping brokers means
writing one adapter rather than touching the strategy, risk, or engine code.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass
class Bar:
    """One OHLCV bar.

    `bar_seconds` travels WITH the bar deliberately. A single broker
    connection can carry several subscriptions at different resolutions, and
    the callback fires for all of them -- without this field a consumer
    cannot tell a 1-minute bar from a 5-minute one, and will happily feed the
    wrong series into an indicator. That exact bug once starved several
    strategies of data for their entire deployed life, because a de-duplication
    key used the symbol alone and every bar after the first was discarded as a
    repeat.
    """
    ts_et: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    bar_seconds: int


@dataclass
class AccountSnapshot:
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    positions: dict = field(default_factory=dict)


@dataclass
class Fill:
    ts_et: dt.datetime
    symbol: str
    side: int                 # +1 buy, -1 sell
    quantity: int
    price: float
    order_id: str = ""
