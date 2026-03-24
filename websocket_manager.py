"""
websocket_manager.py
--------------------
Manages WebSocket connections to the Polymarket CLOB real-time data streams.

Two channels are supported:
  • Market channel  (wss://ws-subscriptions-clob.polymarket.com/ws/market)
    — Public; no authentication required.
    — Streams order-book snapshots, price-level changes, trade prices,
      best bid/ask updates, market lifecycle events.

  • User channel    (wss://ws-subscriptions-clob.polymarket.com/ws/user)
    — Authenticated via L2 API credentials.
    — Streams personal order placements / updates and trade confirmations.

Features:
  • Callback registration so strategies can react to real-time price data.
  • PING/PONG heartbeat every 10 seconds.
  • Exponential back-off auto-reconnect on disconnect.
  • Graceful shutdown via stop().
"""

import asyncio
import json
import logging
import time
from typing import Callable, Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from config import Config
from client_manager import get_api_credentials

logger = logging.getLogger(__name__)

# ── WebSocket endpoints ──────────────────────────────────────────────────────
MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

HEARTBEAT_INTERVAL = 10     # seconds
MAX_BACKOFF = 60            # maximum reconnect delay in seconds
INITIAL_BACKOFF = 1         # first reconnect delay in seconds


class WebSocketManager:
    """
    Manages async WebSocket connections to Polymarket CLOB streams.

    Usage:
        wsm = WebSocketManager(config)
        wsm.subscribe_market(token_ids)
        wsm.on_best_bid_ask(my_callback)

        # In an asyncio event loop:
        await wsm.start()

        # To stop:
        await wsm.stop()
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

        # Token IDs to subscribe to on the market channel
        self._market_asset_ids: List[str] = []

        # Condition IDs to subscribe to on the user channel
        self._user_market_ids: List[str] = []

        # Registered callbacks: event_type -> list of callables
        self._callbacks: Dict[str, List[Callable]] = {
            "book": [],
            "price_change": [],
            "last_trade_price": [],
            "best_bid_ask": [],
            "new_market": [],
            "market_resolved": [],
            "order": [],
            "trade": [],
        }

        # In-memory cache of latest best bid/ask per asset_id
        self._bba_cache: Dict[str, dict] = {}

        self._running = False
        self._market_task: Optional[asyncio.Task] = None
        self._user_task: Optional[asyncio.Task] = None

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def subscribe_market(self, token_ids: List[str]) -> None:
        """
        Add token IDs to the market channel subscription.

        Can be called before or after start().  If already running, the
        caller should restart or reconnect for new subscriptions to take
        effect (or use update_subscriptions).
        """
        self._market_asset_ids = list(set(self._market_asset_ids + token_ids))
        logger.debug("Market channel subscriptions updated: %d tokens", len(self._market_asset_ids))

    def subscribe_user(self, condition_ids: List[str]) -> None:
        """
        Add condition IDs to the user channel subscription.

        The user channel uses condition IDs (market IDs), NOT asset/token IDs.
        """
        self._user_market_ids = list(set(self._user_market_ids + condition_ids))
        logger.debug("User channel subscriptions updated: %d markets", len(self._user_market_ids))

    def on_best_bid_ask(self, callback: Callable[[dict], None]) -> None:
        """Register a callback fired on every best_bid_ask update."""
        self._callbacks["best_bid_ask"].append(callback)

    def on_price_change(self, callback: Callable[[dict], None]) -> None:
        """Register a callback fired on every price_change event."""
        self._callbacks["price_change"].append(callback)

    def on_book(self, callback: Callable[[dict], None]) -> None:
        """Register a callback fired on every full book snapshot."""
        self._callbacks["book"].append(callback)

    def on_trade(self, callback: Callable[[dict], None]) -> None:
        """Register a callback fired on user trade updates (user channel)."""
        self._callbacks["trade"].append(callback)

    def on_order(self, callback: Callable[[dict], None]) -> None:
        """Register a callback fired on user order updates (user channel)."""
        self._callbacks["order"].append(callback)

    def get_best_bid_ask(self, asset_id: str) -> Optional[dict]:
        """
        Return the latest cached best bid/ask for a token, or None.

        Keys: best_bid, best_ask, spread, timestamp.
        """
        return self._bba_cache.get(asset_id)

    async def start(self) -> None:
        """
        Start both WebSocket connections in parallel background tasks.

        This coroutine returns immediately; the connections run in background
        asyncio tasks.  Call await stop() to shut them down.
        """
        self._running = True

        if self._market_asset_ids:
            self._market_task = asyncio.create_task(
                self._run_with_reconnect(self._connect_market, "market"),
                name="ws-market",
            )
        else:
            logger.info("No market subscriptions; market WebSocket not started.")

        # Always connect to user channel for order/trade notifications
        self._user_task = asyncio.create_task(
            self._run_with_reconnect(self._connect_user, "user"),
            name="ws-user",
        )

        logger.info("WebSocket manager started.")

    async def stop(self) -> None:
        """Gracefully shut down both WebSocket tasks."""
        logger.info("Stopping WebSocket manager…")
        self._running = False

        for task in (self._market_task, self._user_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        logger.info("WebSocket manager stopped.")

    # ─────────────────────────────────────────────────────────────────────────
    # Reconnect logic
    # ─────────────────────────────────────────────────────────────────────────

    async def _run_with_reconnect(
        self, connect_fn: Callable, channel_name: str
    ) -> None:
        """
        Call connect_fn repeatedly with exponential back-off on failure.

        This wrapper ensures the WebSocket always reconnects after a
        disconnect or error, until stop() is called.
        """
        backoff = INITIAL_BACKOFF

        while self._running:
            try:
                await connect_fn()
                backoff = INITIAL_BACKOFF  # reset on clean exit
            except asyncio.CancelledError:
                logger.info("WebSocket task cancelled: %s", channel_name)
                return
            except Exception as exc:
                if not self._running:
                    return
                logger.warning(
                    "WebSocket %s disconnected: %s — reconnecting in %ds",
                    channel_name, exc, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

    # ─────────────────────────────────────────────────────────────────────────
    # Market channel
    # ─────────────────────────────────────────────────────────────────────────

    async def _connect_market(self) -> None:
        """
        Open and maintain the market WebSocket connection.

        On connect, sends a subscription message for all tracked token IDs.
        Then enters a receive loop, dispatching messages to callbacks and
        sending PING heartbeats every HEARTBEAT_INTERVAL seconds.
        """
        logger.info(
            "Connecting to market channel | %d tokens", len(self._market_asset_ids)
        )

        async with websockets.connect(
            MARKET_WS_URL,
            ping_interval=None,     # We handle PING manually
            ping_timeout=None,
            close_timeout=10,
        ) as ws:
            # Send subscription
            sub_msg = {
                "assets_ids": self._market_asset_ids,
                "type": "market",
                "custom_feature_enabled": True,
            }
            await ws.send(json.dumps(sub_msg))
            logger.info("Market channel: subscription sent for %d assets.", len(self._market_asset_ids))

            last_ping = time.time()

            while self._running:
                now = time.time()

                # Send PING heartbeat
                if now - last_ping >= HEARTBEAT_INTERVAL:
                    try:
                        await ws.send("PING")
                        last_ping = now
                        logger.debug("Market WS: PING sent.")
                    except ConnectionClosed:
                        raise

                # Wait for a message with a short timeout so heartbeats fire
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=HEARTBEAT_INTERVAL)
                except asyncio.TimeoutError:
                    continue
                except ConnectionClosed as exc:
                    logger.warning("Market WS connection closed: %s", exc)
                    raise

                # Handle PONG (server may respond with "PONG")
                if raw in ("PONG", "pong"):
                    logger.debug("Market WS: PONG received.")
                    continue

                self._dispatch_market_message(raw)

    def _dispatch_market_message(self, raw: str) -> None:
        """Parse a raw market channel message and call registered callbacks."""
        try:
            # Messages may be a list of events or a single event dict
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Market WS: non-JSON message: %s", raw[:120])
            return

        events = data if isinstance(data, list) else [data]

        for event in events:
            event_type = event.get("event_type")
            if not event_type:
                continue

            logger.debug("Market WS event: %s", event_type)

            # Update BBA cache
            if event_type == "best_bid_ask":
                asset_id = event.get("asset_id")
                if asset_id:
                    self._bba_cache[asset_id] = {
                        "best_bid": float(event.get("best_bid", 0)),
                        "best_ask": float(event.get("best_ask", 1)),
                        "spread": float(event.get("spread", 1)),
                        "timestamp": event.get("timestamp"),
                    }

            # Fire callbacks
            for cb in self._callbacks.get(event_type, []):
                try:
                    cb(event)
                except Exception as exc:
                    logger.error("Callback error for %s: %s", event_type, exc, exc_info=True)

    # ─────────────────────────────────────────────────────────────────────────
    # User channel
    # ─────────────────────────────────────────────────────────────────────────

    async def _connect_user(self) -> None:
        """
        Open and maintain the authenticated user WebSocket connection.

        The user channel requires L2 API credentials in the subscription
        message and delivers personal order and trade lifecycle events.
        """
        logger.info("Connecting to user channel…")

        try:
            creds = get_api_credentials()
        except RuntimeError as exc:
            logger.error("Cannot connect user channel (no credentials): %s", exc)
            # Wait before retrying so we don't spin hard
            await asyncio.sleep(MAX_BACKOFF)
            return

        async with websockets.connect(
            USER_WS_URL,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=10,
        ) as ws:
            # Authenticated subscription
            sub_msg = {
                "auth": creds,
                "markets": self._user_market_ids,
                "type": "user",
            }
            await ws.send(json.dumps(sub_msg))
            logger.info("User channel: authenticated subscription sent.")

            last_ping = time.time()

            while self._running:
                now = time.time()

                if now - last_ping >= HEARTBEAT_INTERVAL:
                    try:
                        await ws.send("PING")
                        last_ping = now
                        logger.debug("User WS: PING sent.")
                    except ConnectionClosed:
                        raise

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=HEARTBEAT_INTERVAL)
                except asyncio.TimeoutError:
                    continue
                except ConnectionClosed as exc:
                    logger.warning("User WS connection closed: %s", exc)
                    raise

                if raw in ("PONG", "pong"):
                    logger.debug("User WS: PONG received.")
                    continue

                self._dispatch_user_message(raw)

    def _dispatch_user_message(self, raw: str) -> None:
        """Parse a raw user channel message and call registered callbacks."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("User WS: non-JSON message: %s", raw[:120])
            return

        events = data if isinstance(data, list) else [data]

        for event in events:
            event_type = event.get("type") or event.get("event_type")
            if not event_type:
                continue

            logger.debug("User WS event: %s", event_type)

            for cb in self._callbacks.get(event_type, []):
                try:
                    cb(event)
                except Exception as exc:
                    logger.error(
                        "User WS callback error for %s: %s", event_type, exc, exc_info=True
                    )
