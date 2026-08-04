"""Per-account risk state machine and kill switches.

Hard halt rules, enforced from the firm's published account terms:
  1. Daily loss limit: today's P&L below the limit -> halt for the day
  2. Trailing drawdown: cumulative P&L falls the limit from its peak ->
     permanent halt
  3. Profit target reached -> block new entries (exits still allowed)
  4. Manual kill flag file -> immediate flat and halt

Meta-rules layered on top, which are POLICY rather than firm requirements:
  a) Daily profit cap: stop entering once the day is far enough ahead
  b) First-loss halt: optionally stop after the first losing trade of a day
  c) Target ladder: reduce size as cumulative P&L approaches the target, so
     one bad day cannot undo most of the progress

All three meta-rules ship DISABLED with neutral thresholds. Their tuned
values move the outcome materially, which is exactly why they are not in a
public repository -- the mechanism is the interesting part, the numbers are
the product.

A self-imposed daily loss limit BELOW the firm's published one is a common
and sensible choice: it leaves room for the difference between what you
think you risked and what a gap actually costs. The value here is the
firm's, so nothing is implied about what a given deployment should use.

Each account has its own AccountRiskState. A trip on one does NOT affect
the others.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


KILL_FLAG_PATH = "logs/KILL.flag"


@dataclass
class AccountRiskState:
    label: str = "account"

    # ── Rule 1: Daily loss limit ──────────────────────────────────────────
    dll_limit: float = 1_000.0          # the firm's published daily loss limit.
                                        # A tighter self-imposed cap is a policy
                                        # choice; set it per deployment.
    today_realized_pnl: float = 0.0
    today_unrealized_pnl: float = 0.0
    today_date: Optional[dt.date] = None
    dll_tripped: bool = False

    # ── Rule 2: Trailing drawdown ─────────────────────────────────────────
    dd_limit: float = 2_000.0
    peak_cumulative_pnl: float = 0.0
    cum_pnl: float = 0.0
    dd_tripped: bool = False

    # ── Rule 3: Profit target guard ───────────────────────────────────────
    profit_target: float = 3_000.0
    pt_reached: bool = False

    # ── Global halt ───────────────────────────────────────────────────────
    tripped: bool = False
    trip_reason: Optional[str] = None
    trip_kind: Optional[str] = None

    # ── Consecutive losers (soft warning, not a hard halt) ────────────────
    consec_losers: int = 0
    consec_losers_warn: int = 0    # 0 = disabled; set per deployment

    # ── Eval meta-rule a): daily profit cap ───────────────────────────────
    # Block entries when today_realized_pnl ≥ daily_profit_cap_pct * remaining_target.
    daily_profit_cap_pct: float = 0.0   # 0 = disabled; set per deployment
    daily_profit_cap_enabled: bool = False
    daily_profit_capped: bool = False    # True = cap hit for today

    # ── Eval meta-rule b): first-loss halt ────────────────────────────────
    first_loss_halt_enabled: bool = False   # enable on high-vol days manually
    first_loss_halted: bool = False         # True = lost today → no more entries

    # ── Meta-rule c): target-ladder sizing ───────────────────────────────
    # Size down in tiers as cumulative P&L approaches the target, and stop
    # entirely once the target is close enough that one bad day would undo
    # the progress. The MECHANISM is here; the tier boundaries and contract
    # counts are a deployment-specific policy decision and are left neutral.
    #
    # (In the private deployment these are tuned per account. The numbers
    # matter a great deal to the outcome, which is exactly why they are not
    # in a public repository.)
    ladder_enabled: bool = False
    full_contracts: int = 1
    reduced_contracts: int = 1
    ladder_lower_threshold: float = 0.0    # fraction of profit_target; 0 = unset
    ladder_upper_threshold: float = 0.0    # fraction of profit_target; 0 = unset


def on_new_day(state: AccountRiskState, today: dt.date) -> None:
    """Reset intraday counters on a new calendar day."""
    if state.today_date != today:
        state.today_date = today
        state.today_realized_pnl = 0.0
        state.today_unrealized_pnl = 0.0
        state.dll_tripped = False
        state.daily_profit_capped = False
        state.first_loss_halted = False


def check_dll(state: AccountRiskState) -> Optional[tuple[str, str]]:
    """
    Trip if today's total P&L (realized + unrealized) < -DLL limit.
    Called after every position update / bar.
    """
    today_pnl = state.today_realized_pnl + state.today_unrealized_pnl
    if today_pnl <= -state.dll_limit:
        state.dll_tripped = True
        return (
            "dll",
            f"[{state.label}] Daily loss limit breached: "
            f"today PnL ${today_pnl:,.2f} below -${state.dll_limit:,.2f}",
        )
    return None


def check_trailing_dd(state: AccountRiskState) -> Optional[tuple[str, str]]:
    """
    Trip if cumulative PnL has dropped > DD limit from its peak.
    Cumulative PnL = sum of all closed trade PnL since eval start.
    """
    if state.cum_pnl > state.peak_cumulative_pnl:
        state.peak_cumulative_pnl = state.cum_pnl
    dd = state.peak_cumulative_pnl - state.cum_pnl
    if dd >= state.dd_limit:
        return (
            "trailing_dd",
            f"[{state.label}] Trailing drawdown ${dd:,.2f} ≥ limit ${state.dd_limit:,.2f} "
            f"(peak ${state.peak_cumulative_pnl:,.2f}, current ${state.cum_pnl:,.2f})",
        )
    return None


def check_profit_target(state: AccountRiskState) -> bool:
    """
    Returns True if profit target has been reached (block new entries, not exits).
    Does not trip the kill switch — allows existing positions to close normally.
    """
    if state.cum_pnl >= state.profit_target and not state.pt_reached:
        state.pt_reached = True
    return state.pt_reached


def check_daily_profit_cap(state: AccountRiskState) -> bool:
    """
    Meta-rule a): block entries if today's realized PnL has reached
    daily_profit_cap_pct × (profit_target − cum_pnl).

    Purpose: prevent a single outsized day from creating dangerous convexity.
    Once the day is far enough ahead relative to what remains, the marginal
    trade is risking a good day to gain a little. The threshold is a policy
    parameter and ships disabled.

    Returns True if the cap is hit (caller should skip new entries for the day).
    Does NOT trip the kill switch — exits still execute normally.
    """
    if not state.daily_profit_cap_enabled:
        return False
    remaining = state.profit_target - state.cum_pnl
    if remaining <= 0:
        return True  # already hit PT
    cap = state.daily_profit_cap_pct * remaining
    if state.today_realized_pnl >= cap:
        state.daily_profit_capped = True
    return state.daily_profit_capped


def check_first_loss_halt(state: AccountRiskState) -> bool:
    """
    Meta-rule b): block entries for the rest of the day after the first losing trade.
    Returns True if halted (caller skips entries; exits still allowed).
    Enable only on extremely volatile days (e.g. FOMC, CPI).
    """
    return state.first_loss_halt_enabled and state.first_loss_halted


def get_ladder_contracts(state: AccountRiskState) -> int:
    """Target-ladder sizing: contracts allowed for a new entry.

    The idea: as cumulative P&L approaches the profit target, the downside of
    a bad day grows relative to what is left to earn. Near the target you are
    risking a nearly-complete evaluation to gain a little, so size steps down
    and eventually to zero.

        below the lower threshold   -> full size
        between the two thresholds  -> reduced size
        above the upper threshold   -> no new entries

    Thresholds and contract counts come from the state object and are left
    neutral by default here. They are a deployment policy decision, and they
    move the outcome materially -- which is why the tuned values are not in
    this repository.

    Caller must also check check_profit_target().
    """
    if not state.ladder_enabled:
        return state.full_contracts
    if state.ladder_upper_threshold <= 0 or state.ladder_lower_threshold <= 0:
        return state.full_contracts   # thresholds unset -> ladder inert

    pt = state.profit_target
    hi = state.ladder_upper_threshold * pt
    lo = state.ladder_lower_threshold * pt

    if state.cum_pnl >= hi:
        return 0                        # too close to the target to risk it
    if state.cum_pnl >= lo:
        return state.reduced_contracts
    return state.full_contracts


def check_kill_flag(flag_path: str = KILL_FLAG_PATH) -> Optional[tuple[str, str]]:
    p = Path(flag_path)
    if p.exists():
        note = p.read_text().strip()[:200] if p.exists() else ""
        return ("manual_flag", f"Manual kill flag at {flag_path}: {note}")
    return None


def record_trade_pnl(state: AccountRiskState, realized_pnl: float) -> Optional[str]:
    """
    Record a closed trade's P&L.  Returns a warning string if consec_losers
    threshold is reached (does NOT trip the kill switch — caller decides).
    Also updates first-loss-halt and daily profit cap state.
    """
    state.cum_pnl += realized_pnl
    state.today_realized_pnl += realized_pnl

    if realized_pnl < 0:
        state.consec_losers += 1
        # Meta-rule b): first-loss halt
        if state.first_loss_halt_enabled and not state.first_loss_halted:
            state.first_loss_halted = True
    else:
        state.consec_losers = 0

    if state.consec_losers >= state.consec_losers_warn:
        return (
            f"[{state.label}] WARNING: {state.consec_losers} consecutive losing trades"
        )
    return None


def trip(state: AccountRiskState, kind: str, reason: str) -> None:
    state.tripped = True
    state.trip_kind = kind
    state.trip_reason = reason
    if kind == "dll":
        state.dll_tripped = True


def manual_reset(state: AccountRiskState, clear_flag: bool = True) -> None:
    """Clear kill-switch state. Use only after manual diagnosis."""
    state.tripped = False
    state.trip_reason = None
    state.trip_kind = None
    state.dll_tripped = False
    if clear_flag and Path(KILL_FLAG_PATH).exists():
        Path(KILL_FLAG_PATH).unlink()


def run_all_checks(state: AccountRiskState, today: dt.date) -> Optional[tuple[str, str]]:
    """
    Convenience: run all hard-halt checks.  Returns first trip reason found, or None.
    Should be called at the start of each bar dispatch and after each trade.
    """
    on_new_day(state, today)

    # Already tripped
    if state.tripped:
        return ("already_tripped", state.trip_reason or "kill switch tripped")

    # Manual flag file
    r = check_kill_flag()
    if r:
        return r

    # DLL
    r = check_dll(state)
    if r:
        return r

    # Trailing DD
    r = check_trailing_dd(state)
    if r:
        return r

    return None
