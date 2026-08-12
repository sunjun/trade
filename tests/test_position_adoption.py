"""重启后接管交易所已有持仓

不接管的后果：策略以为自己空仓，下一个信号再开一笔变成双倍仓位，
而 reconcile 只告警不纠正。
"""
from types import SimpleNamespace

import pytest

from engine.strategy_engine import StrategyEngine
from gateway.models import InstType, Position, PosSide
from strategies.grid import GridStrategy
from strategies.mtftrend import MtfTrendStrategy
from strategies.rightside import RightSideStrategy
from strategies.trend import TrendStrategy
from tests.conftest import ETH_SWAP


def _pos(side=PosSide.LONG, size=200.0, entry=3000.0):
    return Position(inst_id=ETH_SWAP, pos_side=side, size=size, entry_price=entry)


async def _warmed(make_strategy, candles, cls, **kw):
    s = make_strategy(cls, **kw)
    for c in candles(80):
        await s.on_candle(c)
    s.reset_position_state()
    return s


async def test_pct_stop_strategy_rebuilds_stop(make_strategy, candles):
    s = await _warmed(make_strategy, candles, RightSideStrategy)
    assert s.adopt_position(_pos()) is True
    assert not s._state.flat
    assert s._state.pos_side == PosSide.LONG
    assert s._state.stop_loss == pytest.approx(2700.0)   # entry × (1 - 10%)
    assert s._half_reduced is False, "无从得知重启前是否减过仓，按未减仓处理"


async def test_short_stop_direction(make_strategy, candles):
    s = await _warmed(make_strategy, candles, RightSideStrategy)
    assert s.adopt_position(_pos(PosSide.SHORT)) is True
    assert s._state.stop_loss == pytest.approx(3300.0)   # entry × (1 + 10%)


async def test_atr_stop_strategy(make_strategy, candles):
    s = await _warmed(make_strategy, candles, TrendStrategy,
                      config={"atr_sl_multiplier": 2.0})
    atr = s._atr.value
    assert s.adopt_position(_pos()) is True
    assert s._state.stop_loss == pytest.approx(3000.0 - 2.0 * atr)


async def test_mtf_uses_m15_atr(make_strategy, candles):
    s = make_strategy(MtfTrendStrategy)
    for c in candles(80):
        await s.on_candle(c)
    s.reset_position_state()
    assert s.adopt_position(_pos()) is True
    assert s._state.stop_loss == pytest.approx(3000.0 - s._sl_mult * s._m15.atr.value)


def test_refuses_when_stop_cannot_be_rebuilt(make_strategy):
    """指标未就绪时不能塞个 0 进去：SHORT 的止损判断 close >= 0 会恒真，
    下一根K线就自己平掉。宁可拒绝启动。"""
    s = make_strategy(TrendStrategy)          # 未喂K线，ATR 未就绪
    assert s.adopt_position(_pos(PosSide.SHORT)) is False
    assert s._state.flat, "拒绝接管时不应污染状态机"


async def test_adoption_blocks_double_open(make_strategy, candles):
    s = await _warmed(make_strategy, candles, RightSideStrategy)
    s.adopt_position(_pos())
    assert not s._state.flat, "非 FLAT 则入场分支被短路，不会双开"


def test_grid_adopts_as_single_slot(make_strategy):
    """重启后无从得知原来的分格明细，整笔塞进一个槽是保守做法：
    下次向上跨格整笔平掉，而不会在已有仓位之上再铺满 n_grids 格。"""
    s = make_strategy(GridStrategy, config={
        "grid_lower": 2500, "grid_upper": 3500, "n_grids": 6})
    assert s.adopt_position(_pos(size=180.0)) is True
    assert s._long_slots == [180.0]


# ── 引擎侧 ────────────────────────────────────────────────────────────────────

class _Engine(SimpleNamespace):
    _adopt_existing_position = StrategyEngine._adopt_existing_position
    _warn_if_risk_limits_too_tight = StrategyEngine._warn_if_risk_limits_too_tight


class _Strat:
    def __init__(self, adopts=True, inst_type=InstType.SWAP, config=None,
                 td_mode="cross"):
        self.name, self.symbol = "s", ETH_SWAP
        self.inst_type, self.config = inst_type, config or {}
        self.td_mode = td_mode
        self._adopts, self.adopted = adopts, None

    def adopt_position(self, position):
        self.adopted = position
        return self._adopts


class _Rest:
    def __init__(self, positions):
        self._positions = positions

    async def get_positions(self, inst_id=None):
        return self._positions


async def test_engine_adopts_on_start():
    s, p = _Strat(), _pos()
    await _Engine(_rest=_Rest([p]))._adopt_existing_position(s)
    assert s.adopted is p


async def test_engine_noop_without_position():
    s = _Strat()
    await _Engine(_rest=_Rest([]))._adopt_existing_position(s)
    assert s.adopted is None


async def test_engine_aborts_when_adoption_fails():
    s = _Strat(adopts=False)
    with pytest.raises(RuntimeError, match="未能接管"):
        await _Engine(_rest=_Rest([_pos()]))._adopt_existing_position(s)


async def test_abort_mode_does_not_adopt():
    s = _Strat(config={"on_existing_position": "abort"})
    with pytest.raises(RuntimeError):
        await _Engine(_rest=_Rest([_pos()]))._adopt_existing_position(s)
    assert s.adopted is None


async def test_ignore_mode_leaves_manual_position_alone():
    """手动开的仓位由人工管理，策略以空仓启动，不接管也不因此拒启动"""
    s = _Strat(config={"on_existing_position": "ignore"})
    await _Engine(_rest=_Rest([_pos()]))._adopt_existing_position(s)
    assert s.adopted is None


async def test_ignore_mode_tolerates_both_directions():
    """不接管就无所谓「状态机只能管一个方向」，多空并存也不该拦启动"""
    s = _Strat(config={"on_existing_position": "ignore"})
    positions = [_pos(), _pos(PosSide.SHORT)]
    await _Engine(_rest=_Rest(positions))._adopt_existing_position(s)
    assert s.adopted is None


async def test_engine_aborts_on_both_directions():
    s = _Strat()
    with pytest.raises(RuntimeError, match="只能接管一个方向"):
        await _Engine(
            _rest=_Rest([_pos(), _pos(PosSide.SHORT)])
        )._adopt_existing_position(s)
    assert s.adopted is None


async def test_isolated_position_not_adopted_by_cross_strategy():
    """逐仓持仓与全仓策略互不相干；硬接管的话每次平仓都会撞 sCode 51169"""
    p = _pos()
    p.mgn_mode = "isolated"
    s = _Strat(td_mode="cross")
    await _Engine(_rest=_Rest([p]))._adopt_existing_position(s)
    assert s.adopted is None


async def test_matching_margin_mode_still_adopts():
    p = _pos()
    p.mgn_mode = "cross"
    s = _Strat(td_mode="cross")
    await _Engine(_rest=_Rest([p]))._adopt_existing_position(s)
    assert s.adopted is p


async def test_only_same_margin_mode_counts_as_conflict():
    """一笔逐仓 + 一笔全仓不算「多空并存」，全仓那笔照常接管"""
    iso, cross = _pos(PosSide.SHORT), _pos()
    iso.mgn_mode, cross.mgn_mode = "isolated", "cross"
    s = _Strat(td_mode="cross")
    await _Engine(_rest=_Rest([iso, cross]))._adopt_existing_position(s)
    assert s.adopted is cross


async def test_spot_is_skipped():
    called = []
    e = _Engine(_rest=SimpleNamespace(
        get_positions=lambda *a, **k: called.append(1)))
    await e._adopt_existing_position(_Strat(inst_type=InstType.SPOT))
    assert not called, "OKX 没有现货持仓接口"


def test_warns_when_risk_limits_too_tight(caplog):
    risk_cfg = SimpleNamespace(max_daily_loss_pct=0.02, max_drawdown_pct=0.05,
                               max_position_pct=1.0)
    e = _Engine(_settings=SimpleNamespace(risk=risk_cfg))
    # 项目当前 eth_rightside_swap 的实际配置：20% × 3x × 10% = 单笔 6%
    tight = _Strat(config={"sl_pct": 0.10, "position_size_pct": 0.2, "leverage": 3})

    messages = []
    from loguru import logger
    sink = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        e._warn_if_risk_limits_too_tight(tight)
        assert any("风控阈值可能过紧" in m for m in messages)

        messages.clear()
        loose = _Engine(_settings=SimpleNamespace(
            risk=SimpleNamespace(max_daily_loss_pct=0.10, max_drawdown_pct=0.20,
                                 max_position_pct=1.0)))
        loose._warn_if_risk_limits_too_tight(tight)
        assert not any("风控阈值可能过紧" in m for m in messages)

        messages.clear()
        e._warn_if_risk_limits_too_tight(_Strat(config={"atr_sl_multiplier": 2.0}))
        assert not messages, "ATR 止损无法静态估算，不该误报"
    finally:
        logger.remove(sink)


def test_warns_when_position_would_be_capped():
    """策略配置的名义价值超过全局上限时，实际下单量会被悄悄缩减——必须告警"""
    from loguru import logger
    risk_cfg = SimpleNamespace(max_daily_loss_pct=0.5, max_drawdown_pct=0.5,
                               max_position_pct=0.1)
    e = _Engine(_settings=SimpleNamespace(risk=risk_cfg))
    # 20% 仓位 × 3x = 60% 名义，远超 10% 上限
    s = _Strat(config={"position_size_pct": 0.2, "leverage": 3})

    messages = []
    sink = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        e._warn_if_risk_limits_too_tight(s)
        assert any("仓位会被全局上限截断" in m for m in messages)

        messages.clear()
        loose = _Engine(_settings=SimpleNamespace(risk=SimpleNamespace(
            max_daily_loss_pct=0.5, max_drawdown_pct=0.5, max_position_pct=1.0)))
        loose._warn_if_risk_limits_too_tight(s)
        assert not any("仓位会被全局上限截断" in m for m in messages)
    finally:
        logger.remove(sink)
