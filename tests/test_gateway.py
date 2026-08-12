"""网关层：余额去重、REST 健壮性、WS 不阻塞读取循环

REST 部分用本地 aiohttp 服务端测真实行为，不 mock socket。
"""
import asyncio
import time

import pytest
from aiohttp import web

from engine.portfolio import Portfolio
from gateway.models import (
    InstType,
    Order,
    OrderSide,
    OrderType,
    PosSide,
)
from gateway.okx_rest import MAX_RETRIES, OKXError, OKXRestClient
from gateway.okx_ws import OKXWebSocketClient

CANDLE_ROW = [["1700000000000", "1", "2", "0.5", "1.5", "10", "0", "0", "1"]]


# ── Portfolio 余额去重 ────────────────────────────────────────────────────────

def _fill(oid, side=OrderSide.BUY, qty=1.0, price=3000.0, fee=-1.5):
    from gateway.models import OrderStatus
    return Order(inst_id="ETH-USDT", side=side, order_type=OrderType.MARKET,
                 qty=qty, order_id=oid, status=OrderStatus.FILLED,
                 filled_qty=qty, avg_fill_price=price, fee=fee)


@pytest.fixture
def portfolio(usdt_balance):
    p = Portfolio()
    p._balances["USDT"] = usdt_balance
    return p


async def test_repeated_fill_debits_once(portfolio):
    """OKX 对同一订单会多次推送（部分成交→完全成交→重连重推）"""
    o = _fill("X1")
    await portfolio.on_order_filled(o)
    once = portfolio.get_available()
    assert once == pytest.approx(10_000 - 3000 - 1.5)

    for _ in range(5):
        await portfolio.on_order_filled(o)
    assert portfolio.get_available() == once


async def test_distinct_orders_each_settle(portfolio):
    await portfolio.on_order_filled(_fill("X1"))
    after_first = portfolio.get_available()
    await portfolio.on_order_filled(_fill("X2"))
    assert portfolio.get_available() < after_first


async def test_fill_without_order_id_skipped(portfolio):
    before = portfolio.get_available()
    await portfolio.on_order_filled(_fill(""))
    assert portfolio.get_available() == before, "无 order_id 无法去重，只能跳过"


def test_dedupe_table_is_bounded(portfolio):
    assert portfolio._settled_orders.maxlen == 512


# ── REST 健壮性 ───────────────────────────────────────────────────────────────

class FakeOKX:
    """可编排每次请求返回什么的本地 OKX 桩"""

    def __init__(self):
        self.hits = 0
        self.script: list[str] = []

    async def handler(self, request):
        self.hits += 1
        kind = self.script.pop(0) if self.script else "ok"
        if kind == "ok":
            return web.json_response({"code": "0", "data": [
                {"last": "3000", "bidPx": "2999", "askPx": "3001",
                 "ts": "1700000000000"}]})
        if kind == "502":
            return web.Response(status=502, text="<html>Bad Gateway</html>")
        if kind == "html200":
            return web.Response(status=200, text="<html>oops</html>",
                                content_type="text/html")
        if kind in ("429", "400"):
            return web.Response(status=int(kind), text=kind)
        if kind == "slow":
            await asyncio.sleep(30)
            return web.json_response({"code": "0", "data": []})
        if kind == "bizerr":
            return web.json_response({"code": "51000", "msg": "Parameter error"})
        raise AssertionError(kind)


@pytest.fixture
async def okx(monkeypatch):
    """起一个本地 OKX 桩，返回 (client, fake)"""
    import aiohttp

    import gateway.okx_rest as rest_mod

    fake = FakeOKX()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    monkeypatch.setattr(rest_mod, "REST_BASE", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(rest_mod, "REQUEST_TIMEOUT",
                        aiohttp.ClientTimeout(total=1))
    async with OKXRestClient("k", "s", "p", is_demo=True) as client:
        yield client, fake
    await runner.cleanup()


def test_okxerror_is_runtimeerror():
    """既有调用点都是 except RuntimeError，网关错误必须能被它们兜住"""
    assert issubclass(OKXError, RuntimeError)


async def test_non_json_becomes_okxerror(okx):
    client, fake = okx
    fake.script = ["html200"]
    with pytest.raises(OKXError, match="Non-JSON"):
        await client.get_ticker("ETH-USDT")


async def test_retries_5xx_then_succeeds(okx):
    client, fake = okx
    fake.script = ["502", "502", "ok"]
    assert (await client.get_ticker("ETH-USDT")).last == 3000.0
    assert fake.hits == 3


async def test_retry_exhausted(okx):
    client, fake = okx
    fake.script = ["429"] * (MAX_RETRIES + 1)
    with pytest.raises(OKXError) as e:
        await client.get_ticker("ETH-USDT")
    assert e.value.status == 429
    assert fake.hits == MAX_RETRIES + 1


async def test_no_retry_on_4xx(okx):
    client, fake = okx
    fake.script = ["400"] * 5
    with pytest.raises(OKXError):
        await client.get_ticker("ETH-USDT")
    assert fake.hits == 1, "4xx 是请求本身有问题，重试无意义"


async def test_timeout_recovers_via_retry(okx):
    client, fake = okx
    fake.script = ["slow", "ok"]
    assert (await client.get_ticker("ETH-USDT")).last == 3000.0
    assert fake.hits == 2


async def test_timeout_gives_up(okx):
    client, fake = okx
    fake.script = ["slow"] * (MAX_RETRIES + 1)
    t0 = time.perf_counter()
    with pytest.raises(OKXError, match="Timeout"):
        await client.get_ticker("ETH-USDT")
    assert time.perf_counter() - t0 < 30, "必须在有限时间内放弃"


async def test_order_is_never_retried(okx):
    """下单绝不能重试：超时不代表没成交，重试可能变成开两笔仓"""
    client, fake = okx
    fake.script = ["502"] * 5
    o = Order(inst_id="ETH-USDT-SWAP", side=OrderSide.BUY,
              order_type=OrderType.MARKET, qty=1.0, pos_side=PosSide.LONG)
    with pytest.raises(OKXError):
        await client.place_order(o, InstType.SWAP)
    assert fake.hits == 1


async def test_business_error_carries_code(okx):
    client, fake = okx
    fake.script = ["bizerr"]
    with pytest.raises(OKXError) as e:
        await client.get_ticker("ETH-USDT")
    assert e.value.code == "51000"
    assert fake.hits == 1, "业务错误码不重试"


# ── WS 不阻塞读取循环 ─────────────────────────────────────────────────────────

@pytest.fixture
async def ws():
    client = OKXWebSocketClient("k", "s", "p")
    yield client
    await client.stop()


def _candle_msg(inst_id, close="1.5"):
    row = [["1700000000000", "1", "2", "0.5", close, "10", "0", "0", "1"]]
    return {"arg": {"channel": "candle15m", "instId": inst_id}, "data": row}


async def test_dispatch_returns_immediately(ws):
    """回调链里有 REST 往返，在 socket 读取循环内 await 会让连接停止收包"""
    async def slow(_):
        await asyncio.sleep(0.4)

    ws.subscribe_candles("ETH-USDT-SWAP", "15m", slow)
    t0 = time.perf_counter()
    await ws._dispatch(_candle_msg("ETH-USDT-SWAP"))
    assert time.perf_counter() - t0 < 0.05


async def test_slow_subscription_does_not_block_others(ws):
    slow_done, fast_done = asyncio.Event(), asyncio.Event()

    async def slow(_):
        await asyncio.sleep(0.4)
        slow_done.set()

    async def fast(_):
        fast_done.set()

    ws.subscribe_candles("ETH-USDT-SWAP", "15m", slow)
    ws.subscribe_candles("BTC-USDT-SWAP", "15m", fast)

    await ws._dispatch(_candle_msg("ETH-USDT-SWAP"))
    await ws._dispatch(_candle_msg("BTC-USDT-SWAP"))
    await asyncio.wait_for(fast_done.wait(), timeout=1)
    assert not slow_done.is_set(), "慢订阅此时应仍在处理"

    await asyncio.wait_for(slow_done.wait(), timeout=2)


async def test_order_preserved_within_subscription(ws):
    seen, done = [], asyncio.Event()

    async def collect(candles):
        seen.append(candles[0].close)
        await asyncio.sleep(0.01)
        if len(seen) == 5:
            done.set()

    ws.subscribe_candles("X", "15m", collect)
    for i in range(5):
        await ws._dispatch(_candle_msg("X", close=str(i)))
    await asyncio.wait_for(done.wait(), timeout=2)
    assert seen == [0.0, 1.0, 2.0, 3.0, 4.0]


async def test_callback_error_does_not_kill_worker(ws):
    """worker 是长驻循环，未捕获异常会让该订阅永久停止消费"""
    calls = []

    async def boom(_):
        calls.append(1)
        raise ValueError("callback exploded")

    ws.subscribe_candles("X", "15m", boom)
    for _ in range(2):
        await ws._dispatch(_candle_msg("X"))
        await asyncio.sleep(0.05)
    assert len(calls) == 2


async def test_stop_cancels_workers():
    client = OKXWebSocketClient("k", "s", "p")

    async def noop(_):
        pass

    client.subscribe_candles("X", "15m", noop)
    await client._dispatch(_candle_msg("X"))
    assert client._workers

    await client.stop()
    assert not client._workers and not client._queues
