"""策略基类——所有策略必须继承此类"""
import hashlib
import re
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from loguru import logger

from gateway.models import Candle, InstType, Order, Position, PosSide, Signal
from gateway.precision import round_qty

# clOrdId 中标识策略的前缀长度（8 位可读名 + 4 位哈希）
CLIENT_TAG_LEN = 12


def make_client_tag(strategy_name: str) -> str:
    """由策略名生成固定 12 字符的 clOrdId 前缀。

    OKX 的 clOrdId 只接受字母和数字（不允许下划线等分隔符），最长 32 字符——
    所以旧代码 `clOrdId.split("_")[0]` 那套约定在 OKX 上根本无法成立。
    这里改成定长前缀：前 8 位取策略名的字母数字（便于在 OKX 后台肉眼识别），
    后 4 位取策略名的哈希（保证不同策略前缀不会撞车，且定长可精确切片）。
    """
    readable = re.sub(r"[^A-Za-z0-9]", "", strategy_name)[:8].ljust(8, "0")
    digest = hashlib.md5(strategy_name.encode()).hexdigest()[:4]
    return readable + digest

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

        self.client_tag = make_client_tag(name)  # clOrdId 前缀，用于回推订单归属
        self._order_seq = 0

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

    def adopt_position(self, position: "Position") -> bool:
        """把交易所上已存在的持仓接管进策略状态机（进程重启/崩溃恢复时由引擎调用）。

        不接管的后果是：策略以为自己空仓，下一个开仓信号会再开一笔，变成双倍仓位。
        返回 False 表示无法安全接管（引擎会据此拒绝启动该策略）。
        """
        state = getattr(self, "_state", None)
        if state is None:
            logger.error(f"[{self.name}] No position state machine, cannot adopt")
            return False

        stop_loss = self._recompute_stop_loss(position.entry_price, position.pos_side)
        if stop_loss is None:
            logger.critical(
                f"[{self.name}] Cannot rebuild stop-loss for existing "
                f"{position.pos_side.value} position (entry={position.entry_price}); "
                f"refusing to adopt — 本地止损缺失比双开更危险"
            )
            return False

        state.open(position.pos_side, position.entry_price, stop_loss)
        logger.warning(
            f"[{self.name}] ADOPTED existing exchange position: "
            f"{position.pos_side.value} size={position.size} "
            f"entry={position.entry_price:.4f} → rebuilt SL={stop_loss:.4f}"
        )
        return True

    def _recompute_stop_loss(
        self, entry_price: float, pos_side: "PosSide"
    ) -> float | None:
        """接管持仓时重建本地止损价。返回 None 表示无法重建。

        默认实现按 ATR 止损（`_atr` + `_sl_mult`），与 trend / bbrsi / donchian /
        vwap 的开仓公式一致。止损公式不同的策略需 override（如 RightSideStrategy
        用固定百分比、MtfTrendStrategy 的 ATR 挂在 `_m15` 上）。
        """
        atr = getattr(self, "_atr", None)
        mult = getattr(self, "_sl_mult", None)
        if atr is None or mult is None or not atr.ready:
            return None
        delta = mult * atr.value
        return entry_price + delta if pos_side == PosSide.SHORT else entry_price - delta

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
        """由引擎调用，处理来自WS的K线数据。

        未收盘K线（confirmed=False）直接丢弃，不进入 on_candle。
        OKX 的 candle 频道在一根K线未收盘期间会随每笔成交推送一次，
        若把这些盘中快照喂给指标，EMA/MACD/ATR 会在同一根K线内被更新
        成百上千次，实盘指标将完全失真（且与只喂收盘K线的回测行为不一致）。
        """
        for candle in candles:
            if not candle.confirmed:
                continue
            signals = await self.on_candle(candle)
            if not self._warm_up_done:
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
        # 开仓信号下单失败时要回滚本地状态，避免策略以为已开仓
        is_open_signal = not signal.reduce_only
        # 平仓腿的已实现盈亏要在下单前取快照（成交后持仓就没了），下单成功后再报给风控
        closing_pnl = None if is_open_signal else self._snapshot_close_pnl(signal)
        order = Signal.to_order(signal, self.name)
        order.client_order_id = self._next_client_order_id()
        order.realized_pnl = closing_pnl
        try:
            order = await self._rest.place_order(order, self.inst_type)
            await self._db.save_order(order, self.name)
            self._risk.on_order_sent(self.name)
            if closing_pnl is not None:
                self._risk.on_realized_pnl(self.name, closing_pnl)
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

    def _next_client_order_id(self) -> str:
        """生成本次下单的 clOrdId：12 位策略标签 + 13 位毫秒时间戳 + 3 位序号 = 28 字符。
        引擎按前 12 位把交易所推回来的订单路由回本策略。"""
        self._order_seq = (self._order_seq + 1) % 1000
        return f"{self.client_tag}{int(time.time() * 1000)}{self._order_seq:03d}"

    def _snapshot_close_pnl(self, signal: Signal) -> float | None:
        """平仓/减仓下单前，估算本次平仓腿的已实现盈亏（USDT），用于风控日亏损统计。

        取交易所侧的未实现盈亏（OKX 的 upl，按标记价计），再按本次平掉的比例折算。
        与最终成交价会有滑点/手续费级别的偏差，作为熔断触发器足够。
        现货没有 positions 概念（Portfolio 不跟踪），返回 None 表示无法统计。
        """
        pos = self._portfolio.get_position(signal.inst_id, signal.pos_side.value)
        if pos is None or pos.size <= 0:
            return None
        ratio = min(signal.qty / pos.size, 1.0) if signal.qty > 0 else 1.0
        return pos.unrealized_pnl * ratio

    def _entry_pct(self) -> float:
        """开仓使用的资金比例。子类可覆盖（如网格按格数分摊）。"""
        return self.config.get("position_size_pct", 0.1)

    async def _calc_qty(self, signal: Signal) -> float:
        """根据账户余额和配置计算开仓量。

        平仓/减仓信号的 qty 已由策略按交易所真实持仓算好（见
        `_base_state.build_close_signal` 与各策略的 `_reduce_signal`），
        绝不能用开仓的仓位公式覆盖——否则平仓下的是错误张数，
        会在交易所留下无人管理的残仓。
        """
        if signal.qty > 0:
            return signal.qty

        pct = self._entry_pct()
        ticker = await self._rest.get_ticker(signal.inst_id)
        price = ticker.last

        balance = self._portfolio.get_available("USDT")
        info = await self._rest.get_instrument(signal.inst_id, self.inst_type)

        if self.inst_type == InstType.SPOT:
            qty = balance * pct / price
        else:  # SWAP：张数 = 名义价值 / (合约面值 × 价格)
            leverage = self.config.get("leverage", 1)
            qty = balance * pct * leverage / (info.ct_val * price)

        return round_qty(qty, info.lot_sz, info.min_sz)


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
        reduce_only=signal.reduce_only,
    )


Signal.to_order = _signal_to_order  # type: ignore[attr-defined]
