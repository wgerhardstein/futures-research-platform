"""Environment loader.

Imported at the top of every entry-point module so credentials in the
project-root `.env` are picked up automatically, with no need to `source` it
first.

`override=False` is the important detail: shell-exported variables and
command-line overrides WIN, and `.env` only fills what is not already set.
That ordering matters when one machine runs several configurations.

A note on why this exists rather than `set -a; source .env`: shell sourcing
performs expansion. A broker password containing `$` characters was silently
mangled by the shell into a different string, and the resulting failure
surfaced as an authentication error with no indication that the credential had
been altered in transit. This loader splits on the first `=` and does nothing
else -- no expansion, no quoting rules, no surprises.

Usage:
    from execution import env_loader  # noqa: F401 -- side-effectful import
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path

_LOADED = False


def load_once() -> dict:
    """Idempotent loader. Returns a dict describing what happened (for tests / diagnostics)."""
    global _LOADED
    if _LOADED:
        return {"already_loaded": True}
    _LOADED = True

    project_root = Path(__file__).resolve().parent.parent
    env_path = project_root / ".env"
    if not env_path.exists():
        warnings.warn(
            f"env_loader: .env not found at {env_path}. "
            f"Relying entirely on already-set environment variables.",
            RuntimeWarning, stacklevel=2,
        )
        return {"env_path": str(env_path), "loaded": False, "reason": "not_found"}

    try:
        from dotenv import dotenv_values
    except ImportError as e:
        warnings.warn(f"env_loader: python-dotenv not installed: {e}",
                      RuntimeWarning, stacklevel=2)
        return {"env_path": str(env_path), "loaded": False, "reason": "dotenv_missing"}

    values = dotenv_values(str(env_path))
    filled: list[str] = []
    already: list[str] = []
    for key, value in values.items():
        if value is None:
            continue
        if key in os.environ and os.environ[key] != "":
            already.append(key)
            continue
        os.environ[key] = value
        filled.append(key)
    return {
        "env_path": str(env_path),
        "loaded": True,
        "filled_from_dotenv": filled,
        "overridden_by_shell": already,
    }


# Side-effect on import: do the load.
_result = load_once()
