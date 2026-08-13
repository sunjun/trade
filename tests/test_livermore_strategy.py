"""利弗摩尔式突破加码（反马丁格尔）

与 PyramidStrategy 方向相反：向盈利加码，最大仓位出现在判断已被验证的时刻，
而不是判断最错的时刻。最坏亏损由构造有界，不靠参数调优。
"""
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from gateway.models import (
    Candle,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PosSide,
)
from strategies.livermore import LivermoreStrategy
from tests.conftest import ETH_SWAP, FakePortfolio

CFG = {"timeframe": "4H", "entry_period": 5, "exit_period": 2, "atr_period": 3,
       "unit_risk_pct": 0.005, "max_units": 4, "add_atr_step": 0.5,
       "stop_atr_mult": 2.0, "cooldown_candles": 0}
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def s(make_strategy, fake_rest):
    return make_strategy(LivermoreStrategy, rest=fake_rest, config=dict(CFG))


def _c(close, i=0, high=None, low=None):
    return Candle(ts=T0 + timedelta(hours=4 * i), open=close,
                  high=high if high is not None else close,
                  low=low if low is not None else close,
                  close=close, volume=10.0, confirmed=True)


async def _warm(strat, n=20, base=3000.0):
    """喂一段窄幅震荡，让通道和 ATR 就绪且不触发突破。"""
    for i in range(n):
        p = base + (i % 3) * 2.0
        await strat.on_candle(_c(p, i, high=p + 3, low=p - 3))


async def _fill(strat, price, qty, side=OrderSide.BUY):
    await strat.on_order_update(Order(
        inst_id=ETH_SWAP, side=side, order_type=OrderType.MARKET, qty=qty,
        order_id=f"o{strat._units}", status=OrderStatus.FILLED,
        filled_qty=qty, avg_fill_price=price))


# ── 风险上界 ──────────────────────────────────────────────────────────────────

def test_worst_case_is_max_over_intermediate_unit_counts(s):
    """必须对中途单位数取最大，只算加满会把风险上界说小

    单位越多，早期单位在最终止损位上的浮盈越大、抵消越多，
    加满反而不是最坏情况。
    """
    s._max_units = 8
    full_load_only = sum(
        (s._unit_ratio ** i) * (s._sl_mult - (8 - 1 - i) * s._add_step)
        for i in range(8)
    ) * s._unit_risk_pct / s._sl_mult
    assert full_load_only == pytest.approx(0.005)          # 只算加满 = 0.5%
    assert s.worst_case_risk_pct() == pytest.approx(0.0125)  # 真实上界 = 1.25%


def test_risk_is_bounded_and_stops_growing_with_max_units(s):
    """反马丁的核心性质：加码上限提高，风险不再线性增长"""
    risks = []
    for n in (1, 2, 4, 6, 8, 16):
        s._max_units = n
        risks.append(s.worst_case_risk_pct())
    assert risks == sorted(risks), "风险随上限单调不减"
    assert max(risks) <= 0.0125 + 1e-9, "但收敛到一个上界，不会无限增长"
    assert risks[-1] == pytest.approx(risks[2]), "4 个单位之后就不再增加了"


def test_decreasing_unit_size_lowers_the_bound(s):
    """越晚越轻（利弗摩尔原味）比等量单位（海龟原版）风险更低"""
    s._unit_ratio = 1.0
    equal = s.worst_case_risk_pct()
    s._unit_ratio = 0.6
    assert s.worst_case_risk_pct() < equal


# ── 入场 ──────────────────────────────────────────────────────────────────────

async def test_no_entry_without_breakout(s):
    await _warm(s)
    assert await s.on_candle(_c(3002.0, 99)) == []
    assert "未突破关键点" in s.decision_note()


async def test_entry_on_breakout_above_channel(s):
    await _warm(s)
    sig = await s.on_candle(_c(3100.0, 99))
    assert len(sig) == 1
    assert sig[0].side == OrderSide.BUY
    assert sig[0].reduce_only is False
    assert sig[0].stop_loss == pytest.approx(3100.0 - 2.0 * s._atr.value)


async def test_unit_size_risks_exactly_unit_risk_pct(s):
    """单位张数由「触及自身初始止损时恰好亏 unit_risk_pct」反推"""
    await _warm(s)
    equity, atr, ct_val = 10_000.0, 50.0, 0.01
    qty = s._unit_size(equity, atr, ct_val)
    loss = qty * (s._sl_mult * atr) * ct_val
    assert loss == pytest.approx(equity * s._unit_risk_pct)


# ── 向盈利加码，而不是向亏损加码 ──────────────────────────────────────────────

async def test_adds_only_after_price_advances(s):
    await _warm(s)
    await s.on_candle(_c(3100.0, 99))
    await _fill(s, 3100.0, 10)
    n = s._round_atr

    # 价格没走够 0.5N，不加
    assert await s.on_candle(_c(3100.0 + 0.2 * n, 100)) == []
    assert "下一单位需价格再走到" in s.decision_note()

    # 走够了才加
    sig = await s.on_candle(_c(3100.0 + 0.6 * n, 101))
    assert len(sig) == 1 and sig[0].side == OrderSide.BUY


async def test_never_adds_when_price_moves_against(s):
    """这是与 PyramidStrategy 的根本分歧：逆向绝不补仓"""
    await _warm(s)
    await s.on_candle(_c(3100.0, 99))
    await _fill(s, 3100.0, 10)
    n = s._round_atr

    for k in (0.2, 0.5, 1.0, 1.5):
        out = await s.on_candle(_c(3100.0 - k * n, 100 + int(k * 10)))
        assert all(sig.side != OrderSide.BUY for sig in out), \
            f"下跌 {k}N 时不该有任何买入"


async def test_stop_rises_with_every_add(s):
    """加仓的同时止损上移——仓位变大，风险敞口反而收窄"""
    await _warm(s)
    await s.on_candle(_c(3100.0, 99))
    await _fill(s, 3100.0, 10)
    n, stops = s._round_atr, [s._stop_price]

    price = 3100.0
    for i in range(1, s._max_units):
        price += 0.6 * n
        await s.on_candle(_c(price, 100 + i))
        await _fill(s, price, 10)
        stops.append(s._stop_price)

    assert s._units == s._max_units
    assert all(a < b for a, b in pairwise(stops)), \
        f"止损必须逐次上移，实际 {stops}"
    assert stops[-1] > 3100.0 - 1e-9 or stops[-1] > stops[0]


async def test_stops_adding_at_max_units(s):
    await _warm(s)
    await s.on_candle(_c(3100.0, 99))
    await _fill(s, 3100.0, 10)
    n, price = s._round_atr, 3100.0
    for i in range(1, s._max_units):
        price += 0.6 * n
        await s.on_candle(_c(price, 100 + i))
        await _fill(s, price, 10)

    price += 5 * n
    assert await s.on_candle(_c(price, 200)) == []
    assert "已满仓" in s.decision_note()


# ── 出场 ──────────────────────────────────────────────────────────────────────

async def _open_one_unit(s, exchange_size=10.0):
    """建一个单位，并让 portfolio 侧反映出交易所持仓。"""
    await _warm(s)
    await s.on_candle(_c(3100.0, 99))
    await _fill(s, 3100.0, 10)
    s._portfolio = FakePortfolio(position=Position(
        inst_id=ETH_SWAP, pos_side=PosSide.LONG, size=exchange_size,
        entry_price=3100.0))


async def test_stop_loss_exit(s):
    await _open_one_unit(s)

    out = await s.on_candle(_c(s._stop_price - 1, 100))
    assert len(out) == 1 and out[0].reduce_only is True
    assert s._units == 0


async def test_channel_exit(s):
    """未触及止损，但收盘跌破 exit 通道下沿也要走"""
    await _open_one_unit(s)
    n = s._round_atr
    # 先往上走一段，把 exit 通道抬起来，再回落穿越它
    for i, p in enumerate((3100 + n, 3100 + 2 * n, 3100 + 3 * n)):
        await s.on_candle(_c(p, 100 + i))
    out = await s.on_candle(_c(3100.0 + 0.1 * n, 110))
    assert len(out) == 1 and out[0].reduce_only is True
    assert s._units == 0


async def test_close_never_exceeds_own_position(s):
    """交易所持仓可能掺着手动仓，只平自己开出来的那部分"""
    await _open_one_unit(s, exchange_size=999.0)

    out = await s.on_candle(_c(s._stop_price - 1, 100))
    assert out[0].qty == pytest.approx(10.0)


def test_rejects_exit_period_wider_than_entry(make_strategy, fake_rest):
    with pytest.raises(ValueError, match="exit_period"):
        make_strategy(LivermoreStrategy, rest=fake_rest,
                      config={"entry_period": 10, "exit_period": 10})


# ── 做空 ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def ss(make_strategy, fake_rest):
    """允许做空的实例"""
    return make_strategy(LivermoreStrategy, rest=fake_rest,
                         config=dict(CFG, allow_short=True))


async def test_short_entry_on_breakdown(ss):
    await _warm(ss)
    sig = await ss.on_candle(_c(2900.0, 99))
    assert len(sig) == 1
    assert sig[0].side == OrderSide.SELL
    assert sig[0].pos_side == PosSide.SHORT
    assert sig[0].stop_loss == pytest.approx(2900.0 + 2.0 * ss._atr.value)


async def test_short_fill_registers_a_position(ss):
    """空仓时 SELL 是开空，不是平仓

    这里曾把「首笔成交是不是开仓」写成 `order.side == BUY`，于是开空成交
    被当成平仓：策略把自己重置回空仓，空单却已经挂在交易所上，接着又去开
    下一笔。回测里表现为 open_short 只增不减、净张数无限累积。
    """
    await _warm(ss)
    await ss.on_candle(_c(2900.0, 99))
    await _fill(ss, 2900.0, 10, side=OrderSide.SELL)

    assert ss._units == 1, "开空成交后应持有 1 个单位"
    assert ss._total_qty == pytest.approx(10.0)
    assert ss._state.pos_side == PosSide.SHORT
    assert ss._stop_price == pytest.approx(2900.0 + 2.0 * ss._round_atr)


async def test_short_adds_as_price_falls_and_stop_moves_down(ss):
    """空头对称：价格每跌 add_atr_step 加一个单位，止损同步下移"""
    await _warm(ss)
    await ss.on_candle(_c(2900.0, 99))
    await _fill(ss, 2900.0, 10, side=OrderSide.SELL)
    n, stops, price = ss._round_atr, [ss._stop_price], 2900.0

    for i in range(1, ss._max_units):
        price -= 0.6 * n
        out = await ss.on_candle(_c(price, 100 + i))
        assert len(out) == 1 and out[0].side == OrderSide.SELL
        await _fill(ss, price, 10, side=OrderSide.SELL)
        stops.append(ss._stop_price)

    assert ss._units == ss._max_units
    assert all(a > b for a, b in pairwise(stops)), f"止损必须逐次下移，实际 {stops}"


async def test_short_never_adds_when_price_rises(ss):
    await _warm(ss)
    await ss.on_candle(_c(2900.0, 99))
    await _fill(ss, 2900.0, 10, side=OrderSide.SELL)
    n = ss._round_atr
    for k in (0.2, 0.5, 1.0, 1.5):
        out = await ss.on_candle(_c(2900.0 + k * n, 100 + int(k * 10)))
        assert all(s.side != OrderSide.SELL for s in out), f"上涨 {k}N 时不该加空"


async def test_short_closes_on_stop(ss):
    await _warm(ss)
    await ss.on_candle(_c(2900.0, 99))
    await _fill(ss, 2900.0, 10, side=OrderSide.SELL)
    ss._portfolio = FakePortfolio(position=Position(
        inst_id=ETH_SWAP, pos_side=PosSide.SHORT, size=10.0, entry_price=2900.0))

    out = await ss.on_candle(_c(ss._stop_price + 1, 120))
    assert len(out) == 1 and out[0].reduce_only is True
    assert ss._units == 0


async def test_short_disabled_by_config(make_strategy, fake_rest):
    """allow_short=False 时跌破通道只观望，不开空"""
    ns = make_strategy(LivermoreStrategy, rest=fake_rest,
                       config=dict(CFG, allow_short=False))
    assert ns._can_short is False
    await _warm(ns)
    assert await ns.on_candle(_c(2900.0, 99)) == []
    assert "未突破关键点" in ns.decision_note()


async def test_late_close_fill_does_not_reopen(ss):
    """平仓成交回报迟到，不能被当成新开仓

    _close() 在发出信号那一刻就重置了状态，成交回报晚一步才到，此时
    units=0 且没有 _pending_side。平空的腿是 BUY，只看方向会把它认成开多，
    策略凭空多出一个不存在的多头单位。
    """
    await _warm(ss)
    await ss.on_candle(_c(2900.0, 99))
    await _fill(ss, 2900.0, 10, side=OrderSide.SELL)
    ss._portfolio = FakePortfolio(position=Position(
        inst_id=ETH_SWAP, pos_side=PosSide.SHORT, size=10.0, entry_price=2900.0))

    out = await ss.on_candle(_c(ss._stop_price + 1, 120))   # 止损离场
    assert out and out[0].side == OrderSide.BUY
    assert ss._units == 0 and ss._pending_side is None

    await _fill(ss, 3200.0, 10, side=OrderSide.BUY)         # 迟到的平仓回报
    assert ss._units == 0, "平仓回报不该开出新仓位"
    assert ss._state.pos_side in (None, PosSide.NET)
