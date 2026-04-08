"""网格策略：在固定价格区间内等分做多/做空

逻辑：
  将 [grid_lower, grid_upper] 区间等分为 n_grids 格，形成 n_grids+1 条价格线。
  价格每下穿一条格线：开一个 long 槽（买入一格）
  价格每上穿一条格线：平最近一个 long 槽（卖出一格）
  SWAP 双向模式（grid_dual=true）：
    价格每上穿一条格线同时：开一个 short 槽
    价格每下穿一条格线同时：平最近一个 short 槽

  价格跌破下轨或涨破上轨时：触发边界止损，平掉所有槽

每个槽的仓位大小 = position_size_pct / n_grids × 账户可用余额
"""
import math
from typing import TYPE_CHECKING

from loguru import logger

from engine.base_strategy import BaseStrategy
from gateway.models import (
    Candle, InstType, Order, OrderSide, OrderStatus, OrderType, PosSide, Signal,
)

if TYPE_CHECKING:
    from engine.portfolio import Portfolio
    from engine.risk_manager import RiskManager
    from gateway.okx_rest import OKXRestClient
    from storage.db import Database


class GridStrategy(BaseStrategy):
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
        super().__init__(name, inst_type, symbol, config, rest, risk, portfolio, db)

        lower: float = config["grid_lower"]
        upper: float = config["grid_upper"]
        n_grids: int = config.get("n_grids", 10)

        if lower >= upper:
            raise ValueError(f"grid_lower({lower}) must be < grid_upper({upper})")

        step = (upper - lower) / n_grids
        self._levels = [lower + i * step for i in range(n_grids + 1)]
        self._n_grids = n_grids
        self._can_short = inst_type == InstType.SWAP
        self._dual = config.get("grid_dual", self._can_short)  # 双向网格（合约默认开启）

        # 已开仓的槽（LIFO：越晚开的越先平）
        # 元素为开仓时的 unit_qty（close 时复用）
        self._long_slots: list[float] = []   # 最多 n_grids 个
        self._short_slots: list[float] = []  # 最多 n_grids 个（dual 模式）

        self._prev_zone: int | None = None
        self.warm_up_period = 2  # 只需一根前置K线确定初始区间

    # ── 核心逻辑 ───────────────────────────────────────────────────────────────

    async def on_candle(self, candle: Candle) -> list[Signal]:
        if not candle.confirmed:
            return []

        close = candle.close
        tf = self.config.get("timeframe", "?")
        zone = self._zone(close)

        logger.info(
            f"[{self.name}] {candle.ts.strftime('%m-%d %H:%M')} [{tf}] C={close:.4f} | "
            f"zone={zone}/{self._n_grids} "
            f"longs={len(self._long_slots)} shorts={len(self._short_slots)} "
            f"levels=[{self._levels[0]:.2f}..{self._levels[-1]:.2f}]"
        )
        await self._db.save_candle(candle, self.symbol, tf)

        signals = []

        # ── 边界止损：价格超出网格范围 ────────────────────────────────────────
        if zone == -1 and self._long_slots:
            logger.warning(f"[{self.name}] Price below lower bound, closing all longs")
            sig = self._close_all_longs(close)
            if sig:
                signals.append(sig)
                self._long_slots.clear()

        elif zone == self._n_grids and self._short_slots:
            logger.warning(f"[{self.name}] Price above upper bound, closing all shorts")
            sig = self._close_all_shorts(close)
            if sig:
                signals.append(sig)
                self._short_slots.clear()

        # ── 区间内：按格线变化开/平仓 ─────────────────────────────────────────
        elif self._prev_zone is not None and zone != self._prev_zone:
            diff = zone - self._prev_zone

            if diff < 0:  # 价格下移
                # 开 long 槽
                open_count = min(abs(diff), self._n_grids - len(self._long_slots))
                for _ in range(open_count):
                    sig = self._open_signal(OrderSide.BUY, PosSide.LONG if self._can_short else PosSide.NET,
                                            f"Grid open long zone={zone}")
                    signals.append(sig)
                    self._long_slots.append(0.0)  # qty 由 _calc_qty 填充

                # 平 short 槽（dual）
                if self._dual and self._short_slots:
                    close_count = min(abs(diff), len(self._short_slots))
                    for _ in range(close_count):
                        unit_qty = self._short_slots[-1]
                        if unit_qty > 0:
                            sig = Signal(
                                inst_id=self.symbol, side=OrderSide.BUY,
                                order_type=OrderType.MARKET, qty=unit_qty,
                                pos_side=PosSide.SHORT,
                                reason=f"Grid close short zone={zone}",
                            )
                            signals.append(sig)
                        self._short_slots.pop()

            elif diff > 0:  # 价格上移
                # 平 long 槽
                if self._long_slots:
                    close_count = min(diff, len(self._long_slots))
                    for _ in range(close_count):
                        unit_qty = self._long_slots[-1]
                        if unit_qty > 0:
                            pos_side = PosSide.LONG if self._can_short else PosSide.NET
                            sig = Signal(
                                inst_id=self.symbol, side=OrderSide.SELL,
                                order_type=OrderType.MARKET, qty=unit_qty,
                                pos_side=pos_side,
                                reason=f"Grid close long zone={zone}",
                            )
                            signals.append(sig)
                        self._long_slots.pop()

                # 开 short 槽（dual）
                if self._dual:
                    open_count = min(diff, self._n_grids - len(self._short_slots))
                    for _ in range(open_count):
                        sig = self._open_signal(OrderSide.SELL, PosSide.SHORT,
                                                f"Grid open short zone={zone}")
                        signals.append(sig)
                        self._short_slots.append(0.0)

        self._prev_zone = zone

        for sig in signals:
            await self._db.save_signal(sig, self.name)
        return signals

    # ── 辅助方法 ───────────────────────────────────────────────────────────────

    def _zone(self, price: float) -> int:
        """返回价格所在区间索引：-1=下轨以下，0..n-1=区间内，n=上轨以上"""
        if price <= self._levels[0]:
            return -1
        if price >= self._levels[-1]:
            return self._n_grids
        for i in range(self._n_grids):
            if self._levels[i] <= price < self._levels[i + 1]:
                return i
        return self._n_grids

    def _open_signal(self, side: OrderSide, pos_side: PosSide, reason: str) -> Signal:
        return Signal(
            inst_id=self.symbol, side=side,
            order_type=OrderType.MARKET, qty=0,  # _calc_qty 填充
            pos_side=pos_side, reason=reason,
        )

    def _close_all_longs(self, price: float) -> Signal | None:
        pos = self._portfolio.get_position(self.symbol,
                                           PosSide.LONG.value if self._can_short else PosSide.NET.value)
        qty = pos.size if pos else 0.0
        if qty <= 0:
            return None
        return Signal(
            inst_id=self.symbol, side=OrderSide.SELL,
            order_type=OrderType.MARKET, qty=qty,
            pos_side=PosSide.LONG if self._can_short else PosSide.NET,
            reason="Grid boundary stop: close all longs",
        )

    def _close_all_shorts(self, price: float) -> Signal | None:
        pos = self._portfolio.get_position(self.symbol, PosSide.SHORT.value)
        qty = pos.size if pos else 0.0
        if qty <= 0:
            return None
        return Signal(
            inst_id=self.symbol, side=OrderSide.BUY,
            order_type=OrderType.MARKET, qty=qty,
            pos_side=PosSide.SHORT,
            reason="Grid boundary stop: close all shorts",
        )

    # ── _calc_qty 覆盖：开仓用格仓位，平仓用预设qty ───────────────────────────

    async def _calc_qty(self, signal: Signal) -> float:
        # 平仓信号 qty 已预设（非零），直接返回
        if signal.qty > 0:
            return signal.qty

        # 开仓信号：每格 = position_size_pct / n_grids × 可用余额
        pct = self.config.get("position_size_pct", 0.3)
        grid_pct = pct / self._n_grids
        ticker = await self._rest.get_ticker(signal.inst_id)
        price = ticker.last

        if self.inst_type == InstType.SPOT:
            balance = self._portfolio.get_available("USDT")
            info = await self._rest.get_instrument(signal.inst_id, self.inst_type)
            qty = balance * grid_pct / price
            if info.lot_sz > 0:
                precision = max(0, -int(math.floor(math.log10(info.lot_sz))))
                factor = 10 ** precision
                qty = math.floor(qty * factor / (info.lot_sz * factor)) * info.lot_sz
            return qty if qty >= info.min_sz else 0.0

        else:  # SWAP
            balance = self._portfolio.get_available("USDT")
            leverage = self.config.get("leverage", 1)
            info = await self._rest.get_instrument(signal.inst_id, self.inst_type)
            notional = balance * grid_pct * leverage
            contracts = math.floor(notional / (info.ct_val * price))
            return float(contracts) if contracts >= info.min_sz else 0.0

    # ── 订单回调：记录成交qty供平仓使用 ─────────────────────────────────────

    async def on_order_update(self, order: Order):
        if order.status != OrderStatus.FILLED:
            return
        logger.info(
            f"[{self.name}] Order filled: {order.side.value} "
            f"{order.filled_qty}@{order.avg_fill_price:.4f}"
        )
        await self._db.save_order(order, self.name)

        # 将实际成交量回填到最新的空槽（qty=0 的槽）
        if order.side == OrderSide.BUY and not self._dual:
            # 现货开多：回填 long_slots
            for i in range(len(self._long_slots) - 1, -1, -1):
                if self._long_slots[i] == 0.0:
                    self._long_slots[i] = order.filled_qty
                    break
        elif order.side == OrderSide.BUY and self._can_short:
            # 合约买入 = 开多 或 平空
            for i in range(len(self._long_slots) - 1, -1, -1):
                if self._long_slots[i] == 0.0:
                    self._long_slots[i] = order.filled_qty
                    break
        elif order.side == OrderSide.SELL and self._can_short:
            # 合约卖出 = 平多 或 开空
            for i in range(len(self._short_slots) - 1, -1, -1):
                if self._short_slots[i] == 0.0:
                    self._short_slots[i] = order.filled_qty
                    break

    async def on_stop(self):
        logger.info(
            f"[{self.name}] Stopped. "
            f"longs={len(self._long_slots)} shorts={len(self._short_slots)}"
        )
