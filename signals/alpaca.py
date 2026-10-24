"""Thin Alpaca market-data client (free account is enough).

Keys come from the environment (or the project-root `.env`, via execution.env_loader):
    ALPACA_API_KEY, ALPACA_SECRET_KEY

Endpoints used, all read-only. Nothing in this package can place an order.
    stock bars / latest trades    data.alpaca.markets/v2/stocks/...       (feed=iex live, sip for history > 15 min old)
    option contracts              paper-api.alpaca.markets/v2/options/contracts
    option latest quotes          data.alpaca.markets/v1beta1/options/quotes/latest  (feed=indicative: real time, not full OPRA NBBO)
    option bars                   data.alpaca.markets/v1beta1/options/bars           (history from 2024-02)
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests

from execution import env_loader

env_loader.load_once()

DATA = "https://data.alpaca.markets"
TRADING = "https://paper-api.alpaca.markets"


def headers() -> dict[str, str]:
    k = os.environ.get("ALPACA_API_KEY") or os.environ.get("ALPACA_KEY_ID")
    s = os.environ.get("ALPACA_SECRET_KEY")
    if not k or not s:
        raise SystemExit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env (free account at alpaca.markets -> Paper trading -> API keys).")
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


def get(url: str, params: dict[str, Any], tries: int = 5) -> dict:
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers(), params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 + 3 * i)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(1 + 2 * i)
    return {}


def paged(url: str, params: dict[str, Any], key: str) -> dict | list:
    """Follow next_page_token, merging `key` (dict of lists or a list)."""
    out: dict | list | None = None
    tok = None
    while True:
        p = dict(params, **({"page_token": tok} if tok else {}))
        r = get(url, p)
        part = r.get(key) or ({} if out is None or isinstance(out, dict) else [])
        if out is None:
            out = part
        elif isinstance(out, dict):
            for s, v in part.items():
                out.setdefault(s, []).extend(v)
        else:
            out.extend(part)
        tok = r.get("next_page_token")
        if not tok:
            return out if out is not None else {}


def stock_bars(syms: list[str], tf: str, start: str, end: str, feed: str = "iex") -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for i in range(0, len(syms), 100):
        part = paged(f"{DATA}/v2/stocks/bars", {"symbols": ",".join(syms[i:i + 100]), "timeframe": tf, "start": start, "end": end,
                                                "feed": feed, "limit": 10000, "adjustment": "split"}, "bars")
        for s, v in part.items():
            out.setdefault(s, []).extend(v)
    return out


def last_trades(syms: list[str]) -> dict[str, float]:
    r = get(f"{DATA}/v2/stocks/trades/latest", {"symbols": ",".join(syms), "feed": "iex"})
    return {s: float(t["p"]) for s, t in (r.get("trades") or {}).items()}


def last_trade_time(sym: str) -> str | None:
    """ISO timestamp (UTC) of the latest IEX trade, used to tell a live session from a holiday."""
    r = get(f"{DATA}/v2/stocks/trades/latest", {"symbols": sym, "feed": "iex"})
    t = (r.get("trades") or {}).get(sym)
    return t["t"] if t else None


def contracts(underlying: str, typ: str, exp_gte: str, exp_lte: str, k_lo: float, k_hi: float, status: str = "active") -> list[dict]:
    return paged(f"{TRADING}/v2/options/contracts", {"underlying_symbols": underlying, "status": status, "type": typ,
                                                       "expiration_date_gte": exp_gte, "expiration_date_lte": exp_lte,
                                                       "strike_price_gte": round(k_lo, 2), "strike_price_lte": round(k_hi, 2),
                                                       "limit": 10000}, "option_contracts")


def option_quotes(osyms: list[str]) -> dict[str, tuple[float, float]]:
    out = {}
    for i in range(0, len(osyms), 100):
        r = get(f"{DATA}/v1beta1/options/quotes/latest", {"symbols": ",".join(osyms[i:i + 100]), "feed": "indicative"})
        for s, q in (r.get("quotes") or {}).items():
            if q.get("ap") and q.get("bp") is not None:
                out[s] = (float(q["bp"]), float(q["ap"]))
    return out


def assets() -> list[dict]:
    return get(f"{TRADING}/v2/assets", {"status": "active", "asset_class": "us_equity"}) or []
