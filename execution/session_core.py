#!/usr/bin/env python3
"""Streaming session state: 5-minute bars and continuous indicators.

This component feeds BOTH the backtest and the live runtime, which makes it
the most important thing in the system to get right -- and the hardest to
check, for a reason worth spelling out.

THE BLIND SPOT THIS MODULE CREATES. The live runtime and the research
backtest are verified against each other: same inputs, same signals. That is
a real result about consistency and says nothing about correctness, because
this module sits on BOTH sides of the comparison. A wrong ATR would be wrong
identically in both, and the parity check would report success forever.
Parity verification cannot see a shared dependency; it can only detect
divergence between the two things it compares.

That is why `validation/indicator_audit.py` exists -- it recomputes every
value here against an independent library.

SEMANTICS, stated because they are easy to get subtly wrong:

  * 5-minute grid over the regular session, bucket labelled by its START
    minute, aggregated from 1-minute bars.
  * ATR14 = rolling 14-bucket MEAN of true range, SHIFTED BY ONE, and
    CONTINUOUS across days (the ring is never reset). The value available at
    bucket i excludes bucket i's own true range -- a snapshot taken before
    the current bucket is appended. Using a bucket's own range to decide
    something about that bucket is lookahead.
  * That is an SMA of true range, NOT Wilder's smoothing. Most charting
    platforms use Wilder and will differ by roughly 10%. A deliberate choice,
    held consistently on both sides of the system; not a defect, but it will
    surprise you if you compare against a chart.
  * EMA9 = span-9 exponential mean, adjust=False, also SHIFTED BY ONE and
    continuous across days.
  * Session VWAP from bucket typical price (h+l+c)/3, volume weighted, RESET
    each session. Unlike the two above it is NOT shifted: it includes the
    current bucket, because VWAP summarises what has already happened rather
    than predicting what comes next.
  * Previous day high/low come from 1-MINUTE extremes across the full
    session, not from the 5-minute grid -- the last minutes of a session
    routinely print its extreme and a coarse grid can miss it.

WHAT IS NOT HERE. The private system layers additional pattern detection on
top of this core: structural, participation, and regime filters. Those are
the strategy and are not in this repository. What remains is the generic bar
and indicator machinery, which is the reusable part anyway.
"""
from __future__ import annotations

import datetime as dt
from collections import deque
from typing import Optional

_RTH_START_MIN = 9 * 60 + 30
_SESS_END_MIN = 15 * 60 + 59
_GRID_END_MIN = 15 * 60 + 54
_BUCKET_MIN = 5

_ATR_LEN = 14
_EMA9_ALPHA = 2.0 / (9 + 1)


class SessionCore:
    """Feed it 1-minute bars in order; it publishes 5-minute buckets and the
    indicator values legitimately available at each bucket close."""

    def __init__(self) -> None:
        self.today: Optional[dt.date] = None

        # published state
        self.atr_now: Optional[float] = None      # ATR14, shift(1)
        self.ema9_sig: Optional[float] = None     # ema9, shift(1)
        self.vwap: Optional[float] = None         # session VWAP, unshifted
        self.pdh: Optional[float] = None
        self.pdl: Optional[float] = None
        self.prior_close: Optional[float] = None
        self.today_bkts: list = []

        # internal
        self._bkt: Optional[dict] = None
        self._bkt_key: Optional[dt.datetime] = None
        self._atr_ring: deque = deque(maxlen=_ATR_LEN)
        self.prev_bkt_close: Optional[float] = None
        self._ema9: Optional[float] = None
        self._vw_pv = 0.0
        self._vw_v = 0.0
        self._sess_h: Optional[float] = None
        self._sess_l: Optional[float] = None
        self._sess_c: Optional[float] = None

    # -- ingestion ---------------------------------------------------------

    def update(self, ts: dt.datetime, o: float, h: float, l: float,
               c: float, v: float) -> Optional[dict]:
        """Feed one 1-minute bar. Returns the closed 5m bucket, or None."""
        m = ts.hour * 60 + ts.minute
        self._roll_day(ts)
        if _RTH_START_MIN <= m <= _SESS_END_MIN:
            self._sess_h = h if self._sess_h is None else max(self._sess_h, h)
            self._sess_l = l if self._sess_l is None else min(self._sess_l, l)
            self._sess_c = c
        return self._update_5m(ts, m, o, h, l, c, v)

    def _roll_day(self, ts: dt.datetime) -> None:
        d = ts.date()
        if d == self.today:
            return
        if self._bkt is not None:
            self._close_bucket()          # flush a straggler before rolling
        if self._sess_h is not None:
            self.pdh, self.pdl = self._sess_h, self._sess_l
        if self._sess_c is not None:
            self.prior_close = self._sess_c
        self.today = d
        self._sess_h = self._sess_l = self._sess_c = None
        self.today_bkts = []
        # The ATR ring and ema9 are deliberately NOT reset -- they are
        # continuous across sessions. VWAP is reset, because it is a session
        # statistic by definition.
        self._vw_pv = self._vw_v = 0.0
        self.vwap = None

    def _update_5m(self, ts: dt.datetime, m: int, o: float, h: float,
                   l: float, c: float, v: float) -> Optional[dict]:
        if not (_RTH_START_MIN <= m <= _GRID_END_MIN + 4):
            return None
        start = m - ((m - _RTH_START_MIN) % _BUCKET_MIN)
        key = ts.replace(hour=start // 60, minute=start % 60,
                         second=0, microsecond=0)
        out = None
        if key != self._bkt_key:
            if self._bkt is not None:
                out = self._close_bucket()
            self._bkt_key = key
            self._bkt = {"m": start, "o": o, "h": h, "l": l, "c": c, "v": v}
        else:
            b = self._bkt
            b["h"] = max(b["h"], h)
            b["l"] = min(b["l"], l)
            b["c"] = c
            b["v"] += v
        return out

    def _close_bucket(self) -> Optional[dict]:
        b = self._bkt
        if b is None or not (_RTH_START_MIN <= b["m"] <= _GRID_END_MIN):
            self._bkt = None
            return None

        # ATR snapshot BEFORE appending this bucket's TR -- this IS the
        # shift(1). Taking it after would leak the current bucket into a
        # decision about the current bucket.
        self.atr_now = (sum(self._atr_ring) / len(self._atr_ring)
                        if len(self._atr_ring) >= _ATR_LEN else None)
        if self.prev_bkt_close is not None:
            tr = max(b["h"] - b["l"],
                     abs(b["h"] - self.prev_bkt_close),
                     abs(b["l"] - self.prev_bkt_close))
        else:
            tr = b["h"] - b["l"]
        self._atr_ring.append(tr)

        # ema9 shift(1): the value usable AT this bucket excludes its close
        self.ema9_sig = self._ema9
        self._ema9 = (b["c"] if self._ema9 is None
                      else self._ema9 + _EMA9_ALPHA * (b["c"] - self._ema9))

        # session VWAP INCLUDES this bucket
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        self._vw_pv += tp * b["v"]
        self._vw_v += b["v"]
        self.vwap = (self._vw_pv / self._vw_v) if self._vw_v > 0 else None

        self.prev_bkt_close = b["c"]
        self.today_bkts.append(dict(b))
        self._bkt = None
        return b

    # -- persistence -------------------------------------------------------

    def save_state(self) -> dict:
        """Indicator state must survive a restart. Re-priming from scratch
        produces a measurably different EMA than one carried continuously,
        and a restart is not supposed to change what the strategy sees."""
        return {
            "today": str(self.today) if self.today else None,
            "atr_ring": list(self._atr_ring),
            "prev_bkt_close": self.prev_bkt_close,
            "ema9": self._ema9,
            "pdh": self.pdh, "pdl": self.pdl,
            "prior_close": self.prior_close,
            "vw_pv": self._vw_pv, "vw_v": self._vw_v,
        }

    def load_state(self, s: dict) -> None:
        self.today = (dt.date.fromisoformat(s["today"])
                      if s.get("today") else None)
        self._atr_ring = deque(s.get("atr_ring", []), maxlen=_ATR_LEN)
        self.prev_bkt_close = s.get("prev_bkt_close")
        self._ema9 = s.get("ema9")
        self.pdh, self.pdl = s.get("pdh"), s.get("pdl")
        self.prior_close = s.get("prior_close")
        self._vw_pv = s.get("vw_pv", 0.0)
        self._vw_v = s.get("vw_v", 0.0)
