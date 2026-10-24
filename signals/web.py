"""Local dashboard: `python -m signals dashboard` (also started by `python -m signals start`).

Serves http://localhost:8321 on this computer only. The page reads /data.json, which is rebuilt from the live paper session
(data/signals/orb_session.json), the paper log (orb_paper.csv) and today's SPY RSI(2) state. Read-only.
"""
from __future__ import annotations

import csv
import json
import re
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from signals import alpaca, orb, spy_rsi2

ET = ZoneInfo("America/New_York")
PAGE = Path(__file__).parent / "dashboard" / "index.html"
PORT = 8321
_cache: dict = {"t": 0.0, "body": b""}


def nice(osym: str) -> str:
    m = re.match(r"([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$", osym or "")
    if not m:
        return osym or ""
    return f"{m.group(1)} {int(m.group(3))}/{int(m.group(4))} {int(m.group(6)) / 1000:g} {'call' if m.group(5) == 'C' else 'put'}"


def orb_state() -> dict:
    empty = dict(date=None, vix_prev=None, trades_today=None, ranked=[], skipped=[], watch=[], events=[], ended=False)
    if not orb.SESSION_FILE.exists():
        return empty
    s = json.loads(orb.SESSION_FILE.read_text())
    live = [w["osym"] for w in s["watch"] if w["state"] == "open"]
    if live:
        try:
            q = alpaca.option_quotes(live)
            for w in s["watch"]:
                if w["osym"] in q and w["state"] == "open":
                    b, a = q[w["osym"]]
                    w.update(bid_now=b, ask_now=a, ret_now=round(100 * ((b + a) / 2 / w["mid_in"] - 1)), usd_now=round(100 * ((b + a) / 2 - w["mid_in"])))
        except Exception:
            pass
    return s


def trades() -> list[dict]:
    if not orb.LOG.exists():
        return []
    return [dict(date=r["date"], sym=r["sym"], side=r["side"], contract=nice(r["osym"]), primary=r["primary"] == "True", filled=r["filled_at_mid"] == "True",
                 mid_in=float(r["mid_in"]), mid_out=float(r["mid_out"]), ret=round(100 * float(r["ret_mid"])), ret_cons=round(100 * float(r["ret_conservative"])),
                 usd=float(r["usd_mid_1lot"]), exit_why=r["exit_why"]) for r in csv.DictReader(orb.LOG.open())]


def buy_level(prev: pd.Series) -> float | None:
    """Highest close for the current/next session that would fire BUY (RSI(2) < 10 while still above the 200-day)."""
    base = list(prev.values)
    rs = lambda x: float(spy_rsi2.rsi2(pd.Series(base + [x])).iloc[-1])  # noqa: E731
    lo, hi = float(prev.iloc[-1]) * 0.85, float(prev.iloc[-1])
    if rs(lo) >= 10:
        return None
    for _ in range(40):
        m = (lo + hi) / 2
        lo, hi = (m, hi) if rs(m) < 10 else (lo, m)
    return round(lo, 2) if lo > (sum(base[-199:]) + lo) / 200 else None


def spy() -> dict:
    s = spy_rsi2.closes()
    now = datetime.now(ET)
    live = s.index[-1] == now.date() and (9, 30) <= (now.hour, now.minute) < (16, 0)
    d = spy_rsi2.decide(s, False)
    sma = s.rolling(200).mean()
    hist = [dict(d=str(k), c=round(v, 2), sma200=None if pd.isna(m) else round(float(m), 2)) for (k, v), m in zip(s.iloc[-130:].items(), sma.iloc[-130:])]
    return dict(price=round(d["close"], 2), live=bool(live), sma200=round(d["sma200"], 2), sma5=round(d["sma5"], 2), rsi2=round(d["rsi2"], 1),
                action=d["action"], buy_below=buy_level(s.iloc[:-1] if live else s), history=hist, asof=str(s.index[-1]))


def build() -> bytes:
    if time.time() - _cache["t"] < 20:
        return _cache["body"]
    out = dict(updated=datetime.now(ET).strftime("%Y-%m-%d %H:%M"), orb=orb_state(), trades=trades())
    try:
        out["spy"] = spy()
    except Exception as e:
        out["spy"] = dict(error=type(e).__name__)
    _cache.update(t=time.time(), body=json.dumps(out).encode())
    return _cache["body"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/data.json"):
            body, ctype = build(), "application/json"
        elif self.path in ("/", "/index.html"):
            body, ctype = PAGE.read_bytes(), "text/html; charset=utf-8"
        else:
            self.send_error(404); return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a) -> None:
        pass


def serve(open_browser: bool = True, block: bool = True) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Dashboard: {url}  (this computer only)", flush=True)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    if block:
        srv.serve_forever()
    else:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
