"""OKX WebSocket v5 客户端

OKX 三个端点的分工：
  /ws/v5/public   — tickers、trades、books 等公共行情
  /ws/v5/business — K线（candle*）走这里，不是 public！
  /ws/v5/private  — 订单、持仓、账户（需登录）

注意：现货(SPOT)没有 positions 频道，只有 SWAP/FUTURES/MARGIN 才有。
"""
import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any

import websockets
from loguru import logger

from gateway.models import Candle, Order, OrderSide, OrderStatus, OrderType, PosSide, Position

# ── WebSocket 端点 ─────────────────────────────────────────────────────────────
WS_PUBLIC   = "wss://ws.okx.com:8443/ws/v5/public"
WS_BUSINESS = "wss://ws.okx.com:8443/ws/v5/business"
WS_PRIVATE  = "wss://ws.okx.com:8443/ws/v5/private"

WS_PUBLIC_DEMO   = "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"
WS_BUSINESS_DEMO = "wss://wspap.okx.com:8443/ws/v5/business?brokerId=9999"
WS_PRIVATE_DEMO  = "wss://wspap.okx.com:8443/ws/v5/private?brokerId=9999"

TIMEFRAME_CHANNEL = {
    "1m": "candle1m", "3m": "candle3m", "5m": "candle5m",
    "15m": "candle15m", "30m": "candle30m",
    "1H": "candle1H", "2H": "candle2H", "4H": "candle4H",
    "6H": "candle6H", "12H": "candle12H",
    "1D": "candle1D", "1W": "candle1W",
}

# 需要 positions 频道的品种类型（SPOT 没有持仓概念）
POSITIONS_INST_TYPES = {"SWAP", "FUTURES", "MARGIN", "OPTION"}

Callback = Callable[[Any], Coroutine]


class OKXWebSocketClient:
    def __init__(self, api_key: str, secret_key: str, passphrase: str, is_demo: bool = False):
        self._api_key = api_key
        self._secret_key = secret_key
        self._passphrase = passphrase
        self._is_demo = is_demo

        # channel_key -> list of async callbacks
        self._callbacks: dict[str, list[Callback]] = {}
        # 各端点的订阅列表（重连时重订阅用）
        self._subscriptions: dict[str, list[dict]] = {
            "public": [], "business": [], "private": [],
        }

        self._tasks: list[asyncio.Task] = []
        self._running = False

    # ── 公开订阅接口 ──────────────────────────────────────────────────────────

    def subscribe_candles(self, inst_id: str, timeframe: str, callback: Callback):
        """K线走 business 端点"""
        channel = TIMEFRAME_CHANNEL.get(timeframe, f"candle{timeframe}")
        self._add_callback(channel, "instId", inst_id, "business", callback)

    def subscribe_tickers(self, inst_id: str, callback: Callback):
        self._add_callback("tickers", "instId", inst_id, "public", callback)

    def subscribe_orders(self, inst_type: str, callback: Callback):
        """订单推送，SPOT 也支持"""
        self._add_callback("orders", "instType", inst_type, "private", callback)

    def subscribe_positions(self, inst_type: str, callback: Callback):
        """持仓推送，仅 SWAP/FUTURES/MARGIN 有效"""
        if inst_type not in POSITIONS_INST_TYPES:
            logger.debug(f"Skip positions subscription for {inst_type} (SPOT has no positions channel)")
            return
        self._add_callback("positions", "instType", inst_type, "private", callback)

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    async def start(self):
        self._running = True
        scopes = ["public", "business", "private"]
        self._tasks = [
            asyncio.create_task(self._run(scope), name=f"ws-{scope}")
            for scope in scopes
        ]
        logger.info("WebSocket client started (public / business / private)")

    async def stop(self):
        self._running = False
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("WebSocket client stopped")

    # ── 内部：连接与重连 ──────────────────────────────────────────────────────

    def _add_callback(
        self, channel: str, arg_key: str, arg_val: str, scope: str, callback: Callback
    ):
        key = f"{channel}:{arg_val}"
        self._callbacks.setdefault(key, []).append(callback)
        sub = {"channel": channel, arg_key: arg_val}
        if sub not in self._subscriptions[scope]:
            self._subscriptions[scope].append(sub)

    async def _run(self, scope: str):
        """带自动重连的 WebSocket 主循环"""
        logger.info(f"WebSocket [{scope}] task started")
        url = self._ws_url(scope)
        retry_delay = 1
        logger.info(f"WebSocket [{scope}] → {url}")
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=None, open_timeout=15) as ws:
                    logger.info(f"WebSocket [{scope}] connected")
                    retry_delay = 1
                    if scope == "private":
                        await self._login(ws)
                    await self._resubscribe(ws, scope)
                    await self._listen(ws, scope)
            except asyncio.CancelledError:
                logger.info(f"WebSocket [{scope}] cancelled")
                break
            except Exception as e:
                # 捕获所有异常（含 InvalidHandshake、SSLError 等），不静默吞掉
                if not self._running:
                    break
                logger.warning(
                    f"WebSocket [{scope}] {type(e).__name__}: {e} — retry in {retry_delay}s"
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
        logger.info(f"WebSocket [{scope}] task exited")

    async def _listen(self, ws, scope: str):
        ping_task = asyncio.create_task(self._heartbeat(ws))
        try:
            async for raw in ws:
                if raw == "pong":
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(msg)
        finally:
            ping_task.cancel()

    async def _heartbeat(self, ws):
        while True:
            await asyncio.sleep(25)
            try:
                await ws.send("ping")
            except websockets.ConnectionClosed:
                break

    async def _login(self, ws):
        ts = str(int(time.time()))
        msg_str = ts + "GET" + "/users/self/verify"
        sign = base64.b64encode(
            hmac.new(self._secret_key.encode(), msg_str.encode(), hashlib.sha256).digest()
        ).decode()
        await ws.send(json.dumps({
            "op": "login",
            "args": [{"apiKey": self._api_key, "passphrase": self._passphrase,
                       "timestamp": ts, "sign": sign}],
        }))
        resp = await asyncio.wait_for(ws.recv(), timeout=10)
        data = json.loads(resp)
        if data.get("event") != "login" or data.get("code", "0") != "0":
            raise RuntimeError(f"WS login failed: {data}")
        logger.info("WebSocket [private] logged in")

    async def _resubscribe(self, ws, scope: str):
        args = self._subscriptions.get(scope, [])
        if not args:
            logger.info(f"WebSocket [{scope}] no subscriptions, skipping")
            return
        payload = {"op": "subscribe", "args": args}
        await ws.send(json.dumps(payload))
        logger.info(f"WebSocket [{scope}] subscribe sent: {[a['channel'] for a in args]}")

    # ── 消息分发 ──────────────────────────────────────────────────────────────

    async def _dispatch(self, msg: dict):
        if "event" in msg:
            evt = msg["event"]
            if evt == "error":
                logger.error(f"WS error [{msg.get('code')}]: {msg.get('msg')}")
            elif evt == "subscribe":
                logger.info(f"WS subscribe confirmed: {msg.get('arg')}")
            elif evt == "login":
                logger.info(f"WS login confirmed")
            else:
                logger.debug(f"WS event: {msg}")
            return

        arg = msg.get("arg", {})
        channel = arg.get("channel", "")
        arg_val = arg.get("instId") or arg.get("instType", "")
        key = f"{channel}:{arg_val}"

        callbacks = self._callbacks.get(key, [])
        if not callbacks:
            logger.warning(f"WS data arrived but no callback for key={key} (registered: {list(self._callbacks.keys())})")
            return

        parsed = self._parse_message(channel, msg.get("data", []))
        if parsed is None:
            return

        for cb in callbacks:
            try:
                await cb(parsed)
            except Exception as e:
                logger.error(f"Callback error [{key}]: {e}", exc_info=True)

    # ── 消息解析 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_message(channel: str, data: list) -> Any:
        if not data:
            return None

        if channel.startswith("candle"):
            results = []
            for row in data:
                # OKX candle row: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
                confirm = row[8] if len(row) > 8 else "0"
                results.append(Candle(
                    ts=datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc),
                    open=float(row[1]), high=float(row[2]),
                    low=float(row[3]), close=float(row[4]),
                    volume=float(row[5]),
                    confirmed=(confirm == "1"),
                ))
            return results

        if channel == "orders":
            results = []
            for d in data:
                results.append(Order(
                    inst_id=d["instId"],
                    side=OrderSide(d["side"]),
                    order_type=OrderType(d["ordType"]),
                    qty=float(d["sz"]),
                    price=float(d["px"]) if d.get("px") else None,
                    order_id=d["ordId"],
                    client_order_id=d.get("clOrdId", ""),
                    pos_side=PosSide(d["posSide"]) if d.get("posSide") else PosSide.NET,
                    status=_parse_order_status(d["state"]),
                    filled_qty=float(d.get("fillSz", 0)),
                    avg_fill_price=float(d["avgPx"]) if d.get("avgPx") else 0.0,
                    fee=float(d.get("fee", 0)),
                    strategy_name=d.get("clOrdId", "").split("_")[0],  # clOrdId 约定: strategy_xxx
                ))
            return results

        if channel == "positions":
            results = []
            for d in data:
                results.append(Position(
                    inst_id=d["instId"],
                    pos_side=PosSide(d["posSide"]) if d.get("posSide") else PosSide.NET,
                    size=float(d.get("pos", 0)),
                    entry_price=float(d["avgPx"]) if d.get("avgPx") else 0.0,
                    mark_price=float(d["markPx"]) if d.get("markPx") else 0.0,
                    unrealized_pnl=float(d["upl"]) if d.get("upl") else 0.0,
                    leverage=int(float(d["lever"])) if d.get("lever") else 1,
                ))
            return results

        return data


    def _ws_url(self, scope: str) -> str:
        if self._is_demo:
            return {
                "public":   WS_PUBLIC_DEMO,
                "business": WS_BUSINESS_DEMO,
                "private":  WS_PRIVATE_DEMO,
            }.get(scope, WS_PUBLIC_DEMO)
        return {
            "public":   WS_PUBLIC,
            "business": WS_BUSINESS,
            "private":  WS_PRIVATE,
        }.get(scope, WS_PUBLIC)


def _parse_order_status(state: str) -> OrderStatus:
    return {
        "live": OrderStatus.LIVE,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
        "filled": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELLED,
    }.get(state, OrderStatus.PENDING)
