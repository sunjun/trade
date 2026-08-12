"""订单归属：clOrdId 标签 → 策略

OKX 的 clOrdId 只接受字母数字，旧代码 `clOrdId.split("_")[0]` 的约定
在 OKX 上根本不可能成立，归属永远为空，引擎退化成按品种路由——
同品种跑两个策略就会互相收到对方的订单回报。
"""
from types import SimpleNamespace

import pytest

from engine.base_strategy import CLIENT_TAG_LEN, make_client_tag
from engine.strategy_engine import StrategyEngine
from gateway.models import Order, OrderSide, OrderType
from strategies.rightside import RightSideStrategy
from tests.conftest import ETH_SWAP


def test_tag_is_fixed_length_alphanumeric():
    tag = make_client_tag("eth_rightside_swap")
    assert len(tag) == CLIENT_TAG_LEN == 12
    assert tag.isalnum(), "OKX 只接受字母数字"
    assert tag[:8] == "ethright", "前 8 位保留可读性，便于在 OKX 后台辨认"


def test_tag_is_deterministic():
    assert make_client_tag("eth_rightside_swap") == make_client_tag("eth_rightside_swap")


@pytest.mark.parametrize("a,b", [
    ("eth_rightside_swap", "btc_rightside_swap"),
    ("eth_trend_v1", "eth_trend_v2"),          # 前 8 位相同，靠哈希区分
    ("a", "b"),                                 # 超短名要补齐到定长
])
def test_tags_are_distinct(a, b):
    ta, tb = make_client_tag(a), make_client_tag(b)
    assert ta != tb
    assert len(ta) == len(tb) == CLIENT_TAG_LEN
    assert ta.isalnum() and tb.isalnum()


async def test_clordid_is_generated_and_within_limit(make_strategy, fake_rest):
    s = make_strategy(RightSideStrategy, rest=fake_rest, name="eth_rightside_swap")
    await s._execute_signal(s._open_long_signal(3000.0, 2700.0))

    cloid = fake_rest.orders[-1].client_order_id
    assert cloid.startswith(s.client_tag)
    assert len(cloid) <= 32, "OKX 上限 32 字符"
    assert cloid.isalnum()


async def test_clordid_is_unique_per_order(make_strategy, fake_rest):
    s = make_strategy(RightSideStrategy, rest=fake_rest)
    for _ in range(3):
        await s._execute_signal(s._open_long_signal(3000.0, 2700.0))
    ids = [o.client_order_id for o in fake_rest.orders]
    assert len(set(ids)) == len(ids)


# ── 引擎路由 ──────────────────────────────────────────────────────────────────

class _Engine(SimpleNamespace):
    _strategy_for_order = StrategyEngine._strategy_for_order


def _strategy(name, symbol=ETH_SWAP):
    return SimpleNamespace(name=name, symbol=symbol, client_tag=make_client_tag(name))


def _order(client_order_id="", inst_id=ETH_SWAP):
    return Order(inst_id=inst_id, side=OrderSide.BUY, order_type=OrderType.MARKET,
                 qty=1.0, order_id="Z1", client_order_id=client_order_id)


@pytest.fixture
def two_on_same_symbol():
    s1, s2 = _strategy("eth_rightside_swap"), _strategy("eth_trend_swap")
    return _Engine(_strategies=[s1, s2],
                   _strategy_by_tag={s1.client_tag: s1, s2.client_tag: s2}), s1, s2


def test_routes_by_tag(two_on_same_symbol):
    engine, _, s2 = two_on_same_symbol
    o = _order(s2.client_tag + "1700000000000001")
    assert engine._strategy_for_order(o) is s2


def test_refuses_to_guess_when_ambiguous(two_on_same_symbol):
    """外部手工下单没有我方标签，同品种多策略时不能瞎归属"""
    engine, _, _ = two_on_same_symbol
    assert engine._strategy_for_order(_order()) is None


def test_falls_back_to_symbol_when_unique():
    s1 = _strategy("eth_rightside_swap")
    engine = _Engine(_strategies=[s1], _strategy_by_tag={s1.client_tag: s1})
    assert engine._strategy_for_order(_order()) is s1


def test_unknown_symbol_is_unattributed():
    s1 = _strategy("eth_rightside_swap")
    engine = _Engine(_strategies=[s1], _strategy_by_tag={s1.client_tag: s1})
    assert engine._strategy_for_order(_order(inst_id="SOL-USDT-SWAP")) is None
