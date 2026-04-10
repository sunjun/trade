"""回测入口

用法:
    python -m backtest.run_backtest --strategy eth_mtf_swap --capital 10000

参数:
    --strategy   strategies.yaml 中的策略名称（默认 eth_mtf_swap）
    --capital    初始资金 USDT（默认 10000）
    --max-bars   最多拉取 15m K 线根数（默认 10000，约 3 个月）
    --out-dir    输出目录（默认当前目录）
    --no-chart   跳过绘图（仅输出统计和 CSV）
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from pathlib import Path

import yaml
from loguru import logger

# 确保项目根目录在 sys.path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from backtest.data_loader import fetch_all_candles
from backtest.engine import BacktestEngine
from backtest.report import _calc_metrics, export_trades_csv, plot_results, print_report
from gateway.models import InstType, InstrumentInfo


# ── 品种静态信息（避免在回测时发 REST 请求获取合约信息）────────────────────────
# OKX 合约信息（可手动扩展）
INST_INFO_MAP = {
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


def _load_strategy_entry(strategy_name: str, config_path: str = "config/strategies.yaml") -> dict:
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for entry in cfg.get("strategies", []):
        if entry["name"] == strategy_name:
            return entry
    raise ValueError(f"Strategy '{strategy_name}' not found in {config_path}")


def _import_strategy_cls(class_name: str):
    module_name = class_name.lower().replace("strategy", "")
    module = importlib.import_module(f"strategies.{module_name}")
    return getattr(module, class_name)


async def run(args: argparse.Namespace) -> None:
    # ── 加载策略配置 ──────────────────────────────────────────────────────────
    entry = _load_strategy_entry(args.strategy)
    inst_id      = entry["symbol"]
    inst_type    = InstType(entry["inst_type"])
    strategy_cfg = entry.get("config", {})
    strategy_cls = _import_strategy_cls(entry["class"])
    inst_info    = INST_INFO_MAP.get(inst_id)
    if inst_info is None:
        raise ValueError(f"No InstrumentInfo for {inst_id}. Add it to INST_INFO_MAP.")

    logger.info(f"Strategy: {args.strategy} | Symbol: {inst_id} | Capital: {args.capital} USDT")

    # ── 动态读取预热需求 ──────────────────────────────────────────────────────
    from backtest.run_all import _get_strategy_warm_ups
    warm_primary, extra_tfs = _get_strategy_warm_ups(
        strategy_cls, inst_id, inst_type, strategy_cfg, inst_info
    )

    tf_primary = strategy_cfg.get("timeframe", "15m")
    primary_per_day = {"1m": 1440, "3m": 480, "5m": 288, "15m": 96,
                       "30m": 48, "1H": 24, "2H": 12, "4H": 6, "1D": 1}
    bars_per_day = primary_per_day.get(tf_primary, 96)
    days_bt = args.max_bars / bars_per_day

    fetch_primary = args.max_bars + warm_primary + 50

    cache_dir = str(Path(args.out_dir) / "cache")
    force_dl  = args.force_download

    # ── 拉取主时框数据 ────────────────────────────────────────────────────────
    candles_primary = await fetch_all_candles(
        inst_id, tf_primary, max_candles=fetch_primary,
        cache_dir=cache_dir, force_download=force_dl,
    )

    # ── 拉取高时框数据 ────────────────────────────────────────────────────────
    extra_candles: dict[str, list] = {}
    for tf, tf_warm in extra_tfs:
        bars_per_day_tf = primary_per_day.get(tf, 6)
        fetch_tf = tf_warm + int(days_bt * bars_per_day_tf) + 100
        extra_candles[tf] = await fetch_all_candles(
            inst_id, tf, max_candles=fetch_tf,
            cache_dir=cache_dir, force_download=force_dl,
        )

    if len(candles_primary) < warm_primary + 5:
        logger.error(f"Not enough candles: got {len(candles_primary)}, need >{warm_primary}")
        return

    # ── 构建并运行回测引擎 ────────────────────────────────────────────────────
    h1_candles, h4_candles = [], []
    warm_h1 = warm_h4 = 0
    for tf, tf_warm in extra_tfs:
        if "4H" in tf:
            h4_candles, warm_h4 = extra_candles.get(tf, []), tf_warm
        elif "1H" in tf:
            h1_candles, warm_h1 = extra_candles.get(tf, []), tf_warm

    engine = BacktestEngine(
        strategy_cls=strategy_cls,
        strategy_name=args.strategy,
        strategy_config=strategy_cfg,
        inst_id=inst_id,
        inst_info=inst_info,
        initial_capital=args.capital,
        inst_type=inst_type,
    )

    await engine.run(
        candles_m15=candles_primary,
        candles_h1=h1_candles,
        candles_h4=h4_candles,
        warm_up_m15=warm_primary,
        warm_up_h1=warm_h1,
        warm_up_h4=warm_h4,
    )

    # ── 输出目录 ──────────────────────────────────────────────────────────────
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 统计报告 ──────────────────────────────────────────────────────────────
    metrics = _calc_metrics(
        equity_curve=engine.equity_curve,
        initial_capital=engine.initial_capital,
        trades=engine.trades,
        candle_per_day=bars_per_day,
    )
    print_report(metrics)

    # ── CSV ───────────────────────────────────────────────────────────────────
    csv_path = str(out_dir / f"backtest_{args.strategy}_trades.csv")
    export_trades_csv(engine.trades, csv_path)

    # ── 图表 ──────────────────────────────────────────────────────────────────
    if not args.no_chart:
        bt_candles = candles_primary[warm_primary:]
        chart_path = str(out_dir / f"backtest_{args.strategy}_chart.png")
        plot_results(
            candles=bt_candles,
            trades=engine.trades,
            equity_curve=engine.equity_curve,
            equity_ts=engine.equity_timestamps,
            strategy_name=args.strategy,
            output_path=chart_path,
        )



def main() -> None:
    parser = argparse.ArgumentParser(description="策略回测工具")
    parser.add_argument("--strategy", default="eth_mtf_swap", help="策略名称（strategies.yaml 中的 name）")
    parser.add_argument("--capital",  type=float, default=10_000.0, help="初始资金（USDT）")
    parser.add_argument("--max-bars", type=int,   default=10_000,   help="回测期最多 15m K 线根数")
    parser.add_argument("--out-dir",  default=".",                   help="输出目录（CSV + 图表）")
    parser.add_argument("--no-chart",       action="store_true", help="跳过图表生成")
    parser.add_argument("--force-download", action="store_true", help="忽略缓存，强制重新下载所有数据")
    args = parser.parse_args()

    # 配置日志
    from loguru import logger as _logger
    _logger.remove()
    _logger.add(sys.stderr, level="INFO",
                format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
