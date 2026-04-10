"""回测报告：统计指标 + K 线图（含买卖点）+ 资金曲线"""
from __future__ import annotations

import csv
import math
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
from loguru import logger

from backtest.engine import TradeRecord


# ── 统计指标 ───────────────────────────────────────────────────────────────────

def _calc_metrics(
    equity_curve: list[float],
    initial_capital: float,
    trades: list[TradeRecord],
    risk_free_rate: float = 0.04,  # 年化无风险利率（4%）
    candle_per_day: int = 96,       # 15m K 线：96 根/天
) -> dict:
    equity = np.array(equity_curve, dtype=float)

    # 总收益率
    total_return = (equity[-1] - initial_capital) / initial_capital

    # 年化收益率（按 K 线数量估算持续天数）
    total_bars = len(equity) - 1
    years = total_bars / (candle_per_day * 365) if total_bars > 0 else 1e-9
    annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0.0

    # 最大回撤
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak
    max_drawdown = float(drawdown.min())

    # Sharpe 比率（基于每根 K 线收益率）
    bar_returns = np.diff(equity) / equity[:-1]
    bar_rf = risk_free_rate / (candle_per_day * 365)
    excess = bar_returns - bar_rf
    sharpe = (
        float(excess.mean() / excess.std() * math.sqrt(candle_per_day * 365))
        if excess.std() > 0 else 0.0
    )

    # 交易统计（只看平仓交易）
    close_trades = [t for t in trades if "close" in t.action or "sl" in t.action]
    wins = [t for t in close_trades if t.pnl > 0]
    losses = [t for t in close_trades if t.pnl <= 0]
    sl_hits = [t for t in close_trades if "sl" in t.action]

    win_rate = len(wins) / len(close_trades) if close_trades else 0.0
    avg_win = float(np.mean([t.pnl for t in wins])) if wins else 0.0
    avg_loss = float(np.mean([abs(t.pnl) for t in losses])) if losses else 0.0
    profit_factor = (
        sum(t.pnl for t in wins) / abs(sum(t.pnl for t in losses))
        if losses and sum(t.pnl for t in losses) != 0 else float("inf")
    )
    total_pnl = sum(t.pnl for t in close_trades)

    return {
        "initial_capital": initial_capital,
        "final_equity": float(equity[-1]),
        "total_return_pct": total_return * 100,
        "annual_return_pct": annual_return * 100,
        "max_drawdown_pct": max_drawdown * 100,
        "sharpe": sharpe,
        "total_trades": len(close_trades),
        "win_rate_pct": win_rate * 100,
        "avg_win_usdt": avg_win,
        "avg_loss_usdt": avg_loss,
        "profit_factor": profit_factor,
        "total_pnl_usdt": total_pnl,
        "sl_hits": len(sl_hits),
        "years": years,
    }


def print_report(metrics: dict) -> None:
    sep = "─" * 52
    print(f"\n{sep}")
    print(f"{'BACKTEST REPORT':^52}")
    print(sep)
    print(f"  初始资金:      {metrics['initial_capital']:>12.2f} USDT")
    print(f"  最终权益:      {metrics['final_equity']:>12.2f} USDT")
    print(f"  总收益率:      {metrics['total_return_pct']:>11.2f}%")
    print(f"  年化收益率:    {metrics['annual_return_pct']:>11.2f}%  ({metrics['years']:.2f} 年)")
    print(f"  最大回撤:      {metrics['max_drawdown_pct']:>11.2f}%")
    print(f"  Sharpe 比率:   {metrics['sharpe']:>12.2f}")
    print(sep)
    print(f"  交易次数:      {metrics['total_trades']:>12d}")
    print(f"  胜率:          {metrics['win_rate_pct']:>11.2f}%")
    print(f"  平均盈利:      {metrics['avg_win_usdt']:>12.2f} USDT")
    print(f"  平均亏损:      {metrics['avg_loss_usdt']:>12.2f} USDT")
    print(f"  盈亏比:        {metrics['profit_factor']:>12.2f}")
    print(f"  净盈亏:        {metrics['total_pnl_usdt']:>12.2f} USDT")
    print(f"  止损触发次数:  {metrics['sl_hits']:>12d}")
    print(sep)


# ── CSV 导出 ───────────────────────────────────────────────────────────────────

def export_trades_csv(trades: list[TradeRecord], output_path: str = "backtest_trades.csv") -> None:
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ts", "action", "price", "contracts", "pnl_usdt", "reason"])
        for t in trades:
            writer.writerow([
                t.ts.strftime("%Y-%m-%d %H:%M"),
                t.action,
                f"{t.price:.4f}",
                t.contracts,
                f"{t.pnl:.4f}",
                t.reason,
            ])
    logger.info(f"Trades exported → {output_path}")


# ── 图表 ───────────────────────────────────────────────────────────────────────

def plot_results(
    candles: list,               # list[Candle]，15m 回测期
    trades: list[TradeRecord],
    equity_curve: list[float],
    equity_ts: list[datetime],
    strategy_name: str = "",
    output_path: str = "backtest_chart.png",
) -> None:
    """绘制：上图=收盘价折线+买卖标记，下图=资金曲线"""
    if not candles:
        logger.warning("No candles to plot")
        return

    # ── 准备数据 ───────────────────────────────────────────────────────────────
    ts_list = [c.ts for c in candles]
    close_list = [c.close for c in candles]

    # 构建 ts→index 映射
    ts_to_idx = {c.ts: i for i, c in enumerate(candles)}

    buy_open_x, buy_open_y = [], []   # 开多
    sell_open_x, sell_open_y = [], []  # 开空
    buy_close_x, buy_close_y = [], []  # 平空（买入平仓）
    sell_close_x, sell_close_y = [], []  # 平多（卖出平仓）
    sl_x, sl_y = [], []               # 止损

    for t in trades:
        idx = ts_to_idx.get(t.ts)
        if idx is None:
            # 找最近的 candle
            for i, c in enumerate(candles):
                if c.ts >= t.ts:
                    idx = i
                    break
        if idx is None:
            continue
        px = ts_list[idx]
        py = t.price

        if "sl" in t.action:
            sl_x.append(px)
            sl_y.append(py)
        elif "open_long" in t.action:
            buy_open_x.append(px)
            buy_open_y.append(py)
        elif "open_short" in t.action:
            sell_open_x.append(px)
            sell_open_y.append(py)
        elif "close_long" in t.action:
            sell_close_x.append(px)
            sell_close_y.append(py)
        elif "close_short" in t.action:
            buy_close_x.append(px)
            buy_close_y.append(py)

    # ── 绘图 ──────────────────────────────────────────────────────────────────
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(20, 12),
        gridspec_kw={"height_ratios": [3, 1]},
        sharex=False,
    )
    fig.patch.set_facecolor("#0d1117")
    for ax in (ax1, ax2):
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#c9d1d9")
        ax.spines[:].set_color("#30363d")
        ax.xaxis.label.set_color("#c9d1d9")
        ax.yaxis.label.set_color("#c9d1d9")
        ax.title.set_color("#e6edf3")

    title = f"Backtest: {strategy_name}  |  {ts_list[0].strftime('%Y-%m-%d')} → {ts_list[-1].strftime('%Y-%m-%d')}"
    fig.suptitle(title, color="#e6edf3", fontsize=13, y=0.98)

    # ── 上图：价格 + 买卖点 ───────────────────────────────────────────────────
    ax1.plot(ts_list, close_list, color="#58a6ff", linewidth=0.7, label="Close", zorder=1)

    marker_size = max(4, min(10, 20_000 // max(len(candles), 1)))

    if buy_open_x:
        ax1.scatter(buy_open_x, buy_open_y, marker="^", color="#3fb950", s=marker_size**2,
                    zorder=5, label="Open Long ▲", edgecolors="#2ea043", linewidths=0.5)
    if sell_open_x:
        ax1.scatter(sell_open_x, sell_open_y, marker="v", color="#f85149", s=marker_size**2,
                    zorder=5, label="Open Short ▼", edgecolors="#da3633", linewidths=0.5)
    if sell_close_x:
        ax1.scatter(sell_close_x, sell_close_y, marker="x", color="#d29922", s=marker_size**2,
                    zorder=5, label="Close Long x", linewidths=1.2)
    if buy_close_x:
        ax1.scatter(buy_close_x, buy_close_y, marker="x", color="#79c0ff", s=marker_size**2,
                    zorder=5, label="Close Short x", linewidths=1.2)
    if sl_x:
        ax1.scatter(sl_x, sl_y, marker="*", color="#ff7b72", s=(marker_size + 2)**2,
                    zorder=6, label="Stop Loss *")

    ax1.set_ylabel("Price (USDT)", fontsize=10)
    ax1.legend(loc="upper left", fontsize=8, facecolor="#161b22",
               labelcolor="#c9d1d9", edgecolor="#30363d")
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.2f}"))
    ax1.grid(True, color="#21262d", linewidth=0.5)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax1.xaxis.set_major_locator(mdates.MonthLocator())
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=30, ha="right", fontsize=8)

    # ── 下图：资金曲线 ────────────────────────────────────────────────────────
    if equity_ts and equity_curve:
        eq_arr = np.array(equity_curve[1:])  # 去掉初始值，与 equity_ts 对齐
        initial = equity_curve[0]

        # 着色：盈利=绿，亏损=红
        color_arr = ["#3fb950" if v >= initial else "#f85149" for v in eq_arr]
        ax2.fill_between(equity_ts, initial, eq_arr,
                         where=(eq_arr >= initial), color="#3fb95033", step="post")
        ax2.fill_between(equity_ts, initial, eq_arr,
                         where=(eq_arr < initial), color="#f8514933", step="post")
        ax2.plot(equity_ts, eq_arr, color="#58a6ff", linewidth=0.8, label="Equity")
        ax2.axhline(initial, color="#8b949e", linewidth=0.6, linestyle="--", label="Initial")

    ax2.set_ylabel("Equity (USDT)", fontsize=10)
    ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax2.grid(True, color="#21262d", linewidth=0.5)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax2.xaxis.set_major_locator(mdates.MonthLocator())
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=30, ha="right", fontsize=8)
    ax2.legend(loc="upper left", fontsize=8, facecolor="#161b22",
               labelcolor="#c9d1d9", edgecolor="#30363d")

    plt.tight_layout(rect=[0, 0, 1, 0.97])
    plt.savefig(output_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info(f"Chart saved → {output_path}")
