"""Stocks-in-play universe: the 250 most-traded US stocks by average daily dollar volume (last 60 sessions), price > $5.

Rebuilt from Alpaca daily bars once a month (cached in data/signals/universe.json). ETFs, funds and trusts are dropped by name,
because the strategy (Zarattini, Barbon & Aziz 2024) ranks operating companies.
    python -m signals universe        rebuild now
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import numpy as np

from signals import alpaca
from signals.paths import OUT

FILE = OUT / "universe.json"
FUND = re.compile(r"\b(ETF|ETN|Fund|Trust|iShares|SPDR|ProShares|Direxion|Invesco|Vanguard|Leveraged|Daily|Shares)\b", re.I)
EXTRA = ["QQQ"]


def build(n: int = 250) -> list[str]:
    cands = [a["symbol"] for a in alpaca.assets()
             if a.get("tradable") and a.get("exchange") in ("NYSE", "NASDAQ", "ARCA", "AMEX", "BATS")
             and "." not in a["symbol"] and "/" not in a["symbol"] and not FUND.search(a.get("name") or "")]
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
    bars = alpaca.stock_bars(cands, "1Day", start, end, feed="iex")
    # IEX volume is a stable fraction of consolidated volume, so it ranks liquidity correctly; SIP daily bars also work if > 15 min old.
    dv = {}
    for s, b in bars.items():
        b = b[-60:]
        if len(b) < 40 or b[-1]["c"] <= 5:
            continue
        dv[s] = float(np.mean([x["c"] * x["v"] for x in b]))
    top = sorted(dv, key=dv.get, reverse=True)[:n]
    FILE.write_text(json.dumps({"built": end, "symbols": top}))
    print(f"universe: {len(top)} names (from {len(cands)} listed), saved {FILE}")
    return top


def load() -> list[str]:
    if FILE.exists():
        u = json.loads(FILE.read_text())
        if (datetime.now() - datetime.fromisoformat(u["built"])).days < 31:
            return sorted(set(u["symbols"]) | set(EXTRA))
    return sorted(set(build()) | set(EXTRA))
