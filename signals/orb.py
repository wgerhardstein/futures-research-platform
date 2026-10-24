"""0DTE opening-range breakout on the stock in play: live signals + paper log. Sends no orders.

Rule (research/opt1 OPT1-OPT8; stock entry from Zarattini, Barbon & Aziz 2024, "A Profitable Day Trading Strategy for the
U.S. Equity Market"):
  * skip the day if VIX closed above 20 yesterday (logged anyway, flagged SKIPDAY)
  * 9:35 ET: rank the 250-name universe by first-5-minute relative volume (RVOL5 = today's 9:30-9:35 volume / the average of the
    same 5 minutes over the last 14 sessions). Eligible: price > $5, 14-day average volume > 1M shares, ATR14 > $0.50, RVOL5 >= 1
  * side = colour of the first 5-minute candle; trigger = its high (long) or low (short)
  * contract = nearest expiry (0DTE when listed), strike nearest the price at the trigger; skip if the ask is above $2.00
  * entry = limit at the mid; counted filled only if the ask reaches the limit within 60 s, otherwise no trade
  * stop = a completed 1-minute CLOSE beyond trigger -/+ 10% of ATR14 (wicks do not count); otherwise exit 15:55
  * one trade a day: the highest-ranked of the top 3 eligible names to trigger first (PRIMARY); the others are logged too

Evidence, honestly: +8.3% per trade on 2024-26 real option prices where the rule was built, +2.3% on an untouched 2024
holdout (confidence interval includes zero), about zero at poor fills. Paper first.
    python -m signals orb           run live (start before 9:30 ET; waits for 9:35, ends 15:55)
    python -m signals orb --now     one ranking right now (any time after 9:35)
"""
from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from signals import alpaca, universe
from signals.paths import OUT

ET = ZoneInfo("America/New_York")
CAP = 2.00
WATCH = 3
STOP_ATR = 0.10
LOG = OUT / "orb_paper.csv"
FIELDS = ["date", "sym", "rank", "primary", "skipday", "vix_prev", "side", "rvol5", "trigger", "stop", "trig_time", "osym",
          "bid_in", "ask_in", "mid_in", "filled_at_mid", "fill_secs", "exit_time", "exit_why", "bid_out", "ask_out", "mid_out",
          "ret_mid", "ret_conservative", "usd_mid_1lot"]


def say(msg: str) -> None:
    print(f"{datetime.now(ET):%H:%M:%S} ORB {msg}", flush=True)


# ---------- pure rule pieces (unit-tested) ----------
def daily_stats(bars: list[dict]) -> dict | None:
    """prev close, ATR14 (simple mean of true range), 14-day average volume from daily bars that end yesterday."""
    if len(bars) < 15:
        return None
    h, l, c, v = (np.array([b[k] for b in bars], dtype=float) for k in ("h", "l", "c", "v"))
    pc = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.vstack([h - l, np.abs(h - pc), np.abs(l - pc)]), axis=0)
    return dict(prev_c=float(c[-1]), atr14=float(tr[-14:].mean()), avgvol14=float(v[-14:].mean()))


def eligible(st: dict) -> bool:
    return st["prev_c"] > 5 and st["avgvol14"] > 1e6 and st["atr14"] > 0.5


def opening_range(f5: pd.DataFrame) -> tuple[bool, float] | None:
    """(long?, trigger) from the five 1-minute bars 9:30-9:34; None on a doji."""
    o, c = float(f5.o.iloc[0]), float(f5.c.iloc[-1])
    if c == o:
        return None
    long = c > o
    return long, float(f5.h.max() if long else f5.l.min())


def stop_closed(bars: list[dict], entry_min: pd.Timestamp, stop: float, long: bool, now: pd.Timestamp) -> bool:
    """True if any COMPLETED 1-minute bar from the entry minute on closed beyond the stop."""
    for b in bars:
        ts = pd.Timestamp(b["t"]).tz_convert(ET)
        if ts < entry_min or ts + pd.Timedelta(minutes=1) > now:
            continue
        if (long and b["c"] <= stop) or (not long and b["c"] >= stop):
            return True
    return False


def nearest_contract(cs: list[dict], px: float) -> str | None:
    if not cs:
        return None
    e = min(c["expiration_date"] for c in cs)
    cs = [c for c in cs if c["expiration_date"] == e]
    return min(cs, key=lambda c: abs(float(c["strike_price"]) - px))["symbol"]


# ---------- live plumbing ----------
def vix_prev(today: str) -> float | None:
    try:
        import yfinance as yf
        v = yf.download("^VIX", period="15d", progress=False, auto_adjust=False)["Close"].squeeze().dropna()
        v.index = pd.DatetimeIndex(v.index).tz_localize(None)
        return float(v[v.index < pd.Timestamp(today)].iloc[-1])
    except Exception:
        return None


def prep(today: str) -> dict:
    """Daily stats + the IEX 9:30-9:35 volume baseline (last 14 sessions). Cached per day."""
    fn = OUT / "orb_base.json"
    if fn.exists():
        b = json.loads(fn.read_text())
        if b.get("day") == today:
            return b["base"]
    syms = universe.load()
    end = pd.Timestamp(f"{today} 00:00", tz=ET).isoformat()
    d = alpaca.stock_bars(syms, "1Day", (pd.Timestamp(today) - pd.Timedelta(days=40)).strftime("%Y-%m-%d"), end, feed="iex")
    # daily volume from IEX is a fraction of the consolidated tape; scale the 1M-share filter with SIP daily bars when available
    try:
        sip = alpaca.stock_bars(syms, "1Day", (pd.Timestamp(today) - pd.Timedelta(days=40)).strftime("%Y-%m-%d"), end, feed="sip")
        d = {s: sip.get(s, b) for s, b in d.items()}
    except Exception:
        pass
    base = {s: st for s, b in d.items() if (st := daily_stats(b))}
    sessions = sorted({b["t"][:10] for b in d.get("QQQ", [])})[-14:]
    acc: dict[str, list[float]] = {}
    for day in sessions:
        t0 = pd.Timestamp(f"{day} 09:30", tz=ET)
        got = alpaca.stock_bars(list(base), "1Min", t0.isoformat(), (t0 + pd.Timedelta(minutes=5)).isoformat(), feed="iex")
        for s, b in got.items():
            acc.setdefault(s, []).append(sum(x["v"] for x in b))
    for s in base:
        v = acc.get(s, [])
        base[s]["v5_base"] = float(np.mean(v)) if len(v) >= 8 else None
    fn.write_text(json.dumps({"day": today, "base": base}))
    say(f"prep {today}: {len(base)} names, 5-min volume baseline over {len(sessions)} sessions")
    return base


def rank(base: dict, today: str) -> pd.DataFrame:
    t0 = pd.Timestamp(f"{today} 09:30", tz=ET)
    live = alpaca.stock_bars(list(base), "1Min", t0.isoformat(), (t0 + pd.Timedelta(minutes=5)).isoformat(), feed="iex")
    rows = []
    for s, b in live.items():
        st = base.get(s)
        if s == "QQQ" or not st or not st.get("v5_base") or len(b) < 3 or not eligible(st):
            continue
        f5 = pd.DataFrame(b)
        rv5 = f5.v.sum() / st["v5_base"]
        orr = opening_range(f5)
        if rv5 < 1 or orr is None:
            continue
        rows.append(dict(sym=s, rvol5=rv5, long=orr[0], lvl=orr[1], risk=STOP_ATR * st["atr14"]))
    return pd.DataFrame(rows).sort_values("rvol5", ascending=False) if rows else pd.DataFrame()


def contract(sym: str, call: bool, px: float, today: str) -> str | None:
    cs = alpaca.contracts(sym, "call" if call else "put", today, (pd.Timestamp(today) + pd.Timedelta(days=8)).strftime("%Y-%m-%d"),
                          px * 0.9, px * 1.1)
    return nearest_contract(cs, px)


def quote(osym: str) -> tuple[float, float] | None:
    return alpaca.option_quotes([osym]).get(osym)


def write(row: dict) -> None:
    new = not LOG.exists()
    with open(LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in FIELDS})


def close(w: dict, why: str, today: str) -> None:
    bid, ask = quote(w["osym"]) or (0.0, 0.0)
    mid = (bid + ask) / 2
    rm, rc = mid / w["mid_in"] - 1, bid / w["ask_in"] - 1
    w["state"] = "done"
    say(f"EXIT {w['sym']} ({why}) {w['osym']} {bid:.2f}/{ask:.2f} | mid-to-mid {100 * rm:+.0f}% (${100 * (mid - w['mid_in']):+.0f}/contract)"
        f" | ask-in bid-out {100 * rc:+.0f}%{' | PRIMARY' if w['primary'] else ''}{'' if w['filled'] else ' | (entry would NOT have filled)'}")
    write(dict(date=today, sym=w["sym"], rank=w["rank"], primary=w["primary"], skipday=w["skip"], vix_prev=w["vix"],
               side="L" if w["long"] else "S", rvol5=round(w["rvol5"], 2), trigger=w["lvl"], stop=round(w["stop"], 2),
               trig_time=w["trig_time"], osym=w["osym"], bid_in=w["bid_in"], ask_in=w["ask_in"], mid_in=w["mid_in"],
               filled_at_mid=w["filled"], fill_secs=w["secs"], exit_time=f"{datetime.now(ET):%H:%M:%S}", exit_why=why,
               bid_out=bid, ask_out=ask, mid_out=round(mid, 3), ret_mid=round(rm, 4), ret_conservative=round(rc, 4),
               usd_mid_1lot=round(100 * (mid - w["mid_in"]), 2)))


def run(now_only: bool = False) -> None:
    now = datetime.now(ET)
    today = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5:
        say(f"{today} is a weekend: no session"); return
    base = prep(today)
    vx = vix_prev(today)
    skip = vx is not None and vx > 20
    say(f"{today}: VIX prior close {vx if vx is None else round(vx, 2)} -> {'SKIPDAY (VIX > 20): logged only, the rule does not trade' if skip else 'the rule trades today'}")
    t935 = now.replace(hour=9, minute=35, second=5, microsecond=0)
    if not now_only:
        while datetime.now(ET) < t935:
            time.sleep(1)
    r = rank(base, today)
    if r.empty:
        say("no eligible names"); return
    say("ranked: " + ", ".join(f"{x.sym} {x.rvol5:.1f}x {'L' if x.long else 'S'} @{x.lvl:.2f}" for x in r.head(8).itertuples()))
    watch = []
    for x in r.itertuples():
        if len(watch) >= WATCH:
            break
        px = alpaca.last_trades([x.sym]).get(x.sym, x.lvl)
        osym = contract(x.sym, x.long, px, today)
        q = quote(osym) if osym else None
        if not q:
            say(f"{x.sym}: no listed/quoted option, skip"); continue
        bid, ask = q
        if ask > CAP:
            say(f"{x.sym}: {osym} ask {ask:.2f} > ${CAP:.2f} cap, skip"); continue
        watch.append(dict(sym=x.sym, rank=len(watch) + 1, long=x.long, lvl=x.lvl, risk=x.risk, rvol5=x.rvol5, state="wait", skip=skip, vix=vx))
        say(f"WATCH #{len(watch)} {x.sym} {'LONG' if x.long else 'SHORT'} trigger {x.lvl:.2f}, stop = 1-min close "
            f"{'below' if x.long else 'above'} trigger {'-' if x.long else '+'}{x.risk:.2f} | now {osym} {bid:.2f}/{ask:.2f}")
    if now_only:
        return
    primary_taken = False
    end = now.replace(hour=15, minute=55, second=0, microsecond=0)
    while datetime.now(ET) < end and any(w["state"] in ("wait", "open") for w in watch):
        try:
            px = alpaca.last_trades([w["sym"] for w in watch])
        except Exception:
            time.sleep(5); continue
        for w in watch:
            p = px.get(w["sym"])
            if p is None:
                continue
            if w["state"] == "wait" and ((w["long"] and p >= w["lvl"]) or (not w["long"] and p <= w["lvl"])):
                trig = datetime.now(ET)
                osym = contract(w["sym"], w["long"], p, today)
                q = quote(osym) if osym else None
                if not q or q[1] > CAP:
                    w["state"] = "done"; say(f"{w['sym']} triggered at {p:.2f} but no quote / ask above cap, skip"); continue
                bid, ask = q
                mid = round((bid + ask) / 2, 2)
                filled, secs, t0 = ask <= mid, 0, time.time()
                while not filled and time.time() - t0 < 60:
                    time.sleep(3)
                    qq = quote(osym)
                    if qq and qq[1] <= mid:
                        filled, secs = True, int(time.time() - t0)
                prim = not primary_taken and not skip
                primary_taken = primary_taken or prim
                stop = p - w["risk"] if w["long"] else p + w["risk"]
                w.update(state="open", entry_min=pd.Timestamp(trig).floor("min"), last_chk=None, osym=osym, stop=stop, bid_in=bid, ask_in=ask,
                         mid_in=mid, filled=filled, secs=secs, primary=prim, trig_time=f"{trig:%H:%M:%S}")
                say(f"{'PRIMARY ' if prim else ''}ENTRY {w['sym']} {'LONG' if w['long'] else 'SHORT'} at {p:.2f} -> {osym} {bid:.2f}/{ask:.2f} "
                    f"mid {mid:.2f} | limit at mid {'FILLED in ' + str(secs) + 's' if filled else 'NOT filled in 60s (rule: no trade)'} | stop {stop:.2f}")
            elif w["state"] == "open":
                nowm = datetime.now(ET).replace(second=0, microsecond=0)
                if datetime.now(ET).second >= 3 and w["last_chk"] != nowm:
                    w["last_chk"] = nowm
                    try:
                        t1 = pd.Timestamp(nowm)
                        b = alpaca.stock_bars([w["sym"]], "1Min", (t1 - pd.Timedelta(minutes=2)).isoformat(), t1.isoformat(), feed="iex")
                        if stop_closed(b.get(w["sym"], []), w["entry_min"], w["stop"], w["long"], t1):
                            close(w, "stop (1-min close)", today)
                    except Exception:
                        w["last_chk"] = None
        time.sleep(5)
    for w in watch:
        if w["state"] == "open":
            close(w, "15:55", today)
    say(f"END (paper log: {LOG})")
