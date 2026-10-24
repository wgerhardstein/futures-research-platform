"""Command line for the daily signal tool.

    python -m signals setup            save your Alpaca keys and test them (the installer runs this)
    python -m signals start            run everything automatically every weekday (leave it open)
    python -m signals dashboard        open the dashboard only (http://localhost:8321)
    python -m signals check            verify keys + data access (run this first)
    python -m signals spy [--record]   SPY RSI(2) dip-buy signal (run ~3:50 ET; --record updates the paper position)
    python -m signals orb [--now]      0DTE breakout: live paper session 9:35-15:55 ET (--now: one ranking, any time after 9:35)
    python -m signals status           paper results so far
    python -m signals universe         rebuild the 250-stock universe (automatic monthly)

Nothing here places orders. Not financial advice.
"""
from __future__ import annotations

import sys


def check() -> None:
    from signals import alpaca, universe
    print("keys:", "ok" if alpaca.headers() else "missing")
    px = alpaca.last_trades(["SPY", "AAPL"])
    print("live stock prices (IEX):", px)
    from datetime import date, timedelta
    cs = alpaca.contracts("SPY", "call", date.today().isoformat(), (date.today() + timedelta(days=7)).isoformat(),
                          px["SPY"] * 0.99, px["SPY"] * 1.01)
    print(f"option contracts: {len(cs)} SPY calls this week")
    if cs:
        print("option quote:", cs[0]["symbol"], alpaca.option_quotes([cs[0]["symbol"]]).get(cs[0]["symbol"]))
    print(f"universe: {len(universe.load())} names")
    print("all good.")


def main() -> None:
    cmd, flags = (sys.argv[1] if len(sys.argv) > 1 else "help"), set(sys.argv[2:])
    if cmd == "setup":
        from signals import setup
        setup.main()
    elif cmd == "start":
        from signals import scheduler
        scheduler.main()
    elif cmd == "dashboard":
        from signals import web
        web.serve()
    elif cmd == "check":
        check()
    elif cmd == "spy":
        from signals import spy_rsi2
        spy_rsi2.run(record="--record" in flags)
    elif cmd == "orb":
        from signals import orb
        orb.run(now_only="--now" in flags)
    elif cmd == "status":
        from signals import status
        status.run()
    elif cmd == "universe":
        from signals import universe
        universe.build()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
