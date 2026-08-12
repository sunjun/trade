"""未收盘K线绝不能进入指标

OKX 的 candle 频道在一根K线未收盘期间会随每笔成交推送。若把这些盘中快照
喂给指标，EMA/MACD/ATR 会在同一根K线内被更新成百上千次，实盘指标完全失真，
且与只喂收盘K线的回测行为不一致。
"""
from datetime import UTC, datetime

import pytest

from gateway.models import Candle
from strategies.bbrsi import BbRsiStrategy
from strategies.donchian import DonchianStrategy
from strategies.mtftrend import MtfTrendStrategy
from strategies.rightside import RightSideStrategy
from strategies.trend import TrendStrategy
from strategies.vwap import VwapStrategy

ALL_STRATEGIES = [
    TrendStrategy, RightSideStrategy, BbRsiStrategy,
    DonchianStrategy, VwapStrategy, MtfTrendStrategy,
]


def _tick(ts, close=9999.0, confirmed=False):
    return Candle(ts=ts, open=close, high=close + 1, low=close - 1,
                  close=close, volume=50.0, confirmed=confirmed)


async def test_handle_candle_drops_unconfirmed(make_strategy, candles):
    """引擎入口 handle_candle 就该把未收盘K线挡掉"""
    s = make_strategy(TrendStrategy)
    for c in candles(60):
        await s.handle_candle([c])
    settled = s._ema_fast.value
    assert settled is not None

    ts = datetime(2026, 3, 1, tzinfo=UTC)
    for _ in range(200):
        await s.handle_candle([_tick(ts)])

    assert s._ema_fast.value == settled, "盘中推送污染了 EMA"


async def test_confirmed_candle_advances_once(make_strategy, candles):
    s = make_strategy(TrendStrategy)
    for c in candles(60):
        await s.handle_candle([c])
    before = s._ema_fast.value

    ts = datetime(2026, 3, 1, tzinfo=UTC)
    for _ in range(50):
        await s.handle_candle([_tick(ts, close=3400.0)])
    assert s._ema_fast.value == before

    await s.handle_candle([_tick(ts, close=3400.0, confirmed=True)])
    assert s._ema_fast.value != before, "收盘后指标应推进"


@pytest.mark.parametrize("cls", ALL_STRATEGIES, ids=lambda c: c.__name__)
async def test_on_candle_guards_directly(cls, make_strategy, candles):
    """预热(strategy_engine)与回测都直接调用 on_candle，绕过 handle_candle，
    所以每个策略自己也必须挡住未收盘K线。"""
    s = make_strategy(cls, config={"grid_lower": 2500, "grid_upper": 3500})
    for c in candles(80, tf_hours=1):
        await s.on_candle(c)

    snapshot = _indicator_snapshot(s)
    ts = datetime(2026, 3, 1, tzinfo=UTC)
    for _ in range(50):
        await s.on_candle(_tick(ts))

    assert _indicator_snapshot(s) == snapshot, f"{cls.__name__} 未挡住未收盘K线"


async def test_mtf_extra_timeframes_guarded(make_strategy, candles):
    """mtftrend 的 1H/4H 回调由引擎直接订阅，不经过 handle_candle"""
    s = make_strategy(MtfTrendStrategy)
    for c in candles(80, tf_hours=4):
        await s._handle_h4([c])
    for c in candles(80, tf_hours=1):
        await s._handle_h1([c])

    h4_before = (s._h4.ema_fast.value, s._h4.ema_slow.value)
    h1_before = (s._h1.ema_fast.value, s._h1.macd.hist)

    ts = datetime(2026, 3, 1, tzinfo=UTC)
    for _ in range(50):
        await s._handle_h4([_tick(ts)])
        await s._handle_h1([_tick(ts)])

    assert (s._h4.ema_fast.value, s._h4.ema_slow.value) == h4_before
    assert (s._h1.ema_fast.value, s._h1.macd.hist) == h1_before


def _indicator_snapshot(strategy):
    """把策略上所有指标的当前值抓成一个可比较的元组"""
    values = []
    for attr in sorted(vars(strategy)):
        obj = getattr(strategy, attr)
        for field in ("value", "hist", "middle", "upper", "lower", "highest", "lowest"):
            v = getattr(obj, field, None)
            if isinstance(v, (int, float)):
                values.append((attr, field, v))
    return tuple(values)
