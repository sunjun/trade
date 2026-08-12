"""金字塔加仓策略"""
from datetime import UTC, datetime, timedelta

import pytest

from gateway.models import Candle, Order, OrderSide, OrderStatus, OrderType, Position, PosSide
from strategies.pyramid import PyramidStrategy, pyramid_margin_ratios
from tests.conftest import ETH_SWAP, FakePortfolio


def test_margin_ratios_sum_to_one():
    """无论怎么配，加满恰好用尽预算，不会超额"""
    for steps in (3, 6, 10):
        for ratio in (1.0, 1.2, 1.3, 2.0):
            r = pyramid_margin_ratios(steps, ratio)
            assert len(r) == steps
            assert sum(r) == pytest.approx(1.0)
            assert all(x > 0 for x in r)


def test_margin_ratios_are_increasing():
    r = pyramid_margin_ratios(6, 1.3)
    assert all(r[i] < r[i + 1] for i in range(len(r) - 1)), "正金字塔应逐档加大"
    # 末档接近首档的 1.3^5 ≈ 3.7 倍
    assert r[-1] / r[0] == pytest.approx(1.3 ** 5)


def test_rejects_short_tp_schedule(make_strategy):
    with pytest.raises(ValueError, match="tp_schedule"):
        make_strategy(PyramidStrategy, config={"max_steps": 6, "tp_schedule": [0.01]})


@pytest.fixture
def pyramid(make_strategy, fake_rest):
    s = make_strategy(PyramidStrategy, rest=fake_rest, config={
        "timeframe": "15m", "max_steps": 6, "pyramid_ratio": 1.3,
        "capital_pct": 0.5, "leverage": 5, "stop_loss_pct": 0.15,
        "atr_multiplier": 1.2, "fib_lookback": 20,
    })
    return s


async def _warm_higher(s, high=4000.0, low=3000.0, n=20):
    """喂 n 根高时框K线，构造一段从 high 缓慢跌到 low 的区间。

    n 必须 <= fib_lookback，否则最早那根（含区间最高点）会被挤出回看窗口。
    """
    assert n <= s._fib_lookback
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    span = (high - low) / n
    for i in range(n):
        top = high - span * i
        c = Candle(ts=t0 + timedelta(hours=4 * i), open=top, high=top,
                   low=top - span, close=top - span,
                   volume=100.0, confirmed=True)
        await s._handle_higher_tf([c])
    s.on_extra_tf_warmed(s._higher_tf)


def _m15(close, i=0):
    return Candle(ts=datetime(2026, 2, 1, tzinfo=UTC) + timedelta(minutes=15 * i),
                  open=close, high=close, low=close, close=close,
                  volume=10.0, confirmed=True)


async def _fill(s, price, qty):
    await s.on_order_update(Order(
        inst_id=ETH_SWAP, side=OrderSide.BUY, order_type=OrderType.MARKET,
        qty=qty, order_id=f"o{s._step}", status=OrderStatus.FILLED,
        filled_qty=qty, avg_fill_price=price))


async def test_supports_are_fibonacci_of_range(pyramid):
    await _warm_higher(pyramid, high=4000.0, low=3000.0)
    supports = pyramid._supports
    assert supports == sorted(supports, reverse=True), "应从高到低排列"
    assert supports[0] == pytest.approx(4000 - 1000 * 0.236)
    assert supports[-1] == pytest.approx(3000.0), "最后一档是前低"


async def test_first_entry_is_unconditional(pyramid):
    await _warm_higher(pyramid)
    signals = await pyramid.on_candle(_m15(3990.0))
    assert len(signals) == 1
    assert signals[0].side == OrderSide.BUY
    assert signals[0].reduce_only is False
    assert signals[0].qty == 0, "数量由 _calc_qty 按当档预算填充"


async def test_no_signal_before_higher_tf_ready(pyramid):
    assert await pyramid.on_candle(_m15(3990.0)) == []


async def test_add_requires_both_support_and_atr_gap(pyramid):
    await _warm_higher(pyramid, high=4000.0, low=3000.0)
    await pyramid.on_candle(_m15(3990.0))
    await _fill(pyramid, 3990.0, 100)
    assert pyramid._step == 1

    support = pyramid._supports[1]
    atr_gap = pyramid._atr.value * pyramid._atr_mult

    # 只到支撑位、但离上次成交不足一个 ATR 间隔 → 不加
    # （把上次成交价挪到支撑位上方，让 ATR 间隔成为约束条件）
    pyramid._last_entry_price = support + atr_gap * 0.5
    assert await pyramid.on_candle(_m15(support)) == []

    # 只满足 ATR 间隔、但还没跌到支撑位 → 不加
    pyramid._last_entry_price = 3990.0
    assert await pyramid.on_candle(_m15(support + 1)) == []

    # 两个条件同时满足 → 加仓
    signals = await pyramid.on_candle(_m15(support - 1))
    assert len(signals) == 1
    assert signals[0].side == OrderSide.BUY
    assert signals[0].reduce_only is False


async def test_average_entry_and_step_tracking(pyramid):
    await _warm_higher(pyramid)
    await _fill(pyramid, 4000.0, 100)
    await _fill(pyramid, 3800.0, 100)
    assert pyramid._step == 2
    assert pyramid._total_qty == 200
    assert pyramid._avg_entry == pytest.approx(3900.0)
    assert pyramid._last_entry_price == 3800.0


async def test_stops_adding_at_max_steps(pyramid):
    await _warm_higher(pyramid)
    for i in range(pyramid._max_steps):
        await _fill(pyramid, 4000.0 - i * 200, 100)
    assert pyramid._step == pyramid._max_steps

    # 跌到最深也不再加仓
    assert not [s for s in await pyramid.on_candle(_m15(2000.0))
                if s.side == OrderSide.BUY]


async def test_tiered_take_profit(pyramid, fake_rest):
    pos = Position(inst_id=ETH_SWAP, pos_side=PosSide.LONG, size=100.0,
                   entry_price=4000.0)
    pyramid._portfolio = FakePortfolio(position=pos)
    await _warm_higher(pyramid)
    await _fill(pyramid, 4000.0, 100)
    pyramid._ct_val = 0.01

    tp_rate = pyramid._tp_schedule[0]
    assert await pyramid.on_candle(_m15(4000.0 * (1 + tp_rate) - 1)) == []

    signals = await pyramid.on_candle(_m15(4000.0 * (1 + tp_rate) + 1))
    assert len(signals) == 1
    assert signals[0].reduce_only is True
    assert signals[0].qty == 100.0, "全平，数量取交易所真实持仓"
    assert pyramid._step == 0, "止盈后重置，可开始新一轮"


async def test_hard_stop_on_total_drawdown(pyramid):
    pos = Position(inst_id=ETH_SWAP, pos_side=PosSide.LONG, size=100.0,
                   entry_price=4000.0)
    pyramid._portfolio = FakePortfolio(position=pos, equity=10_000.0)
    await _warm_higher(pyramid)
    await pyramid.on_candle(_m15(4000.0))       # 触发 _calc_qty 前先建仓
    await _fill(pyramid, 4000.0, 100)
    pyramid._ct_val = 0.01
    pyramid._committed = 5000.0                  # capital_pct=0.5 × 10000

    # 浮亏 = (price-4000) × 100 × 0.01；-750 需要跌 750 点
    sigs = await pyramid.on_candle(_m15(3400.0))
    assert not [x for x in sigs if x.reduce_only], "浮亏 -600，未到 -750，不该平仓"

    signals = await pyramid.on_candle(_m15(3200.0))   # 浮亏 -800
    assert len(signals) == 1 and signals[0].reduce_only is True
    assert pyramid._step == 0


async def test_zero_committed_does_not_stop_out_instantly(pyramid):
    """_committed 若为 0，止损阈值会退化成 0——任何浮亏都立刻全平"""
    pyramid._portfolio = FakePortfolio(equity=10_000.0)
    await _warm_higher(pyramid)
    await _fill(pyramid, 4000.0, 100)
    pyramid._ct_val = 0.01
    assert pyramid._committed == 0.0

    sigs = await pyramid.on_candle(_m15(3999.0))     # 浮亏仅 -1 USDT
    assert not [x for x in sigs if x.reduce_only], "不该因阈值退化而立刻止损"
    assert pyramid._committed == pytest.approx(5000.0), "应按权益回填"


async def test_supports_freeze_once_in_position(pyramid):
    await _warm_higher(pyramid, high=4000.0, low=3000.0)
    before = list(pyramid._supports)
    await _fill(pyramid, 3990.0, 100)

    # 持仓期间高时框继续走低，支撑阶梯不应变动
    t0 = datetime(2026, 3, 1, tzinfo=UTC)
    for i in range(10):
        await pyramid._handle_higher_tf([Candle(
            ts=t0 + timedelta(hours=4 * i), open=2000, high=2000, low=1000,
            close=1500, volume=1.0, confirmed=True)])
    assert pyramid._supports == before, "加仓阶梯不能在脚下移动"


async def test_supports_refresh_while_flat(pyramid):
    await _warm_higher(pyramid, high=4000.0, low=3000.0)
    before = list(pyramid._supports)
    t0 = datetime(2026, 3, 1, tzinfo=UTC)
    for i in range(25):
        await pyramid._handle_higher_tf([Candle(
            ts=t0 + timedelta(hours=4 * i), open=2000, high=2000, low=1000,
            close=1500, volume=1.0, confirmed=True)])
    assert pyramid._supports != before, "空仓时应跟随行情刷新"


async def test_unconfirmed_candles_ignored(pyramid):
    await _warm_higher(pyramid)
    c = _m15(3990.0)
    c.confirmed = False
    assert await pyramid.on_candle(c) == []


async def test_adopt_assumes_max_step(pyramid):
    """重启后档位未知，按已加满处理——宁可少赚也不要超预算加仓"""
    await _warm_higher(pyramid)
    pos = Position(inst_id=ETH_SWAP, pos_side=PosSide.LONG, size=300.0,
                   entry_price=3500.0)
    assert pyramid.adopt_position(pos) is True
    assert pyramid._step == pyramid._max_steps
    assert pyramid._total_qty == 300.0
    assert pyramid._avg_entry == 3500.0
    assert not pyramid._state.flat


async def test_margin_budget_is_snapshotted(pyramid, fake_rest):
    """浮亏不应让后续档位的预算跟着缩水"""
    pyramid._portfolio = FakePortfolio(equity=10_000.0)
    await _warm_higher(pyramid)

    sig = pyramid._entry_signal(step=0, reason="t")
    qty0 = await pyramid._calc_qty(sig)
    assert pyramid._committed == pytest.approx(5000.0)

    await _fill(pyramid, 4000.0, qty0)
    pyramid._portfolio._equity = 4000.0        # 账户浮亏

    sig2 = pyramid._entry_signal(step=1, reason="t")
    qty1 = await pyramid._calc_qty(sig2)
    raw = 5000.0 * pyramid._margin_ratios[1] * 5 / (0.01 * 3000.0)
    assert qty1 == pytest.approx(float(int(raw))), "应按 lot_sz=1 向下取整"
    assert qty1 > qty0, "第二档保证金应大于第一档"


def test_extra_tf_configs_is_accessible():
    """这个 property 曾因内部访问 RunningATR.period（当时不存在）而抛
    AttributeError，被引擎的 hasattr() 静默吞掉，导致高时框数据完全不喂。"""
    from strategies._indicators import RunningATR
    assert RunningATR(14).period == 14


async def test_extra_tf_configs_declares_higher_tf(pyramid):
    cfgs = pyramid.extra_tf_configs
    assert len(cfgs) == 1
    tf, warm, handler = cfgs[0]
    assert tf == "4H"
    assert warm > pyramid._fib_lookback
    assert handler == pyramid._handle_higher_tf
    assert hasattr(pyramid, "extra_tf_configs"), "hasattr 不能因内部异常而返回 False"
