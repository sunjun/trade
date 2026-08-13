"""BaseStrategy 的资金分配

所有策略共用一个 Portfolio，权益是整个账户的。不分配的话，同时启用 N 个策略
时每个都按全部权益算仓位——各自看着都合规，合起来是 N 倍风险。
"""
import pytest

from engine.base_strategy import BaseStrategy
from tests.conftest import FakePortfolio


class _Dummy(BaseStrategy):
    async def on_candle(self, candle):
        return []


def _mk(make_strategy, equity=10_000.0, **cfg):
    return make_strategy(_Dummy, config=cfg, portfolio=FakePortfolio(equity=equity))


def test_defaults_to_the_whole_account(make_strategy):
    s = _mk(make_strategy)
    assert s.equity_pct == 1.0 and s.max_equity is None
    assert s.strategy_equity() == pytest.approx(10_000.0)


def test_equity_pct_splits_the_account(make_strategy):
    s = _mk(make_strategy, equity_pct=0.3)
    assert s.strategy_equity() == pytest.approx(3_000.0)


def test_max_equity_caps_absolutely(make_strategy):
    """账户涨上去以后，绝对上限把本策略的额度钉住"""
    assert _mk(make_strategy, equity=50_000.0, max_equity=2_000.0
               ).strategy_equity() == pytest.approx(2_000.0)


def test_below_the_cap_actual_equity_is_used(make_strategy):
    assert _mk(make_strategy, equity=1_500.0, max_equity=2_000.0
               ).strategy_equity() == pytest.approx(1_500.0)


@pytest.mark.parametrize("equity,expected", [(10_000.0, 2_000.0), (3_000.0, 1_500.0)])
def test_both_limits_take_the_tighter_one(make_strategy, equity, expected):
    s = _mk(make_strategy, equity=equity, equity_pct=0.5, max_equity=2_000.0)
    assert s.strategy_equity() == pytest.approx(expected)


def test_negative_equity_floors_at_zero(make_strategy):
    assert _mk(make_strategy, equity=-500.0).strategy_equity() == 0.0


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.5])
def test_rejects_out_of_range_equity_pct(make_strategy, bad):
    with pytest.raises(ValueError, match="equity_pct"):
        _mk(make_strategy, equity_pct=bad)


def test_rejects_non_positive_max_equity(make_strategy):
    with pytest.raises(ValueError, match="max_equity"):
        _mk(make_strategy, max_equity=0)


# ── 与 25% 名义闸门的联动 ─────────────────────────────────────────────────────

async def test_position_cap_applies_to_the_allocation_not_the_account(
    make_strategy, fake_rest
):
    """闸门要按本策略的额度算

    否则分了 equity_pct=0.2 的策略仍然能占到全账户 25% 的名义，
    等于把额度加了 5 倍，分配形同虚设。
    """
    from gateway.models import OrderSide, OrderType, Signal

    s = make_strategy(_Dummy, config={"equity_pct": 0.2}, rest=fake_rest,
                      portfolio=FakePortfolio(equity=10_000.0))
    sig = Signal(inst_id=s.symbol, side=OrderSide.BUY,
                 order_type=OrderType.MARKET, qty=0)

    info = await fake_rest.get_instrument(s.symbol, s.inst_type)
    ticker = await fake_rest.get_ticker(s.symbol)
    unit_value = info.ct_val * ticker.last

    huge = 10_000.0 / unit_value           # 远超任何闸门的张数
    notional = await s._cap_by_max_position(sig, huge) * unit_value

    pct = s._risk.max_position_pct
    allocated = 10_000.0 * 0.2 * pct       # 按本策略额度
    account = 10_000.0 * pct               # 按整个账户（错误口径）
    assert notional <= allocated + 1e-9
    assert notional > allocated * 0.8, "只该被 lot_sz 取整少掉一点"
    assert notional < account * 0.5, f"闸门按账户全额算了：{notional} vs {allocated}"
