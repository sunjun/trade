"""回测数据缓存

缓存只做增量更新（向前补新）时，调大 --max-bars 完全不起作用：
永远停在上次那个窗口宽度上，而报告里看不出样本被截短了。
"""
from datetime import UTC, datetime, timedelta

import pytest

from backtest import data_loader
from backtest.data_loader import _cache_path, _save_cache, fetch_all_candles
from gateway.models import Candle

INST, TF = "ETH-USDT-SWAP", "15m"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _candles(start: datetime, n: int) -> list[Candle]:
    return [
        Candle(ts=start + timedelta(minutes=15 * i), open=100.0, high=101.0,
               low=99.0, close=100.5, volume=1.0, confirmed=True)
        for i in range(n)
    ]


@pytest.fixture
def cache_dir(tmp_path):
    return str(tmp_path)


@pytest.fixture
def fake_download(monkeypatch):
    """替掉网络层，记录每次调用的参数。"""
    calls = []

    async def _fake(inst_id, bar, max_candles, stop_after_ts=None, start_after=None):
        calls.append({"max_candles": max_candles, "stop_after_ts": stop_after_ts,
                      "start_after": start_after})
        if start_after is not None:                      # 回补历史
            oldest = datetime.fromtimestamp(start_after / 1000, tz=UTC)
            return _candles(oldest - timedelta(minutes=15 * max_candles), max_candles)
        if stop_after_ts is not None:                    # 增量补新
            return []
        return _candles(T0, max_candles)                 # 全量下载

    monkeypatch.setattr(data_loader, "_download_backward", _fake)
    return calls


async def test_first_run_downloads_full_window(cache_dir, fake_download):
    got = await fetch_all_candles(INST, TF, max_candles=300, cache_dir=cache_dir)
    assert len(got) == 300
    assert fake_download[0]["start_after"] is None


async def test_backfills_when_cache_is_shorter_than_requested(cache_dir, fake_download):
    """上次用 --max-bars 100 建的缓存，这次要 300 根，得把更早的 200 根补回来"""
    _save_cache(_cache_path(INST, TF, cache_dir), _candles(T0, 100))

    got = await fetch_all_candles(INST, TF, max_candles=300, cache_dir=cache_dir)

    assert len(got) == 300, "缓存不足时必须回补历史，而不是静默返回旧窗口"
    assert got == sorted(got, key=lambda c: c.ts)
    assert len({c.ts for c in got}) == 300, "回补的数据不能和缓存重叠"
    backfill = [c for c in fake_download if c["start_after"] is not None]
    assert len(backfill) == 1 and backfill[0]["max_candles"] == 200


async def test_backfill_persists_to_cache(cache_dir, fake_download):
    _save_cache(_cache_path(INST, TF, cache_dir), _candles(T0, 100))
    await fetch_all_candles(INST, TF, max_candles=300, cache_dir=cache_dir)

    # 再跑一次：缓存已经够长，不该再触发回补
    fake_download.clear()
    got = await fetch_all_candles(INST, TF, max_candles=300, cache_dir=cache_dir)
    assert len(got) == 300
    assert not [c for c in fake_download if c["start_after"] is not None]


async def test_no_backfill_when_cache_already_long_enough(cache_dir, fake_download):
    _save_cache(_cache_path(INST, TF, cache_dir), _candles(T0, 500))
    got = await fetch_all_candles(INST, TF, max_candles=300, cache_dir=cache_dir)
    assert len(got) == 300, "多余的部分截断掉，保留最新的"
    assert not [c for c in fake_download if c["start_after"] is not None]


async def test_exhausted_history_returns_what_exists(cache_dir, monkeypatch, caplog):
    """交易所没有更早的数据时不能死循环，且要明说样本比请求的短"""
    async def _fake(inst_id, bar, max_candles, stop_after_ts=None, start_after=None):
        if stop_after_ts is not None or start_after is not None:
            return []
        return _candles(T0, 50)

    monkeypatch.setattr(data_loader, "_download_backward", _fake)
    _save_cache(_cache_path(INST, TF, cache_dir), _candles(T0, 50))

    got = await fetch_all_candles(INST, TF, max_candles=300, cache_dir=cache_dir)
    assert len(got) == 50
