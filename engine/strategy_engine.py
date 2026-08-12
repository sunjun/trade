"""策略引擎——整合所有组件，管理策略生命周期"""
import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from config.settings import Settings
from engine.base_strategy import CLIENT_TAG_LEN
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
        self._strategy_by_tag: dict[str, Any] = {}  # clOrdId 前缀 -> 策略
        self._tasks: list[asyncio.Task] = []
        self._running = False

    # ── 启动 / 停止 ────────────────────────────────────────────────────────────

    async def start(self):
        logger.info("Starting Strategy Engine...")
        await self._db.init()

        # REST session 覆盖引擎完整生命周期：WS 推送后需要下单/查询时 session 必须有效
        await self._rest.__aenter__()
        try:
            # 初始化持仓视图，并给风控播种权益高水位（否则回撤/日亏损无基准）
            await self._portfolio.refresh(self._rest)
            self._risk.on_equity_update(self._portfolio.get_total_equity())

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
            failed = []
            for strategy in self._strategies:
                try:
                    self._warn_if_risk_limits_too_tight(strategy)
                    await self._setup_strategy(strategy)
                except Exception as e:
                    logger.error(
                        f"[{strategy.name}] Setup failed, removing from active strategies: "
                        f"{type(e).__name__}: {e}",
                        exc_info=True,
                    )
                    failed.append(strategy)
            for s in failed:
                self._strategies.remove(s)

            self._strategy_by_tag = {s.client_tag: s for s in self._strategies}
            if len(self._strategy_by_tag) != len(self._strategies):
                logger.error(
                    "Duplicate clOrdId tags across strategies — order attribution "
                    "will be ambiguous; give the strategies more distinct names"
                )

            if not self._strategies:
                logger.error("All strategies failed to set up, engine will not start")
                return

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
        finally:
            await self._rest.__aexit__(None, None, None)

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

        # 预热后立刻接管交易所上已存在的持仓（进程重启/崩溃恢复）
        await self._adopt_existing_position(strategy)

        # 设置合约杠杆（失败不阻断启动，仅告警——账户可能有挂单导致 OKX 拒绝调整）
        if strategy.inst_type == InstType.SWAP:
            leverage = strategy.config.get("leverage", 1)
            td_mode  = strategy.config.get("td_mode", "cross")
            try:
                await self._rest.set_leverage(symbol, leverage, td_mode)
            except RuntimeError as e:
                logger.warning(
                    f"[{strategy.name}] set_leverage failed (strategy will still run): {e}"
                )

        # 订阅主执行时框实时 K 线
        self._ws.subscribe_candles(symbol, timeframe, strategy.handle_candle)

    async def _adopt_existing_position(self, strategy):
        """启动时若交易所已有该品种持仓，接管进策略状态机。

        不接管的后果：策略以为自己空仓，下一个开仓信号会再开一笔，变成双倍仓位，
        而 reconcile 只会告警不会纠正。

        策略配置 `on_existing_position` 可选：
          adopt（默认）— 接管，由策略按自己的出场逻辑管理
          abort        — 拒绝启动该策略，交由人工处理
        接管失败（如止损价无法重建）一律降级为 abort。
        """
        if strategy.inst_type != InstType.SWAP:
            return  # 现货没有 positions 频道/接口，无从查起

        positions = await self._rest.get_positions(strategy.symbol)
        position = next((p for p in positions if p.size > 0), None)
        if position is None:
            return

        mode = strategy.config.get("on_existing_position", "adopt")
        if mode == "adopt" and strategy.adopt_position(position):
            return

        raise RuntimeError(
            f"交易所已有 {strategy.symbol} 持仓 "
            f"({position.pos_side.value} size={position.size} entry={position.entry_price}) "
            f"但策略未能接管（on_existing_position={mode}）。"
            f"该持仓仍受交易所侧止损保护，请人工确认后再启动。"
        )

    def _warn_if_risk_limits_too_tight(self, strategy):
        """单笔止损打满就会触发熔断时告警——风控阈值与策略仓位/杠杆不匹配。

        只对固定百分比止损（sl_pct）的策略静态估算；ATR 止损无法事先算出。
        """
        sl_pct = strategy.config.get("sl_pct")
        if not sl_pct:
            return
        pct = strategy.config.get("position_size_pct", 0.1)
        lev = strategy.config.get("leverage", 1) if strategy.inst_type == InstType.SWAP else 1
        worst = pct * lev * sl_pct  # 单笔止损打满占账户权益的比例
        risk_cfg = self._settings.risk

        if worst >= risk_cfg.max_daily_loss_pct or worst >= risk_cfg.max_drawdown_pct:
            logger.warning(
                f"[{strategy.name}] 风控阈值可能过紧：单笔止损打满 = "
                f"{pct:.0%} 仓位 × {lev}x 杠杆 × {sl_pct:.0%} 止损 = 账户权益的 {worst:.1%}，"
                f"而日亏损上限 {risk_cfg.max_daily_loss_pct:.1%} / 回撤熔断 {risk_cfg.max_drawdown_pct:.1%}。"
                f"照此设置，一次止损就会暂停该策略或触发全局熔断。"
            )

    # ── WebSocket 事件处理 ─────────────────────────────────────────────────────

    def _strategy_for_order(self, order: Order):
        """按 clOrdId 前 12 位标签把订单回推给下单的策略。

        标签由策略名确定性生成，重启后依然对得上。
        解析不出（外部手工下单、旧订单）时退回按品种匹配，
        但同品种多策略会串扰，所以此时只在恰好唯一命中时才路由。
        """
        tag = order.client_order_id[:CLIENT_TAG_LEN]
        strategy = self._strategy_by_tag.get(tag)
        if strategy is not None:
            return strategy

        candidates = [s for s in self._strategies if s.symbol == order.inst_id]
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            logger.warning(
                f"Order {order.order_id} (clOrdId={order.client_order_id!r}) matches "
                f"{len(candidates)} strategies on {order.inst_id}; cannot attribute, skipping routing"
            )
        return None

    async def _on_order_update(self, orders: list[Order]):
        for order in orders:
            await self._portfolio.on_order_filled(order)
            strategy = self._strategy_for_order(order)
            if strategy is not None:
                order.strategy_name = strategy.name
                await strategy.on_order_update(order)
            # 持久化（按 order_id 幂等 upsert，可能已在下单时写过一次）
            await self._db.save_order(order, order.strategy_name)

    async def _on_position_update(self, positions: list[Position]):
        await self._portfolio.on_position_update(positions)

    # ── 后台循环 ───────────────────────────────────────────────────────────────

    async def _portfolio_refresh_loop(self):
        """每 60 秒通过 REST 全量刷新账户状态，并让各策略对齐本地持仓视图"""
        while self._running:
            await asyncio.sleep(60)
            await self._portfolio.refresh(self._rest)
            # 驱动账户维度风控（高水位 / 回撤熔断 / 日亏损比例重算）
            self._risk.on_equity_update(self._portfolio.get_total_equity())
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
