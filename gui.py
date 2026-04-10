#!/usr/bin/env python3
"""OKX 策略交易系统 — 图形界面

用法:
  python gui.py
"""
import asyncio
import os
import queue
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── matplotlib backend 必须在 pyplot 之前设置 ──────────────────────────────────
import matplotlib
matplotlib.use('TkAgg')

import matplotlib.animation as animation
import matplotlib.patches as mpatches
import matplotlib.transforms as mtrans
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

from config.settings import settings
from gateway.models import Candle
from gateway.okx_rest import OKXRestClient
from gateway.okx_ws import OKXWebSocketClient
from storage.db import Database

# ── 颜色主题（与 chart.py 保持一致）──────────────────────────────────────────
BG      = '#131722'
PANEL   = '#1e222d'
BORDER  = '#2a2e39'
FG      = '#d1d4dc'
FG_DIM  = '#787b86'
UP      = '#26a69a'
DN      = '#ef5350'
ACCENT  = '#2962ff'
BAR_W   = 0.6
EMA_COLORS = ['#f6e05e', '#68d391', '#fc8181', '#76e4f7', '#d6bcfa', '#fbb6ce']

TIMEFRAMES = ['1m', '3m', '5m', '15m', '30m', '1H', '4H', '1D']
COMMON_SYMBOLS = [
    'BTC-USDT', 'ETH-USDT', 'SOL-USDT', 'BNB-USDT', 'XRP-USDT',
    'BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'BTC-USDT-SWAP',
]


# ── 读取 strategies.yaml 获取策略名 ───────────────────────────────────────────

def _load_strategy_names() -> list[str]:
    try:
        import yaml
        with open(ROOT / 'config' / 'strategies.yaml', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        return [s['name'] for s in cfg.get('strategies', [])]
    except Exception:
        return []


# ── REST 工厂 ─────────────────────────────────────────────────────────────────

def _get_rest() -> OKXRestClient:
    okx = settings.okx
    return OKXRestClient(okx.api_key, okx.secret_key, okx.passphrase, okx.is_demo)


# ── 异步桥：在后台线程运行 asyncio 事件循环 ────────────────────────────────────

class AsyncBridge:
    def __init__(self):
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()

    def submit(self, coro, callback=None):
        """提交协程；完成后在原线程调用 callback(result, error)。"""
        async def _wrap():
            try:
                result = await coro
                if callback:
                    callback(result, None)
            except Exception as exc:
                if callback:
                    callback(None, exc)
        asyncio.run_coroutine_threadsafe(_wrap(), self._loop)

    def stop(self):
        self._loop.call_soon_threadsafe(self._loop.stop)


# ── EMA 计算（复用 chart.py 逻辑）────────────────────────────────────────────

def _ema(closes: list[float], period: int) -> list[Optional[float]]:
    k = 2.0 / (period + 1)
    result: list[Optional[float]] = [None] * len(closes)
    val: Optional[float] = None
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


# ── 图表状态（线程安全）──────────────────────────────────────────────────────

class ChartState:
    def __init__(self):
        self.lock = threading.Lock()
        self.buf: deque[Candle] = deque(maxlen=500)
        self.dirty = threading.Event()
        self.selected: dict = {'idx': None}
        self.symbol = 'BTC-USDT'
        self.tf = '5m'
        self.ema_periods: list[int] = [5, 9, 21]

    def push(self, new_candles: list[Candle]):
        with self.lock:
            for c in new_candles:
                if self.buf and c.ts == self.buf[-1].ts:
                    self.buf[-1] = c
                elif not self.buf or c.ts > self.buf[-1].ts:
                    self.buf.append(c)
        self.dirty.set()

    def snapshot(self) -> list[Candle]:
        with self.lock:
            return list(self.buf)


# ── K线主绘图（复用 chart.py 核心逻辑）───────────────────────────────────────

def _draw_chart(ax_c, ax_v, state: ChartState):
    candles = state.snapshot()
    if not candles:
        return

    n = len(candles)
    closes = [c.close for c in candles]
    emas = {p: _ema(closes, p) for p in state.ema_periods}
    ema_colors = {p: EMA_COLORS[i % len(EMA_COLORS)]
                  for i, p in enumerate(state.ema_periods)}

    ax_c.clear()
    ax_v.clear()

    for ax in (ax_c, ax_v):
        ax.set_facecolor(BG)
        ax.grid(True, alpha=0.1, color='white', lw=0.4)
        ax.tick_params(colors='#aaaaaa', labelsize=7)
        for sp in ax.spines.values():
            sp.set_color('#2a2a2a')

    # K 线实体 + 影线
    for i, c in enumerate(candles):
        color = UP if c.close >= c.open else DN
        lo, hi = min(c.open, c.close), max(c.open, c.close)
        hi = max(hi, lo + 1e-9)
        ax_c.plot([i, i], [c.low, c.high], color=color, lw=0.8, zorder=1)
        ax_c.add_patch(mpatches.Rectangle(
            (i - BAR_W / 2, lo), BAR_W, hi - lo, fc=color, ec=color, zorder=2))

    # EMA 折线
    for period, vals in emas.items():
        pairs = [(i, v) for i, v in enumerate(vals) if v is not None]
        if pairs:
            xs, ys = zip(*pairs)
            ax_c.plot(xs, ys, color=ema_colors[period], lw=1.3, zorder=3,
                      label=f'EMA{period}  {ys[-1]:.2f}')

    # 当前价格虚线
    last = candles[-1]
    p_color = UP if last.close >= last.open else DN
    ax_c.axhline(last.close, color=p_color, lw=0.7, ls='--', alpha=0.75, zorder=1)
    ax_c.text(n + 0.3, last.close, f'{last.close:.2f}',
              color=p_color, va='center', fontsize=8, fontweight='bold')

    chg = last.close - last.open
    chg_pct = chg / last.open * 100 if last.open else 0
    arrow = '▲' if chg >= 0 else '▼'
    ax_c.set_title(
        f'{state.symbol}  [{state.tf}]    '
        f'O {last.open:.2f}  H {last.high:.2f}  L {last.low:.2f}  C {last.close:.2f}  '
        f'{arrow} {chg:+.2f} ({chg_pct:+.2f}%)    '
        f'{last.ts.strftime("%Y-%m-%d %H:%M")} UTC',
        color='white', fontsize=9, loc='left', pad=5,
    )
    if state.ema_periods:
        ax_c.legend(loc='upper left', fontsize=8,
                    facecolor='#1a1a2e', labelcolor='white',
                    framealpha=0.75, borderpad=0.5)
    ax_c.set_xlim(-1, n + 1.5)
    ax_c.tick_params(labelbottom=False)

    # 成交量柱
    for i, c in enumerate(candles):
        ax_v.bar(i, c.volume, color=(UP if c.close >= c.open else DN),
                 alpha=0.55, width=BAR_W)
    ax_v.set_xlim(-1, n + 1.5)
    ax_v.set_ylabel('Vol', color='#888', fontsize=7)
    step = max(1, n // 10)
    ticks = list(range(0, n, step))
    ax_v.set_xticks(ticks)
    ax_v.set_xticklabels([candles[i].ts.strftime('%m/%d\n%H:%M') for i in ticks], fontsize=6)

    # 选中K线标注
    idx = state.selected['idx']
    if idx is not None and 0 <= idx < n:
        c = candles[idx]
        for ax in (ax_c, ax_v):
            ax.axvline(idx, color='#ffffff', lw=0.8, alpha=0.35, zorder=4)
        for period, vals in emas.items():
            v = vals[idx]
            if v is not None:
                ax_c.plot(idx, v, 'o', color=ema_colors[period], ms=5, zorder=6,
                          markeredgecolor='white', markeredgewidth=0.4)
        ax_v.bar(idx, c.volume, color='white', alpha=0.18, width=BAR_W + 0.15, zorder=4)

        chg_s = c.close - c.open
        chg_pct_s = chg_s / c.open * 100 if c.open else 0
        arrow_s = '▲' if chg_s >= 0 else '▼'
        lines = [
            c.ts.strftime('%Y-%m-%d  %H:%M'), '─' * 21,
            f'O     {c.open:>14.4f}', f'H     {c.high:>14.4f}',
            f'L     {c.low:>14.4f}',  f'C     {c.close:>14.4f}',
            f'{arrow_s}    {chg_s:>+13.4f}  ({chg_pct_s:>+.2f}%)',
            f'Vol   {c.volume:>14.4f}',
        ]
        if emas:
            lines.append('─' * 21)
            for period, vals in emas.items():
                v = vals[idx]
                val_str = f'{v:>14.4f}' if v is not None else f'{"—":>14}'
                lines.append(f'EMA{period:<3} {val_str}')

        transform = mtrans.blended_transform_factory(ax_c.transData, ax_c.transAxes)
        x_anchor, ha = (idx - 1.2, 'right') if idx > n * 0.6 else (idx + 1.2, 'left')
        ax_c.text(x_anchor, 0.975, '\n'.join(lines),
                  transform=transform, fontsize=7.5, color='white',
                  ha=ha, va='top', fontfamily='monospace', linespacing=1.45,
                  bbox=dict(boxstyle='round,pad=0.55', facecolor='#0d1117',
                            edgecolor='#4a4a6a', alpha=0.93),
                  zorder=7)


# ── 主应用窗口 ─────────────────────────────────────────────────────────────────

class TradeApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self._bridge = AsyncBridge()
        self._engine_proc: Optional[subprocess.Popen] = None
        self._log_queue: queue.Queue = queue.Queue()
        self._chart_state = ChartState()
        self._chart_session = 0   # 每次加载新图递增，用于判断 WS 是否过期

        self.title('OKX 策略交易系统')
        self.geometry('1440x880')
        self.minsize(1100, 700)
        self.configure(bg=BG)

        self._strategy_names = _load_strategy_names()

        self._setup_style()
        self._build_ui()
        self._poll_log()

        # 启动后自动加载仪表板
        self.after(600, self._refresh_dashboard)

    # ── ttk 主题配置 ───────────────────────────────────────────────────────────

    def _setup_style(self):
        s = ttk.Style(self)
        s.theme_use('clam')

        s.configure('.', background=BG, foreground=FG, bordercolor=BORDER,
                    fieldbackground=PANEL, troughcolor=PANEL, relief='flat')

        s.configure('TNotebook', background='#0d1117', bordercolor=BORDER)
        s.configure('TNotebook.Tab', background=PANEL, foreground=FG_DIM,
                    padding=[16, 7], bordercolor=BORDER)
        s.map('TNotebook.Tab',
              background=[('selected', BG), ('active', '#252836')],
              foreground=[('selected', FG), ('active', FG)])

        s.configure('TFrame', background=BG)
        s.configure('Card.TFrame', background=PANEL)

        s.configure('TLabel', background=BG, foreground=FG)
        s.configure('Card.TLabel', background=PANEL, foreground=FG)
        s.configure('Dim.TLabel', background=BG, foreground=FG_DIM)
        s.configure('CDim.TLabel', background=PANEL, foreground=FG_DIM, font=('Segoe UI', 9))
        s.configure('Title.TLabel', background=PANEL, foreground=FG,
                    font=('Segoe UI', 11, 'bold'))
        s.configure('Value.TLabel', background=PANEL, foreground=FG,
                    font=('Consolas', 15, 'bold'))
        s.configure('Up.TLabel', background=PANEL, foreground=UP, font=('Consolas', 15, 'bold'))
        s.configure('Down.TLabel', background=PANEL, foreground=DN, font=('Consolas', 15, 'bold'))

        s.configure('TButton', background='#252836', foreground=FG,
                    bordercolor=BORDER, padding=[10, 5])
        s.map('TButton', background=[('active', '#3a3f5a'), ('pressed', '#1e2230')])
        s.configure('Accent.TButton', background=ACCENT, foreground='white', bordercolor=ACCENT)
        s.map('Accent.TButton', background=[('active', '#3d79ff'), ('pressed', '#1a4fcc')])
        s.configure('Danger.TButton', background='#c62828', foreground='white',
                    bordercolor='#c62828')
        s.map('Danger.TButton', background=[('active', '#ef5350')])

        s.configure('Treeview', background=PANEL, foreground=FG,
                    fieldbackground=PANEL, bordercolor=BORDER,
                    rowheight=26, font=('Consolas', 9))
        s.configure('Treeview.Heading', background='#252836', foreground=FG_DIM,
                    font=('Segoe UI', 9, 'bold'), relief='flat')
        s.map('Treeview',
              background=[('selected', ACCENT)],
              foreground=[('selected', 'white')])

        s.configure('TCombobox', fieldbackground=PANEL, foreground=FG,
                    selectbackground=ACCENT, bordercolor=BORDER, arrowcolor=FG_DIM)
        s.configure('TEntry', fieldbackground=PANEL, foreground=FG,
                    insertcolor=FG, bordercolor=BORDER)
        s.configure('TCheckbutton', background=BG, foreground=FG)
        s.configure('Vertical.TScrollbar', background=PANEL, troughcolor=BG, arrowcolor=FG_DIM)
        s.configure('Horizontal.TScrollbar', background=PANEL, troughcolor=BG, arrowcolor=FG_DIM)

    # ── 构建主界面 ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_toolbar()
        tk.Frame(self, bg=BORDER, height=1).pack(fill='x')
        self._build_notebook()
        tk.Frame(self, bg=BORDER, height=1).pack(fill='x')
        self._build_statusbar()

    def _build_toolbar(self):
        tb = tk.Frame(self, bg='#0d1117', height=52)
        tb.pack(fill='x')
        tb.pack_propagate(False)

        # 左：标题 + 模式徽章
        tk.Label(tb, text='  OKX 策略交易', bg='#0d1117', fg=FG,
                 font=('Segoe UI', 14, 'bold')).pack(side='left', padx=(8, 0))
        mode = 'DEMO' if settings.okx.is_demo else 'LIVE'
        mode_bg = '#c87900' if settings.okx.is_demo else UP
        tk.Label(tb, text=f'  {mode}  ', bg=mode_bg, fg='#000',
                 font=('Segoe UI', 8, 'bold')).pack(side='left', padx=8, pady=14)

        # 右：操作按钮
        bf = tk.Frame(tb, bg='#0d1117')
        bf.pack(side='right', padx=10)

        ttk.Button(bf, text='刷新数据', command=self._refresh_all).pack(side='left', padx=3)
        self._btn_start = ttk.Button(bf, text='▶ 启动引擎',
                                     command=self._start_engine, style='Accent.TButton')
        self._btn_start.pack(side='left', padx=3)
        self._btn_stop = ttk.Button(bf, text='■ 停止引擎',
                                    command=self._stop_engine, style='Danger.TButton',
                                    state='disabled')
        self._btn_stop.pack(side='left', padx=3)

    def _build_notebook(self):
        self._nb = ttk.Notebook(self)
        self._nb.pack(fill='both', expand=True, padx=0, pady=0)

        self._tab_dash    = ttk.Frame(self._nb)
        self._tab_chart   = ttk.Frame(self._nb)
        self._tab_orders  = ttk.Frame(self._nb)
        self._tab_signals = ttk.Frame(self._nb)
        self._tab_pnl     = ttk.Frame(self._nb)
        self._tab_log     = ttk.Frame(self._nb)

        self._nb.add(self._tab_dash,    text='  仪表板  ')
        self._nb.add(self._tab_chart,   text='  K线图表  ')
        self._nb.add(self._tab_orders,  text='  订单历史  ')
        self._nb.add(self._tab_signals, text='  信号记录  ')
        self._nb.add(self._tab_pnl,     text='  盈亏统计  ')
        self._nb.add(self._tab_log,     text='  运行日志  ')

        self._build_dashboard_tab()
        self._build_chart_tab()
        self._build_orders_tab()
        self._build_signals_tab()
        self._build_pnl_tab()
        self._build_log_tab()

        self._nb.bind('<<NotebookTabChanged>>', self._on_tab_change)

    def _build_statusbar(self):
        sb = tk.Frame(self, bg='#0d1117', height=26)
        sb.pack(fill='x')
        sb.pack_propagate(False)

        self._status_var = tk.StringVar(value='就绪')
        self._time_var   = tk.StringVar(value='')

        tk.Label(sb, textvariable=self._status_var,
                 bg='#0d1117', fg=FG_DIM, font=('Segoe UI', 8)).pack(side='left', padx=10)
        tk.Label(sb, textvariable=self._time_var,
                 bg='#0d1117', fg=FG_DIM, font=('Segoe UI', 8)).pack(side='right', padx=10)

    # ── 仪表板 Tab ─────────────────────────────────────────────────────────────

    def _build_dashboard_tab(self):
        p = self._tab_dash

        # ── 账户卡片 ──────────────────────────────────────────────────────────
        card_row = ttk.Frame(p)
        card_row.pack(fill='x', padx=14, pady=(14, 8))

        self._balance_vars: dict[str, tk.StringVar] = {}

        card_defs = [
            ('total',     'USDT 总资产', 'Value.TLabel'),
            ('available', 'USDT 可用',   'Up.TLabel'),
            ('frozen',    'USDT 冻结',   'Value.TLabel'),
        ]
        for i, (key, label, val_style) in enumerate(card_defs):
            card = ttk.Frame(card_row, style='Card.TFrame', padding=16)
            card.grid(row=0, column=i, padx=6, sticky='nsew')
            card_row.columnconfigure(i, weight=1)
            ttk.Label(card, text=label, style='CDim.TLabel').pack(anchor='w')
            var = tk.StringVar(value='—')
            ttk.Label(card, textvariable=var, style=val_style).pack(anchor='w', pady=(6, 0))
            self._balance_vars[key] = var

        # 刷新按钮卡片
        btn_card = ttk.Frame(card_row, style='Card.TFrame', padding=16)
        btn_card.grid(row=0, column=3, padx=6, sticky='nsew')
        card_row.columnconfigure(3, weight=0)
        ttk.Label(btn_card, text='账户操作', style='CDim.TLabel').pack(anchor='w')
        ttk.Button(btn_card, text='刷新账户', command=self._refresh_dashboard,
                   style='Accent.TButton').pack(anchor='w', pady=(6, 0))

        # ── 持仓表格 ──────────────────────────────────────────────────────────
        pf = ttk.Frame(p, style='Card.TFrame', padding=10)
        pf.pack(fill='both', expand=True, padx=14, pady=(0, 14))

        hf = ttk.Frame(pf, style='Card.TFrame')
        hf.pack(fill='x', pady=(0, 6))
        ttk.Label(hf, text='当前持仓', style='Title.TLabel').pack(side='left')

        cols    = ('inst_id', 'pos_side', 'size', 'entry_price', 'mark_price',
                   'unrealized_pnl', 'leverage')
        labels  = ('品种', '方向', '数量', '入场价', '标记价', '未实现盈亏', '杠杆')
        widths  = (170, 70, 110, 120, 120, 140, 65)
        self._pos_tree = self._make_treeview(pf, cols, labels, widths)

    def _refresh_dashboard(self):
        self._set_status('刷新账户数据...')

        async def _fetch():
            async with _get_rest() as rest:
                bal = await rest.get_balance('USDT')
                positions = await rest.get_positions()
            return bal, positions

        def _update(result, err):
            if err:
                self._set_status(f'刷新失败: {err}')
                return
            bal, positions = result
            self._balance_vars['total'].set(f'{bal.total:,.4f}')
            self._balance_vars['available'].set(f'{bal.available:,.4f}')
            self._balance_vars['frozen'].set(f'{bal.frozen:,.4f}')

            for row in self._pos_tree.get_children():
                self._pos_tree.delete(row)

            if not positions:
                self._pos_tree.insert('', 'end',
                    values=('— 当前无持仓 —', '', '', '', '', '', ''))
            else:
                for pos in positions:
                    tag = 'up' if pos.unrealized_pnl >= 0 else 'dn'
                    self._pos_tree.insert('', 'end', tags=(tag,), values=(
                        pos.inst_id,
                        pos.pos_side.value.upper(),
                        f'{pos.size:.4f}',
                        f'{pos.entry_price:.4f}',
                        f'{pos.mark_price:.4f}',
                        f'{pos.unrealized_pnl:+.4f}',
                        f'{pos.leverage}x',
                    ))
                self._pos_tree.tag_configure('up', foreground=UP)
                self._pos_tree.tag_configure('dn', foreground=DN)
            self._set_status('就绪')

        self._bridge.submit(_fetch(), lambda r, e: self.after(0, lambda: _update(r, e)))

    # ── K线图表 Tab ────────────────────────────────────────────────────────────

    def _build_chart_tab(self):
        p = self._tab_chart
        state = self._chart_state

        # 控制条
        ctrl = tk.Frame(p, bg=PANEL, pady=8)
        ctrl.pack(fill='x')

        def lbl(text):
            tk.Label(ctrl, text=text, bg=PANEL, fg=FG_DIM,
                     font=('Segoe UI', 9)).pack(side='left', padx=(10, 3))

        lbl('品种')
        self._c_symbol = tk.StringVar(value=state.symbol)
        sym_cb = ttk.Combobox(ctrl, textvariable=self._c_symbol,
                              values=COMMON_SYMBOLS, width=18, state='readonly')
        sym_cb.pack(side='left', padx=(0, 8))

        lbl('周期')
        self._c_tf = tk.StringVar(value=state.tf)
        ttk.Combobox(ctrl, textvariable=self._c_tf,
                     values=TIMEFRAMES, width=6, state='readonly').pack(side='left', padx=(0, 8))

        lbl('EMA')
        self._c_ema = tk.StringVar(value='5 9 21')
        ttk.Entry(ctrl, textvariable=self._c_ema, width=12).pack(side='left', padx=(0, 8))

        lbl('条数')
        self._c_limit = tk.StringVar(value='200')
        ttk.Entry(ctrl, textvariable=self._c_limit, width=6).pack(side='left', padx=(0, 12))

        ttk.Button(ctrl, text='加载图表', command=self._load_chart,
                   style='Accent.TButton').pack(side='left', padx=(0, 10))

        self._chart_msg = tk.StringVar(value='请点击"加载图表"开始')
        tk.Label(ctrl, textvariable=self._chart_msg, bg=PANEL,
                 fg=FG_DIM, font=('Segoe UI', 9)).pack(side='left')

        # matplotlib 画布
        self._chart_fig = Figure(facecolor=BG)
        self._chart_fig.subplots_adjust(left=0.06, right=0.95, top=0.95, bottom=0.08, hspace=0.06)
        gs = self._chart_fig.add_gridspec(10, 1, hspace=0.06)
        self._ax_c = self._chart_fig.add_subplot(gs[:7])
        self._ax_v = self._chart_fig.add_subplot(gs[7:])
        for ax in (self._ax_c, self._ax_v):
            ax.set_facecolor(BG)

        canvas = FigureCanvasTkAgg(self._chart_fig, master=p)
        canvas.get_tk_widget().pack(fill='both', expand=True)
        self._chart_canvas = canvas

        self._chart_fig.canvas.mpl_connect('button_press_event', self._chart_onclick)

        # 动画每 500ms 检查 dirty 并刷新
        self._chart_anim = animation.FuncAnimation(
            self._chart_fig, self._chart_frame,
            interval=500, cache_frame_data=False
        )

    def _load_chart(self):
        symbol = self._c_symbol.get().upper()
        tf = self._c_tf.get()

        try:
            ema_str = self._c_ema.get().strip()
            ema_periods = sorted(set(int(x) for x in ema_str.split())) if ema_str else []
        except ValueError:
            messagebox.showerror('格式错误', 'EMA 周期请输入空格分隔的整数，例如: 5 9 21')
            return
        try:
            limit = max(10, int(self._c_limit.get()))
        except ValueError:
            limit = 200

        # 更新 session，旧 WS 循环检测到 session 变化后自动退出
        self._chart_session += 1
        session = self._chart_session

        state = self._chart_state
        state.symbol = symbol
        state.tf = tf
        state.ema_periods = ema_periods
        with state.lock:
            state.buf.clear()
        state.dirty.clear()
        state.selected['idx'] = None

        self._chart_msg.set(f'正在加载 {symbol} [{tf}]...')

        async def _fetch():
            async with _get_rest() as rest:
                candles = await rest.get_candles(symbol, tf, limit=limit)
            return candles

        def _on_loaded(candles, err):
            if err:
                self.after(0, lambda: self._chart_msg.set(f'加载失败: {err}'))
                return
            state.push(candles)
            self.after(0, lambda: self._chart_msg.set(
                f'已加载 {len(candles)} 根K线，实时更新中...'))
            # 启动 WS 实时推送
            self._bridge.submit(self._chart_ws_loop(symbol, tf, session))

        self._bridge.submit(_fetch(), _on_loaded)

    async def _chart_ws_loop(self, symbol: str, tf: str, session: int):
        okx = settings.okx
        ws = OKXWebSocketClient(okx.api_key, okx.secret_key, okx.passphrase, is_demo=False)

        async def _on_candle(new_candles: list[Candle]):
            if self._chart_session == session:
                self._chart_state.push(new_candles)

        ws.subscribe_candles(symbol, tf, _on_candle)
        await ws.start()
        try:
            # 每 5s 检查一次 session 是否仍为当前 session
            while self._chart_session == session:
                await asyncio.sleep(5)
        finally:
            await ws.stop()

    def _chart_frame(self, _):
        state = self._chart_state
        if state.dirty.is_set():
            state.dirty.clear()
            _draw_chart(self._ax_c, self._ax_v, state)
            self._chart_canvas.draw_idle()

    def _chart_onclick(self, event):
        state = self._chart_state
        if event.button == 3 or event.inaxes not in (self._ax_c, self._ax_v):
            if state.selected['idx'] is not None:
                state.selected['idx'] = None
                state.dirty.set()
            return
        if event.xdata is None:
            return
        idx = int(round(event.xdata))
        n = len(state.buf)
        new_idx = idx if 0 <= idx < n else None
        if new_idx == state.selected['idx']:
            new_idx = None
        state.selected['idx'] = new_idx
        state.dirty.set()

    # ── 订单历史 Tab ───────────────────────────────────────────────────────────

    def _build_orders_tab(self):
        p = self._tab_orders
        fb = self._filter_bar(p)

        ttk.Label(fb, text='策略:').pack(side='left', padx=(0, 4))
        self._ord_strategy = tk.StringVar(value='全部')
        ttk.Combobox(fb, textvariable=self._ord_strategy,
                     values=['全部'] + self._strategy_names,
                     width=22).pack(side='left', padx=(0, 10))

        ttk.Label(fb, text='条数:').pack(side='left', padx=(0, 4))
        self._ord_limit = tk.StringVar(value='50')
        ttk.Entry(fb, textvariable=self._ord_limit, width=6).pack(side='left', padx=(0, 10))

        ttk.Button(fb, text='查询', command=self._refresh_orders,
                   style='Accent.TButton').pack(side='left')

        tf = ttk.Frame(p, style='Card.TFrame', padding=6)
        tf.pack(fill='both', expand=True, padx=10, pady=(0, 10))

        cols   = ('id', 'created_at', 'inst_id', 'strategy', 'side', 'status',
                  'filled_qty', 'avg_price', 'fee')
        labels = ('ID', '时间', '品种', '策略', '方向', '状态', '成交量', '均价', '手续费')
        widths = (50, 155, 140, 170, 58, 115, 105, 115, 95)
        self._orders_tree = self._make_treeview(tf, cols, labels, widths)

    def _refresh_orders(self):
        strategy = self._ord_strategy.get()
        if strategy == '全部':
            strategy = None
        try:
            limit = int(self._ord_limit.get())
        except ValueError:
            limit = 50

        async def _fetch():
            db = Database(settings.db_path)
            await db.init()
            rows = await db.get_orders(strategy=strategy, limit=limit)
            await db.close()
            return rows

        def _update(rows, err):
            if err:
                self._set_status(f'查询订单失败: {err}')
                return
            for item in self._orders_tree.get_children():
                self._orders_tree.delete(item)
            for r in (rows or []):
                tag = 'up' if r['side'] == 'buy' else 'dn'
                self._orders_tree.insert('', 'end', tags=(tag,), values=(
                    r['id'], r['created_at'][:19], r['inst_id'],
                    r['strategy'], r['side'].upper(), r['status'],
                    f"{r['filled_qty']:.4f}", f"{r['avg_price']:.4f}",
                    f"{r['fee']:.6f}",
                ))
            self._orders_tree.tag_configure('up', foreground=UP)
            self._orders_tree.tag_configure('dn', foreground=DN)

        self._bridge.submit(_fetch(), lambda r, e: self.after(0, lambda: _update(r, e)))

    # ── 信号记录 Tab ───────────────────────────────────────────────────────────

    def _build_signals_tab(self):
        p = self._tab_signals
        fb = self._filter_bar(p)

        ttk.Label(fb, text='策略:').pack(side='left', padx=(0, 4))
        self._sig_strategy = tk.StringVar(value='全部')
        ttk.Combobox(fb, textvariable=self._sig_strategy,
                     values=['全部'] + self._strategy_names,
                     width=22).pack(side='left', padx=(0, 10))

        ttk.Label(fb, text='条数:').pack(side='left', padx=(0, 4))
        self._sig_limit = tk.StringVar(value='50')
        ttk.Entry(fb, textvariable=self._sig_limit, width=6).pack(side='left', padx=(0, 10))

        ttk.Button(fb, text='查询', command=self._refresh_signals,
                   style='Accent.TButton').pack(side='left')

        tf = ttk.Frame(p, style='Card.TFrame', padding=6)
        tf.pack(fill='both', expand=True, padx=10, pady=(0, 10))

        cols   = ('id', 'created_at', 'strategy', 'inst_id', 'side',
                  'order_type', 'qty', 'stop_loss', 'reason')
        labels = ('ID', '时间', '策略', '品种', '方向', '类型', '数量', '止损', '信号原因')
        widths = (50, 155, 170, 140, 58, 85, 95, 115, 250)
        self._signals_tree = self._make_treeview(tf, cols, labels, widths)

    def _refresh_signals(self):
        strategy = self._sig_strategy.get()
        if strategy == '全部':
            strategy = None
        try:
            limit = int(self._sig_limit.get())
        except ValueError:
            limit = 50

        async def _fetch():
            db = Database(settings.db_path)
            await db.init()
            rows = await db.get_signals(strategy=strategy, limit=limit)
            await db.close()
            return rows

        def _update(rows, err):
            if err:
                return
            for item in self._signals_tree.get_children():
                self._signals_tree.delete(item)
            for r in (rows or []):
                tag = 'up' if r['side'] == 'buy' else 'dn'
                self._signals_tree.insert('', 'end', tags=(tag,), values=(
                    r['id'], r['created_at'][:19], r['strategy'],
                    r['inst_id'], r['side'].upper(), r['order_type'],
                    f"{r['qty']:.4f}", r['stop_loss'] or '—',
                    r['reason'] or '',
                ))
            self._signals_tree.tag_configure('up', foreground=UP)
            self._signals_tree.tag_configure('dn', foreground=DN)

        self._bridge.submit(_fetch(), lambda r, e: self.after(0, lambda: _update(r, e)))

    # ── 盈亏统计 Tab ───────────────────────────────────────────────────────────

    def _build_pnl_tab(self):
        p = self._tab_pnl
        fb = self._filter_bar(p)

        ttk.Label(fb, text='统计天数:').pack(side='left', padx=(0, 4))
        self._pnl_days = tk.StringVar(value='14')
        ttk.Entry(fb, textvariable=self._pnl_days, width=6).pack(side='left', padx=(0, 10))
        ttk.Button(fb, text='加载盈亏图', command=self._refresh_pnl,
                   style='Accent.TButton').pack(side='left')

        self._pnl_fig = Figure(facecolor=BG)
        self._pnl_ax = self._pnl_fig.add_subplot(111)
        self._pnl_ax.set_facecolor(BG)

        pnl_canvas = FigureCanvasTkAgg(self._pnl_fig, master=p)
        pnl_canvas.get_tk_widget().pack(fill='both', expand=True)
        self._pnl_canvas = pnl_canvas

    def _refresh_pnl(self):
        try:
            days = int(self._pnl_days.get())
        except ValueError:
            days = 14

        async def _fetch():
            db = Database(settings.db_path)
            await db.init()
            rows = await db.get_daily_stats(days=days)
            await db.close()
            return rows

        def _draw(rows, err):
            if err:
                return
            ax = self._pnl_ax
            ax.clear()
            ax.set_facecolor(BG)
            ax.tick_params(colors='#aaaaaa', labelsize=8)
            for sp in ax.spines.values():
                sp.set_color(BORDER)
            ax.grid(True, alpha=0.1, color='white', lw=0.4, axis='y')
            ax.set_title('每日净盈亏统计  (USDT)', color=FG, fontsize=11, loc='left')
            ax.set_ylabel('净盈亏', color=FG_DIM, fontsize=9)

            if not rows:
                ax.text(0.5, 0.5, '暂无盈亏数据', transform=ax.transAxes,
                        ha='center', va='center', color=FG_DIM, fontsize=14)
            else:
                # 按日期汇总所有策略净盈亏
                date_pnl: dict[str, float] = {}
                for r in rows:
                    net = r.get('net_pnl', r.get('gross_pnl', 0) - r.get('fees', 0))
                    date_pnl[r['date']] = date_pnl.get(r['date'], 0.0) + net

                dates = sorted(date_pnl)[-days:]
                pnls  = [date_pnl[d] for d in dates]
                bar_colors = [UP if v >= 0 else DN for v in pnls]

                x = list(range(len(dates)))
                ax.bar(x, pnls, color=bar_colors, width=0.6, edgecolor='none', alpha=0.85)
                ax.axhline(0, color=BORDER, lw=0.8)
                ax.set_xticks(x)
                ax.set_xticklabels(dates, rotation=30, ha='right', fontsize=7)

                span = max(pnls) - min(pnls) if len(pnls) > 1 else abs(pnls[0]) or 1
                for xi, val in zip(x, pnls):
                    if val != 0:
                        offset = span * 0.025
                        va = 'bottom' if val > 0 else 'top'
                        y_pos = val + offset if val > 0 else val - offset
                        ax.text(xi, y_pos, f'{val:+.2f}',
                                ha='center', va=va, color=FG, fontsize=7)

            self._pnl_fig.tight_layout()
            self._pnl_canvas.draw()

        self._bridge.submit(_fetch(), lambda r, e: self.after(0, lambda: _draw(r, e)))

    # ── 运行日志 Tab ───────────────────────────────────────────────────────────

    def _build_log_tab(self):
        p = self._tab_log
        ctrl = ttk.Frame(p)
        ctrl.pack(fill='x', padx=10, pady=6)

        ttk.Button(ctrl, text='清空', command=self._clear_log).pack(side='left', padx=(0, 8))
        self._autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctrl, text='自动滚动', variable=self._autoscroll_var).pack(side='left')

        self._log_text = scrolledtext.ScrolledText(
            p, bg='#0d1117', fg=FG, insertbackground=FG,
            font=('Consolas', 9), wrap='word', state='disabled',
            relief='flat', borderwidth=0,
        )
        self._log_text.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        self._log_text.tag_configure('info',    foreground=FG)
        self._log_text.tag_configure('success', foreground=UP)
        self._log_text.tag_configure('warning', foreground='#f6ad55')
        self._log_text.tag_configure('error',   foreground=DN)

    def _clear_log(self):
        self._log_text.configure(state='normal')
        self._log_text.delete('1.0', 'end')
        self._log_text.configure(state='disabled')

    def _append_log(self, text: str, tag: str = 'info'):
        self._log_text.configure(state='normal')
        ts = datetime.now().strftime('%H:%M:%S')
        self._log_text.insert('end', f'[{ts}] {text}\n', tag)
        if self._autoscroll_var.get():
            self._log_text.see('end')
        self._log_text.configure(state='disabled')

    def _poll_log(self):
        try:
            while True:
                line = self._log_queue.get_nowait().rstrip()
                if not line:
                    continue
                lo = line.lower()
                tag = ('error'   if ('error' in lo or 'exception' in lo) else
                       'warning' if ('warn' in lo) else
                       'success' if ('✓' in line or 'success' in lo or 'started' in lo) else
                       'info')
                self._append_log(line, tag)
        except queue.Empty:
            pass
        self.after(150, self._poll_log)

    # ── 引擎控制 ───────────────────────────────────────────────────────────────

    def _start_engine(self):
        if self._engine_proc and self._engine_proc.poll() is None:
            messagebox.showinfo('提示', '交易引擎已在运行中')
            return

        main_py = ROOT / 'main.py'
        if not main_py.exists():
            messagebox.showerror('错误', f'找不到 {main_py}')
            return

        self._append_log('正在启动交易引擎...', 'info')
        try:
            self._engine_proc = subprocess.Popen(
                [sys.executable, str(main_py)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(ROOT),
                env={**os.environ},
            )
        except Exception as exc:
            messagebox.showerror('启动失败', str(exc))
            return

        self._btn_start.configure(state='disabled')
        self._btn_stop.configure(state='normal')
        self._set_status('交易引擎运行中...')

        def _reader():
            for line in self._engine_proc.stdout:
                self._log_queue.put(line)
            self._log_queue.put('[引擎进程已退出]\n')
            self.after(0, self._on_engine_stopped)

        threading.Thread(target=_reader, daemon=True).start()
        self._nb.select(self._tab_log)

    def _stop_engine(self):
        if self._engine_proc and self._engine_proc.poll() is None:
            self._engine_proc.terminate()
            self._append_log('已发送终止信号...', 'warning')
        else:
            self._on_engine_stopped()

    def _on_engine_stopped(self):
        self._btn_start.configure(state='normal')
        self._btn_stop.configure(state='disabled')
        self._set_status('就绪')

    # ── 通用助手 ───────────────────────────────────────────────────────────────

    def _filter_bar(self, parent) -> ttk.Frame:
        fb = tk.Frame(parent, bg=PANEL, pady=7, padx=10)
        fb.pack(fill='x')
        return fb

    def _make_treeview(self, parent, cols, labels, widths) -> ttk.Treeview:
        frame = ttk.Frame(parent, style='Card.TFrame')
        frame.pack(fill='both', expand=True)

        tree = ttk.Treeview(frame, columns=cols, show='headings', selectmode='browse')
        for col, label, width in zip(cols, labels, widths):
            tree.heading(col, text=label)
            tree.column(col, width=width, minwidth=40, anchor='center')

        vsb = ttk.Scrollbar(frame, orient='vertical',   command=tree.yview)
        hsb = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def _set_status(self, text: str):
        self._status_var.set(text)
        self._time_var.set(datetime.now().strftime('%H:%M:%S'))

    def _refresh_all(self):
        self._refresh_dashboard()
        self._refresh_orders()
        self._refresh_signals()
        self._refresh_pnl()

    def _on_tab_change(self, _):
        tab  = self._nb.select()
        name = self._nb.tab(tab, 'text').strip()
        if name == '订单历史':
            self._refresh_orders()
        elif name == '信号记录':
            self._refresh_signals()
        elif name == '盈亏统计':
            self._refresh_pnl()

    def on_closing(self):
        if self._engine_proc and self._engine_proc.poll() is None:
            if not messagebox.askyesno('退出确认', '交易引擎仍在运行，确认退出并停止引擎？'):
                return
            self._engine_proc.terminate()
        self._bridge.stop()
        self.destroy()


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main():
    app = TradeApp()
    app.protocol('WM_DELETE_WINDOW', app.on_closing)
    app.mainloop()


if __name__ == '__main__':
    main()
