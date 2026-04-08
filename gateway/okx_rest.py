"""OKX REST API v5 客户端"""
import base64
import hashlib
import hmac
import json
import math
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import aiohttp
from loguru import logger

from gateway.models import (
    Balance, Candle, InstType, InstrumentInfo, Order, OrderSide, OrderStatus,
    OrderType, PosSide, Position, Ticker,
)


# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────
REST_BASE = "https://www.okx.com"

TIMEFRAME_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1H": "1H", "2H": "2H", "4H": "4H", "6H": "6H", "12H": "12H",
    "1D": "1D", "1W": "1W",
}


# ──────────────────────────────────────────────────────────────────────────────
# 签名工具
# ──────────────────────────────────────────────────────────────────────────────
def _sign(secret_key: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    message = timestamp + method.upper() + path + body
    mac = hmac.new(secret_key.encode(), message.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ──────────────────────────────────────────────────────────────────────────────
# REST Client
# ──────────────────────────────────────────────────────────────────────────────
class OKXRestClient:
    def __init__(self, api_key: str, secret_key: str, passphrase: str, is_demo: bool = True):
        self._api_key = api_key
        self._secret_key = secret_key
        self._passphrase = passphrase
        self._is_demo = is_demo
        self._session: aiohttp.ClientSession | None = None
        self._inst_cache: dict[str, InstrumentInfo] = {}

    async def __aenter__(self):
        headers = {"Content-Type": "application/json"}
        if self._is_demo:
            headers["x-simulated-trading"] = "1"
        self._session = aiohttp.ClientSession(base_url=REST_BASE, headers=headers)
        return self

    async def __aexit__(self, *_):
        if self._session:
            await self._session.close()

    # ── 内部请求 ──────────────────────────────────────────────────────────────

    def _auth_headers(self, method: str, path: str, body: str = "") -> dict:
        ts = _timestamp()
        return {
            "OK-ACCESS-KEY": self._api_key,
            "OK-ACCESS-SIGN": _sign(self._secret_key, ts, method, path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self._passphrase,
        }

    async def _get(self, path: str, params: dict | None = None, auth: bool = True) -> dict:
        query = "?" + urlencode(params) if params else ""
        full_path = path + query
        headers = self._auth_headers("GET", full_path) if auth else {}
        async with self._session.get(full_path, headers=headers) as resp:
            data = await resp.json()
        self._check(data, path)
        return data

    async def _post(self, path: str, body: dict) -> dict:
        body_str = json.dumps(body)
        headers = self._auth_headers("POST", path, body_str)
        async with self._session.post(path, data=body_str, headers=headers) as resp:
            data = await resp.json()
        self._check(data, path)
        return data

    @staticmethod
    def _check(data: dict, path: str):
        code = data.get("code", "0")
        if code != "0":
            msg = data.get("msg", "unknown error")
            raise RuntimeError(f"OKX API error [{code}] {path}: {msg}")

    # ── 行情 ─────────────────────────────────────────────────────────────────

    async def get_ticker(self, inst_id: str) -> Ticker:
        data = await self._get("/api/v5/market/ticker", {"instId": inst_id}, auth=False)
        d = data["data"][0]
        return Ticker(
            inst_id=inst_id,
            last=float(d["last"]),
            bid=float(d["bidPx"]) if d["bidPx"] else float(d["last"]),
            ask=float(d["askPx"]) if d["askPx"] else float(d["last"]),
            ts=datetime.fromtimestamp(int(d["ts"]) / 1000, tz=timezone.utc),
        )

    async def get_candles(
        self, inst_id: str, timeframe: str = "15m", limit: int = 100
    ) -> list[Candle]:
        """返回按时间升序的已收盘K线列表"""
        bar = TIMEFRAME_MAP.get(timeframe, timeframe)
        data = await self._get(
            "/api/v5/market/candles",
            {"instId": inst_id, "bar": bar, "limit": min(limit, 300)},
            auth=False,
        )
        candles = []
        for row in reversed(data["data"]):  # API返回降序，反转为升序
            ts, o, h, l, c, vol, _, _, confirm = row
            candles.append(Candle(
                ts=datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc),
                open=float(o), high=float(h), low=float(l),
                close=float(c), volume=float(vol),
                confirmed=(confirm == "1"),
            ))
        return candles

    # ── 账户 ─────────────────────────────────────────────────────────────────

    async def get_balance(self, currency: str = "USDT") -> Balance:
        data = await self._get("/api/v5/account/balance", {"ccy": currency})
        details = data["data"][0]["details"]
        for d in details:
            if d["ccy"] == currency:
                return Balance(
                    currency=currency,
                    total=float(d["eq"]),
                    available=float(d["availEq"] or d["availBal"]),
                    frozen=float(d["frozenBal"]),
                )
        return Balance(currency=currency, total=0.0, available=0.0)

    async def get_positions(self, inst_id: str | None = None) -> list[Position]:
        params = {}
        if inst_id:
            params["instId"] = inst_id
        data = await self._get("/api/v5/account/positions", params)
        positions = []
        for d in data["data"]:
            if float(d.get("pos", 0)) == 0:
                continue
            positions.append(Position(
                inst_id=d["instId"],
                pos_side=PosSide(d["posSide"]),
                size=float(d["pos"]),
                entry_price=float(d["avgPx"]) if d["avgPx"] else 0.0,
                mark_price=float(d["markPx"]) if d["markPx"] else 0.0,
                unrealized_pnl=float(d["upl"]) if d["upl"] else 0.0,
                leverage=int(float(d["lever"])) if d["lever"] else 1,
            ))
        return positions

    # ── 合约设置 ──────────────────────────────────────────────────────────────

    async def set_leverage(self, inst_id: str, lever: int, td_mode: str = "cross") -> None:
        await self._post("/api/v5/account/set-leverage", {
            "instId": inst_id,
            "lever": str(lever),
            "mgnMode": td_mode,
        })
        logger.info(f"Set leverage {inst_id} x{lever} ({td_mode})")

    # ── 品种信息 ──────────────────────────────────────────────────────────────

    async def get_instrument(self, inst_id: str, inst_type: InstType) -> InstrumentInfo:
        if inst_id in self._inst_cache:
            return self._inst_cache[inst_id]
        data = await self._get(
            "/api/v5/public/instruments",
            {"instType": inst_type.value, "instId": inst_id},
            auth=False,
        )
        d = data["data"][0]
        info = InstrumentInfo(
            inst_id=inst_id,
            inst_type=inst_type,
            base_ccy=d["baseCcy"],
            quote_ccy=d["quoteCcy"],
            lot_sz=float(d["lotSz"]),
            min_sz=float(d["minSz"]),
            ct_val=float(d.get("ctVal", 1) or 1),
            tick_sz=float(d["tickSz"]),
        )
        self._inst_cache[inst_id] = info
        return info

    # ── 下单 ─────────────────────────────────────────────────────────────────

    async def place_order(self, order: Order, inst_type: InstType) -> Order:
        """下单，返回带 order_id 的 Order 对象"""
        is_swap = inst_type == InstType.SWAP
        path = "/api/v5/trade/order"
        body: dict[str, Any] = {
            "instId": order.inst_id,
            "tdMode": "cross" if is_swap else "cash",
            "side": order.side.value,
            "ordType": order.order_type.value,
            "sz": str(self._round_qty(order.qty, order.inst_id)),
        }
        if is_swap:
            body["posSide"] = order.pos_side.value
        # 现货市价买单：sz 默认被 OKX 解读为 USDT 金额，需指定 tgtCcy=base_ccy 表示 sz 是基础货币数量
        if not is_swap and order.order_type == OrderType.MARKET and order.side == OrderSide.BUY:
            body["tgtCcy"] = "base_ccy"
        if order.order_type == OrderType.LIMIT and order.price:
            body["px"] = str(order.price)
        if order.client_order_id:
            body["clOrdId"] = order.client_order_id

        logger.info(f"Placing order: {body}")

        # 不走通用 _post/_check，直接解析以获取内层真实错误码
        body_str = json.dumps(body)
        headers = self._auth_headers("POST", path, body_str)
        async with self._session.post(path, data=body_str, headers=headers) as resp:
            data = await resp.json()

        result = (data.get("data") or [{}])[0]
        outer_code = data.get("code", "0")
        s_code = result.get("sCode", "0")
        s_msg = result.get("sMsg", data.get("msg", ""))

        if outer_code != "0" or s_code != "0":
            logger.error(f"Order rejected — outerCode={outer_code} sCode={s_code} sMsg={s_msg} | body={body}")
            raise RuntimeError(f"Order failed [sCode={s_code}]: {s_msg}")

        order.order_id = result["ordId"]
        order.status = OrderStatus.LIVE
        logger.info(f"Order placed: {order.order_id} {order.inst_id} {order.side.value} {order.qty}")
        return order

    async def cancel_order(self, inst_id: str, order_id: str) -> bool:
        try:
            await self._post("/api/v5/trade/cancel-order", {
                "instId": inst_id, "ordId": order_id,
            })
            return True
        except RuntimeError as e:
            logger.warning(f"Cancel order {order_id} failed: {e}")
            return False

    async def get_order(self, inst_id: str, order_id: str) -> Order | None:
        try:
            data = await self._get("/api/v5/trade/order", {"instId": inst_id, "ordId": order_id})
        except RuntimeError:
            return None
        d = data["data"][0]
        return Order(
            inst_id=d["instId"],
            side=OrderSide(d["side"]),
            order_type=OrderType(d["ordType"]),
            qty=float(d["sz"]),
            price=float(d["px"]) if d.get("px") else None,
            order_id=d["ordId"],
            pos_side=PosSide(d["posSide"]) if d.get("posSide") else PosSide.NET,
            status=self._parse_status(d["state"]),
            filled_qty=float(d["fillSz"]),
            avg_fill_price=float(d["avgPx"]) if d.get("avgPx") else 0.0,
            fee=float(d["fee"]) if d.get("fee") else 0.0,
            ts=datetime.fromtimestamp(int(d["cTime"]) / 1000, tz=timezone.utc),
        )

    @staticmethod
    def _parse_status(state: str) -> OrderStatus:
        return {
            "live": OrderStatus.LIVE,
            "partially_filled": OrderStatus.PARTIALLY_FILLED,
            "filled": OrderStatus.FILLED,
            "canceled": OrderStatus.CANCELLED,
        }.get(state, OrderStatus.PENDING)

    def _round_qty(self, qty: float, inst_id: str) -> float:
        info = self._inst_cache.get(inst_id)
        if not info or info.lot_sz <= 0:
            return qty
        # 按 lot_sz 向下取整
        precision = max(0, -int(math.floor(math.log10(info.lot_sz))))
        factor = 10 ** precision
        return math.floor(qty * factor / (info.lot_sz * factor)) * info.lot_sz
