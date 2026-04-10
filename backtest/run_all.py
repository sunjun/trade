"""批量回测所有策略，输出统一报告

用法:
    python -m backtest.run_all --capital 10000 --max-bars 10000 --out-dir backtest_results

参数:
    --capital        初始资金 USDT（默认 10000）
    --max-bars       回测期主时框 K 线数（默认 10000）
    --out-dir        输出目录（默认 backtest_results）
    --no-chart       不生成图表
    --force-download 强制重新下载所有数据（忽略缓存）
    --strategies     指定策略名列表（逗号分隔），默认跑全部
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml
from loguru import logger

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from backtest.data_loader import fetch_all_candles
from backtest.engine import BacktestEngine
from backtest.report import _calc_metrics, export_trades_csv, plot_results, print_report
from gateway.models import InstType, InstrumentInfo

# ── 合约静态信息表（与 run_backtest.py 保持同步）─────────────────────────────
INST_INFO_MAP: dict[str, InstrumentInfo] = {
    "ETH-USDT-SWAP": InstrumentInfo(
        inst_id="ETH-USDT-SWAP", inst_type=InstType.SWAP,
        base_ccy="ETH", quote_ccy="USDT",
        lot_sz=1.0, min_sz=1.0, ct_val=0.01, tick_sz=0.01,
    ),
    "BTC-USDT-SWAP": InstrumentInfo(
        inst_id="BTC-USDT-SWAP", inst_type=InstType.SWAP,
        base_ccy="BTC", quote_ccy="USDT",
        lot_sz=1.0, min_sz=1.0, ct_val=0.01, tick_sz=0.1,
    ),
    "ETH-USDT": InstrumentInfo(
        inst_id="ETH-USDT", inst_type=InstType.SPOT,
        base_ccy="ETH", quote_ccy="USDT",
        lot_sz=0.001, min_sz=0.001, ct_val=1.0, tick_sz=0.01,
    ),
    "BTC-USDT": InstrumentInfo(
        inst_id="BTC-USDT", inst_type=InstType.SPOT,
        base_ccy="BTC", quote_ccy="USDT",
        lot_sz=0.00001, min_sz=0.00001, ct_val=1.0, tick_sz=0.1,
    ),
}


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _load_all_strategies(config_path: str = "config/strategies.yaml") -> list[dict]:
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("strategies", [])


def _import_strategy_cls(class_name: str):
    module_name = class_name.lower().replace("strategy", "")
    module = importlib.import_module(f"strategies.{module_name}")
    return getattr(module, class_name)


def _get_strategy_warm_ups(strategy_cls, inst_id: str, inst_type: InstType,
                            config: dict, inst_info: InstrumentInfo):
    """实例化策略读取预热需求，避免硬编码各策略的参数名。"""
    from backtest.engine import BacktestDB, BacktestPortfolio, BacktestRest
    from engine.risk_manager import RiskManager

    lev = config.get("leverage", 1)
    port = BacktestPortfolio(10_000, inst_info.ct_val, lev)
    rest = BacktestRest(port, inst_info)
    db   = BacktestDB()
    risk = RiskManager(max_position_pct=1.0, max_daily_loss_pct=1.0,
                       max_drawdown_pct=1.0, order_rate_limit=9999)

    s = strategy_cls(name="_probe", inst_type=inst_type, symbol=inst_id,
                     config=config, rest=rest, risk=risk, portfolio=port, db=db)

    warm_primary = getattr(s, "warm_up_period", 60) + 10
    extra_tfs: list[tuple[str, int]] = []  # [(timeframe, warm_candles)]
    if hasattr(s, "extra_tf_configs"):
        for tf, tf_warm, _ in s.extra_tf_configs:
            extra_tfs.append((tf, tf_warm + 5))

    return warm_primary, extra_tfs


def _format_report_block(name: str, metrics: dict) -> str:
    sep = "─" * 52
    lines = [
        f"\n{'═'*52}",
        f"  Strategy : {name}",
        sep,
        f"  初始资金:      {metrics['initial_capital']:>12.2f} USDT",
        f"  最终权益:      {metrics['final_equity']:>12.2f} USDT",
        f"  总收益率:      {metrics['total_return_pct']:>11.2f}%",
        f"  年化收益率:    {metrics['annual_return_pct']:>11.2f}%  ({metrics['years']:.2f} 年)",
        f"  最大回撤:      {metrics['max_drawdown_pct']:>11.2f}%",
        f"  Sharpe 比率:   {metrics['sharpe']:>12.2f}",
        sep,
        f"  交易次数:      {metrics['total_trades']:>12d}",
        f"  胜率:          {metrics['win_rate_pct']:>11.2f}%",
        f"  平均盈利:      {metrics['avg_win_usdt']:>12.2f} USDT",
        f"  平均亏损:      {metrics['avg_loss_usdt']:>12.2f} USDT",
        f"  盈亏比:        {metrics['profit_factor']:>12.2f}",
        f"  净盈亏:        {metrics['total_pnl_usdt']:>12.2f} USDT",
        f"  止损触发次数:  {metrics['sl_hits']:>12d}",
        sep,
    ]
    return "\n".join(lines)


def _format_summary_table(results: list[dict]) -> str:
    """生成所有策略的横向对比表格。"""
    header = (
        f"\n{'策略名':<22} {'年化收益%':>9} {'最大回撤%':>9} "
        f"{'Sharpe':>7} {'胜率%':>7} {'PF':>6} {'净盈亏':>9} {'交易数':>6}"
    )
    sep = "─" * 80
    rows = [header, sep]
    for r in results:
        m = r["metrics"]
        status = r.get("status", "ok")
        if status != "ok":
            rows.append(f"  {r['name']:<20} {'ERROR: ' + status[:40]}")
            continue
        rows.append(
            f"  {r['name']:<20} "
            f"{m['annual_return_pct']:>9.2f} "
            f"{m['max_drawdown_pct']:>9.2f} "
            f"{m['sharpe']:>7.2f} "
            f"{m['win_rate_pct']:>7.2f} "
            f"{m['profit_factor']:>6.2f} "
            f"{m['total_pnl_usdt']:>9.2f} "
            f"{m['total_trades']:>6d}"
        )
    rows.append(sep)
    return "\n".join(rows)


# ── 单策略回测 ────────────────────────────────────────────────────────────────

async def run_one(
    entry: dict,
    capital: float,
    max_bars: int,
    cache_dir: str,
    out_dir: Path,
    force_download: bool,
    no_chart: bool,
) -> dict:
    """回测单个策略，返回结果字典。"""
    name       = entry["name"]
    class_name = entry["class"]
    inst_id    = entry["symbol"]
    inst_type  = InstType(entry["inst_type"])
    config     = entry.get("config", {})

    inst_info = INST_INFO_MAP.get(inst_id)
    if inst_info is None:
        return {"name": name, "status": f"no InstrumentInfo for {inst_id}", "metrics": {}}

    try:
        strategy_cls = _import_strategy_cls(class_name)
    except Exception as e:
        return {"name": name, "status": f"import error: {e}", "metrics": {}}

    # ── 读取预热需求 ──────────────────────────────────────────────────────────
    try:
        warm_primary, extra_tfs = _get_strategy_warm_ups(
            strategy_cls, inst_id, inst_type, config, inst_info
        )
    except Exception as e:
        return {"name": name, "status": f"init error: {e}", "metrics": {}}

    tf_primary = config.get("timeframe", "15m")

    # 主时框K线总量 = 回测期 + 预热量
    fetch_primary = max_bars + warm_primary + 50

    # 高时框拉取量：按主时框换算天数
    primary_per_day = {"1m": 1440, "3m": 480, "5m": 288, "15m": 96,
                       "30m": 48, "1H": 24, "2H": 12, "4H": 6, "1D": 1}
    bars_per_day = primary_per_day.get(tf_primary, 96)
    days_bt = max_bars / bars_per_day

    logger.info(f"\n{'─'*50}")
    logger.info(f"Running backtest: {name}  [{inst_id}]  tf={tf_primary}")

    # ── 拉取数据 ──────────────────────────────────────────────────────────────
    try:
        candles_primary = await fetch_all_candles(
            inst_id, tf_primary, max_candles=fetch_primary,
            cache_dir=cache_dir, force_download=force_download,
        )

        # 高时框（如 MtfTrendStrategy 的 1H / 4H）
        extra_candles: dict[str, list] = {}
        for tf, tf_warm in extra_tfs:
            bars_per_day_tf = primary_per_day.get(tf, 6)
            fetch_tf = tf_warm + int(days_bt * bars_per_day_tf) + 100
            extra_candles[tf] = await fetch_all_candles(
                inst_id, tf, max_candles=fetch_tf,
                cache_dir=cache_dir, force_download=force_download,
            )
    except Exception as e:
        return {"name": name, "status": f"data error: {e}", "metrics": {}}

    if len(candles_primary) < warm_primary + 5:
        return {"name": name, "status": "not enough candles", "metrics": {}}

    # ── 运行回测引擎 ──────────────────────────────────────────────────────────
    try:
        engine = BacktestEngine(
            strategy_cls=strategy_cls,
            strategy_name=name,
            strategy_config=config,
            inst_id=inst_id,
            inst_info=inst_info,
            initial_capital=capital,
            inst_type=inst_type,
        )

        # 构建高时框参数（按 extra_tf_configs 的 (tf, warm) 顺序）
        h1_candles: list = []
        h4_candles: list = []
        warm_h1 = warm_h4 = 0

        for tf, tf_warm in extra_tfs:
            if "4H" in tf or "4h" in tf:
                h4_candles = extra_candles.get(tf, [])
                warm_h4 = tf_warm
            elif "1H" in tf or "1h" in tf:
                h1_candles = extra_candles.get(tf, [])
                warm_h1 = tf_warm

        await engine.run(
            candles_m15=candles_primary,
            candles_h1=h1_candles,
            candles_h4=h4_candles,
            warm_up_m15=warm_primary,
            warm_up_h1=warm_h1,
            warm_up_h4=warm_h4,
        )
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"[{name}] Backtest failed:\n{tb}")
        return {"name": name, "status": f"runtime error: {e}", "metrics": {}}

    # ── 统计 ──────────────────────────────────────────────────────────────────
    # 主时框每天根数用于年化计算
    metrics = _calc_metrics(
        equity_curve=engine.equity_curve,
        initial_capital=engine.initial_capital,
        trades=engine.trades,
        candle_per_day=bars_per_day,
    )

    # ── 输出子目录 ────────────────────────────────────────────────────────────
    strat_dir = out_dir / name
    strat_dir.mkdir(parents=True, exist_ok=True)

    csv_path = str(strat_dir / "trades.csv")
    export_trades_csv(engine.trades, csv_path)

    if not no_chart:
        try:
            bt_candles = candles_primary[warm_primary:]
            chart_path = str(strat_dir / "chart.png")
            plot_results(
                candles=bt_candles,
                trades=engine.trades,
                equity_curve=engine.equity_curve,
                equity_ts=engine.equity_timestamps,
                strategy_name=name,
                output_path=chart_path,
            )
        except Exception as e:
            logger.warning(f"[{name}] Chart failed: {e}")

    return {"name": name, "status": "ok", "metrics": metrics}


# ── 主入口 ────────────────────────────────────────────────────────────────────

async def run_all(args: argparse.Namespace) -> None:
    out_dir   = Path(args.out_dir)
    cache_dir = str(out_dir / "cache")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 加载策略配置
    all_entries = _load_all_strategies()

    # 筛选指定策略（默认全部）
    if args.strategies:
        names = {s.strip() for s in args.strategies.split(",")}
        all_entries = [e for e in all_entries if e["name"] in names]
        if not all_entries:
            logger.error(f"No matching strategies found for: {args.strategies}")
            return

    logger.info(f"Will backtest {len(all_entries)} strategies: "
                f"{[e['name'] for e in all_entries]}")

    # 逐一运行
    results: list[dict] = []
    for entry in all_entries:
        result = await run_one(
            entry=entry,
            capital=args.capital,
            max_bars=args.max_bars,
            cache_dir=cache_dir,
            out_dir=out_dir,
            force_download=args.force_download,
            no_chart=args.no_chart,
        )
        results.append(result)
        if result["status"] == "ok":
            print_report(result["metrics"])  # 实时打印单策略报告

    # ── 写统一日志文件 ────────────────────────────────────────────────────────
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"all_backtest_{ts_str}.log"

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"All Strategies Backtest Report\n")
        f.write(f"Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Capital   : {args.capital:.2f} USDT\n")
        f.write(f"Max Bars  : {args.max_bars}\n")
        f.write("=" * 52 + "\n")

        # 各策略详细报告
        for r in results:
            if r["status"] == "ok":
                f.write(_format_report_block(r["name"], r["metrics"]) + "\n")
            else:
                f.write(f"\n{'═'*52}\n")
                f.write(f"  Strategy : {r['name']}\n")
                f.write(f"  STATUS   : FAILED — {r['status']}\n")
                f.write("─" * 52 + "\n")

        # 汇总对比表
        summary = _format_summary_table(results)
        f.write("\n\n" + "=" * 52 + "\n")
        f.write("SUMMARY COMPARISON\n")
        f.write(summary + "\n")

    # 终端也打印汇总
    print("\n" + "=" * 80)
    print("SUMMARY COMPARISON")
    print(_format_summary_table(results))
    print(f"\nFull report saved → {log_path}")
    ok_count  = sum(1 for r in results if r["status"] == "ok")
    err_count = len(results) - ok_count
    print(f"Done: {ok_count} succeeded, {err_count} failed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="批量回测所有策略")
    parser.add_argument("--capital",         type=float, default=10_000.0,
                        help="初始资金（USDT）")
    parser.add_argument("--max-bars",        type=int,   default=10_000,
                        help="回测期主时框 K 线数")
    parser.add_argument("--out-dir",         default="backtest_results",
                        help="输出目录（含缓存、CSV、图表、报告）")
    parser.add_argument("--no-chart",        action="store_true",
                        help="跳过图表生成")
    parser.add_argument("--force-download",  action="store_true",
                        help="忽略缓存，强制重新下载")
    parser.add_argument("--strategies",      default="",
                        help="仅回测指定策略（逗号分隔名称，默认全部）")
    args = parser.parse_args()

    from loguru import logger as _logger
    _logger.remove()
    _logger.add(sys.stderr, level="INFO",
                format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")

    asyncio.run(run_all(args))


if __name__ == "__main__":
    main()
