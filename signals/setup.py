"""`python -m signals setup`: ask for the Alpaca keys once, save them to .env, then run the data check."""
from __future__ import annotations

import getpass
import os

from signals.paths import ROOT


def main() -> None:
    env = ROOT / ".env"
    text = env.read_text() if env.exists() else (ROOT / ".env.example").read_text()
    if "<key-id>" not in text and "ALPACA_API_KEY=" in text:
        print("Alpaca keys already saved in .env (delete that file to enter new ones).")
    else:
        print("\nPaste your free Alpaca keys (alpaca.markets -> sign up -> Paper Trading -> API Keys -> Generate).")
        print("They only read market data. Nothing can place orders.\n")
        key = input("  API Key ID: ").strip()
        secret = getpass.getpass("  Secret Key (hidden as you paste): ").strip()
        text = text.replace("<key-id>", key).replace("<secret>", secret)
        env.write_text(text)
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"] = key, secret
        print("  saved to .env\n")
    from signals.__main__ import check
    check()
