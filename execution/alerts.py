"""
Telegram notifications and kill-switch commands.

Signal flow (instant-approve mode):
  1. Strategy emits OrderIntent → TelegramApprovalGate.request_approval() sends
     an informational notification and returns True immediately (no wait).
  2. Exits execute without any gate.

Kill-switch commands (handled by _poll_loop):
  /kill    — sets kill_requested; runner flattens all, stays alive
  /flatten — same as /kill
  /stop    — sets kill_requested + stop_requested; runner flattens and exits
  /status  — runner replies with positions, P&L, EMA snapshot

Credentials (from the environment):
  TELEGRAM_BOT_TOKEN   — bot API token from @BotFather
  TELEGRAM_CHAT_ID     — your personal chat ID (get via @userinfobot)

If either is missing the gate degrades gracefully: notifications print to stdout,
kill-switch flags are still functional (tests can set them directly).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import re
from typing import Callable, Optional


class TelegramKillSwitch:
    """
    Tracks Telegram /kill, /flatten, /stop, /status command state.

    Command dispatch is driven by TelegramApprovalGate._poll_loop().
    Register via gate.set_kill_switch(ks) after creating both objects.

    Flags are plain bools — tests can set them directly without any Telegram
    infrastructure.
    """

    def __init__(self) -> None:
        self.kill_requested: bool = False       # /kill or /flatten — flatten all, keep running
        self.stop_requested: bool = False       # /stop — flatten and shut down
        self.clear_kill_requested: bool = False # /clear-kill — clear parity kill state
        self.reset_halt_requested: bool = False # /reset-halt — clear a broker halt flag
        # Added after a live session where a winning position was open, an
        # opposing entry was possible, and the ONLY levers were /kill and
        # /stop -- both of which close everything. There was no way to stop
        # NEW entries while letting an open position run. /hold is that switch.
        self.hold_requested: bool = False       # /hold   — no NEW entries, positions RUN
        self.resume_requested: bool = False     # /resume — re-enable entries
        self._status_fn: Optional[Callable[[], str]] = None

    def set_status_callback(self, fn: Callable[[], str]) -> None:
        self._status_fn = fn

    def check_and_reset_kill(self) -> bool:
        """Returns True and clears the flag if /kill was pending."""
        if self.kill_requested:
            self.kill_requested = False
            return True
        return False

    def check_stop(self) -> bool:
        return self.stop_requested

    def reset_stop(self) -> None:
        self.stop_requested = False
        self.kill_requested = False


class TelegramApprovalGate:
    """
    Sends pre-trade notifications and polls for kill-switch commands.

    Instant-approve mode: request_approval() sends a notification and returns
    True immediately — no buttons, no 15s wait. The /kill and /stop Telegram
    commands provide the manual override channel.
    """

    def __init__(self, auto_approve_secs: float = 15.0):
        self._token: Optional[str] = os.environ.get("TELEGRAM_BOT_TOKEN")
        self._chat_id: Optional[str] = os.environ.get("TELEGRAM_CHAT_ID")
        self._auto_secs = auto_approve_secs   # kept for config compat, unused in instant mode
        self._bot = None
        self._kill_switch: Optional[TelegramKillSwitch] = None

        if not self._token or not self._chat_id:
            print(
                "[TelegramGate] WARNING: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. "
                "Notifications disabled; kill-switch flags still work via direct assignment.",
                file=sys.stderr,
            )

    def set_kill_switch(self, ks: TelegramKillSwitch) -> None:
        self._kill_switch = ks

    async def start(self) -> None:
        """Connect bot and start polling. Call once at startup."""
        if not self._token:
            return
        try:
            from telegram import Bot
        except ImportError:
            print("[TelegramGate] python-telegram-bot not installed. Notifications disabled.",
                  file=sys.stderr)
            self._token = None
            return

        self._bot = Bot(token=self._token)
        asyncio.get_event_loop().create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop polling (no pending futures to resolve in instant mode)."""
        pass

    async def request_approval(self, intent, accounts: list[str]) -> bool:
        """
        Instant-approve: send a signal notification and return True immediately.
        No buttons, no wait. /kill is the user's override channel.
        """
        direction = "LONG" if intent.side > 0 else "SHORT"
        accts_str  = " + ".join(accounts)
        n_accts    = len(accounts)
        entry_px   = f"{intent.limit_price:.2f}" if intent.limit_price else "MARKET"
        stop_px    = f"{intent.stop_price:.2f}"  if intent.stop_price  else "?"
        tgt_px     = f"{intent.target_price:.2f}" if intent.target_price else "?"
        ts_et      = dt.datetime.now(dt.timezone.utc).astimezone(
            dt.timezone(dt.timedelta(hours=-4))   # ET (EDT)
        ).strftime("%H:%M:%S")

        text = (
            f"⚡ <b>SIGNAL</b>  [{ts_et} ET]\n"
            f"Side: <b>{direction}</b>  |  Entry: <b>{entry_px}</b>\n"
            f"Stop: {stop_px}  |  Target: {tgt_px}\n"
            f"Accounts ({n_accts}): {accts_str}\n"
            f"✅ <b>AUTO-APPROVED</b>"
        )
        await self.notify(text)
        return True

    async def notify(self, text: str) -> None:
        """Send a one-way notification. Prints to stdout when bot is unavailable.

        DELIVERY OVER FORMATTING: a kill-switch alert once died with
        "Can't parse entities" -- a raw '<' in a P&L comparison read as a
        broken HTML tag, so the single most important message of that night
        was never delivered. If the HTML send fails,
        retry as plain text with our tags stripped. An ugly alert beats a
        silent one, always."""
        if not self._token or not self._chat_id or not self._bot:
            print(f"[NOTIFY] {text}")
            return
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception as exc:
            print(f"[TelegramGate] HTML notify failed ({exc}) — retrying plain",
                  file=sys.stderr)
            try:
                plain = re.sub(r"</?(b|i|code|pre|u|s)>", "", text)
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=plain,
                )
            except Exception as exc2:
                print(f"[TelegramGate] notify failed: {exc2}\n  message: {text}",
                      file=sys.stderr)

    # ── Command polling ────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Poll for both callback_query (legacy) and message commands (/kill etc.)."""
        if not self._bot:
            return
        offset = None
        while True:
            try:
                updates = await self._bot.get_updates(offset=offset, timeout=5)
                for upd in updates:
                    offset = upd.update_id + 1
                    if upd.callback_query is not None:
                        await self._handle_callback(upd.callback_query)
                    elif upd.message and upd.message.text:
                        await self._handle_message(upd.message.text.strip())
            except asyncio.CancelledError:
                return
            except Exception as exc:
                print(f"[TelegramGate] poll error: {exc}", file=sys.stderr)
            await asyncio.sleep(0.5)

    async def _handle_callback(self, cq) -> None:
        """Handle legacy Approve/Reject inline buttons (no-op in instant mode)."""
        try:
            await cq.answer("Auto-approve is ON — no manual approval needed.")
            await cq.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    async def _handle_message(self, text: str) -> None:
        """Dispatch /kill, /flatten, /stop, /status commands."""
        cmd = text.lower().split()[0] if text else ""
        ks = self._kill_switch

        if cmd in ("/kill", "/flatten"):
            print(f"[TelegramGate] {cmd} command received")
            if ks:
                ks.kill_requested = True
            await self.notify("⚠️ Kill command received — flattening all positions.")

        elif cmd == "/stop":
            print("[TelegramGate] /stop command received")
            if ks:
                ks.kill_requested = True
                ks.stop_requested = True
            await self.notify("🛑 Stop command received — flattening and shutting down.")

        elif cmd == "/hold":
            print("[TelegramGate] /hold command received")
            if ks:
                ks.hold_requested = True
            await self.notify("✋ <b>HOLD</b> — no NEW entries. Open positions keep "
                              "running with their brackets and EOD flatten. "
                              "Send /resume to re-enable entries.")

        elif cmd == "/resume":
            print("[TelegramGate] /resume command received")
            if ks:
                ks.resume_requested = True
            await self.notify("▶️ <b>RESUME</b> — entries re-enabled.")

        elif cmd == "/clear-kill":
            print("[TelegramGate] /clear-kill command received")
            if ks:
                ks.clear_kill_requested = True
            await self.notify("🔑 Clear-kill received — will clear parity kill state on next bar.")

        elif cmd == "/reset-halt":
            print("[TelegramGate] /reset-halt command received")
            if ks:
                ks.reset_halt_requested = True
            await self.notify("🔑 Reset-halt received — will clear the halt on next bar.")

        elif cmd == "/status":
            if ks and ks._status_fn:
                try:
                    status = ks._status_fn()
                except Exception as exc:
                    status = f"Status error: {exc}"
            else:
                status = "No status callback registered."
            await self.notify(f"📊 <b>STATUS</b>\n{status}")

        elif cmd.startswith("/"):
            # Unknown commands were once dropped silently, which
            # made a typo ("/atatus") look like a dead bot. Always answer.
            await self.notify(
                f"❓ Unrecognized command <code>{cmd}</code>\n"
                f"Available: /status /kill /flatten /stop /clear-kill /reset-halt")
