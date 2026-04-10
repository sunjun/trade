"""策略引擎——整合所有组件，管理策略生命周期"""
import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from config.settings import Settings
from engine.portfolio import Portfolio
from engine.risk_manager import RiskManager
from gateway.models import InstType, Order, Position
from gateway.okx_rest import OKXRestClient
from gateway.okx_ws import OKXWebSocketClient
from storage.db import Database


class StrategyEngine:
    def __init__(self, settings: Settings):
        self._settings = settings
        okx = settings.okx
        risk_cfg = settings.risk

        self._rest = OKXRestClient(
            okx.api_key, okx.secret_key, okx.passphrase, okx.is_demo
        )
        self._ws = OKXWebSocketClient(
            okx.api_key, okx.secret_key, okx.passphrase, okx.is_demo
        )
        self._portfolio = Portfolio()
        self._risk = RiskManager(
            max_position_pct=risk_cfg.max_position_pct,
            max_daily_loss_pct=risk_cfg.max_daily_loss_pct,
            max_drawdown_pct=risk_cfg.max_drawdown_pct,
            order_rate_limit=risk_cfg.order_rate_limit,
        )
        self._db = Database(settings.db_path)
        self._strategies: list[Any] = []
        self._tasks: list[asyncio.Task] = []
        self._running = False

    # ── 启动 / 停止 ────────────────────────────────────────────────────────────

    async def start(self):
        logger.info("Starting Strategy Engine...")
        await self._db.init()

        async with self._rest:
            # 初始化持仓视图
            await self._portfolio.refresh(self._rest)

            # 加载策略配置
            self._strategies = await self._load_strategies()
            if not self._strategies:
                logger.warning("No enabled strategies found in config")
                return

            # 订阅私有频道（订单/持仓）
            inst_types = {s.inst_type.value for s in self._strategies}
            for it in inst_types:
                self._ws.subscribe_orders(it, self._on_order_update)
                self._ws.subscribe_positions(it, self._on_position_update)

            # 订阅各策略行情，并完成历史数据预热
            for strategy in self._strategies:
                await self._setup_strategy(strategy)

            # 启动 WebSocket
            await self._ws.start()

            self._running = True
            logger.info(f"Engine started with {len(self._strategies)} strategy(s)")

            # 启动后台任务
            self._tasks = [
                asyncio.create_task(self._portfolio_refresh_loop(), name="portfolio-refresh"),
                asyncio.create_task(self._daily_reset_loop(), name="daily-reset"),
            ]

            # 阻塞直到停止信号
            try:
                await asyncio.gather(*self._tasks)
            except asyncio.CancelledError:
                pass

    async def stop(self):
        logger.info("Stopping Strategy Engine...")
        self._running = False
        for t in self._tasks:
            t.cancel()
        await self._ws.stop()
        for strategy in self._strategies:
            try:
                await strategy.on_stop()
            except Exception as e:
                logger.error(f"Strategy {strategy.name} stop error: {e}")
        logger.info("Engine stopped")

    # ── 策略加载 ───────────────────────────────────────────────────────────────

    async def _load_strategies(self) -> list[Any]:
        config_path = Path(self._settings.strategy_config)
        if not config_path.exists():
            logger.error(f"Strategy config not found: {config_path}")
            return []

        with open(config_path, encoding='utf-8') as f:
            cfg = yaml.safe_load(f)

        strategies = []
        for entry in cfg.get("strategies", []):
            if not entry.get("enabled", True):
                logger.info(f"Strategy '{entry['name']}' is disabled, skipping")
                continue
            try:
                strategy = self._instantiate_strategy(entry)
                await strategy.on_start()
                strategies.append(strategy)
                logger.info(f"Loaded strategy: {strategy.name} [{strategy.symbol}]")
            except Exception as e:
                logger.error(f"Failed to load strategy '{entry.get('name')}': {e}", exc_info=True)
        return strategies

    def _instantiate_strategy(self, entry: dict) -> Any:
        class_name = entry["class"]
        # TrendStrategy -> strategies.trend, GridStrategy -> strategies.grid, 以此类推
        module_name = class_name.lower().replace("strategy", "")
        module = importlib.import_module(f"strategies.{module_name}")
        cls = getattr(module, class_name)
        return cls(
            name=entry["name"],
            inst_type=InstType(entry["inst_type"]),
            symbol=entry["symbol"],
            config=entry.get("config", {}),
            rest=self._rest,
            risk=self._risk,
            portfolio=self._portfolio,
            db=self._db,
        )

    async def _setup_strategy(self, strategy):
        """预热历史K线，然后订阅实时行情。
        支持多时框策略：若策略暴露 extra_tf_configs 属性，
        则额外预热并订阅更高时框的 K 线。
        """
        symbol    = strategy.symbol
        timeframe = strategy.config.get("timeframe", "15m")
        warm_up   = strategy.warm_up_period

        # ── 先预热额外时框（4H / 1H），使高时框指标在主周期启动前就绪 ──────────
        if hasattr(strategy, "extra_tf_configs"):
            for tf, tf_warm_up, handler in strategy.extra_tf_configs:
                logger.info(
                    f"[{strategy.name}] Warming up {tf_warm_up} candles ({tf}) [extra TF]...")
                extra_candles = await self._rest.get_candles(
                    symbol, tf, limit=tf_warm_up + 5)
                for candle in extra_candles:
                    candle.confirmed = True
                    await handler([candle])
                # 通知策略该时框预热完成
                if hasattr(strategy, "on_extra_tf_warmed"):
                    strategy.on_extra_tf_warmed(tf)
                # 订阅该时框实时 K 线
                self._ws.subscribe_candles(symbol, tf, handler)
                logger.info(f"[{strategy.name}] {tf} warm-up done, subscribed")

        # ── 预热主执行时框（15M）──────────────────────────────────────────────
        logger.info(f"[{strategy.name}] Warming up {warm_up} candles ({timeframe})...")
        candles = await self._rest.get_candles(symbol, timeframe, limit=warm_up + 5)
        for candle in candles:
            candle.confirmed = True
            await strategy.on_candle(candle)
        strategy._warm_up_done = True
        strategy.reset_position_state()   # 重置为 FLAT，避免预热期间的虚假信号污染状态机
        logger.info(f"[{strategy.name}] Warm-up complete, state reset to FLAT")

        # 设置合约杠杆
        if strategy.inst_type == InstType.SWAP:
            leverage = strategy.config.get("leverage", 1)
            td_mode  = strategy.config.get("td_mode", "cross")
            await self._rest.set_leverage(symbol, leverage, td_mode)

        # 订阅主执行时框实时 K 线
        self._ws.subscribe_candles(symbol, timeframe, strategy.handle_candle)

    # ── WebSocket 事件处理 ─────────────────────────────────────────────────────

    async def _on_order_update(self, orders: list[Order]):
        for order in orders:
            await self._portfolio.on_order_filled(order)
            # 路由给对应策略
            for strategy in self._strategies:
                if order.strategy_name == strategy.name or order.inst_id == strategy.symbol:
                    await strategy.on_order_update(order)
            # 持久化
            await self._db.save_order(order, order.strategy_name)

    async def _on_position_update(self, positions: list[Position]):
        await self._portfolio.on_position_update(positions)

    # ── 后台循环 ───────────────────────────────────────────────────────────────

    async def _portfolio_refresh_loop(self):
        """每 60 秒通过 REST 全量刷新账户状态，并让各策略对齐本地持仓视图"""
        while self._running:
            await asyncio.sleep(60)
            await self._portfolio.refresh(self._rest)
            # 刷新后让每个策略用真实持仓修正本地状态（爆仓/外部平仓/交易所SL触发等）
            for strategy in self._strategies:
                try:
                    # 合约走 pos_side 分仓；现货统一 NET
                    pos_long  = self._portfolio.get_position(strategy.symbol, "long")
                    pos_short = self._portfolio.get_position(strategy.symbol, "short")
                    pos_net   = self._portfolio.get_position(strategy.symbol, "net")
                    position = pos_long or pos_short or pos_net
                    strategy.reconcile_position(position)
                except Exception as e:
                    logger.error(f"[{strategy.name}] reconcile failed: {e}")

    async def _daily_reset_loop(self):
        """每天 UTC 00:01 重置日内风控统计"""
        while self._running:
            now = datetime.now(timezone.utc)
            # 计算到明天 00:01 的秒数
            tomorrow = now.replace(hour=0, minute=1, second=0, microsecond=0)
            if tomorrow <= now:
                tomorrow = tomorrow + timedelta(days=1)
            await asyncio.sleep((tomorrow - now).total_seconds())
            self._risk.reset_daily()
