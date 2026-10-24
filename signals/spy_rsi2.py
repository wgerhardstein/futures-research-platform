"""SPY "buy the dip above the 200-day" rule (Connors RSI(2)): daily signal + paper position. Sends no orders.

Rule (Connors & Alvarez, "Short Term Trading Strategies That Work", 2008):
  * BUY at the close when SPY closes ABOVE its 200-day simple average AND its 2-day RSI (Wilder) is below 10
  * SELL at the first close ABOVE the 5-day simple average
Run it in the last minutes of the session (about 3:50 ET); the current price stands in for the close.

Evidence (research/RESULTS.md, reproduce with `python -m research.spy_rsi2_backtest`): 263 trades 1994-2026, 76% winners,
+0.63% per trade after costs, positive in both halves and above the 95th percentile of 2,000 random-entry controls. Invested
only ~11% of days, so as shares it compounds slowly (+5.1%/yr, max drawdown -11%; SPY itself +10.8%/yr, -55%). Options version: buy a ~45-day at-the-money SPY call on the signal; on real 2024-26 prices
+8.9% per trade (24 trades, 71% winners), but one contract costs ~$1,500.
    python -m signals spy             show today's signal (read-only)
    python -m signals spy --record    same, and update the paper position (schedule this once a day at ~3:50 ET)
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from signals import alpaca
from signals.paths import OUT

ET = ZoneInfo("America/New_York")
STATE = OUT / "spy_rsi2_state.json"
LOG = OUT / "spy_rsi2_paper.csv"


# ---------- pure rule pieces (unit-tested) ----------
def rsi2(close: pd.Series) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def decide(close: pd.Series, in_position: bool) -> dict:
    """close: daily closes ending with today's (or the current) price."""
    sma200, sma5, r = close.rolling(200).mean().iloc[-1], close.rolling(5).mean().iloc[-1], rsi2(close).iloc[-1]
    c = float(close.iloc[-1])
    if in_position:
        action = "SELL" if c > sma5 else "HOLD"
    else:
        action = "BUY" if (c > sma200 and r < 10) else "WAIT"
    return dict(close=c, sma200=float(sma200), sma5=float(sma5), rsi2=float(r), action=action)


def backtest(close: pd.Series, cost: float = 0.0002) -> pd.DataFrame:
    """Every completed trade of the rule on a daily close series (adjusted closes include dividends)."""
    sma200, sma5, r = close.rolling(200).mean(), close.rolling(5).mean(), rsi2(close)
    out, entry = [], None
    for i, d in enumerate(close.index):
        if entry is None and close.iloc[i] > sma200.iloc[i] and r.iloc[i] < 10:
            entry = i
        elif entry is not None and close.iloc[i] > sma5.iloc[i]:
            out.append(dict(entry=close.index[entry], exit=d, days=i - entry, ret=close.iloc[i] / close.iloc[entry] - 1 - 2 * cost))
            entry = None
    return pd.DataFrame(out)


# ---------- live ----------
def closes() -> pd.Series:
    """Up to yesterday from SIP daily bars (free accounts may read data older than 15 minutes) + the live IEX price."""
    now = datetime.now(ET)
    start = (now - pd.Timedelta(days=420)).strftime("%Y-%m-%d")
    end = (pd.Timestamp.now(tz=ET) - pd.Timedelta(minutes=16)).isoformat()  # free plan: SIP data must be > 15 minutes old
    b = alpaca.stock_bars(["SPY"], "1Day", start, end, feed="sip")["SPY"]
    s = pd.Series({pd.Timestamp(x["t"]).tz_convert(ET).date(): float(x["c"]) for x in b}).sort_index()
    if now.weekday() < 5 and 9 * 60 + 30 <= now.hour * 60 + now.minute < 16 * 60:
        s[now.date()] = alpaca.last_trades(["SPY"])["SPY"]
    return s


def atm_call(px: float) -> str:
    today = datetime.now(ET).strftime("%Y-%m-%d")
    cs = alpaca.contracts("SPY", "call", (pd.Timestamp(today) + pd.Timedelta(days=38)).strftime("%Y-%m-%d"),
                          (pd.Timestamp(today) + pd.Timedelta(days=52)).strftime("%Y-%m-%d"), px * 0.98, px * 1.02)
    if not cs:
        return "no ~45-day contract found"
    e = min({c["expiration_date"] for c in cs}, key=lambda z: abs((pd.Timestamp(z) - pd.Timestamp(today)).days - 45))
    k = min((c for c in cs if c["expiration_date"] == e), key=lambda c: abs(float(c["strike_price"]) - px))
    q = alpaca.option_quotes([k["symbol"]]).get(k["symbol"])
    return f"{k['symbol']} (exp {e}, strike {float(k['strike_price']):g})" + (f" bid/ask {q[0]:.2f}/{q[1]:.2f} (~${100 * q[1]:,.0f} per contract)" if q else "")


def run(record: bool = False) -> None:
    st = json.loads(STATE.read_text()) if STATE.exists() else {"open": None}
    s = closes()
    d = decide(s, st["open"] is not None)
    when = datetime.now(ET).strftime("%Y-%m-%d %H:%M")
    print(f"SPY RSI(2) {when} ET | price {d['close']:.2f} | 200-day {d['sma200']:.2f} ({'above' if d['close'] > d['sma200'] else 'BELOW'}) | "
          f"RSI(2) {d['rsi2']:.1f} | 5-day {d['sma5']:.2f}")
    if d["action"] == "BUY":
        print(f"  -> BUY at the close: SPY shares, or the options version: {atm_call(d['close'])}")
        st["open"] = {"date": when[:10], "px": d["close"]}
    elif d["action"] == "SELL":
        o = st["open"]
        ret = d["close"] / o["px"] - 1
        print(f"  -> SELL at the close (above the 5-day average). Paper trade from {o['date']} at {o['px']:.2f}: {100 * ret:+.2f}% on SPY")
        new = not LOG.exists()
        with open(LOG, "a") if record else open(os.devnull, "w") as f:
            f.write(("entry_date,entry_px,exit_date,exit_px,ret\n" if new else "") + f"{o['date']},{o['px']},{when[:10]},{d['close']},{ret:.5f}\n")
        st["open"] = None
    elif d["action"] == "HOLD":
        print(f"  -> HOLD (bought {st['open']['date']} at {st['open']['px']:.2f}); sell on the first close above {d['sma5']:.2f}")
    else:
        why = "SPY is below its 200-day average" if d["close"] <= d["sma200"] else f"RSI(2) {d['rsi2']:.1f} is not below 10"
        print(f"  -> no trade ({why})")
    if record:
        STATE.write_text(json.dumps(st))
    elif d["action"] in ("BUY", "SELL"):
        print("  (preview only: run with --record near the close to update the paper position)")
