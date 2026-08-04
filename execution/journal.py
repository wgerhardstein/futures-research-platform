"""Append-only CSV trade journal and decision log."""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Any

FIELDS = [
    "utc_iso", "et_iso", "mode", "event",       # e.g. signal, order, fill, exit, switch, kill
    "active_strategy", "side", "contracts",
    "symbol", "price", "quantity",
    "pnl_net", "cum_pnl", "account_equity",
    "reason", "order_id", "note",
]


class TradeJournal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="") as f:
                csv.writer(f).writerow(FIELDS)

    def log(self, **kwargs: Any) -> None:
        row = {k: "" for k in FIELDS}
        now = dt.datetime.now(dt.timezone.utc)
        row["utc_iso"] = now.isoformat(timespec="seconds")
        et_now = now.astimezone(dt.timezone(dt.timedelta(hours=-4)))  # naive ET; adapter will refine
        row["et_iso"] = et_now.isoformat(timespec="seconds")
        row.update({k: v for k, v in kwargs.items() if k in FIELDS})
        with self.path.open("a", newline="") as f:
            csv.writer(f).writerow([row[k] for k in FIELDS])
