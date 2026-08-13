"""HTML 报告：回合聚合与数据打包

图表本身不测（渲染在浏览器里），测的是喂给它的那份数据——尤其是把逐笔记录
合并成「回合」这步，多次加码 + 多次减仓的配对最容易错。
"""
import json
from datetime import UTC, datetime, timedelta

import pytest

from backtest.engine import TradeRecord
from backtest.html_report import _build_payload, export_html_report, group_rounds
from gateway.models import Candle

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _t(i, action, price, contracts, pnl=0.0, reason=""):
    return TradeRecord(ts=T0 + timedelta(hours=4 * i), action=action, price=price,
                       contracts=contracts, pnl=pnl, reason=reason)


# ── 回合聚合 ──────────────────────────────────────────────────────────────────

def test_single_open_close_is_one_round():
    r = group_rounds([_t(0, "open_long", 100, 10, reason="突破关键点"),
                      _t(5, "close_long", 120, 10, pnl=200.0)])
    assert len(r) == 1
    assert r[0].side == "long" and r[0].adds == 0
    assert r[0].entry_price == 100 and r[0].exit_price == 120
    assert r[0].pnl == 200.0
    assert r[0].exit_kind == "signal"
    assert r[0].open_reason == "突破关键点"


def test_pyramid_adds_collapse_into_one_round_with_weighted_entry():
    """三次加码算一个回合，开仓价取张数加权平均"""
    r = group_rounds([
        _t(0, "open_long", 100, 10),
        _t(1, "open_long", 110, 10),
        _t(2, "open_long", 120, 20),
        _t(9, "close_long", 130, 40, pnl=500.0),
    ])
    assert len(r) == 1
    assert r[0].adds == 2
    assert r[0].contracts == 40
    assert r[0].entry_price == pytest.approx((100 * 10 + 110 * 10 + 120 * 20) / 40)


def test_partial_exits_stay_in_the_same_round_until_flat():
    """分批止盈不算多个回合——只有净张数归零才结束"""
    r = group_rounds([
        _t(0, "open_long", 100, 30),
        _t(3, "reduce_long", 110, 10, pnl=100.0),
        _t(5, "reduce_long", 120, 10, pnl=200.0),
        _t(7, "close_long", 130, 10, pnl=300.0),
    ])
    assert len(r) == 1
    assert r[0].pnl == pytest.approx(600.0)
    assert r[0].exit_price == pytest.approx(120.0)   # 三次离场的加权均价


def test_consecutive_rounds_are_separated():
    r = group_rounds([
        _t(0, "open_long", 100, 10),
        _t(2, "close_long", 110, 10, pnl=100.0),
        _t(4, "open_long", 105, 10),
        _t(6, "sl_long", 95, 10, pnl=-100.0),
    ])
    assert [x.idx for x in r] == [1, 2]
    assert r[0].exit_kind == "signal"
    assert r[1].exit_kind == "stop"
    assert r[1].pnl == -100.0


def test_unclosed_tail_is_still_reported():
    """最后一段行情里仓位还没平，不能从报告里消失"""
    r = group_rounds([_t(0, "open_long", 100, 10), _t(1, "open_long", 110, 10)])
    assert len(r) == 1
    assert r[0].exit_kind == "open"
    assert r[0].close_ts is None and r[0].exit_price is None
    assert r[0].pnl == 0.0


def test_short_side_detected():
    r = group_rounds([_t(0, "open_short", 100, 10), _t(2, "close_short", 90, 10, pnl=100)])
    assert r[0].side == "short"


def test_no_trades_yields_no_rounds():
    assert group_rounds([]) == []


# ── 数据打包 ──────────────────────────────────────────────────────────────────

def _candles(n=10):
    return [Candle(ts=T0 + timedelta(hours=4 * i), open=100 + i, high=101 + i,
                   low=99 + i, close=100 + i, volume=1.0, confirmed=True)
            for i in range(n)]


def test_timestamps_render_as_utc8():
    """前端用 getUTC* 读，所以这里的 epoch 必须已经带上 +8 偏移"""
    p = _build_payload(_candles(2), [], [10_000.0], [T0], "s", 10_000.0, None)
    assert p["price"]["t"][0] == int(T0.timestamp() * 1000) + 8 * 3600 * 1000


def test_equity_curve_aligned_to_timestamps():
    """权益曲线首元素是初始资金、没有对应 K 线，必须对齐掉"""
    eq_ts = [T0 + timedelta(hours=4 * i) for i in range(3)]
    p = _build_payload(_candles(3), [], [10_000.0, 10_100.0, 10_200.0, 10_300.0],
                       eq_ts, "s", 10_000.0, None)
    assert len(p["equity"]["v"]) == len(p["equity"]["t"]) == 3
    assert p["equity"]["v"][0] == 10_100.0


def test_payload_is_json_serialisable_with_metrics():
    trades = [_t(0, "open_long", 100, 10, reason="突破"), _t(2, "sl_long", 90, 10, pnl=-100)]
    p = _build_payload(_candles(), trades, [10_000.0], [T0], "eth", 10_000.0,
                       {"sharpe": 0.4, "profit_factor": 1.3})
    json.dumps(p, ensure_ascii=False)
    assert p["metrics"]["sharpe"] == 0.4
    assert p["marks"][0]["open"] is True and p["marks"][1]["stop"] is True


# ── 落盘 ──────────────────────────────────────────────────────────────────────

def test_writes_self_contained_html(tmp_path):
    out = tmp_path / "sub" / "r.html"
    trades = [_t(0, "open_long", 100, 10, reason="突破 20 根高点"),
              _t(4, "close_long", 120, 10, pnl=200.0, reason="跌破 10 根低点")]
    path = export_html_report(_candles(), trades, [10_000.0, 10_200.0],
                              [T0, T0 + timedelta(hours=4)], "eth_livermore_swap",
                              10_000.0, {"sharpe": 0.4}, str(out))
    html = (tmp_path / "sub" / "r.html").read_text(encoding="utf-8")

    assert path == str(out)
    assert "__PAYLOAD__" not in html and "__TITLE__" not in html
    assert "<title>eth_livermore_swap</title>" in html
    assert "突破 20 根高点" in html
    # 自包含：不许有任何外链
    for bad in ("http://", "https://", "src=\"//", "@import"):
        assert bad not in html, f"报告引用了外部资源: {bad}"


def test_empty_candles_writes_nothing(tmp_path):
    out = tmp_path / "r.html"
    assert export_html_report([], [], [], [], "s", 10_000.0, None, str(out)) == ""
    assert not out.exists()
