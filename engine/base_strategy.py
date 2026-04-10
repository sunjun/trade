"""策略基类——所有策略必须继承此类"""
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from loguru import logger

from gateway.models import Candle, InstType, Order, Position, Signal

if TYPE_CHECKING:
    from engine.portfolio import Portfolio
    from engine.risk_manager import RiskManager
    from gateway.okx_rest import OKXRestClient
    from storage.db import Database


class BaseStrategy(ABC):
    def __init__(
        self,
        name: str,
        inst_type: InstType,
        symbol: str,
        config: dict,
        rest: "OKXRestClient",
        risk: "RiskManager",
        portfolio: "Portfolio",
        db: "Database",
    ):
        self.name = name
        self.inst_type = inst_type
        self.symbol = symbol
        self.config = config
        self._rest = rest
        self._risk = risk
        self._portfolio = portfolio
        self._db = db

        self._running = False
        self._warm_up_done = False   # 历史数据预热完成标志

    # ── 子类实现 ───────────────────────────────────────────────────────────────

    @abstractmethod
    async def on_candle(self, candle: Candle) -> list[Signal]:
        """收到新K线时调用。返回信号列表（空列表=无操作）。
        预热期间此方法也会被调用，但信号不会被执行。"""

    async def on_order_update(self, order: Order):
        """订单状态变更回调（可选override）"""

    async def on_start(self):
        """策略启动前的初始化（可选override）"""

    async def on_stop(self):
        """策略停止时的清理（可选override）"""

    def reset_position_state(self):
        """重置策略内部持仓状态机为 FLAT（预热结束或外部强制平仓后调用）。
        默认实现：若子类持有 `_state` 且其有 `close()` 方法，则调用。
        子类可 override 实现更复杂的重置逻辑。"""
        state = getattr(self, "_state", None)
        if state is not None and hasattr(state, "close"):
            state.close()

    def reconcile_position(self, position: "Position | None"):
        """将策略本地状态与交易所真实持仓对齐。引擎在 REST 刷新后调用。
        默认实现：
          - 本地认为无仓但交易所有仓：仅告警（可能是外部手动开仓，不接管）
          - 本地认为有仓但交易所无仓：重置为 FLAT（爆仓/手动平仓/交易所SL触发后的恢复）
        子类可 override 以实现更精细的对齐（如 Grid 清理 slots）。
        """
        state = getattr(self, "_state", None)
        if state is None:
            return

        exchange_has = position is not None and position.size > 0
        local_has = not getattr(state, "flat", True)

        if local_has and not exchange_has:
            logger.warning(
                f"[{self.name}] Reconcile: local state has position but exchange does not; "
                f"resetting to FLAT (likely closed externally or SL triggered)"
            )
            self.reset_position_state()
        elif exchange_has and not local_has:
            logger.warning(
                f"[{self.name}] Reconcile: exchange has position {position.pos_side.value} "
                f"size={position.size} but local state is FLAT; ignoring (not adopting)"
            )

    # ── 引擎调用 ───────────────────────────────────────────────────────────────

    async def handle_candle(self, candles: list[Candle]):
        """由引擎调用，处理来自WS的K线数据"""
        for candle in candles:
            signals = await self.on_candle(candle)
            if not self._warm_up_done or not candle.confirmed:
                continue
            for signal in signals:
                await self._execute_signal(signal)

    async def _execute_signal(self, signal: Signal):
        """经过风控检查后下单"""
        logger.info(
            f"[{self.name}] >>> Signal: {signal.side.value.upper()} {signal.inst_id} "
            f"type={signal.order_type.value} pos={signal.pos_side.value} "
            f"sl={signal.stop_loss} | {signal.reason}"
        )

        allowed, reason = self._risk.check_signal(signal, self._portfolio, self.name)
        if not allowed:
            logger.warning(f"[{self.name}] Signal BLOCKED by risk: {reason}")
            return

        qty = await self._calc_qty(signal)
        if qty <= 0:
            avail = self._portfolio.get_available("USDT")
            logger.warning(
                f"[{self.name}] Signal SKIPPED: qty=0 "
                f"(available={avail:.2f} USDT, position_size_pct={self.config.get('position_size_pct')})"
            )
            return
        signal.qty = qty

        logger.info(
            f"[{self.name}] Placing order: {signal.side.value.upper()} "
            f"{qty} {signal.inst_id} @ MARKET"
        )
        # 带 stop_loss 的信号视为"开仓"，失败时应回滚本地状态，避免策略以为已开仓
        is_open_signal = signal.stop_loss is not None
        order = Signal.to_order(signal, self.name)
        try:
            order = await self._rest.place_order(order, self.inst_type)
            await self._db.save_order(order, self.name)
            self._risk.on_order_sent(self.name)
            logger.info(
                f"[{self.name}] Order PLACED ✓ id={order.order_id} "
                f"{signal.side.value.upper()} {qty} {signal.inst_id}"
            )
        except RuntimeError as e:
            logger.error(f"[{self.name}] Order FAILED: {e}")
            if is_open_signal:
                logger.critical(
                    f"[{self.name}] Rolling back local state to FLAT after open-signal failure"
                )
                self.reset_position_state()
            # 平仓失败时保留 _state，下根 K 线或下次 reconcile 会重试/修正

    async def _calc_qty(self, signal: Signal) -> float:
        """根据账户余额和配置计算下单量"""
        pct = self.config.get("position_size_pct", 0.1)
        ticker = await self._rest.get_ticker(signal.inst_id)
        price = ticker.last

        if self.inst_type == InstType.SPOT:
            balance = self._portfolio.get_available("USDT")
            usdt_amount = balance * pct
            info = await self._rest.get_instrument(signal.inst_id, self.inst_type)
            qty = usdt_amount / price
            # 按 lot_sz 向下取整
            import math
            if info.lot_sz > 0:
                precision = max(0, -int(math.floor(math.log10(info.lot_sz))))
                factor = 10 ** precision
                qty = math.floor(qty * factor / (info.lot_sz * factor)) * info.lot_sz
            return qty if qty >= info.min_sz else 0.0

        else:  # SWAP
            balance = self._portfolio.get_available("USDT")
            leverage = self.config.get("leverage", 1)
            info = await self._rest.get_instrument(signal.inst_id, self.inst_type)
            notional = balance * pct * leverage
            # 合约张数 = notional / (ct_val * price)
            import math
            contracts = math.floor(notional / (info.ct_val * price))
            return float(contracts) if contracts >= info.min_sz else 0.0


# ── Signal → Order 转换（挂在 Signal 上方便使用）───────────────────────────────
def _signal_to_order(signal: Signal, strategy_name: str) -> Order:
    from gateway.models import Order
    return Order(
        inst_id=signal.inst_id,
        side=signal.side,
        order_type=signal.order_type,
        qty=signal.qty,
        price=signal.price,
        pos_side=signal.pos_side,
        strategy_name=strategy_name,
        stop_loss=signal.stop_loss,
    )


Signal.to_order = _signal_to_order  # type: ignore[attr-defined]
