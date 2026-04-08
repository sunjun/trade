#!/usr/bin/env python3
"""OKX K线实时图表 — EMA5 / EMA9 / EMA21

用法:
  python chart.py                      # 默认 BTC-USDT 5m
  python chart.py ETH-USDT 15m
  python chart.py BTC-USDT 1H --limit 300
"""
import argparse
import asyncio
import sys
import threading
from collections import deque
from pathlib import Path

import matplotlib.animation as animation
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt

# 确保能找到项目模块（从项目根目录运行时无需额外配置）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

load_dotenv()

from config.settings import settings
from gateway.models import Candle
from gateway.okx_rest import OKXRestClient
from gateway.okx_ws import OKXWebSocketClient

# ── 共享状态（asyncio 线程写，matplotlib 主线程读）─────────────────────────────
_lock = threading.Lock()
_buf: deque[Candle] = deque(maxlen=500)
_dirty = threading.Event()   # 有新数据时 set，绘制后 clear

# ── 主题色 ─────────────────────────────────────────────────────────────────────
BG      = '#131722'
UP      = '#26a69a'   # 阳线绿
DN      = '#ef5350'   # 阴线红
EMA_CLR = {5: '#f6e05e', 9: '#68d391', 21: '#fc8181'}  # 黄/绿/红
BAR_W   = 0.6         # K线实体宽度


# ── EMA 增量计算（全量重算，足够快）──────────────────────────────────────────────

def _ema(closes: list[float], period: int) -> list[float | None]:
    k = 2.0 / (period + 1)
    result: list[float | None] = [None] * len(closes)
    val: float | None = None
    seed: list[float] = []
    for i, c in enumerate(closes):
        if val is None:
            seed.append(c)
            if len(seed) == period:
                val = sum(seed) / period
                result[i] = val
        else:
            val = c * k + val * (1 - k)
            result[i] = val
    return result


def _plot_ema(ax, vals, period):
    pairs = [(i, v) for i, v in enumerate(vals) if v is not None]
    if not pairs:
        return
    xs, ys = zip(*pairs)
    ax.plot(xs, ys, color=EMA_CLR[period], lw=1.3, zorder=3,
            label=f'EMA{period}  {ys[-1]:.2f}')


# ── 绘图 ───────────────────────────────────────────────────────────────────────

def _draw(ax_c: plt.Axes, ax_v: plt.Axes, symbol: str, tf: str):
    with _lock:
        candles = list(_buf)
    if not candles:
        return

    n = len(candles)
    closes = [c.close for c in candles]

    # ── 清空旧内容 ─────────────────────────────────────────────────────────────
    ax_c.clear()
    ax_v.clear()

    for ax in (ax_c, ax_v):
        ax.set_facecolor(BG)
        ax.grid(True, alpha=0.1, color='white', lw=0.4)
        ax.tick_params(colors='#aaaaaa', labelsize=7)
        for sp in ax.spines.values():
            sp.set_color('#2a2a2a')

    # ── K线实体 + 影线 ─────────────────────────────────────────────────────────
    for i, c in enumerate(candles):
        up = c.close >= c.open
        color = UP if up else DN
        lo, hi = (min(c.open, c.close), max(c.open, c.close))
        hi = max(hi, lo + 1e-9)
        ax_c.plot([i, i], [c.low, c.high], color=color, lw=0.8, zorder=1)
        ax_c.add_patch(mpatches.Rectangle(
            (i - BAR_W / 2, lo), BAR_W, hi - lo,
            fc=color, ec=color, zorder=2,
        ))

    # ── EMA 线 ─────────────────────────────────────────────────────────────────
    for period in (5, 9, 21):
        _plot_ema(ax_c, _ema(closes, period), period)

    # ── 当前价格虚线 ───────────────────────────────────────────────────────────
    last = candles[-1]
    p_color = UP if last.close >= last.open else DN
    ax_c.axhline(last.close, color=p_color, lw=0.7, ls='--', alpha=0.75, zorder=1)
    ax_c.text(n + 0.3, last.close, f'{last.close:.2f}',
              color=p_color, va='center', fontsize=8, fontweight='bold')

    # ── 标题 ───────────────────────────────────────────────────────────────────
    chg = last.close - last.open
    chg_pct = chg / last.open * 100 if last.open else 0
    arrow = '▲' if chg >= 0 else '▼'
    ax_c.set_title(
        f'{symbol}  [{tf}]    '
        f'O {last.open:.2f}  H {last.high:.2f}  L {last.low:.2f}  C {last.close:.2f}  '
        f'{arrow} {chg:+.2f} ({chg_pct:+.2f}%)    '
        f'{last.ts.strftime("%Y-%m-%d %H:%M")} UTC',
        color='white', fontsize=9, loc='left', pad=5,
    )
    ax_c.legend(
        loc='upper left', fontsize=8,
        facecolor='#1a1a2e', labelcolor='white',
        framealpha=0.75, borderpad=0.5,
    )
    ax_c.set_xlim(-1, n + 1.5)
    ax_c.tick_params(labelbottom=False)

    # ── 成交量柱 ───────────────────────────────────────────────────────────────
    for i, c in enumerate(candles):
        ax_v.bar(i, c.volume, color=(UP if c.close >= c.open else DN),
                 alpha=0.55, width=BAR_W)
    ax_v.set_xlim(-1, n + 1.5)
    ax_v.set_ylabel('Vol', color='#888', fontsize=7)

    # ── X 轴时间标签（仅在成交量区显示）──────────────────────────────────────
    step = max(1, n // 10)
    ticks = list(range(0, n, step))
    ax_v.set_xticks(ticks)
    ax_v.set_xticklabels(
        [candles[i].ts.strftime('%m/%d\n%H:%M') for i in ticks],
        fontsize=6,
    )


# ── asyncio 后台任务 ───────────────────────────────────────────────────────────

async def _load_history(symbol: str, tf: str, limit: int):
    okx = settings.okx
    # 行情接口无需签名；is_demo=False 避免模拟盘 WS 鉴权问题
    async with OKXRestClient(okx.api_key, okx.secret_key, okx.passphrase,
                             is_demo=False) as rest:
        candles = await rest.get_candles(symbol, tf, limit=limit)
    with _lock:
        _buf.clear()
        _buf.extend(candles)
    _dirty.set()
    print(f"✓ Loaded {len(candles)} historical candles [{symbol} {tf}]")


async def _on_candle(new_candles: list[Candle]):
    """WebSocket 推送回调（实时 + 收盘 K 线）"""
    with _lock:
        for c in new_candles:
            if _buf and c.ts == _buf[-1].ts:
                _buf[-1] = c          # 更新当前未收盘K线
            elif not _buf or c.ts > _buf[-1].ts:
                _buf.append(c)        # 新K线
    _dirty.set()


async def _ws_loop(symbol: str, tf: str):
    okx = settings.okx
    ws = OKXWebSocketClient(okx.api_key, okx.secret_key, okx.passphrase,
                            is_demo=False)
    ws.subscribe_candles(symbol, tf, _on_candle)
    await ws.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await ws.stop()


def _async_thread_main(symbol: str, tf: str, limit: int):
    async def _main():
        await _load_history(symbol, tf, limit)
        await _ws_loop(symbol, tf)

    asyncio.run(_main())


# ── 主程序 ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='OKX K线实时图表  EMA5/9/21',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('symbol', nargs='?', default='BTC-USDT',
                        help='交易对 (default: BTC-USDT)')
    parser.add_argument('timeframe', nargs='?', default='5m',
                        help='K线周期: 1m 3m 5m 15m 30m 1H 4H 1D (default: 5m)')
    parser.add_argument('--limit', type=int, default=200,
                        help='加载历史K线条数 (default: 200)')
    args = parser.parse_args()

    symbol = args.symbol.upper()
    tf = args.timeframe

    # 后台线程运行 asyncio（REST 历史拉取 + WS 实时订阅）
    t = threading.Thread(
        target=_async_thread_main,
        args=(symbol, tf, args.limit),
        daemon=True,
    )
    t.start()

    print(f"Loading {symbol} [{tf}] — waiting for data...")
    if not _dirty.wait(timeout=20):
        print("ERROR: Timeout waiting for data. Check network / API config.")
        sys.exit(1)

    # ── Matplotlib 主线程 ──────────────────────────────────────────────────────
    plt.style.use('dark_background')
    fig = plt.figure(figsize=(16, 9), facecolor=BG)
    try:
        fig.canvas.manager.set_window_title(f'{symbol}  {tf} — OKX Chart')
    except Exception:
        pass

    # 主图 7/10，成交量 3/10，留少量间距
    gs = fig.add_gridspec(10, 1, hspace=0.06, left=0.06, right=0.95,
                          top=0.95, bottom=0.08)
    ax_c = fig.add_subplot(gs[:7])
    ax_v = fig.add_subplot(gs[7:])

    _draw(ax_c, ax_v, symbol, tf)

    def _frame(_):
        if _dirty.is_set():
            _dirty.clear()
            _draw(ax_c, ax_v, symbol, tf)
            fig.canvas.draw_idle()

    # 每 500ms 检查一次是否有新数据
    _ani = animation.FuncAnimation(fig, _frame, interval=500,
                                   cache_frame_data=False)

    print("Chart ready. Close the window to exit.")
    plt.show()


if __name__ == '__main__':
    main()
