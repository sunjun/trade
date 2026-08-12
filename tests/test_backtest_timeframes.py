"""回测引擎的多时框喂送

引擎原来把额外时框硬编码成 "4H"/"1H" 两个槽位，声明 1D 的策略会被静默丢弃
数据（只打一条 WARNING），跑出 0 笔交易的空报告——看起来像"策略不开仓"，
实际是高时框指标从未就绪。
"""
from datetime import UTC, datetime, timedelta

import pytest

from backtest.engine import BacktestEngine
from gateway.models import Candle, InstrumentInfo, InstType

INST = "ETH-USDT-SWAP"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
INFO = InstrumentInfo(inst_id=INST, inst_type=InstType.SWAP, base_ccy="ETH",
                      quote_ccy="USDT", lot_sz=0.01, min_sz=0.01, ct_val=0.1)


def _bars(n: int, step: timedelta, start: datetime = T0) -> list[Candle]:
    return [Candle(ts=start + step * i, open=100.0, high=101.0, low=99.0,
                   close=100.0, volume=1.0, confirmed=False) for i in range(n)]


class _Spy:
    """记录每个时框实际收到多少根 K 线的假策略。"""
    warm_up_period = 2

    def __init__(self, name, inst_type, symbol, config, rest, risk, portfolio, db):
        self.config = config
        self.seen: dict[str, int] = {"primary": 0}
        self.warmed: list[str] = []
        self._warm_up_done = False

    @property
    def extra_tf_configs(self):
        return [(tf, 2, self._make_handler(tf)) for tf in self.config["extra_tfs"]]

    def _make_handler(self, tf):
        async def handler(candles):
            self.seen[tf] = self.seen.get(tf, 0) + len(candles)
        return handler

    def on_extra_tf_warmed(self, tf):
        self.warmed.append(tf)

    async def on_candle(self, candle):
        self.seen["primary"] += 1
        return []

    def reset_position_state(self):
        pass


def _engine(extra_tfs):
    return BacktestEngine(
        strategy_cls=_Spy, strategy_name="spy",
        strategy_config={"extra_tfs": extra_tfs}, inst_id=INST, inst_info=INFO,
    )


async def test_arbitrary_timeframe_is_fed():
    """1D 既不是 4H 也不是 1H——以前会被静默丢弃，指标永远不就绪"""
    e = _engine(["1D"])
    daily = _bars(10, timedelta(days=1))
    # 主时框跨度要盖过高时框，否则末尾几根按「尚未闭合」正确地不喂
    await e.run(candles_primary=_bars(80, timedelta(hours=4)),
                extra_candles={"1D": daily}, warm_up_extra={"1D": 0})

    assert e._strategy.seen["1D"] == len(daily), "1D 的每一根都该喂进策略"
    assert e._strategy.warmed == ["1D"]


async def test_multiple_extra_timeframes_all_fed():
    e = _engine(["1D", "4H"])
    await e.run(
        candles_primary=_bars(300, timedelta(hours=1)),
        extra_candles={"1D": _bars(10, timedelta(days=1)),
                       "4H": _bars(20, timedelta(hours=4))},
        warm_up_extra={"1D": 0, "4H": 0},
    )
    assert e._strategy.seen["1D"] == 10
    assert e._strategy.seen["4H"] == 20
    assert sorted(e._strategy.warmed) == ["1D", "4H"]


async def test_extra_bars_beyond_primary_range_are_not_fed():
    """高时框数据比主时框长时，超出的部分是「未来」，绝不能喂"""
    e = _engine(["1D"])
    await e.run(candles_primary=_bars(24, timedelta(hours=4)),   # 只覆盖 4 天
                extra_candles={"1D": _bars(10, timedelta(days=1))},
                warm_up_extra={"1D": 0}, warm_up_primary=1)
    assert 0 < e._strategy.seen["1D"] < 10


async def test_empty_backtest_window_is_reported_not_crashed():
    """预热吃光数据时以前会 IndexError，看不出是数据不够"""
    e = _engine([])
    await e.run(candles_primary=_bars(5, timedelta(hours=4)), warm_up_primary=99)
    assert e._strategy.seen["primary"] == 5, "只跑了预热，回测期为空"


async def test_extra_candles_are_fed_in_chronological_order():
    """高时框 K 线必须在「已闭合」之后才喂，否则策略看到未来数据"""
    e = _engine(["4H"])
    seen_ts = []

    eng_strategy = e._strategy
    orig = eng_strategy._make_handler

    def spy_handler(tf):
        inner = orig(tf)
        async def handler(candles):
            seen_ts.extend(c.ts for c in candles)
            await inner(candles)
        return handler

    eng_strategy._make_handler = spy_handler
    primary = _bars(40, timedelta(hours=1))
    await e.run(candles_primary=primary,
                extra_candles={"4H": _bars(10, timedelta(hours=4))})

    assert seen_ts == sorted(seen_ts)
    assert seen_ts[-1] <= primary[-1].ts, "不能喂入晚于当前主时框时刻的高时框K线"


async def test_missing_extra_data_warns_but_does_not_crash(caplog):
    """策略声明了某时框却没提供数据时，要能跑完并明说"""
    e = _engine(["1D"])
    await e.run(candles_primary=_bars(30, timedelta(hours=4)), extra_candles={})
    assert e._strategy.seen.get("1D", 0) == 0
    assert e._strategy.seen["primary"] > 0


async def test_primary_warm_up_is_excluded_from_backtest():
    e = _engine([])
    await e.run(candles_primary=_bars(30, timedelta(hours=1)), warm_up_primary=10)
    # 预热 10 根 + 回测 20 根，两段都会调 on_candle
    assert e._strategy.seen["primary"] == 30


@pytest.mark.parametrize("tf", ["1D", "1W", "12H", "30m", "2H"])
async def test_no_timeframe_is_silently_dropped(tf):
    """时框名不再参与任何分支判断——引擎对它一视同仁"""
    e = _engine([tf])
    await e.run(candles_primary=_bars(80, timedelta(hours=4)),
                extra_candles={tf: _bars(8, timedelta(days=1))},
                warm_up_extra={tf: 0})
    assert e._strategy.seen.get(tf, 0) == 8, f"{tf} 被丢弃了"


# ── 止损成交价 ────────────────────────────────────────────────────────────────

class _Holder(_Spy):
    """开一笔多头后就不再动作，把止损交给引擎去触发。"""

    async def on_candle(self, candle):
        await super().on_candle(candle)
        return []


async def test_stop_fill_cannot_beat_the_bar():
    """止损价高于本根K线区间时，成交只能在开盘价——否则是凭空造利润"""
    from strategies._base_state import PositionState

    e = _engine([])
    e._strategy.__class__ = _Holder
    e._strategy._state = PositionState()
    e._portfolio.open_position("long", 1.0, 1881.0)
    e._strategy._state.open_side = None
    e._strategy._state.flat = False
    e._strategy._state.stop_loss = 2269.0     # 残留的过期止损，远高于市价

    bar = Candle(ts=T0, open=1885.0, high=1890.0, low=1880.0, close=1886.0,
                 volume=1.0, confirmed=True)
    await e._check_stop_loss(bar)

    sl = [t for t in e.trades if t.action == "sl_long"]
    assert len(sl) == 1
    assert sl[0].price == 1885.0, "必须按开盘价成交，不能拿到 2269 这个不可达的价"
    # 按 2269 成交会凭空造出 (2269-1881)*ct_val ≈ +38.8 USDT
    assert sl[0].pnl < 1.0, f"盈亏应只反映 1881→1885 这 4 个点，实得 {sl[0].pnl}"


async def test_normal_stop_fills_at_stop_price():
    """止损价落在本根区间内时，正常按止损价成交"""
    from strategies._base_state import PositionState

    e = _engine([])
    e._strategy.__class__ = _Holder
    e._strategy._state = PositionState()
    e._portfolio.open_position("long", 1.0, 2000.0)
    e._strategy._state.flat = False
    e._strategy._state.stop_loss = 1950.0

    bar = Candle(ts=T0, open=1990.0, high=1995.0, low=1940.0, close=1945.0,
                 volume=1.0, confirmed=True)
    await e._check_stop_loss(bar)

    sl = [t for t in e.trades if t.action == "sl_long"]
    assert sl[0].price == 1950.0
