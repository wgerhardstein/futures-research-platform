"""Where the signal tool writes its state and logs (data/ is gitignored)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "signals"
OUT.mkdir(parents=True, exist_ok=True)
