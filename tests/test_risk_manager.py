"""风控熔断——必须真的会拦

这些开关一度是死代码（on_pnl_update 零调用者），配置里的 max_daily_loss_pct
与 max_drawdown_pct 完全不起作用。
"""
import pytest

from engine.risk_manager import RiskManager
from gateway.models import OrderSide, OrderType, Position, PosSide, Signal
from strategies.rightside import RightSideStrategy
from tests.conftest import ETH_SWAP, FakePortfolio


@pytest.fixture
def risk():
    return RiskManager(max_daily_loss_pct=0.02, max_drawdown_pct=0.05)


@pytest.fixture
def signal():
    return Signal(inst_id=ETH_SWAP, side=OrderSide.BUY,
                  order_type=OrderType.MARKET, qty=1)


def allowed(risk, signal, name="s"):
    return risk.check_signal(signal, FakePortfolio(), name)[0]


def test_drawdown_triggers_emergency_stop(risk, signal):
    risk.on_equity_update(10_000.0)
    assert allowed(risk, signal)

    risk.on_equity_update(9_600.0)          # -4%
    assert allowed(risk, signal)

    risk.on_equity_update(9_400.0)          # -6% >= 5%
    ok, reason = risk.check_signal(signal, FakePortfolio(), "s")
    assert not ok and "drawdown" in reason.lower()


def test_emergency_stop_needs_manual_clear(risk, signal):
    risk.on_equity_update(10_000.0)
    risk.on_equity_update(9_000.0)
    assert risk.is_emergency
    risk.on_equity_update(10_000.0)
    assert risk.is_emergency, "权益回来了也不自动解除——需要人工确认"
    risk.clear_emergency()
    assert allowed(risk, signal)


def test_daily_loss_pauses_only_that_strategy(risk, signal):
    risk.on_equity_update(10_000.0)
    risk.on_realized_pnl("A", -120.0)       # 1.2%
    assert allowed(risk, signal, "A")

    risk.on_realized_pnl("A", -90.0)        # 累计 2.1% >= 2%
    assert not allowed(risk, signal, "A")
    assert allowed(risk, signal, "B"), "暂停应按策略隔离"


def test_profit_does_not_lift_pause(risk, signal):
    risk.on_equity_update(10_000.0)
    risk.on_realized_pnl("A", -300.0)
    assert not allowed(risk, signal, "A")
    risk.on_realized_pnl("A", +500.0)
    assert not allowed(risk, signal, "A"), "日亏损是单调累计，盈利不抵消"


def test_reset_daily_clears_pause(risk, signal):
    risk.on_equity_update(10_000.0)
    risk.on_realized_pnl("A", -300.0)
    assert not allowed(risk, signal, "A")

    risk.reset_daily()
    risk.on_equity_update(10_000.0)
    assert allowed(risk, signal, "A")


def test_equity_seeding_backfills_judgement(risk, signal):
    """权益还没播种时不能误判，播种后要把已发生的亏损补算进来"""
    risk.on_realized_pnl("A", -9999.0)
    assert not risk.paused_strategies

    risk.on_equity_update(10_000.0)
    assert "A" in risk.paused_strategies


def test_rate_limit(signal):
    risk = RiskManager(order_rate_limit=2)
    risk.on_equity_update(10_000.0)
    for _ in range(2):
        assert allowed(risk, signal)
        risk.on_order_sent("s")
    ok, reason = risk.check_signal(signal, FakePortfolio(), "s")
    assert not ok and "rate limit" in reason.lower()


# ── 策略上报链路 ──────────────────────────────────────────────────────────────

async def test_close_reports_realized_pnl(make_strategy, risk, fake_rest):
    risk.on_equity_update(10_000.0)
    pos = Position(inst_id=ETH_SWAP, pos_side=PosSide.LONG, size=200.0,
                   entry_price=3000.0, unrealized_pnl=-260.0)
    s = make_strategy(RightSideStrategy, rest=fake_rest,
                      portfolio=FakePortfolio(position=pos), risk=risk)
    s._state.open(PosSide.LONG, 3000.0, 2700.0)

    await s._execute_signal(s._close_signal(2870.0, "stop loss"))
    assert s.name in risk.paused_strategies, "亏损 2.6% > 2% 应暂停"


async def test_reduce_reports_scaled_pnl(make_strategy, fake_rest):
    risk = RiskManager(max_daily_loss_pct=0.99, max_drawdown_pct=0.99)
    risk.on_equity_update(10_000.0)
    pos = Position(inst_id=ETH_SWAP, pos_side=PosSide.LONG, size=200.0,
                   entry_price=3000.0, unrealized_pnl=-300.0)
    s = make_strategy(RightSideStrategy, rest=fake_rest,
                      portfolio=FakePortfolio(position=pos), risk=risk)
    s._state.open(PosSide.LONG, 3000.0, 2700.0)

    await s._execute_signal(s._reduce_signal("reduce 50%"))
    assert risk._daily_loss[s.name] == pytest.approx(150.0), "平一半只算一半亏损"


async def test_open_reports_nothing(make_strategy, fake_rest):
    risk = RiskManager()
    risk.on_equity_update(10_000.0)
    s = make_strategy(RightSideStrategy, rest=fake_rest, risk=risk)
    await s._execute_signal(s._open_long_signal(3000.0, 2700.0))
    assert not risk._daily_loss, "开仓不产生已实现盈亏"


# ── 最大使用资金：全局名义价值闸门 ────────────────────────────────────────────

def test_cap_notional_allows_within_limit():
    r = RiskManager(max_position_pct=0.3)
    assert r.cap_notional(0.0, 2000.0, 10_000.0) == 2000.0


def test_cap_notional_truncates_over_limit():
    r = RiskManager(max_position_pct=0.3)
    # 上限 3000，已有 1000，请求 5000 → 只允许 2000
    assert r.cap_notional(1000.0, 5000.0, 10_000.0) == pytest.approx(2000.0)


def test_cap_notional_returns_zero_when_full():
    r = RiskManager(max_position_pct=0.3)
    assert r.cap_notional(3000.0, 1000.0, 10_000.0) == 0.0


def test_cap_notional_disabled_when_pct_zero():
    r = RiskManager(max_position_pct=0.0)
    assert r.cap_notional(0.0, 999_999.0, 10_000.0) == 999_999.0


async def test_entry_is_capped_by_max_position(make_strategy, fake_rest):
    """策略要多少是策略的事，这道闸门保证单品种名义价值不超过权益的既定比例"""
    from strategies.rightside import RightSideStrategy
    risk = RiskManager(max_position_pct=0.3)
    risk.on_equity_update(10_000.0)
    s = make_strategy(RightSideStrategy, rest=fake_rest, risk=risk,
                      config={"position_size_pct": 0.5, "leverage": 5})

    await s._execute_signal(s._open_long_signal(3000.0, 2700.0))
    # 上限 3000 USDT 名义 / (ct_val 0.01 × 3000) = 100 张
    assert fake_rest.orders[-1].qty == pytest.approx(100.0)


async def test_close_is_not_capped(make_strategy, fake_rest):
    """平仓腿不能被名义上限截断，否则会平不干净留下残仓"""
    from strategies.rightside import RightSideStrategy
    risk = RiskManager(max_position_pct=0.01)      # 极紧的上限
    risk.on_equity_update(10_000.0)
    pos = Position(inst_id=ETH_SWAP, pos_side=PosSide.LONG, size=500.0,
                   entry_price=3000.0)
    s = make_strategy(RightSideStrategy, rest=fake_rest, risk=risk,
                      portfolio=FakePortfolio(position=pos))
    s._state.open(PosSide.LONG, 3000.0, 2700.0)

    await s._execute_signal(s._close_signal(3000.0, "close"))
    assert fake_rest.orders[-1].qty == 500.0
