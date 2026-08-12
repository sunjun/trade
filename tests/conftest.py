"""测试共享桩件

这些桩只实现被测代码真正调用到的方法，不追求完整模拟交易所。
需要真实行为的地方（如 REST 的超时/重试）用本地 aiohttp 服务端测，
见 test_rest_client.py。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import UTC, datetime, timedelta

from engine.risk_manager import RiskManager
from gateway.models import (
    Balance,
    Candle,
    InstrumentInfo,
    InstType,
    Order,
    OrderStatus,
    Ticker,
)

ETH_SWAP = "ETH-USDT-SWAP"


@pytest.fixture
def inst_info():
    return InstrumentInfo(
        inst_id=ETH_SWAP, inst_type=InstType.SWAP, base_ccy="ETH", quote_ccy="USDT",
        lot_sz=1.0, min_sz=1.0, ct_val=0.01, tick_sz=0.01,
    )


class FakeRest:
    def __init__(self, price=3000.0, positions=None, inst_info=None):
        self.price = price
        self.orders: list[Order] = []
        self._positions = positions or []
        self._info = inst_info or InstrumentInfo(
            inst_id=ETH_SWAP, inst_type=InstType.SWAP, base_ccy="ETH",
            quote_ccy="USDT", lot_sz=1.0, min_sz=1.0, ct_val=0.01, tick_sz=0.01,
        )

    async def get_ticker(self, inst_id):
        return Ticker(inst_id=inst_id, last=self.price, bid=self.price, ask=self.price)

    async def get_instrument(self, inst_id, inst_type):
        return self._info

    async def get_positions(self, inst_id=None):
        return self._positions

    async def place_order(self, order, inst_type):
        self.orders.append(order)
        order.order_id = f"fake{len(self.orders)}"
        order.status = OrderStatus.LIVE
        return order


class FakePortfolio:
    def __init__(self, available=10_000.0, position=None, equity=10_000.0):
        self._available = available
        self._position = position
        self._equity = equity

    def get_available(self, currency="USDT"):
        return self._available

    def get_total_equity(self):
        return self._equity

    def get_position(self, inst_id, pos_side="long", mgn_mode=None):
        if self._position is not None and self._position.pos_side.value == pos_side:
            return self._position
        return None


class FakeDB:
    def __init__(self):
        self.orders = []
        self.signals = []

    async def save_candle(self, *a, **kw):
        pass

    async def save_signal(self, signal, strategy):
        self.signals.append((signal, strategy))

    async def save_order(self, order, strategy):
        self.orders.append((order, strategy))


@pytest.fixture
def fake_rest():
    return FakeRest()


@pytest.fixture
def fake_portfolio():
    return FakePortfolio()


@pytest.fixture
def make_strategy(fake_rest, fake_portfolio):
    """构造一个接好桩件的策略实例。"""
    def _make(cls, config=None, rest=None, portfolio=None, risk=None, db=None,
              name="test", inst_type=InstType.SWAP, symbol=ETH_SWAP):
        cfg = {"timeframe": "1H", "position_size_pct": 0.2, "leverage": 3,
               "sl_pct": 0.10}
        cfg.update(config or {})
        return cls(
            name=name, inst_type=inst_type, symbol=symbol, config=cfg,
            rest=rest or fake_rest, risk=risk or RiskManager(),
            portfolio=portfolio or fake_portfolio, db=db or FakeDB(),
        )
    return _make


@pytest.fixture
def candles():
    """生成一段稳定上涨的已收盘K线，够指标预热。"""
    def _make(n=60, start=3000.0, step=5.0, confirmed=True, tf_hours=1):
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        out = []
        for i in range(n):
            p = start + i * step
            out.append(Candle(
                ts=t0 + timedelta(hours=tf_hours * i), open=p, high=p + 8,
                low=p - 8, close=p, volume=100.0, confirmed=confirmed,
            ))
        return out
    return _make


@pytest.fixture
def usdt_balance():
    return Balance(currency="USDT", total=10_000.0, available=10_000.0)
