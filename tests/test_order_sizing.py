"""下单数量：平仓腿按真实持仓，开仓腿按配置仓位"""
import pytest

from gateway.models import (
    InstType,
    OrderSide,
    OrderType,
    Position,
    PosSide,
    Signal,
)
from gateway.precision import round_qty, round_to_step
from strategies.grid import GridStrategy
from strategies.rightside import RightSideStrategy
from tests.conftest import ETH_SWAP, FakePortfolio


@pytest.fixture
def long_position():
    return Position(inst_id=ETH_SWAP, pos_side=PosSide.LONG, size=137.0,
                    entry_price=3000.0, unrealized_pnl=0.0)


async def test_close_qty_uses_real_position(make_strategy, fake_rest, long_position):
    """平仓量必须等于交易所真实持仓，不能被开仓公式覆盖。
    覆盖会导致平错张数，在交易所留下无人管理的残仓。"""
    pf = FakePortfolio(position=long_position)
    s = make_strategy(RightSideStrategy, rest=fake_rest, portfolio=pf)
    s._state.open(PosSide.LONG, 3000.0, 2700.0)

    sig = s._close_signal(3000.0, "test")
    assert sig.qty == 137.0
    await s._execute_signal(sig)
    assert fake_rest.orders[-1].qty == 137.0


async def test_reduce_qty_is_half(make_strategy, fake_rest, long_position):
    pf = FakePortfolio(position=long_position)
    s = make_strategy(RightSideStrategy, rest=fake_rest, portfolio=pf)
    s._state.open(PosSide.LONG, 3000.0, 2700.0)

    await s._execute_signal(s._reduce_signal("reduce"))
    assert fake_rest.orders[-1].qty == pytest.approx(68.5)


async def test_entry_qty_from_config(make_strategy, fake_rest):
    s = make_strategy(RightSideStrategy, rest=fake_rest)
    entry = s._open_long_signal(3000.0, 2700.0)
    assert entry.qty == 0
    # 10000 × 20% × 3x / (0.01 × 3000) = 200 张
    assert await s._calc_qty(entry) == 200.0


async def test_grid_splits_position_across_slots(make_strategy, fake_rest):
    s = make_strategy(GridStrategy, rest=fake_rest, config={
        "position_size_pct": 0.3, "n_grids": 6,
        "grid_lower": 2500, "grid_upper": 3500,
    })
    entry = s._open_signal(OrderSide.BUY, PosSide.LONG, "grid open")
    # 每格 = 30% / 6 格
    assert await s._calc_qty(entry) == 49.0

    preset = Signal(inst_id=ETH_SWAP, side=OrderSide.SELL,
                    order_type=OrderType.MARKET, qty=42.0, pos_side=PosSide.LONG)
    assert await s._calc_qty(preset) == 42.0


async def test_spot_sizing(make_strategy, fake_rest, inst_info):
    from dataclasses import replace
    fake_rest._info = replace(inst_info, inst_type=InstType.SPOT,
                              lot_sz=0.0001, min_sz=0.0001)
    s = make_strategy(RightSideStrategy, rest=fake_rest, inst_type=InstType.SPOT,
                      symbol="ETH-USDT", config={"position_size_pct": 0.1})
    sig = Signal(inst_id="ETH-USDT", side=OrderSide.BUY,
                 order_type=OrderType.MARKET, qty=0, pos_side=PosSide.NET,
                 stop_loss=2700.0)
    # 现货不乘杠杆：10000 × 10% / 3000 = 0.3333...
    assert await s._calc_qty(sig) == pytest.approx(0.3333, abs=1e-4)


# ── 精度 ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,step,expected", [
    (1.23456, 0.001, 1.234),
    (7.9, 1, 7.0),
    (0.1 + 0.2, 0.1, 0.3),          # 浮点误差不能漏出来
    (1.0000000000000002, 0.0001, 1.0),
    (99.999, 0.01, 99.99),
    (5.0, 0, 5.0),                  # step<=0 原样返回
])
def test_round_to_step(value, step, expected):
    assert round_to_step(value, step) == pytest.approx(expected)


def test_round_to_step_has_no_float_tail():
    """旧实现会算出 0.30000000000000004 这类值，超出 lotSz 小数位被 OKX 拒单"""
    assert repr(round_to_step(0.1 + 0.2, 0.1)) == "0.3"
    assert repr(round_to_step(2.675, 0.001)) == "2.675"


def test_round_qty_respects_min_size():
    assert round_qty(0.5, 0.1, min_sz=1.0) == 0.0
    assert round_qty(1.05, 0.1, min_sz=1.0) == pytest.approx(1.0)
