"""从 OKX 拉取历史 K 线数据（分页 + 本地 CSV 缓存）

缓存策略：
  - 缓存目录：backtest_cache/（与项目根目录同级，可通过 cache_dir 参数自定义）
  - 文件名：{cache_dir}/{inst_id}_{timeframe}.csv
  - 首次下载：全量拉取，保存到本地
  - 再次运行：读取本地缓存，仅从 OKX 增量补充"上次缓存最新时间戳之后"的数据
  - 强制刷新：传入 force_download=True（或手动删除缓存文件）

OKX history-candles 接口：
  /api/v5/market/history-candles
  - after  : 返回该时间戳之前的数据（毫秒，用于向历史翻页）
  - limit  : 单次最多 100 条
  - 数据按时间降序返回（最新在前）

数据可用深度（参考）：
  15m  ≈ 6 个月   (约 17000 根)
  1H   ≈ 1.5 年   (约 13000 根)
  4H   ≈ 5 年     (约 11000 根)
"""
from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from loguru import logger

from gateway.models import Candle

BASE_URL = "https://www.okx.com"
TIMEFRAME_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1H": "1H", "2H": "2H", "4H": "4H", "6H": "6H", "12H": "12H",
    "1D": "1D", "1W": "1W",
}

_CSV_HEADER = ["ts_ms", "open", "high", "low", "close", "volume"]


# ── 缓存读写 ───────────────────────────────────────────────────────────────────

def _cache_path(inst_id: str, timeframe: str, cache_dir: str) -> Path:
    safe_id = inst_id.replace("/", "-")
    return Path(cache_dir) / f"{safe_id}_{timeframe}.csv"


def _candle_to_row(c: Candle) -> list:
    return [
        int(c.ts.timestamp() * 1000),
        c.open, c.high, c.low, c.close, c.volume,
    ]


def _row_to_candle(row: list) -> Candle:
    return Candle(
        ts=datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        confirmed=True,
    )


def _load_cache(path: Path) -> list[Candle]:
    candles: list[Candle] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # 跳过 header
        for row in reader:
            if len(row) >= 6:
                candles.append(_row_to_candle(row))
    # 确保升序
    candles.sort(key=lambda c: c.ts)
    return candles


def _save_cache(path: Path, candles: list[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(_CSV_HEADER)
        for c in candles:
            writer.writerow(_candle_to_row(c))


# ── OKX 网络请求 ───────────────────────────────────────────────────────────────

def _parse_okx_row(row: list) -> Candle:
    confirm = row[8] if len(row) > 8 else "1"
    return Candle(
        ts=datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        confirmed=(confirm == "1"),
    )


async def _fetch_page(
    session: aiohttp.ClientSession,
    inst_id: str,
    bar: str,
    after: int | None = None,
    limit: int = 100,
) -> list[list]:
    params: dict = {"instId": inst_id, "bar": bar, "limit": str(limit)}
    if after is not None:
        params["after"] = str(after)
    url = f"{BASE_URL}/api/v5/market/history-candles"
    async with session.get(url, params=params) as resp:
        data = await resp.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX API error [{data.get('code')}]: {data.get('msg')}")
    return data.get("data", [])


async def _download_backward(
    inst_id: str,
    bar: str,
    max_candles: int,
    stop_after_ts: int | None = None,
) -> list[Candle]:
    """
    从最新时刻向历史分页下载，直到：
      - 达到 max_candles 根，或
      - 拉到 stop_after_ts（含）之前（用于增量更新）
    返回升序列表。
    """
    all_rows: list[list] = []
    after: int | None = None
    page = 0

    async with aiohttp.ClientSession() as session:
        while len(all_rows) < max_candles:
            rows = await _fetch_page(session, inst_id, bar, after=after)
            if not rows:
                break

            # 增量模式：若本页最旧数据已早于目标时间，只保留新的部分
            if stop_after_ts is not None:
                kept = [r for r in rows if int(r[0]) > stop_after_ts]
                all_rows.extend(kept)
                if len(kept) < len(rows):
                    # 已覆盖到缓存末尾，停止翻页
                    break
            else:
                all_rows.extend(rows)

            page += 1
            oldest_ts = int(rows[-1][0])
            after = oldest_ts

            logger.debug(
                f"  Page {page}: {len(rows)} rows, "
                f"oldest={datetime.fromtimestamp(oldest_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}, "
                f"total_new={len(all_rows)}"
            )

            if len(rows) < 100:
                break  # 没有更多数据

            await asyncio.sleep(0.25)

    # 解析 + 去重 + 升序
    seen: set[int] = set()
    candles: list[Candle] = []
    for row in all_rows:
        ts_ms = int(row[0])
        if ts_ms not in seen:
            seen.add(ts_ms)
            c = _parse_okx_row(row)
            c.confirmed = True
            candles.append(c)

    candles.sort(key=lambda c: c.ts)
    return candles


# ── 主入口 ────────────────────────────────────────────────────────────────────

async def fetch_all_candles(
    inst_id: str,
    timeframe: str,
    max_candles: int = 20_000,
    cache_dir: str = "backtest_cache",
    force_download: bool = False,
) -> list[Candle]:
    """
    拉取最多 max_candles 根历史 K 线，优先使用本地缓存。

    Args:
        inst_id:        合约/现货 ID，例如 "ETH-USDT-SWAP"
        timeframe:      时框，例如 "15m", "1H", "4H"
        max_candles:    最多保留的 K 线数（保留最新部分）
        cache_dir:      本地缓存目录
        force_download: True 时忽略缓存，重新全量下载
    """
    bar = TIMEFRAME_MAP.get(timeframe, timeframe)
    path = _cache_path(inst_id, timeframe, cache_dir)

    cached: list[Candle] = []
    stop_after_ts: int | None = None

    # ── 读取本地缓存 ──────────────────────────────────────────────────────────
    if path.exists() and not force_download:
        logger.info(f"Loading cache: {path}")
        cached = _load_cache(path)
        if cached:
            newest_ts_ms = int(cached[-1].ts.timestamp() * 1000)
            stop_after_ts = newest_ts_ms
            logger.info(
                f"Cache: {len(cached)} candles, "
                f"{cached[0].ts.strftime('%Y-%m-%d')} → {cached[-1].ts.strftime('%Y-%m-%d')}"
            )

    # ── 判断是否需要下载 ──────────────────────────────────────────────────────
    need_full = not cached or force_download
    if need_full:
        logger.info(f"Downloading {inst_id} {timeframe} (up to {max_candles} candles)...")
        new_candles = await _download_backward(inst_id, bar, max_candles)
    else:
        logger.info(f"Incremental update for {inst_id} {timeframe} (since {cached[-1].ts.strftime('%Y-%m-%d %H:%M')})...")
        new_candles = await _download_backward(inst_id, bar, max_candles, stop_after_ts=stop_after_ts)
        if new_candles:
            logger.info(f"Got {len(new_candles)} new candles")
        else:
            logger.info("Already up to date, no new candles")

    # ── 合并 & 去重 ───────────────────────────────────────────────────────────
    all_candles = cached + new_candles
    seen: set = set()
    merged: list[Candle] = []
    for c in all_candles:
        ts_ms = int(c.ts.timestamp() * 1000)
        if ts_ms not in seen:
            seen.add(ts_ms)
            merged.append(c)
    merged.sort(key=lambda c: c.ts)

    # 截断到 max_candles（保留最新的）
    if len(merged) > max_candles:
        merged = merged[-max_candles:]

    # ── 保存更新后的缓存 ──────────────────────────────────────────────────────
    if new_candles or need_full:
        _save_cache(path, merged)
        logger.info(f"Cache saved: {len(merged)} candles → {path}")

    logger.info(
        f"Ready: {len(merged)} candles for {inst_id} {timeframe}  "
        f"({merged[0].ts.strftime('%Y-%m-%d')} → {merged[-1].ts.strftime('%Y-%m-%d')})"
    )
    return merged
