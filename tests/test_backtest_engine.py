"""回测账户：部分平仓、加仓均价、资金守恒

回测若不支持部分平仓，减仓就会被执行成清仓，回测验证的东西和实盘跑的
就不是同一个策略。
"""
from datetime import UTC, datetime

import pytest

from backtest.engine import FEE_RATE, BacktestPortfolio, BacktestRest
from backtest.report import _calc_metrics
from gateway.models import Candle, InstType, Order, OrderSide, OrderType, PosSide

CT_VAL = 0.01
LEV = 3


@pytest.fixture
def portfolio():
    return BacktestPortfolio(10_000.0, CT_VAL, LEV)


def test_partial_close_releases_proportional_margin(portfolio):
    portfolio.open_position("long", 200, 3000.0)
    margin0 = 200 * CT_VAL * 3000.0 / LEV

    net_pnl, fee, closed = portfolio.close_position(3300.0, 100)

    assert closed == 100
    assert net_pnl == pytest.approx((3300 - 3000) * 100 * CT_VAL - fee)
    assert portfolio._position is not None, "部分平仓不应清空持仓"
    assert portfolio._position["contracts"] == pytest.approx(100)
    assert portfolio._position["margin"] == pytest.approx(margin0 / 2)
    assert portfolio._position["entry_price"] == pytest.approx(3000.0), "剩余持仓沿用原开仓价"


def test_cash_is_conserved(portfolio):
    fee_open = 200 * CT_VAL * 3000.0 * FEE_RATE
    portfolio.open_position("long", 200, 3000.0)
    pnl1, _, _ = portfolio.close_position(3300.0, 100)
    pnl2, _, _ = portfolio.close_position(3600.0)

    assert portfolio._position is None
    assert portfolio._cash == pytest.approx(10_000 - fee_open + pnl1 + pnl2)


def test_full_close_unchanged(portfolio):
    """全平路径的数值必须与支持部分平仓之前一致"""
    portfolio.open_position("short", 150, 3000.0)
    net_pnl, _, closed = portfolio.close_position(2800.0)
    expected = (3000 - 2800) * 150 * CT_VAL - 150 * CT_VAL * 2800 * FEE_RATE
    assert closed == 150
    assert net_pnl == pytest.approx(expected)


def test_over_close_is_clamped(portfolio):
    portfolio.open_position("long", 50, 3000.0)
    _, _, closed = portfolio.close_position(3000.0, 999)
    assert closed == 50, "超量平仓应被夹到持仓量"
    assert portfolio._position is None


def test_add_to_position_averages_entry(portfolio):
    """同向加仓要按张数加权平均；旧实现直接覆盖 _position，丢掉上一笔的保证金"""
    portfolio.open_position("long", 100, 3000.0)
    portfolio.open_position("long", 100, 3400.0)
    p = portfolio._position
    assert p["contracts"] == pytest.approx(200)
    assert p["entry_price"] == pytest.approx(3200.0)
    assert p["margin"] == pytest.approx((100 * CT_VAL * 3000 + 100 * CT_VAL * 3400) / LEV)


def test_reverse_open_closes_existing(portfolio):
    portfolio.open_position("long", 100, 3000.0)
    portfolio.open_position("short", 50, 3200.0)
    assert portfolio._position["pos_side"] == "short"
    assert portfolio._position["contracts"] == pytest.approx(50)


# ── place_order 路由 ──────────────────────────────────────────────────────────

@pytest.fixture
def rest(portfolio, inst_info):
    r = BacktestRest(portfolio, inst_info)
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    r.set_current_candle(Candle(ts=ts, open=3000, high=3000, low=3000,
                                close=3000, volume=1, confirmed=True))
    return r


def _order(qty, reduce_only=True, side=OrderSide.SELL):
    """默认构造平仓腿；开仓腿传 reduce_only=False"""
    return Order(inst_id="ETH-USDT-SWAP", side=side, order_type=OrderType.MARKET,
                 qty=qty, pos_side=PosSide.LONG, reduce_only=reduce_only)


async def test_reduce_is_recorded_separately(rest, portfolio):
    await rest.place_order(_order(200, reduce_only=False, side=OrderSide.BUY),
                           InstType.SWAP)
    filled = await rest.place_order(_order(100), InstType.SWAP)

    assert filled.filled_qty == 100
    assert rest.trades[-1].action == "reduce_long"
    assert portfolio._position["contracts"] == pytest.approx(100)

    await rest.place_order(_order(100), InstType.SWAP)
    assert rest.trades[-1].action == "close_long"
    assert portfolio._position is None


async def test_close_without_position_is_ignored(rest):
    filled = await rest.place_order(_order(100), InstType.SWAP)
    assert filled.filled_qty == 0
    assert not rest.trades


async def test_report_counts_reduce_legs(rest):
    await rest.place_order(_order(200, reduce_only=False, side=OrderSide.BUY),
                           InstType.SWAP)
    await rest.place_order(_order(100), InstType.SWAP)
    await rest.place_order(_order(100), InstType.SWAP)

    m = _calc_metrics([10_000, 10_300, 10_600], 10_000.0, rest.trades)
    assert m["total_trades"] == 2
    assert m["reduce_legs"] == 1
    # 净盈亏必须等于逐笔加总，否则和权益曲线对不上
    assert m["total_pnl_usdt"] == pytest.approx(sum(t.pnl for t in rest.trades))


# ── 与实盘引擎的行为对齐 ──────────────────────────────────────────────────────

async def test_fill_notifies_strategy(rest, portfolio):
    """回测必须像实盘的 WS 订单推送一样回调 on_order_update。

    缺了它，靠成交回报维护状态的策略（网格的槽位、金字塔的档位）在回测里
    状态永远不推进——金字塔会在每根K线重新建仓。
    """
    seen = []

    class Strat:
        async def on_order_update(self, order):
            seen.append((order.side, order.filled_qty, order.avg_fill_price))

    rest.strategy = Strat()
    await rest.place_order(_order(100, reduce_only=False, side=OrderSide.BUY),
                           InstType.SWAP)
    assert len(seen) == 1
    assert seen[0][1] == 100

    await rest.place_order(_order(100), InstType.SWAP)
    assert len(seen) == 2


async def test_no_notify_when_nothing_filled(rest):
    seen = []

    class Strat:
        async def on_order_update(self, order):
            seen.append(order)

    rest.strategy = Strat()
    await rest.place_order(_order(100), InstType.SWAP)   # 无持仓可平
    assert not seen


def test_portfolio_exposes_total_equity(portfolio):
    """策略按权益算仓位预算时会调 get_total_equity，接口需与实盘 Portfolio 一致"""
    assert portfolio.get_total_equity() == pytest.approx(10_000.0)
    portfolio.open_position("long", 100, 3000.0)
    fee = 100 * CT_VAL * 3000.0 * FEE_RATE
    assert portfolio.get_total_equity() == pytest.approx(10_000.0 - fee)
