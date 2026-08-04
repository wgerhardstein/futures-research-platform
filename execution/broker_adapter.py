"""
Async broker adapter implementing the OrderAdapter protocol.

Wraps the async_rithmic library's event-driven API into the callback-based interface
used by the orchestrator.  One RithmicAdapter instance = one broker account.

async_rithmic API surface used:
  RithmicClient(user, password, system_name, app_name, app_version, url)
  client.connect()                               → connects all four plants
  client.disconnect()                            → tears down all plants
  client.get_front_month_contract(sym, exch)     → returns "MNQM5" etc.
  client.subscribe_to_time_bar_data(sym, exch, bar_type, periods)
  client.on_time_bar += callback                 → live bar events
  client.submit_order(order_id, sym, exch, qty, transaction_type, order_type, ...)
    stop_ticks / target_ticks kwargs → template_id 330 (RequestBracketOrder, atomic)
  client.cancel_order(...)
  client.cancel_all_orders(account_id)           → bulk cancel all working orders
  client.list_positions()                        → instrument PnL snapshots
  client.list_account_summary()                  → account PnL snapshots
  client.accounts                                → list of account objects
  client.get_historical_time_bars(sym, exch, bar_type, periods, start, end)
  client.on_exchange_order_notification          → fills / rejects / cancels
  client.on_bracket_update                       → bracket stop/target fill events
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys
import uuid
from pathlib import Path
from typing import Callable, Optional

import pytz

# Neutral types crossing the broker boundary.
sys.path.insert(0, str(Path(__file__).parent.parent))
from execution.types import Bar, AccountSnapshot

_ET = pytz.timezone("America/New_York")
_UTC = pytz.utc

_RITHMIC_TIMEOUT = 10.0   # seconds; prevents history-plant lock deadlocks

# async_rithmic enum imports — deferred so the module loads cleanly even if
# protobuf/async_rithmic is not importable at unit-test time.
def _rithmic_enums():
    from async_rithmic.enums import (
        OrderType, TransactionType, TimeBarType, OrderPlacement
    )
    return OrderType, TransactionType, TimeBarType, OrderPlacement


class RithmicAdapter:
    """
    One adapter = one Rithmic account connection.

    Market data (subscribe_bars) and order routing both use the same
    RithmicClient instance.  For multi-account scenarios, each account
    gets its own RithmicAdapter with its own RithmicClient.

    The single-feed optimisation (one data subscription for many accounts)
    is handled at the runner level: the runner creates ONE RithmicAdapter
    for data and N additional adapters (one per account) for order routing
    only.  This adapter supports both modes transparently.
    """

    APP_NAME = "research-platform"
    APP_VERSION = "1.0"

    def __init__(self, label: str = "rithmic"):
        self._label = label
        self._client = None
        self._symbol = None         # resolved front-month contract, e.g. "MNQM5"
        self._exchange = None
        self._account_id: Optional[str] = None
        self._fcm_id: Optional[str] = None
        self._ib_id: Optional[str] = None

        # Shadow positions for order tracking
        self._local_pos: int = 0
        self._pending_order_ids: set[str] = set()
        self._bar_callbacks: list[tuple[int, Callable]] = []  # (bar_seconds, cb)

        # Deduplication: track last bar ts to avoid firing twice on reconnect.
        # Per-symbol so a second instrument subscription
        # does not get deduped against the primary's same-timestamp bars.
        self._last_bar_ts: Optional[dt.datetime] = None          # legacy (primary)
        # dedup keyed by (symbol, bar_seconds) — see _handle_time_bar's note on
        # the bar-starvation bug that a symbol-only key caused
        self._last_bar_ts_by_symbol: dict[tuple, dt.datetime] = {}
        self._secondary_symbols: dict[str, str] = {}              # root → resolved contract

    # ── Connection ─────────────────────────────────────────────────────────

    async def connect(self, user: str, password: str, server_url: str,
                      system_name: str, symbol: str = "MNQ",
                      exchange: str = "CME", account_id: str = "") -> None:
        """Connect and authenticate.  Resolves front-month contract symbol.

        account_id: the specific sub-account to trade on (e.g. "<account-id>").
          A single Rithmic login returns ALL sub-accounts for that user. Without
          an explicit account_id this adapter would silently use accounts[0] —
          which may be the WRONG account. Always pass the intended account_id.
          Raises RuntimeError if the specified account is not in Rithmic's list.
        """
        from async_rithmic import RithmicClient
        from async_rithmic.objects import ReconnectionSettings

        if "://" not in server_url:
            server_url = f"wss://{server_url}"

        self._client = RithmicClient(
            user=user,
            password=password,
            system_name=system_name,
            app_name=self.APP_NAME,
            app_version=self.APP_VERSION,
            url=server_url,
            reconnection_settings=ReconnectionSettings(
                max_retries=3,
                backoff_type="exponential",
                interval=2,
                max_delay=30,
                jitter_range=(0.5, 2.0),
            ),
        )
        await self._client.connect()
        self._exchange = exchange
        self._fcm_id = self._client.fcm_id
        self._ib_id  = self._client.ib_id

        # Resolve the correct sub-account from the list Rithmic returns.
        # One login returns ALL sub-accounts; we must select the right one.
        accounts = self._client.accounts or []
        if not accounts:
            raise RuntimeError(f"[{self._label}] No accounts found after login")

        if account_id:
            matching = [a for a in accounts if a.account_id == account_id]
            if not matching:
                available = [a.account_id for a in accounts]
                raise RuntimeError(
                    f"[{self._label}] Configured account '{account_id}' not found "
                    f"in Rithmic accounts for this login. "
                    f"Available: {available}"
                )
            self._account_id = matching[0].account_id
        else:
            # Fallback: use first — safe only when there is exactly one account.
            self._account_id = accounts[0].account_id
            if len(accounts) > 1:
                import sys
                print(
                    f"[{self._label}] WARNING: no account_id specified and login has "
                    f"{len(accounts)} accounts. Defaulting to {self._account_id}. "
                    f"Pass account_id to connect() to be explicit.",
                    file=sys.stderr,
                )

        # Resolve front-month symbol (e.g. "MNQ" → "MNQM5")
        front = await self._client.get_front_month_contract(symbol, exchange)
        if not front:
            raise RuntimeError(
                f"[{self._label}] Could not resolve front-month for {symbol}/{exchange}"
            )
        self._symbol = front

    def _attach_shared_client(self, primary: "RithmicAdapter") -> None:
        """Reuse an existing Rithmic connection without opening a second login.

        Call this on secondary adapters when all accounts share the same Rithmic
        username (i.e. several sub-accounts under one login).
        Opening a second login with the same username triggers ForcedLogout on the
        first session.  Instead: share the live client, set account_id here, and
        route orders to the correct sub-account via the account_id field on each
        order request.

        Shared adapters do NOT own the client — they must not call client.disconnect().
        """
        self._client   = primary._client
        self._symbol   = primary._symbol
        self._exchange = primary._exchange
        self._fcm_id   = primary._fcm_id
        self._ib_id    = primary._ib_id
        self._owns_client = False

    async def attach_shared_client_for_symbol(self, primary: "RithmicAdapter",
                                              symbol: str, exchange: str) -> str:
        """Share the Rithmic session but trade a DIFFERENT instrument.

        Unlike _attach_shared_client (which copies the primary's MNQ symbol),
        this resolves THIS adapter's own front month for `symbol` so every order
        / position method — all of which use self._symbol internally — routes to
        the second instrument automatically. No order-path refactor
        needed. Returns the resolved contract."""
        self._client = primary._client
        self._fcm_id = primary._fcm_id
        self._ib_id  = primary._ib_id
        self._owns_client = False
        self._exchange = exchange
        front = await self._client.get_front_month_contract(symbol, exchange)
        if not front:
            raise RuntimeError(
                f"[{self._label}] could not resolve front month for {symbol}/{exchange}")
        self._symbol = front
        return front

    async def disconnect(self) -> None:
        if self._client is not None and getattr(self, "_owns_client", True):
            await self._client.disconnect()
        self._client = None

    # ── Market data ────────────────────────────────────────────────────────

    async def subscribe_bars(self, symbol: str, bar_seconds: int,
                              callback: Callable[[Bar], None]) -> None:
        """
        Subscribe to live 5-min (or other) time bars.

        Rithmic fires on_time_bar for all subscriptions on this client, so the
        handler filters to the requested bar_seconds.  The symbol arg is ignored
        here — we always use self._symbol (front month resolved at connect).

        Bar timestamp: async_rithmic delivers on_time_bar with a dict whose
        'marker' key holds Unix epoch seconds at bar close (field confirmed from
        TimeBar protobuf descriptor).  The entry-window filter in the strategy
        uses the bar's close time (consistent with backtest convention where resample
        labels by period START; a 5-min bar labeled 10:30 in the backtest closes at
        10:35 in Rithmic terms).  The strategy runtime accounts for this via
        ENTRY_WINDOW_RITHMIC_OFFSET_BARS = 1.
        """
        _, _, TimeBarType, _ = _rithmic_enums()

        bar_type, periods = self._bar_seconds_to_rithmic(bar_seconds)
        if bar_type is None:
            raise ValueError(
                f"[{self._label}] bar_seconds={bar_seconds} not supported. "
                f"Use 60, 300, or 900."
            )

        print(
            f"[{self._label}] subscribe_bars: sending subscription request "
            f"symbol={self._symbol} exchange={self._exchange} "
            f"bar_type={bar_type} periods={periods} (bar_seconds={bar_seconds})"
        )

        # Register the callback for dispatching
        self._bar_callbacks.append((bar_seconds, callback))

        # Attach to the live bar event (fired once per bar close by async_rithmic).
        # data is a dict from async_rithmic's _response_to_dict(), not a protobuf object.
        primary_symbol = self._symbol   # capture for the symbol filter

        async def _on_time_bar(data):
            print(
                f"[{self._label}] on_time_bar fired: "
                f"symbol={data.get('symbol')} type={data.get('type')} "
                f"period={data.get('period')} marker={data.get('marker')} "
                f"O={data.get('open_price')} H={data.get('high_price')} "
                f"L={data.get('low_price')} C={data.get('close_price')} "
                f"V={data.get('volume')}"
            )
            await self._handle_time_bar(data, bar_seconds, callback,
                                        match_symbol=primary_symbol)

        self._client.on_time_bar += _on_time_bar

        # Start the subscription — raises RithmicErrorResponse if rejected
        response = await self._client.subscribe_to_time_bar_data(
            symbol=self._symbol,
            exchange=self._exchange,
            bar_type=bar_type,
            bar_type_periods=periods,
        )
        print(
            f"[{self._label}] subscribe_bars: subscription accepted "
            f"(template_id=200 → 201 received without error)"
        )

    async def subscribe_symbol_bars(self, symbol: str, exchange: str,
                                    bar_seconds: int,
                                    callback: Callable[[Bar], None]) -> str:
        """Subscribe to a SECOND instrument's bars on this same Rithmic session
        (e.g. a cross-instrument filter). Resolves the front month, registers a
        symbol-filtered handler, and returns the resolved contract.

        Rithmic allows many instrument subscriptions on one connection; the key
        correctness requirements are (a) the handler filters to THIS symbol
        (on_time_bar fires for all subs) and (b) dedup is per-symbol — both
        handled by _handle_time_bar(..., match_symbol=front)."""
        front = await self._client.get_front_month_contract(symbol, exchange)
        if not front:
            raise RuntimeError(
                f"[{self._label}] could not resolve front month for {symbol}/{exchange}")
        self._secondary_symbols[symbol] = front
        bar_type, periods = self._bar_seconds_to_rithmic(bar_seconds)
        if bar_type is None:
            raise ValueError(f"[{self._label}] bar_seconds={bar_seconds} unsupported")
        self._bar_callbacks.append((bar_seconds, callback))

        async def _on_secondary_bar(data):
            await self._handle_time_bar(data, bar_seconds, callback,
                                        match_symbol=front)

        self._client.on_time_bar += _on_secondary_bar
        await self._client.subscribe_to_time_bar_data(
            symbol=front, exchange=exchange,
            bar_type=bar_type, bar_type_periods=periods,
        )
        print(f"[{self._label}] subscribe_symbol_bars: {symbol} → {front}@{exchange} "
              f"accepted (bar_seconds={bar_seconds})", file=sys.stderr)
        return front

    async def subscribe_ticks(self, callback) -> None:
        """Data-only: subscribe to last-trade ticks and invoke callback(tick_dict)
        per trade print. Fully isolated and best-effort — any failure here is
        logged and swallowed so it can never affect bar dispatch or order routing.
        Ticks arrive on a SEPARATE on_tick stream from bars/orders."""
        try:
            from async_rithmic.enums import DataType
        except Exception as e:
            print(f"[{self._label}] subscribe_ticks: enum import failed ({e})",
                  file=sys.stderr)
            return

        async def _on_tick(data):
            try:
                if data.get("data_type") == DataType.LAST_TRADE:
                    callback(data)
            except Exception:
                pass

        try:
            self._client.on_tick += _on_tick
            await self._client.subscribe_to_market_data(
                self._symbol, self._exchange, DataType.LAST_TRADE)
            print(f"[{self._label}] subscribe_ticks: last-trade stream subscribed")
        except Exception as e:
            print(f"[{self._label}] subscribe_ticks failed (non-fatal): {e}",
                  file=sys.stderr)

    @staticmethod
    def _bar_seconds_to_rithmic(bar_seconds: int):
        """Map bar_seconds to (TimeBarType, periods) for async_rithmic API.

        Confirmed enum values from async_rithmic 1.5.10 BarType descriptor:
          SECOND_BAR = 1, MINUTE_BAR = 2, DAILY_BAR = 3, WEEKLY_BAR = 4
        (NOT 'MINUTE' — that caused AttributeError on live connect.)
        """
        _, _, TimeBarType, _ = _rithmic_enums()
        mapping = {
            60:   (TimeBarType.MINUTE_BAR, 1),
            300:  (TimeBarType.MINUTE_BAR, 5),
            900:  (TimeBarType.MINUTE_BAR, 15),
            3600: (TimeBarType.MINUTE_BAR, 60),
        }
        return mapping.get(bar_seconds, (None, None))

    async def _handle_time_bar(self, data, bar_seconds: int,
                                callback: Callable[[Bar], None],
                                match_symbol: Optional[str] = None) -> None:
        """Convert async_rithmic time-bar dict to Bar and dispatch to user callback.

        data is a plain dict from async_rithmic's history plant _response_to_dict().
        Confirmed field names (from TimeBar protobuf descriptor):
          marker       → Unix epoch seconds at bar close  (NOT bar_end_ssboe)
          open_price, high_price, low_price, close_price  → float prices
          volume       → trade volume
          symbol, exchange, type, period                  → identifying fields

        match_symbol: when set, ONLY dispatch bars whose data['symbol'] equals it.
        Rithmic fires on_time_bar for EVERY subscription on the client, so with a
        second instrument each handler MUST filter to its own
        symbol or it would receive the other instrument's bars.
        """
        try:
            bar_symbol = data.get("symbol")
            # Symbol filter — critical once a second instrument is subscribed.
            if match_symbol is not None and bar_symbol != match_symbol:
                return

            # PERIOD filter. on_time_bar fires EVERY handler for EVERY
            # subscription, so when the same symbol is subscribed at two bar
            # sizes (one for a higher-timeframe filter, one for the strategies)
            # each handler also saw the other's bars and dispatched
            # them under its OWN bar_seconds label. Rithmic echoes the bar size
            # in 'period' as SECONDS (verified live: type=2 period=60 / 300),
            # so each handler now takes only its own size.
            bar_period = data.get("period")
            if bar_period is not None:
                try:
                    if int(bar_period) != int(bar_seconds):
                        return
                except (TypeError, ValueError):
                    pass          # unparseable -> fall through, symbol filter stands

            # 'marker' is the Unix epoch seconds for bar close.
            ts_epoch = data.get("marker")
            if not ts_epoch:
                return
            ts_utc = dt.datetime.fromtimestamp(ts_epoch, tz=_UTC)
            ts_et = ts_utc.astimezone(_ET)

            # Deduplicate PER (SYMBOL, BAR SIZE). THE BUG THIS FIXED: the key
            # was the symbol ALONE, so two subscriptions on the same symbol at
            # different bar sizes shared one dedup slot — whichever handler ran
            # first consumed the bar and the other returned as a "duplicate".
            # One handler always registered first, so it won every bar and the
            # strategies on the other subscription received ZERO bars for their
            # entire deployed life (verified: their bar_count never moved off
            # the pre-warm value). Different instruments still share a
            # bar-close timestamp, so symbol stays in the key.
            dedup_key = (bar_symbol or match_symbol or self._symbol or "_",
                         int(bar_seconds))
            if self._last_bar_ts_by_symbol.get(dedup_key) == ts_et:
                return
            self._last_bar_ts_by_symbol[dedup_key] = ts_et
            if match_symbol is None or match_symbol == self._symbol:
                self._last_bar_ts = ts_et   # keep legacy field in sync for primary

            bar = Bar(
                ts_et=ts_et,
                open=float(data.get("open_price", 0.0)),
                high=float(data.get("high_price", 0.0)),
                low=float(data.get("low_price", 0.0)),
                close=float(data.get("close_price", 0.0)),
                volume=float(data.get("volume", 0.0)),
                bar_seconds=bar_seconds,
            )

            if asyncio.iscoroutinefunction(callback):
                await callback(bar)
            else:
                callback(bar)

        except Exception as exc:
            # Bar handler must never crash the event loop
            print(f"[{self._label}] _handle_time_bar error: {exc}", file=sys.stderr)

    # ── Historical data (for indicator priming) ────────────────────────────

    async def get_historical_bars(self, bar_seconds: int, n_bars: int) -> list[Bar]:
        """
        Fetch historical 5-min bars for EMA/ATR priming.
        Returns up to n_bars most recent bars in chronological order.
        """
        _, _, TimeBarType, _ = _rithmic_enums()
        bar_type, periods = self._bar_seconds_to_rithmic(bar_seconds)
        if bar_type is None:
            return []

        # Rithmic historical API: request bars up to now
        now_utc = dt.datetime.now(_UTC)
        # Request enough days to cover n_bars (5-min: ~78 bars/day)
        bars_per_day = 86400 // bar_seconds
        days_needed = max(2, (n_bars // bars_per_day) + 2)
        start_utc = now_utc - dt.timedelta(days=days_needed)

        raw_bars = await self._rithmic_call(
            self._client.get_historical_time_bars(
                symbol=self._symbol,
                exchange=self._exchange,
                bar_type=bar_type,
                bar_type_periods=periods,
                start_time=start_utc,
                end_time=now_utc,
            )
        )

        bars = []
        for data in raw_bars:
            # Historical bars are also dicts from _response_to_dict(); 'marker' is the timestamp.
            ts_epoch = data.get("marker")
            if not ts_epoch:
                continue
            ts_et = dt.datetime.fromtimestamp(ts_epoch, tz=_UTC).astimezone(_ET)
            bars.append(Bar(
                ts_et=ts_et,
                open=float(data.get("open_price", 0.0)),
                high=float(data.get("high_price", 0.0)),
                low=float(data.get("low_price", 0.0)),
                close=float(data.get("close_price", 0.0)),
                volume=float(data.get("volume", 0.0)),
                bar_seconds=bar_seconds,
            ))

        return bars[-n_bars:]

    # ── Order management ───────────────────────────────────────────────────

    async def place_market_order(self, symbol: str, side: int,
                                  contracts: int, *, note: str = "") -> str:
        """
        Place a market order.  side: +1 = buy/long, -1 = sell/short.
        Returns order_id string.
        """
        OrderType, TransactionType, _, _ = _rithmic_enums()
        order_id = self._new_order_id()
        txn = TransactionType.BUY if side > 0 else TransactionType.SELL

        await self._rithmic_call(self._client.submit_order(
            order_id=order_id,
            symbol=self._symbol,
            exchange=self._exchange,
            qty=int(abs(contracts)),
            transaction_type=txn,
            order_type=OrderType.MARKET,
            account_id=self._account_id,
        ))
        print(
            f"[{self._label}] ORDER SUBMITTED: {order_id} "
            f"{'BUY' if side>0 else 'SELL'} {abs(contracts)} @ MKT "
            f"sym={self._symbol} acct={self._account_id}"
        )
        self._pending_order_ids.add(order_id)
        return order_id

    async def place_limit_order(self, symbol: str, side: int,
                                 contracts: int, limit_price: float,
                                 *, note: str = "") -> str:
        """
        Place a limit order.  Returns order_id.
        Price is rounded to nearest tick (0.25 for MNQ).
        """
        OrderType, TransactionType, _, _ = _rithmic_enums()
        order_id = self._new_order_id()
        txn = TransactionType.BUY if side > 0 else TransactionType.SELL
        # Round to tick
        tick = 0.25
        price = round(round(limit_price / tick) * tick, 2)

        await self._rithmic_call(self._client.submit_order(
            order_id=order_id,
            symbol=self._symbol,
            exchange=self._exchange,
            qty=int(abs(contracts)),
            transaction_type=txn,
            order_type=OrderType.LIMIT,
            price=price,
            account_id=self._account_id,
        ))
        print(
            f"[{self._label}] ORDER SUBMITTED: {order_id} "
            f"{'BUY' if side>0 else 'SELL'} {abs(contracts)} @ LMT {price:.2f} "
            f"sym={self._symbol} acct={self._account_id}"
        )
        self._pending_order_ids.add(order_id)
        return order_id

    async def place_bracket_limit_order(
        self, symbol: str, side: int, contracts: int,
        limit_price: float, stop_ticks: int, target_ticks: int | None,
        *, note: str = ""
    ) -> str:
        """Place a limit entry order with atomic stop(+target) brackets (template 330).

        stop_ticks  — ticks from fill price to stop loss (positive integer)
        target_ticks — ticks from fill price to take profit, or None/0 for a
                       STOP-ONLY bracket (async_rithmic sets bracket_type
                       STOP_ONLY_STATIC when target_ticks is omitted; needed
                       for hold-to-EOD strategies that carry a
                       stop but no profit target)

        Uses async_rithmic's template_id 330 (RequestBracketOrder) so the entry
        and both brackets are a single atomic unit at Rithmic.  If the entry
        doesn't fill, no brackets exist.  If the entry fills, brackets are
        guaranteed live instantly — no separate submission step, no race window.

        Rithmic computes: stop  = fill_price - stop_ticks*tick (for long)
                          target = fill_price + target_ticks*tick (for long)
        The entry side determines direction (BUY→long brackets, SELL→short brackets).
        """
        OrderType, TransactionType, _, _ = _rithmic_enums()
        order_id = self._new_order_id()
        txn = TransactionType.BUY if side > 0 else TransactionType.SELL
        tick = 0.25
        price = round(round(limit_price / tick) * tick, 2)

        submit_kwargs = dict(
            order_id=order_id,
            symbol=self._symbol,
            exchange=self._exchange,
            qty=int(abs(contracts)),
            transaction_type=txn,
            order_type=OrderType.LIMIT,
            price=price,
            account_id=self._account_id,
            stop_ticks=int(abs(stop_ticks)),
        )
        # Omit target_ticks entirely for stop-only brackets: async_rithmic
        # keys bracket_type off the PRESENCE of the kwarg, so passing 0
        # would still request TARGET_AND_STOP_STATIC with a 0-tick target.
        if target_ticks:
            submit_kwargs["target_ticks"] = int(abs(target_ticks))
        await self._rithmic_call(self._client.submit_order(**submit_kwargs))
        print(
            f"[{self._label}] BRACKET ORDER SUBMITTED: {order_id} "
            f"{'BUY' if side>0 else 'SELL'} {abs(contracts)} @ LMT {price:.2f} "
            f"stop={stop_ticks}tk "
            f"target={f'{target_ticks}tk' if target_ticks else 'NONE (stop-only)'} "
            f"sym={self._symbol} acct={self._account_id} (template_id=330)"
        )
        self._pending_order_ids.add(order_id)
        return order_id

    async def place_stop_order(self, symbol: str, side: int,
                                contracts: int, stop_price: float,
                                *, note: str = "") -> str:
        """
        Place a stop-market order (native exchange stop).
        side: direction of the CLOSING trade (+1 to cover short, -1 to stop long).
        """
        OrderType, TransactionType, _, _ = _rithmic_enums()
        order_id = self._new_order_id()
        txn = TransactionType.BUY if side > 0 else TransactionType.SELL
        tick = 0.25
        price = round(round(stop_price / tick) * tick, 2)

        await self._rithmic_call(self._client.submit_order(
            order_id=order_id,
            symbol=self._symbol,
            exchange=self._exchange,
            qty=int(abs(contracts)),
            transaction_type=txn,
            order_type=OrderType.STOP_MARKET,
            trigger_price=price,
            account_id=self._account_id,
        ))
        print(
            f"[{self._label}] ORDER SUBMITTED: {order_id} "
            f"{'BUY' if side>0 else 'SELL'} {abs(contracts)} @ STP {price:.2f} "
            f"sym={self._symbol} acct={self._account_id}"
        )
        self._pending_order_ids.add(order_id)
        return order_id

    async def modify_bracket_stop(self, order_id: str, new_stop_ticks: int,
                                  new_stop_price: Optional[float] = None) -> None:
        """Move the resting STOP of a FILLED bracket entry to a new level.

        REWRITTEN after the first live exercise failed with 'no bracket
        stop on basket' on every account: once the entry FILLS,
        Rithmic converts the bracket's stop leg into a WORKING STOP ORDER
        on its own child basket. list_bracket_stops() -- and therefore
        get_stop_and_target() and the template-334 bracket-level modify --
        only see PRE-fill bracket attachments, so the old path was
        structurally unable to modify a live position's stop, ever. The
        stop-tightening path only ever fires on live positions, which is why
        this never worked and the software breach-backstop carried the exit.

        New approach: find the working stop CHILD via list_orders
        (original_basket_id == entry basket -- the same linkage
        find_closing_fill_price uses -- exit-side, has a trigger price,
        and its LATEST event is a working status, not a fill/cancel),
        then modify THAT order's trigger price directly: a plain
        single-order modify, no bracket machinery. Raises on failure --
        caller alerts and the software breach-backstop remains the
        bounded fallback, exactly as before.
        """
        if new_stop_price is None:
            raise RuntimeError("modify_bracket_stop: new_stop_price required")
        entry = await self._rithmic_call(self._client.get_order(
            order_id=order_id, account_id=self._account_id))
        if entry is None:
            raise RuntimeError(f"modify_bracket_stop: entry order {order_id} not found")
        parent_basket = str(entry.basket_id)

        orders = await self._rithmic_call(
            self._client.list_orders(account_id=self._account_id), timeout=10.0)
        entry_txn = int(getattr(entry, "transaction_type", 0) or 0)
        exit_txn = 1 if entry_txn == 2 else 2
        # newest event per child basket
        latest: dict = {}
        for o in orders:
            if str(getattr(o, "original_basket_id", "") or "") != parent_basket:
                continue
            cb = str(getattr(o, "basket_id", "") or "")
            key = (int(getattr(o, "ssboe", 0) or 0), int(getattr(o, "usecs", 0) or 0))
            if cb not in latest or key > latest[cb][0]:
                latest[cb] = (key, o)
        stop_child = None
        for cb, (_, o) in latest.items():
            if int(getattr(o, "transaction_type", 0) or 0) != exit_txn:
                continue
            trig = float(getattr(o, "trigger_price", 0.0) or 0.0)
            if trig <= 0:
                continue                      # the target child is a plain limit
            nt = int(getattr(o, "notify_type", 0) or 0)
            if nt in (self.NOTIFY_FILL, self.NOTIFY_CANCEL, self.NOTIFY_REJECT):
                continue                      # dead child
            stop_child = o
            break
        if stop_child is None:
            # A live failure here was undiagnosable from the bare count —
            # dump WHY each child was rejected so the next one isn't.
            detail = "; ".join(
                f"basket={cb} txn={getattr(o, 'transaction_type', '?')}"
                f"(want {exit_txn}) trig={getattr(o, 'trigger_price', '?')}"
                f" notify={getattr(o, 'notify_type', '?')}"
                f" status={getattr(o, 'status', '?')}"
                for cb, (_, o) in latest.items()) or "none linked"
            raise RuntimeError(
                f"modify_bracket_stop: no WORKING stop child found for "
                f"parent basket {parent_basket} "
                f"({len(latest)} linked children inspected: {detail})")

        await self._rithmic_call(self._client.modify_order(
            order=stop_child, trigger_price=float(new_stop_price)))
        print(f"[{self._label}] STOP CHILD MODIFIED: child_basket="
              f"{getattr(stop_child, 'basket_id', '?')} trigger -> "
              f"{new_stop_price} (parent {parent_basket}) acct={self._account_id}")

    async def cancel_order(self, order_id: str) -> None:
        """Cancel a pending order by order_id."""
        print(f"[{self._label}] ORDER CANCEL: {order_id} acct={self._account_id}")
        try:
            await self._rithmic_call(self._client.cancel_order(
                order_id=order_id,
                account_id=self._account_id,
            ))
        except Exception as exc:
            print(f"[{self._label}] cancel_order {order_id} failed: {exc}",
                  file=sys.stderr)
        self._pending_order_ids.discard(order_id)

    async def cancel_all_orders_for_account(self) -> None:
        """Cancel ALL working orders on this account (template 346 bulk cancel).

        Preferred over cancel_order() for bracket order cancellation in live mode,
        since bracket child orders (stop/target legs) don't have user_tag set by us
        and cannot be cancelled by order_id.  This cancels the entry order and any
        attached brackets atomically from Rithmic's perspective.
        """
        print(f"[{self._label}] CANCEL ALL ORDERS acct={self._account_id}")
        try:
            await self._rithmic_call(self._client.cancel_all_orders(
                account_id=self._account_id,
            ))
        except Exception as exc:
            print(f"[{self._label}] cancel_all_orders_for_account failed: {exc}",
                  file=sys.stderr)
        self._pending_order_ids.clear()

    async def flatten(self, symbol: str, *, note: str = "") -> None:
        """Close all positions with a market order (used in dry-run / fallback only)."""
        pos = await self.position_for(symbol)
        if pos == 0:
            return
        side = -1 if pos > 0 else 1
        await self.place_market_order(symbol, side, abs(pos), note=f"flatten {note}")

    async def cancel_all_pending(self) -> None:
        """Cancel all pending orders tracked locally (e.g. stale limit order from prior bar)."""
        for oid in list(self._pending_order_ids):
            await self.cancel_order(oid)

    # ── Account state ──────────────────────────────────────────────────────

    async def account_snapshot(self) -> AccountSnapshot:
        """Return current equity / P&L snapshot.

        Field mapping confirmed from live Rithmic response (template_id=451):
          account_balance       → starting balance / current equity (string)
          open_position_pnl     → unrealized PnL (string)
          closed_position_pnl   → today's realized PnL (string)
          min_account_balance   → drawdown floor = account_balance - DD_limit
        """
        summaries = await self._rithmic_call(self._client.list_account_summary(
            account_id=self._account_id,
        ))
        if not summaries:
            return AccountSnapshot(equity=0.0, realized_pnl=0.0,
                                    unrealized_pnl=0.0, positions={})
        acct = summaries[0]

        def _f(field: str) -> float:
            raw = getattr(acct, field, None)
            if raw is None or raw == "":
                return 0.0
            try:
                return float(raw)
            except (ValueError, TypeError):
                return 0.0

        return AccountSnapshot(
            equity=_f("account_balance"),
            realized_pnl=_f("closed_position_pnl"),
            unrealized_pnl=_f("open_position_pnl"),
            positions={self._symbol: await self.position_for(self._symbol)},
        )

    async def position_for(self, symbol: str) -> int:
        """Return signed position (+ long, - short, 0 flat)."""
        positions = await self._rithmic_call(
            self._client.list_positions(account_id=self._account_id)
        )
        for pos in positions:
            sym = getattr(pos, "symbol", None) or getattr(pos, "instrument_id", None)
            if str(sym) == self._symbol:
                size = float(getattr(pos, "position", getattr(pos, "net_quantity", 0)))
                return int(size)
        return 0

    async def account_headroom(self) -> tuple:
        """(balance, floor, unrealized) for the headroom guard.

        This exists because an account once entered a trade whose full-stop
        risk exceeded its remaining drawdown cushion -- the guard had no way
        to know, so it could not refuse. floor = the broker's reported
        liquidation level.

        Raises on any missing data. The caller MUST treat failure as UNKNOWN
        and never as roomy: a guard that silently stops running is worse than
        no guard, because you still believe it is there."""
        summaries = await self._rithmic_call(self._client.list_account_summary(
            account_id=self._account_id), timeout=10.0)
        if not summaries:
            # Retry ONCE after a short pause. This catches a genuine one-off
            # race and costs nothing. It does NOT fix the failure mode where
            # an account returned empty on EVERY call for many hours
            # (thousands consecutive) until the process was restarted — that is
            # connection state, and 2,169 retries already proved retrying
            # cannot clear it. The detection for THAT lives in the runner's
            # consecutive-failure counter, which alerts instead of retrying
            # forever.
            await asyncio.sleep(0.5)
            summaries = await self._rithmic_call(
                self._client.list_account_summary(
                    account_id=self._account_id), timeout=10.0)
        if not summaries:
            raise RuntimeError("account_headroom: empty account summary")
        acct = summaries[0]

        def _f(field: str) -> Optional[float]:
            raw = getattr(acct, field, None)
            if raw is None or raw == "":
                return None
            try:
                return float(raw)
            except (ValueError, TypeError):
                return None

        bal = _f("account_balance")
        floor = _f("min_account_balance")
        if bal is None or floor is None:
            raise RuntimeError(
                f"account_headroom: missing fields (balance={bal}, floor={floor})")
        return bal, floor, _f("open_position_pnl") or 0.0

    async def find_fill_price_for_order(self, order_id: str) -> Optional[float]:
        """Fill price of a specific order by OUR user tag (order_id), from
        Rithmic order history. EOD/timed exits are
        STANDALONE market orders — not bracket children — so
        find_closing_fill_price's parent-basket child search can never see
        them (two real pending-P&L incidents). Entry orders
        set order_id as the user tag; bracket children don't, so this
        lookup is precise for our own market-outs."""
        try:
            orders = await self._rithmic_call(
                self._client.list_orders(account_id=self._account_id),
                timeout=10.0,
            )
        except Exception as exc:
            print(f"[{self._label}] find_fill_price_for_order: list_orders "
                  f"failed ({exc})", file=sys.stderr)
            return None
        best = (None, -1, -1)
        for o in orders:
            try:
                if str(getattr(o, "user_tag", "") or "") != order_id:
                    continue
                if int(getattr(o, "notify_type", 0) or 0) != self.NOTIFY_FILL:
                    continue
                fp = float(getattr(o, "fill_price", 0.0) or 0.0)
                if fp <= 0:
                    continue
                key = (int(getattr(o, "ssboe", 0) or 0),
                       int(getattr(o, "usecs", 0) or 0))
                if key > best[1:]:
                    best = (fp, key[0], key[1])
            except (TypeError, ValueError):
                continue
        return best[0]

    async def find_closing_fill_price(
        self, parent_basket_id: str, exit_txn_type: int,
    ) -> "tuple[Optional[float], Optional[str]]":
        """Query Rithmic order history for the closing fill of a bracket trade.

        Looks up orders whose original_basket_id == parent_basket_id (i.e., the
        bracket children spawned by the atomic entry order) and finds the most
        recent FILL whose transaction_type matches exit_txn_type
        (1=BUY closes a short, 2=SELL closes a long).

        Returns (fill_price, child_basket_id) — both None if no matching fill is found.

        Used by the per-bar reconciler to attach an actual Rithmic fill price
        to a position-flat detection, so EXIT alerts and journal entries
        report what really filled (not an estimated stop/target level).
        """
        try:
            orders = await self._rithmic_call(
                self._client.list_orders(account_id=self._account_id),
                timeout=10.0,
            )
        except Exception as exc:
            print(f"[{self._label}] find_closing_fill_price: list_orders failed ({exc})",
                  file=sys.stderr)
            return None, None

        best: tuple[Optional[float], Optional[str], int, int] = (None, None, -1, -1)
        for o in orders:
            try:
                if str(getattr(o, "original_basket_id", "") or "") != parent_basket_id:
                    continue
                if int(getattr(o, "notify_type", 0) or 0) != 5:  # FILL
                    continue
                if int(getattr(o, "transaction_type", 0) or 0) != exit_txn_type:
                    continue
                fill_price = float(getattr(o, "fill_price", 0.0) or 0.0)
                if fill_price <= 0:
                    continue
                child_basket = str(getattr(o, "basket_id", "") or "")
                ssboe = int(getattr(o, "ssboe", 0) or 0)
                usecs = int(getattr(o, "usecs", 0) or 0)
                if (ssboe, usecs) > (best[2], best[3]):
                    best = (fill_price, child_basket, ssboe, usecs)
            except (TypeError, ValueError):
                continue

        return best[0], best[1]

    # ── Order event callbacks ──────────────────────────────────────────────

    # ExchangeOrderNotification.NotifyType integer constants (protobuf enum).
    # Defined here to avoid importing the generated pb2 module at runtime.
    NOTIFY_STATUS   = 1
    NOTIFY_CANCEL   = 3
    NOTIFY_FILL     = 5
    NOTIFY_REJECT   = 6

    def subscribe_order_updates(self, callback: Callable) -> None:
        """Register a handler for ExchangeOrderNotification events (fills, rejects, cancels).

        The callback receives the raw ExchangeOrderNotification protobuf object.
        Key fields:
          notify_type        int  — NOTIFY_FILL=5, NOTIFY_REJECT=6, NOTIFY_CANCEL=3
          user_tag           str  — our order_id (passed as order_id to submit_order)
          basket_id          str  — Rithmic's internal order ID
          account_id         str  — sub-account identifier
          fill_price         float — fill price (FILL events)
          fill_size          int   — qty of this fill
          total_fill_size    int   — cumulative qty filled
          total_unfilled_size int  — remaining qty (0 = fully filled)
          text / report_text str  — rejection reason (REJECT events)
          is_snapshot        bool — True on reconnect replay; skip if not our order_id

        Registration subscribes globally — all orders on this session.
        OrderPlant auto-subscribes to order updates at login; no extra call needed.
        """
        async def _handler(notification):
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(notification)
                else:
                    callback(notification)
            except Exception as exc:
                print(f"[{self._label}] subscribe_order_updates handler error: {exc}",
                      file=sys.stderr)

        self._client.on_exchange_order_notification += _handler

    def subscribe_bracket_updates(self, callback: Callable) -> None:
        """Register a handler for BracketUpdates events (template_id=353).

        Fires when bracket stop/target quantities change (filled or cancelled).
        Key fields on the BracketUpdates protobuf:
          basket_id               str — parent entry order's Rithmic basket ID
          stop_ticks              int — configured stop tick offset
          stop_quantity           int — original stop quantity
          stop_quantity_released  int — qty filled/cancelled (non-zero = stop triggered)
          target_ticks            int — configured target tick offset
          target_quantity         int — original target quantity
          target_quantity_released int — qty filled/cancelled (non-zero = target triggered)

        Used in live mode only to detect bracket stop/target fills and drive
        position exit without bar-inferred price estimation.
        """
        async def _handler(notification):
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(notification)
                else:
                    callback(notification)
            except Exception as exc:
                print(f"[{self._label}] subscribe_bracket_updates handler error: {exc}",
                      file=sys.stderr)

        self._client.on_bracket_update += _handler

    # ── Dry-run shim ───────────────────────────────────────────────────────

    def as_dry_run(self) -> "DryRunRithmicAdapter":
        """Wrap this adapter in a DryRunRithmicAdapter that blocks live orders."""
        return DryRunRithmicAdapter(self)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _call_lock(self) -> asyncio.Lock:
        """One lock PER RITHMIC CLIENT, shared by every adapter on it.

        ROOT CAUSE OF A REAL INCIDENT. Many adapters share a single
        Rithmic connection and every one of them issued requests
        concurrently — nothing serialised them. Under that contention the
        library returns EMPTY responses and times out, and an empty
        `list_positions` is indistinguishable from "flat" (position_for ends
        `return 0`). That is how the runner concluded a live 4-lot was
        closed, booked a fabricated exit at the stop, and abandoned it.
        The same contention produced `empty account summary` on the
        headroom guard (which then FAILED OPEN) five hours earlier.

        The lock lives on the CLIENT object, so adapters sharing a session
        share the lock automatically and adapters with their own session
        get their own. Requests queue instead of colliding.
        """
        lk = getattr(self._client, "_adapter_call_lock", None)
        if lk is None:
            lk = asyncio.Lock()
            try:
                self._client._adapter_call_lock = lk
            except Exception:
                pass          # exotic client object — degrade to per-call lock
        return lk

    async def _rithmic_call(self, coro, *, timeout: float = _RITHMIC_TIMEOUT):
        """Run a Rithmic client coroutine with a hard timeout, SERIALISED
        against every other call on the same connection (see _call_lock).

        Raises asyncio.TimeoutError (not a deadlock) if the call doesn't complete
        in time.  This guards against the history-plant lock not releasing cleanly.

        The timeout covers waiting for the lock as well as the call itself, so a
        stuck request can never make callers queue behind it indefinitely — they
        get their usual TimeoutError and their usual fail-safe path.
        """
        async def _locked():
            if self._client is None:
                return await coro
            async with self._call_lock():
                return await coro
        try:
            return await asyncio.wait_for(_locked(), timeout=timeout)
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(
                f"[{self._label}] Rithmic call timed out after {timeout}s"
            )

    @staticmethod
    def _new_order_id() -> str:
        return f"ORD-{uuid.uuid4().hex[:12].upper()}"

    @property
    def symbol(self) -> Optional[str]:
        return self._symbol

    @property
    def account_id(self) -> Optional[str]:
        return self._account_id


class DryRunRithmicAdapter:
    """
    Wraps a RithmicAdapter.  Market data and account queries pass through.
    All order placement methods are intercepted and logged but NEVER sent.

    Shadow positions are maintained so kill-switch and P&L tracking remain
    accurate during dry-run simulation.
    """

    def __init__(self, inner: RithmicAdapter):
        self._inner = inner
        self._shadow_pos: int = 0
        self._shadow_entry_px: Optional[float] = None
        self._order_ctr = 0
        self._filled_orders: list[dict] = []
        self._label = f"dry:{getattr(inner, '_label', getattr(inner, 'label', 'unknown'))}"

    async def connect(self, *args, **kwargs) -> None:
        await self._inner.connect(*args, **kwargs)

    async def disconnect(self) -> None:
        await self._inner.disconnect()

    async def subscribe_bars(self, symbol: str, bar_seconds: int,
                              callback: Callable[[Bar], None]) -> None:
        await self._inner.subscribe_bars(symbol, bar_seconds, callback)

    async def subscribe_ticks(self, callback) -> None:
        await self._inner.subscribe_ticks(callback)

    async def get_historical_bars(self, bar_seconds: int, n_bars: int) -> list[Bar]:
        return await self._inner.get_historical_bars(bar_seconds, n_bars)

    async def place_market_order(self, symbol: str, side: int,
                                  contracts: int, *, note: str = "") -> str:
        self._order_ctr += 1
        oid = f"DRY-MKT-{self._order_ctr:04d}"
        self._shadow_pos += side * contracts
        rec = dict(order_id=oid, kind="market", symbol=symbol, side=side,
                   contracts=contracts, note=note,
                   ts=dt.datetime.now(_ET).isoformat())
        self._filled_orders.append(rec)
        print(f"[{self._label}] DRY-RUN {oid}: {'BUY' if side>0 else 'SELL'} "
              f"{contracts} {symbol} @ MARKET  {note}")
        return oid

    async def place_limit_order(self, symbol: str, side: int, contracts: int,
                                 limit_price: float, *, note: str = "") -> str:
        self._order_ctr += 1
        oid = f"DRY-LMT-{self._order_ctr:04d}"
        # In dry-run, assume limit fills at requested price on next bar
        self._shadow_pos += side * contracts
        rec = dict(order_id=oid, kind="limit", symbol=symbol, side=side,
                   contracts=contracts, price=limit_price, note=note,
                   ts=dt.datetime.now(_ET).isoformat())
        self._filled_orders.append(rec)
        print(f"[{self._label}] DRY-RUN {oid}: {'BUY' if side>0 else 'SELL'} "
              f"{contracts} {symbol} @ LMT {limit_price}  {note}")
        return oid

    async def place_bracket_limit_order(
        self, symbol: str, side: int, contracts: int,
        limit_price: float, stop_ticks: int, target_ticks: int,
        *, note: str = ""
    ) -> str:
        """Dry-run: records as a bracket_limit order (no live submission)."""
        self._order_ctr += 1
        oid = f"DRY-BKRD-{self._order_ctr:04d}"
        self._shadow_pos += side * contracts
        rec = dict(order_id=oid, kind="bracket_limit", symbol=symbol, side=side,
                   contracts=contracts, price=limit_price,
                   stop_ticks=stop_ticks, target_ticks=target_ticks, note=note,
                   ts=dt.datetime.now(_ET).isoformat())
        self._filled_orders.append(rec)
        print(f"[{self._label}] DRY-RUN {oid}: {'BUY' if side>0 else 'SELL'} "
              f"{contracts} {symbol} @ BKRD {limit_price} "
              f"stop={stop_ticks}tk tgt={target_ticks}tk  {note}")
        return oid

    async def place_stop_order(self, symbol: str, side: int, contracts: int,
                                stop_price: float, *, note: str = "") -> str:
        self._order_ctr += 1
        oid = f"DRY-STP-{self._order_ctr:04d}"
        print(f"[{self._label}] DRY-RUN {oid}: {'BUY' if side>0 else 'SELL'} "
              f"{contracts} {symbol} @ STP {stop_price}  {note}")
        return oid

    async def modify_bracket_stop(self, order_id: str, new_stop_ticks: int,
                                  new_stop_price: Optional[float] = None) -> None:
        print(f"[{self._label}] DRY-RUN modify bracket stop {order_id} "
              f"-> {new_stop_price} ({int(new_stop_ticks)}tk)")

    async def cancel_order(self, order_id: str) -> None:
        print(f"[{self._label}] DRY-RUN cancel {order_id}")

    async def cancel_all_pending(self) -> None:
        pass

    async def cancel_all_orders_for_account(self) -> None:
        pass

    def subscribe_order_updates(self, callback) -> None:
        pass  # dry-run: bar-inferred detection drives fills; no live callbacks

    def subscribe_bracket_updates(self, callback) -> None:
        pass  # dry-run: bar-inferred exit logic handles bracket simulation

    async def flatten(self, symbol: str, *, note: str = "") -> None:
        if self._shadow_pos == 0:
            return
        side = -1 if self._shadow_pos > 0 else 1
        await self.place_market_order(symbol, side, abs(self._shadow_pos),
                                       note=f"flatten ({note})")
        self._shadow_pos = 0

    async def account_snapshot(self) -> AccountSnapshot:
        snap = await self._inner.account_snapshot()
        return AccountSnapshot(
            equity=snap.equity,
            realized_pnl=snap.realized_pnl,
            unrealized_pnl=snap.unrealized_pnl,
            positions={self._inner._symbol: self._shadow_pos},
        )

    async def position_for(self, symbol: str) -> int:
        return self._shadow_pos

    async def account_headroom(self) -> tuple:
        """Dry-run: no real floor — report unbounded headroom so the
        entry guard never bites in simulation."""
        return 1e12, 0.0, 0.0

    async def find_fill_price_for_order(self, order_id: str) -> Optional[float]:
        """Account queries pass through to the real adapter (fills placed in
        dry-run never reach the broker, so this only ever finds real orders)."""
        return await self._inner.find_fill_price_for_order(order_id)

    async def find_closing_fill_price(
        self, parent_basket_id: str, exit_txn_type: int,
    ) -> "tuple[Optional[float], Optional[str]]":
        """Dry-run stub — bar-inferred exit handler supplies prices directly."""
        return None, None

    @property
    def symbol(self) -> Optional[str]:
        return self._inner.symbol

    @property
    def account_id(self) -> Optional[str]:
        return self._inner.account_id

    @property
    def filled_orders(self) -> list[dict]:
        return list(self._filled_orders)
