"""金字塔加仓策略：风险反推仓位、三道出场、首仓过滤"""
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
from strategies.pyramid import PyramidStrategy, plan_ladder, pyramid_weights
from tests.conftest import ETH_SWAP, FakePortfolio

CT_VAL = 0.01


# ── 权重与阶梯规划 ────────────────────────────────────────────────────────────

def test_weights_sum_to_one():
    for steps in (3, 6, 10):
        for ratio in (1.0, 1.2, 1.3, 2.0):
            w = pyramid_weights(steps, ratio)
            assert len(w) == steps
            assert sum(w) == pytest.approx(1.0)
            assert all(x > 0 for x in w)


def test_weights_increase():
    w = pyramid_weights(6, 1.3)
    assert all(w[i] < w[i + 1] for i in range(len(w) - 1))
    assert w[-1] / w[0] == pytest.approx(1.3 ** 5)


def test_plan_ladder_spends_exactly_the_risk_budget():
    """加满所有档、跌到止损价时，亏损应恰好等于预算"""
    weights = pyramid_weights(6, 1.3)
    entries = [100.0, 96.0, 92.0, 88.0, 84.0, 80.0]
    stop, budget = 78.0, 200.0

    qty = plan_ladder(weights, entries, stop, budget, CT_VAL)

    loss = sum(q * (e - stop) * CT_VAL for q, e in zip(qty, entries, strict=True))
    assert loss == pytest.approx(budget)


def test_plan_ladder_loss_is_monotonic_in_step():
    """任何更早的档位，亏损都严格小于预算——保护强度随暴露单调递增。

    这正是原实现（固定 USDT 金额止损）做不到的：浅档要跌 38% 才触发、
    形同虚设，满档 3% 就触发、一碰就停。
    """
    weights = pyramid_weights(6, 1.3)
    entries = [100.0, 96.0, 92.0, 88.0, 84.0, 80.0]
    stop, budget = 78.0, 200.0
    qty = plan_ladder(weights, entries, stop, budget, CT_VAL)

    losses = [
        sum(qty[i] * (entries[i] - stop) * CT_VAL for i in range(step))
        for step in range(1, 7)
    ]
    assert all(a < b for a, b in pairwise(losses))
    assert losses[-1] == pytest.approx(budget)
    assert all(x < budget for x in losses[:-1])


def test_plan_ladder_rejects_stop_above_entries():
    assert plan_ladder([1.0], [100.0], 120.0, 100.0, CT_VAL) == []


def test_plan_ladder_rejects_when_any_single_entry_is_below_stop():
    """只看加权总和是不够的：支撑位过期时会出现「首仓在止损下方、更深几档
    在止损上方」，正负相抵后 denom 仍为正，于是规划通过并挂出高于入场价的
    止损——实盘会被交易所拒单或瞬间触发，回测里则按那个不可达的价"成交"出利润。
    """
    w = pyramid_weights(3, 1.3)
    # 首仓 1881 在止损 2269 下方，但两档"支撑"（过期数据）在止损上方
    qty = plan_ladder(w, [1881.0, 2600.0, 2500.0], 2269.0, 8.0, 0.1)
    assert qty == [], "任何一档跌破止损，整个规划就不成立"

    # 加权总和确实是正的——旧的 denom>0 校验放行了它
    denom = sum(x * (e - 2269.0)
                for x, e in zip(w, [1881.0, 2600.0, 2500.0], strict=True))
    assert denom > 0


# ── 夹具 ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def pyramid(make_strategy, fake_rest):
    return make_strategy(PyramidStrategy, rest=fake_rest, config={
        "timeframe": "15m", "max_steps": 6, "pyramid_ratio": 1.3,
        "risk_pct": 0.02, "max_leverage": 5, "stop_atr_mult": 1.5,
        "atr_multiplier": 1.2, "fib_lookback": 20,
        "ema_fast": 3, "ema_slow": 5, "max_hold_bars": 10,
    })


async def _warm_higher(s, high=4000.0, low=3000.0, n=20, rising=False):
    """喂 n 根高时框K线。rising=True 构造上升趋势（让趋势过滤通过）。"""
    assert n <= s._fib_lookback
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    span = (high - low) / n
    for i in range(n):
        top = low + span * (i + 1) if rising else high - span * i
        c = Candle(ts=t0 + timedelta(hours=4 * i), open=top, high=top,
                   low=top - span, close=top - span * 0.1,
                   volume=100.0, confirmed=True)
        await s._handle_higher_tf([c])
    s.on_extra_tf_warmed(s._higher_tf)


def _m15(close, i=0):
    return Candle(ts=datetime(2026, 2, 1, tzinfo=UTC) + timedelta(minutes=15 * i),
                  open=close, high=close, low=close, close=close,
                  volume=10.0, confirmed=True)


async def _fill(s, price, qty, side=OrderSide.BUY):
    await s.on_order_update(Order(
        inst_id=ETH_SWAP, side=side, order_type=OrderType.MARKET,
        qty=qty, order_id=f"o{s._step}", status=OrderStatus.FILLED,
        filled_qty=qty, avg_fill_price=price))


# ── 首仓过滤 ──────────────────────────────────────────────────────────────────

async def test_no_entry_when_trend_is_down(pyramid):
    """马丁最大的死法是在下跌趋势里启动一整轮"""
    await _warm_higher(pyramid, rising=False)
    assert pyramid._trend_ok() is False
    assert await pyramid.on_candle(_m15(pyramid._supports[0] - 1)) == []


async def test_no_entry_before_price_reaches_first_support(pyramid):
    """首仓也要等回调——它决定整轮成本基准，
    不该是"机器人启动时价格在哪就在哪买" """
    await _warm_higher(pyramid, rising=True)
    assert pyramid._trend_ok() is True
    assert await pyramid.on_candle(_m15(pyramid._supports[0] + 10)) == []


async def test_first_entry_when_trend_up_and_at_support(pyramid):
    await _warm_higher(pyramid, rising=True)
    signals = await pyramid.on_candle(_m15(pyramid._supports[0] - 1))
    assert len(signals) == 1
    assert signals[0].side == OrderSide.BUY
    assert signals[0].reduce_only is False


async def test_filters_can_be_disabled(make_strategy, fake_rest):
    s = make_strategy(PyramidStrategy, rest=fake_rest, config={
        "fib_lookback": 20, "trend_filter": False, "first_entry_at_support": False,
        "ema_fast": 3, "ema_slow": 5,
    })
    await _warm_higher(s, rising=False)
    assert len(await s.on_candle(_m15(3990.0))) == 1


# ── 风险预算反推仓位 ──────────────────────────────────────────────────────────

async def test_round_planning_sets_stop_and_ladder(pyramid):
    await _warm_higher(pyramid, rising=True)
    assert await pyramid._plan_round(pyramid._supports[0] - 1) is True
    assert pyramid._stop_price == pytest.approx(
        min(pyramid._supports) - 1.5 * pyramid._atr.value)
    assert len(pyramid._step_qty) == pyramid._max_steps
    assert all(q > 0 for q in pyramid._step_qty)


async def test_worst_case_loss_equals_budget(pyramid):
    """开仓前就能算出最坏情况——这是能不能上实盘的分界线"""
    await _warm_higher(pyramid, rising=True)
    price = pyramid._supports[0] - 1
    await pyramid._plan_round(price)

    entries = pyramid._planned_entries(price)
    loss = sum(q * (e - pyramid._stop_price) * CT_VAL
               for q, e in zip(pyramid._step_qty, entries, strict=True))
    budget = pyramid._portfolio.get_total_equity() * pyramid._risk_pct
    assert loss == pytest.approx(budget)


async def test_ladder_spread_reaches_the_lowest_support(make_strategy, fake_rest):
    """ladder_spread=True 时末档必须落在前低上，让加仓射程和止损射程对齐

    默认行为下阶梯只取最浅的几道支撑，而止损始终在最低支撑下方——
    中间一大段满仓无摊薄，回测里的滞留超时和止损都出在那一段。
    """
    s = make_strategy(PyramidStrategy, rest=fake_rest,
                      config={"max_steps": 3, "ladder_spread": True})
    await _warm_higher(s, rising=True)
    price = s._supports[0] - 1

    entries = s._planned_entries(price)
    assert len(entries) == 3
    assert entries[-1] == min(s._supports), "末档应落在最低支撑（前低）"
    assert entries == sorted(entries, reverse=True), "入场价必须逐档走低"


async def test_default_ladder_keeps_shallow_supports(make_strategy, fake_rest):
    """默认（ladder_spread=False）保持原行为：只用最浅的几道支撑"""
    s = make_strategy(PyramidStrategy, rest=fake_rest, config={"max_steps": 3})
    await _warm_higher(s, rising=True)
    price = s._supports[0] - 1

    assert s._planned_entries(price) == [price, s._supports[1], s._supports[2]]


async def test_ladder_spread_still_spends_exactly_the_budget(make_strategy, fake_rest):
    """铺开阶梯不能破坏「加满档打到止损恰好亏掉预算」这个不变量"""
    s = make_strategy(PyramidStrategy, rest=fake_rest,
                      config={"max_steps": 3, "ladder_spread": True})
    await _warm_higher(s, rising=True)
    price = s._supports[0] - 1
    await s._plan_round(price)

    entries = s._planned_entries(price)
    loss = sum(q * (e - s._stop_price) * CT_VAL
               for q, e in zip(s._step_qty, entries, strict=True))
    assert loss == pytest.approx(s._portfolio.get_total_equity() * s._risk_pct)


async def test_round_rejected_when_leverage_exceeds_cap(make_strategy, fake_rest):
    """止损太远会要求过大仓位，此时应放弃这一轮而不是硬上"""
    s = make_strategy(PyramidStrategy, rest=fake_rest, config={
        "fib_lookback": 20, "risk_pct": 0.9, "max_leverage": 1.0,
        "ema_fast": 3, "ema_slow": 5,
    })
    await _warm_higher(s, rising=True)
    assert await s._plan_round(s._supports[0] - 1) is False
    assert s._step_qty == []


async def test_calc_qty_uses_planned_tranche(pyramid):
    await _warm_higher(pyramid, rising=True)
    await pyramid._plan_round(pyramid._supports[0] - 1)
    qty = await pyramid._calc_qty(pyramid._entry_signal("t"))
    assert qty == pytest.approx(float(int(pyramid._step_qty[0])))   # lot_sz = 1


async def test_calc_qty_zero_without_plan(pyramid):
    await _warm_higher(pyramid, rising=True)
    assert await pyramid._calc_qty(pyramid._entry_signal("t")) == 0.0


# ── 加仓 ──────────────────────────────────────────────────────────────────────

async def test_add_requires_support_and_atr_gap(pyramid):
    await _warm_higher(pyramid, rising=True)
    entry = pyramid._supports[0] - 1
    await pyramid.on_candle(_m15(entry))
    await _fill(pyramid, entry, 100)
    assert pyramid._step == 1

    support = pyramid._supports[1]
    gap = pyramid._atr.value * pyramid._atr_mult

    # 到了支撑，但离上次成交不足一个 ATR 间隔
    pyramid._last_entry_price = support + gap * 0.5
    assert await pyramid.on_candle(_m15(support)) == []

    # 满足间隔，但还没跌到支撑
    pyramid._last_entry_price = entry
    assert await pyramid.on_candle(_m15(support + 1)) == []

    # 两者都满足
    signals = await pyramid.on_candle(_m15(support - 1))
    assert len(signals) == 1 and signals[0].side == OrderSide.BUY


async def test_stops_adding_at_max_step(pyramid):
    await _warm_higher(pyramid, rising=True)
    await pyramid.on_candle(_m15(pyramid._supports[0] - 1))
    for i in range(pyramid._max_steps):
        await _fill(pyramid, 3900.0 - i * 100, 10)
    assert pyramid._step == pyramid._max_steps
    assert not [s for s in await pyramid.on_candle(_m15(1000.0))
                if s.side == OrderSide.BUY]


# ── 三道出场 ──────────────────────────────────────────────────────────────────

async def test_structural_stop(pyramid):
    pos = Position(inst_id=ETH_SWAP, pos_side=PosSide.LONG, size=100.0,
                   entry_price=3500.0)
    pyramid._portfolio = FakePortfolio(position=pos)
    await _warm_higher(pyramid, rising=True)
    entry = pyramid._supports[0] - 1
    await pyramid.on_candle(_m15(entry))
    await _fill(pyramid, entry, 100)

    stop = pyramid._stop_price
    assert not [x for x in await pyramid.on_candle(_m15(stop + 1)) if x.reduce_only]

    signals = await pyramid.on_candle(_m15(stop - 1))
    assert len(signals) == 1 and signals[0].reduce_only is True
    assert pyramid._step == 0


async def test_max_hold_timeout(pyramid):
    """马丁的真正死法是被困住，给暴露时间加个上界"""
    pos = Position(inst_id=ETH_SWAP, pos_side=PosSide.LONG, size=100.0,
                   entry_price=3500.0)
    pyramid._portfolio = FakePortfolio(position=pos)
    await _warm_higher(pyramid, rising=True)
    await pyramid.on_candle(_m15(pyramid._supports[0] - 1))
    for i in range(pyramid._max_steps):
        await _fill(pyramid, 3500.0 - i, 10)
    assert pyramid._step == pyramid._max_steps

    # 价格卡在止损与止盈之间横盘，只有超时能让它离场
    idle = (pyramid._stop_price + pyramid._avg_entry) / 2
    for i in range(pyramid._max_hold_bars - 1):
        assert not [x for x in await pyramid.on_candle(_m15(idle, i)) if x.reduce_only]

    signals = await pyramid.on_candle(_m15(idle, 99))
    assert len(signals) == 1 and signals[0].reduce_only is True
    assert "timeout" in signals[0].reason.lower()


def test_tp_schedule_is_decreasing(pyramid):
    """深档的目标是逃出来并重置，不是把利润最大化"""
    sched = pyramid._tp_schedule[:pyramid._max_steps]
    assert all(a > b for a, b in pairwise(sched))


async def test_partial_take_profit_trims_deepest_tranche(pyramid):
    """反弹先平最深一档，立刻降暴露，而不是死等一个大目标全平"""
    pos = Position(inst_id=ETH_SWAP, pos_side=PosSide.LONG, size=300.0,
                   entry_price=3500.0)
    pyramid._portfolio = FakePortfolio(position=pos)
    await _warm_higher(pyramid, rising=True)
    await pyramid.on_candle(_m15(pyramid._supports[0] - 1))
    await _fill(pyramid, 3600.0, 100)
    await _fill(pyramid, 3500.0, 200)
    assert pyramid._step == 2
    avg_before = pyramid._avg_entry

    target = pyramid._avg_entry * (1 + pyramid._tp_schedule[1])
    signals = await pyramid.on_candle(_m15(target + 1))
    assert len(signals) == 1
    assert signals[0].reduce_only is True
    assert signals[0].qty == pytest.approx(pyramid._step_qty[1])
    assert pyramid._step == 2, "减仓要等成交回报才回退档位"

    await _fill(pyramid, target, signals[0].qty, side=OrderSide.SELL)
    assert pyramid._step == 1, "成交后回退一档"
    assert pyramid._avg_entry == pytest.approx(avg_before), "平的是最深一笔，均价不变"


async def test_full_close_at_first_step(pyramid):
    pos = Position(inst_id=ETH_SWAP, pos_side=PosSide.LONG, size=100.0,
                   entry_price=3500.0)
    pyramid._portfolio = FakePortfolio(position=pos)
    await _warm_higher(pyramid, rising=True)
    await pyramid.on_candle(_m15(pyramid._supports[0] - 1))
    await _fill(pyramid, 3500.0, 100)

    target = 3500.0 * (1 + pyramid._tp_schedule[0])
    signals = await pyramid.on_candle(_m15(target + 1))
    assert len(signals) == 1
    assert signals[0].qty == 100.0, "第一档止盈全平"
    assert pyramid._step == 0


# ── 交易所侧止损 / 接管 / 其他 ────────────────────────────────────────────────

async def test_entries_carry_shared_stop_price(pyramid):
    """各档共用同一个结构止损价，交易所侧的附加止损会一起触发，
    不会退化成阶梯式的部分止损"""
    await _warm_higher(pyramid, rising=True)
    await pyramid._plan_round(pyramid._supports[0] - 1)
    assert pyramid._entry_signal("t").stop_loss == pytest.approx(pyramid._stop_price)


async def test_supports_freeze_once_in_position(pyramid):
    await _warm_higher(pyramid, rising=True)
    await pyramid.on_candle(_m15(pyramid._supports[0] - 1))
    await _fill(pyramid, 3900.0, 100)
    before = list(pyramid._supports)

    t0 = datetime(2026, 3, 1, tzinfo=UTC)
    for i in range(10):
        await pyramid._handle_higher_tf([Candle(
            ts=t0 + timedelta(hours=4 * i), open=2000, high=2000, low=1000,
            close=1500, volume=1.0, confirmed=True)])
    assert pyramid._supports == before, "加仓阶梯不能在脚下移动"


async def test_supports_refresh_while_flat(pyramid):
    await _warm_higher(pyramid, rising=True)
    before = list(pyramid._supports)
    t0 = datetime(2026, 3, 1, tzinfo=UTC)
    for i in range(25):
        await pyramid._handle_higher_tf([Candle(
            ts=t0 + timedelta(hours=4 * i), open=2000, high=2000, low=1000,
            close=1500, volume=1.0, confirmed=True)])
    assert pyramid._supports != before


async def test_unconfirmed_candles_ignored(pyramid):
    await _warm_higher(pyramid, rising=True)
    c = _m15(pyramid._supports[0] - 1)
    c.confirmed = False
    assert await pyramid.on_candle(c) == []


async def test_adopt_assumes_max_step(pyramid):
    await _warm_higher(pyramid, rising=True)
    pos = Position(inst_id=ETH_SWAP, pos_side=PosSide.LONG, size=300.0,
                   entry_price=3500.0)
    assert pyramid.adopt_position(pos) is True
    assert pyramid._step == pyramid._max_steps
    assert pyramid._stop_price == pytest.approx(pyramid._invalidation_price())
    assert not pyramid._state.flat


def test_adopt_refused_before_warmup(pyramid):
    """支撑位/ATR 未就绪时无法重建止损，拒绝接管而不是塞个 0 进去"""
    pos = Position(inst_id=ETH_SWAP, pos_side=PosSide.LONG, size=300.0,
                   entry_price=3500.0)
    assert pyramid.adopt_position(pos) is False
    assert pyramid._state.flat


async def test_adopt_refuses_short_position(pyramid):
    """只做多的策略接管空头 = 之后每一单平仓都发 posSide=long 被拒 (sCode 51169)"""
    await _warm_higher(pyramid, rising=True)
    pos = Position(inst_id=ETH_SWAP, pos_side=PosSide.SHORT, size=300.0,
                   entry_price=3500.0)
    assert pyramid.adopt_position(pos) is False
    assert pyramid._state.flat
    assert pyramid._step == 0


async def test_close_signal_uses_long_pos_side_on_swap(pyramid):
    """双向持仓模式下平多必须发 posSide=long；发 net 会被交易所拒单"""
    pos = Position(inst_id=ETH_SWAP, pos_side=PosSide.LONG, size=100.0,
                   entry_price=3500.0)
    pyramid._portfolio = FakePortfolio(position=pos)
    await _warm_higher(pyramid, rising=True)
    entry = pyramid._supports[0] - 1
    await pyramid.on_candle(_m15(entry))
    await _fill(pyramid, entry, 100)

    signals = await pyramid.on_candle(_m15(pyramid._stop_price - 1))
    assert len(signals) == 1
    assert signals[0].pos_side is PosSide.LONG


async def test_partial_tp_and_full_close_agree_on_pos_side(pyramid):
    """分档减仓与全平走的是两条代码路径，方向参数必须一致"""
    assert pyramid._reduce_signal(1.0, "x").pos_side is PosSide.LONG


async def test_full_close_never_touches_manual_position(pyramid):
    """交易所把手动仓和策略仓合并成一笔，全平只该平掉策略自己开的那部分"""
    await _warm_higher(pyramid, rising=True)
    entry = pyramid._supports[0] - 1
    # 交易所侧 900 张 = 策略的 100 张 + 手动开的 800 张
    pyramid._portfolio = FakePortfolio(
        position=Position(inst_id=ETH_SWAP, pos_side=PosSide.LONG, size=900.0,
                          entry_price=3500.0))
    await pyramid.on_candle(_m15(entry))
    await _fill(pyramid, entry, 100)
    assert pyramid._total_qty == 100.0

    signals = await pyramid.on_candle(_m15(pyramid._stop_price - 1))
    assert len(signals) == 1
    assert signals[0].qty == 100.0, "平仓量不能超过策略自己持有的张数"


def test_rejects_short_tp_schedule(make_strategy):
    with pytest.raises(ValueError, match="tp_schedule"):
        make_strategy(PyramidStrategy, config={"max_steps": 6, "tp_schedule": [0.01]})


def test_extra_tf_configs_is_accessible(pyramid):
    """这个 property 曾因内部访问 RunningATR.period（当时不存在）而抛
    AttributeError，被引擎的 hasattr() 静默吞掉，导致高时框数据完全不喂。"""
    assert hasattr(pyramid, "extra_tf_configs")
    cfgs = pyramid.extra_tf_configs
    assert len(cfgs) == 1
    tf, _warm, handler = cfgs[0]
    assert tf == "4H"
    assert handler == pyramid._handle_higher_tf


async def test_sizing_is_independent_of_leverage(make_strategy, fake_rest):
    """本策略的仓位由 risk_pct 与止损距离决定，与 leverage 无关。

    yaml 里的 leverage 只被 StrategyEngine 拿去调交易所的 set-leverage，
    即只影响占用多少保证金。趋势类策略走 BaseStrategy._calc_qty，那里
    leverage 是仓位乘数——两者语义不同，容易混淆，故锁死本策略的行为。
    """
    plans = {}
    for lev in (1, 3, 5, 10):
        s = make_strategy(PyramidStrategy, rest=fake_rest, config={
            "fib_lookback": 20, "risk_pct": 0.02, "max_leverage": 5,
            "ema_fast": 3, "ema_slow": 5, "leverage": lev,
        })
        await _warm_higher(s, rising=True)
        await s._plan_round(s._supports[0] - 1)
        plans[lev] = (s._step_qty, s._stop_price)

    first = plans[1]
    for lev, plan in plans.items():
        assert plan[0] == pytest.approx(first[0]), f"leverage={lev} 改变了仓位"
        assert plan[1] == pytest.approx(first[1]), f"leverage={lev} 改变了止损价"


async def test_sizing_scales_with_risk_pct(make_strategy, fake_rest):
    """想放大仓位应调 risk_pct——它同时按比例放大最坏亏损"""
    sizes = {}
    for rp in (0.02, 0.04):
        s = make_strategy(PyramidStrategy, rest=fake_rest, config={
            "fib_lookback": 20, "risk_pct": rp, "max_leverage": 99,
            "ema_fast": 3, "ema_slow": 5,
        })
        await _warm_higher(s, rising=True)
        await s._plan_round(s._supports[0] - 1)
        sizes[rp] = sum(s._step_qty)

    assert sizes[0.04] == pytest.approx(sizes[0.02] * 2, rel=1e-6)


# ── 超跌门槛 + 右侧确认 ───────────────────────────────────────────────────────

async def test_drop_atr_gate_blocks_shallow_pullbacks(make_strategy, fake_rest):
    """跌幅按 ATR 归一化：固定百分比在不同波动率时期含义完全不同"""
    s = make_strategy(PyramidStrategy, rest=fake_rest, config={
        "fib_lookback": 20, "ema_fast": 3, "ema_slow": 5,
        "drop_atr_min": 99.0,      # 高到任何回调都过不了
    })
    await _warm_higher(s, rising=True)
    assert await s.on_candle(_m15(s._supports[0] - 1)) == []
    assert "跌幅不够" in s.decision_note()


async def test_drop_atr_gate_allows_deep_pullbacks(make_strategy, fake_rest):
    s = make_strategy(PyramidStrategy, rest=fake_rest, config={
        "fib_lookback": 20, "ema_fast": 3, "ema_slow": 5, "drop_atr_min": 0.0,
    })
    await _warm_higher(s, rising=True)
    assert len(await s.on_candle(_m15(s._supports[0] - 1))) == 1


async def test_drop_atr_is_measured_in_atr_units(make_strategy, fake_rest):
    s = make_strategy(PyramidStrategy, rest=fake_rest,
                      config={"fib_lookback": 20, "ema_fast": 3, "ema_slow": 5})
    await _warm_higher(s, rising=True)
    high = max(s._highs)
    close = high - 3.0 * s._atr.value
    assert s.drop_atr(close) == pytest.approx(3.0)
    assert s.drop_atr(high) == pytest.approx(0.0)


async def test_reclaim_requires_bullish_close_above_prev_high(make_strategy, fake_rest):
    """超跌不含「跌势停没停」的信息，单用会在下跌全程接飞刀"""
    s = make_strategy(PyramidStrategy, rest=fake_rest, config={
        "fib_lookback": 20, "ema_fast": 3, "ema_slow": 5, "require_reclaim": True,
    })
    await _warm_higher(s, rising=True)
    price = s._supports[0] - 1

    # 第一根：没有上一根可比，不该开
    down = Candle(ts=datetime(2026, 2, 1, tzinfo=UTC), open=price + 5,
                  high=price + 6, low=price - 1, close=price,
                  volume=10.0, confirmed=True)
    assert await s.on_candle(down) == []

    # 第二根仍是阴线：跌势未止，不该开
    down2 = Candle(ts=datetime(2026, 2, 1, 0, 15, tzinfo=UTC), open=price,
                   high=price + 1, low=price - 5, close=price - 4,
                   volume=10.0, confirmed=True)
    assert await s.on_candle(down2) == []
    assert "跌势未止" in s.decision_note()

    # 第三根收阳且收上前一根高点：确认成立
    up = Candle(ts=datetime(2026, 2, 1, 0, 30, tzinfo=UTC), open=price - 4,
                high=price + 3, low=price - 5, close=price + 2,
                volume=10.0, confirmed=True)
    assert s._reclaimed(up) is True


async def test_reclaim_rejects_bullish_bar_that_stays_below_prev_high(
        make_strategy, fake_rest):
    """收阳但没收上前高 = 反弹力度不够，仍算跌势未止"""
    s = make_strategy(PyramidStrategy, rest=fake_rest,
                      config={"fib_lookback": 20, "require_reclaim": True})
    s._prev_candle = Candle(ts=datetime(2026, 2, 1, tzinfo=UTC), open=110.0,
                            high=112.0, low=100.0, close=101.0,
                            volume=1.0, confirmed=True)
    weak = Candle(ts=datetime(2026, 2, 1, 0, 15, tzinfo=UTC), open=101.0,
                  high=105.0, low=100.0, close=104.0, volume=1.0, confirmed=True)
    assert weak.close > weak.open, "确实是阳线"
    assert s._reclaimed(weak) is False, "但没收上前一根高点 112"


async def test_both_gates_default_off(make_strategy, fake_rest):
    """默认行为不变——这两道闸门是可选的"""
    s = make_strategy(PyramidStrategy, rest=fake_rest,
                      config={"fib_lookback": 20, "ema_fast": 3, "ema_slow": 5})
    await _warm_higher(s, rising=True)
    assert s._drop_atr_min is None and s._require_reclaim is False
    assert len(await s.on_candle(_m15(s._supports[0] - 1))) == 1
