"""`python -m signals start`: leave it running and it does the whole day by itself, every weekday (US Eastern time):
    9:25   0DTE breakout paper session (runs until 15:55)
    15:50  SPY RSI(2) signal, recorded to the paper position
The dashboard opens in your browser at http://localhost:8321 and refreshes every minute.
Each job runs as its own process, so a network error in one cannot stop the other. Output also goes to data/signals/run.log.
Stop with Ctrl+C. Market holidays are detected and skipped by each job.
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from signals.paths import OUT

ET = ZoneInfo("America/New_York")
JOBS = [((9, 25), ["orb"]), ((15, 50), ["spy", "--record"])]


def say(msg: str) -> None:
    line = f"{datetime.now(ET):%Y-%m-%d %H:%M:%S} ET  {msg}"
    print(line, flush=True)
    with open(OUT / "run.log", "a") as f:
        f.write(line + "\n")


def next_run(now: datetime) -> tuple[datetime, list[str]]:
    for days in range(8):
        d = now + timedelta(days=days)
        if d.weekday() >= 5:
            continue
        for (h, m), args in JOBS:
            t = d.replace(hour=h, minute=m, second=0, microsecond=0)
            if t > now:
                return t, args
    raise RuntimeError("no next run")


def main() -> None:
    say("signals started. Leave this window open (and the computer awake). Ctrl+C to stop.")
    try:
        from signals import web
        web.serve(open_browser=True, block=False)
    except OSError:
        say("dashboard already running at http://localhost:8321")
    procs: list[subprocess.Popen] = []
    now = datetime.now(ET)
    if now.weekday() < 5 and (9, 25) <= (now.hour, now.minute) < (9, 35):  # started a few minutes late: still catch today's 9:35
        say("running: orb")
        procs.append(subprocess.Popen([sys.executable, "-m", "signals", "orb"]))
    try:
        while True:
            t, args = next_run(datetime.now(ET))
            say(f"next: {' '.join(args)} at {t:%a %H:%M} ET")
            while datetime.now(ET) < t:
                time.sleep(min(30.0, max(0.5, (t - datetime.now(ET)).total_seconds())))
            say(f"running: {' '.join(args)}")
            log = open(OUT / "run.log", "a")
            procs = [p for p in procs if p.poll() is None]
            procs.append(subprocess.Popen([sys.executable, "-m", "signals", *args], stdout=None if sys.stdout.isatty() else log,
                                          stderr=subprocess.STDOUT))
            time.sleep(61)
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()
        say("stopped.")
