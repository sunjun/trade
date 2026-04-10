#!/usr/bin/env python3
"""OKX K线实时图表 — 可配置 EMA

用法:
  python chart.py                           # 默认 BTC-USDT 5m EMA5/9/21
  python chart.py ETH-USDT 15m
  python chart.py BTC-USDT 1H --limit 300
  python chart.py BTC-USDT 5m --ema 9 21   # 只显示 EMA9 和 EMA21
  python chart.py BTC-USDT 5m --ema 10 20 60
  python chart.py BTC-USDT 5m --ema        # 不显示任何 EMA

交互:
  左键单击 K线     显示该K线 OHLC + EMA 信息
  右键 / 点空白区  取消选中
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
import matplotlib.transforms as mtrans

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

load_dotenv()

from config.settings import settings
from gateway.models import Candle
from gateway.okx_rest import OKXRestClient
from gateway.okx_ws import OKXWebSocketClient

# ── 共享状态 ───────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_buf: deque[Candle] = deque(maxlen=500)
_dirty = threading.Event()

# 选中的 K 线索引（None = 未选中），用 dict 以便在闭包内修改
_selected: dict = {'idx': None}

# ── 主题色 ─────────────────────────────────────────────────────────────────────
BG    = '#131722'
UP    = '#26a69a'
DN    = '#ef5350'
BAR_W = 0.6

# EMA 颜色按索引轮换，支持任意数量
_EMA_PALETTE = ['#f6e05e', '#68d391', '#fc8181', '#76e4f7', '#d6bcfa', '#fbb6ce']

def _ema_color(i: int) -> str:
    return _EMA_PALETTE[i % len(_EMA_PALETTE)]


# ── EMA 计算 ───────────────────────────────────────────────────────────────────

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


# ── 绘制 EMA 折线 ──────────────────────────────────────────────────────────────

def _plot_ema(ax, vals, period, color):
    pairs = [(i, v) for i, v in enumerate(vals) if v is not None]
    if not pairs:
        return
    xs, ys = zip(*pairs)
    ax.plot(xs, ys, color=color, lw=1.3, zorder=3,
            label=f'EMA{period}  {ys[-1]:.2f}')


# ── 选中K线标注（每次 _draw 末尾调用，随刷新自动重绘）─────────────────────────

def _draw_selection(ax_c: plt.Axes, ax_v: plt.Axes,
                    candles: list[Candle],
                    emas: dict[int, list],
                    ema_colors: dict[int, str],
                    idx: int):
    c = candles[idx]
    n = len(candles)

    # ── 竖线贯穿主图和成交量区 ─────────────────────────────────────────────────
    for ax in (ax_c, ax_v):
        ax.axvline(idx, color='#ffffff', lw=0.8, alpha=0.35, zorder=4)

    # ── 在各 EMA 线上标圆点 ────────────────────────────────────────────────────
    for period, vals in emas.items():
        v = vals[idx]
        if v is not None:
            ax_c.plot(idx, v, 'o', color=ema_colors[period], ms=5, zorder=6,
                      markeredgecolor='white', markeredgewidth=0.4)

    # ── 成交量高亮（叠加半透明白色柱）────────────────────────────────────────
    ax_v.bar(idx, c.volume, color='white', alpha=0.18,
             width=BAR_W + 0.15, zorder=4)

    # ── 信息文本框 ─────────────────────────────────────────────────────────────
    chg = c.close - c.open
    chg_pct = chg / c.open * 100 if c.open else 0
    arrow = '▲' if chg >= 0 else '▼'

    def _fmt(label, val):
        return f'{label:<5} {val:>14.4f}'

    sep = '─' * 21
    lines = [
        c.ts.strftime('%Y-%m-%d  %H:%M'),
        sep,
        _fmt('O', c.open),
        _fmt('H', c.high),
        _fmt('L', c.low),
        _fmt('C', c.close),
        f'{arrow}    {chg:>+13.4f}  ({chg_pct:>+.2f}%)',
        _fmt('Vol', c.volume),
    ]
    if emas:
        lines.append(sep)
        for period, vals in emas.items():
            v = vals[idx]
            val_str = f'{v:>14.4f}' if v is not None else f'{"—":>14}'
            lines.append(f'EMA{period:<3} {val_str}')

    text = '\n'.join(lines)

    # 混合坐标：x 用数据坐标，y 用 axes 比例坐标（始终在顶部）
    transform = mtrans.blended_transform_factory(ax_c.transData, ax_c.transAxes)

    # 右侧 40% 的 K 线：信息框显示在左边，否则显示在右边
    if idx > n * 0.6:
        x_anchor, ha = idx - 1.2, 'right'
    else:
        x_anchor, ha = idx + 1.2, 'left'

    ax_c.text(
        x_anchor, 0.975,
        text,
        transform=transform,
        fontsize=7.5,
        color='white',
        ha=ha, va='top',
        fontfamily='monospace',
        linespacing=1.45,
        bbox=dict(
            boxstyle='round,pad=0.55',
            facecolor='#0d1117',
            edgecolor='#4a4a6a',
            alpha=0.93,
        ),
        zorder=7,
    )

    # ── 在 K 线顶部/底部标出价格 ───────────────────────────────────────────────
    price_color = UP if c.close >= c.open else DN
    ax_c.annotate(
        f'{c.high:.2f}',
        xy=(idx, c.high), xytext=(0, 6),
        textcoords='offset points',
        ha='center', va='bottom',
        fontsize=6.5, color='#cccccc',
        zorder=6,
    )
    ax_c.annotate(
        f'{c.low:.2f}',
        xy=(idx, c.low), xytext=(0, -6),
        textcoords='offset points',
        ha='center', va='top',
        fontsize=6.5, color='#cccccc',
        zorder=6,
    )


# ── 主绘图函数 ─────────────────────────────────────────────────────────────────

def _draw(ax_c: plt.Axes, ax_v: plt.Axes, symbol: str, tf: str,
          ema_periods: list[int]):
    with _lock:
        candles = list(_buf)
    if not candles:
        return

    n = len(candles)
    closes = [c.close for c in candles]

    # 一次性计算所有 EMA（供绘线 + 标注复用）
    emas = {p: _ema(closes, p) for p in ema_periods}
    ema_colors = {p: _ema_color(i) for i, p in enumerate(ema_periods)}

    # ── 清空 ───────────────────────────────────────────────────────────────────
    ax_c.clear()
    ax_v.clear()

    for ax in (ax_c, ax_v):
        ax.set_facecolor(BG)
        ax.grid(True, alpha=0.1, color='white', lw=0.4)
        ax.tick_params(colors='#aaaaaa', labelsize=7)
        for sp in ax.spines.values():
            sp.set_color('#2a2a2a')

    # ── K 线实体 + 影线 ────────────────────────────────────────────────────────
    for i, c in enumerate(candles):
        color = UP if c.close >= c.open else DN
        lo = min(c.open, c.close)
        hi = max(c.open, c.close)
        hi = max(hi, lo + 1e-9)
        ax_c.plot([i, i], [c.low, c.high], color=color, lw=0.8, zorder=1)
        ax_c.add_patch(mpatches.Rectangle(
            (i - BAR_W / 2, lo), BAR_W, hi - lo,
            fc=color, ec=color, zorder=2,
        ))

    # ── EMA 折线 ───────────────────────────────────────────────────────────────
    for period, vals in emas.items():
        _plot_ema(ax_c, vals, period, ema_colors[period])

    # ── 当前价格虚线 ───────────────────────────────────────────────────────────
    last = candles[-1]
    p_color = UP if last.close >= last.open else DN
    ax_c.axhline(last.close, color=p_color, lw=0.7, ls='--', alpha=0.75, zorder=1)
    ax_c.text(n + 0.3, last.close, f'{last.close:.2f}',
              color=p_color, va='center', fontsize=8, fontweight='bold')

    # ── 标题（最后一根 K 线信息）──────────────────────────────────────────────
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
        ax_v.bar(i, c.volume,
                 color=(UP if c.close >= c.open else DN),
                 alpha=0.55, width=BAR_W)
    ax_v.set_xlim(-1, n + 1.5)
    ax_v.set_ylabel('Vol', color='#888', fontsize=7)

    step = max(1, n // 10)
    ticks = list(range(0, n, step))
    ax_v.set_xticks(ticks)
    ax_v.set_xticklabels(
        [candles[i].ts.strftime('%m/%d\n%H:%M') for i in ticks],
        fontsize=6,
    )

    # ── 选中K线标注（放在最后，叠在所有元素上方）─────────────────────────────
    idx = _selected['idx']
    if idx is not None and 0 <= idx < n:
        _draw_selection(ax_c, ax_v, candles, emas, ema_colors, idx)


# ── 鼠标点击处理 ───────────────────────────────────────────────────────────────

def _make_onclick(fig, ax_c, ax_v, symbol, tf, ema_periods):
    def onclick(event):
        # 右键或点击在图外：取消选中
        if event.button == 3 or event.inaxes not in (ax_c, ax_v):
            if _selected['idx'] is not None:
                _selected['idx'] = None
                _draw(ax_c, ax_v, symbol, tf, ema_periods)
                fig.canvas.draw_idle()
            return

        if event.xdata is None:
            return

        idx = int(round(event.xdata))
        with _lock:
            n = len(_buf)

        new_idx = idx if 0 <= idx < n else None

        # 同一根K线再次点击：取消选中
        if new_idx == _selected['idx']:
            new_idx = None

        _selected['idx'] = new_idx
        _draw(ax_c, ax_v, symbol, tf, ema_periods)
        fig.canvas.draw_idle()

    return onclick


# ── asyncio 后台任务 ───────────────────────────────────────────────────────────

async def _load_history(symbol: str, tf: str, limit: int):
    okx = settings.okx
    async with OKXRestClient(okx.api_key, okx.secret_key, okx.passphrase,
                             is_demo=False) as rest:
        candles = await rest.get_candles(symbol, tf, limit=limit)
    with _lock:
        _buf.clear()
        _buf.extend(candles)
    _dirty.set()
    print(f"✓ Loaded {len(candles)} historical candles [{symbol} {tf}]")


async def _on_candle(new_candles: list[Candle]):
    with _lock:
        for c in new_candles:
            if _buf and c.ts == _buf[-1].ts:
                _buf[-1] = c
            elif not _buf or c.ts > _buf[-1].ts:
                _buf.append(c)
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
    parser.add_argument('--ema', type=int, nargs='*', default=[5, 9, 21],
                        metavar='N',
                        help='EMA 周期列表，空则不显示 (default: 5 9 21)\n'
                             '示例: --ema 9 21  |  --ema 10 20 60  |  --ema')
    args = parser.parse_args()

    symbol = args.symbol.upper()
    tf = args.timeframe
    ema_periods: list[int] = sorted(set(args.ema)) if args.ema else []

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

    plt.style.use('dark_background')
    fig = plt.figure(figsize=(16, 9), facecolor=BG)
    try:
        fig.canvas.manager.set_window_title(f'{symbol}  {tf} — OKX Chart')
    except Exception:
        pass

    gs = fig.add_gridspec(10, 1, hspace=0.06, left=0.06, right=0.95,
                          top=0.95, bottom=0.08)
    ax_c = fig.add_subplot(gs[:7])
    ax_v = fig.add_subplot(gs[7:])

    _draw(ax_c, ax_v, symbol, tf, ema_periods)

    # 鼠标点击事件
    fig.canvas.mpl_connect('button_press_event',
                           _make_onclick(fig, ax_c, ax_v, symbol, tf, ema_periods))

    def _frame(_):
        if _dirty.is_set():
            _dirty.clear()
            _draw(ax_c, ax_v, symbol, tf, ema_periods)
            fig.canvas.draw_idle()

    _ani = animation.FuncAnimation(fig, _frame, interval=500,
                                   cache_frame_data=False)

    ema_desc = ' '.join(f'EMA{p}' for p in ema_periods) if ema_periods else '（无EMA）'
    print(f"Chart ready  {symbol} [{tf}]  {ema_desc}")
    print("左键点击K线查看详情，右键取消选中。")
    plt.show()


if __name__ == '__main__':
    main()
