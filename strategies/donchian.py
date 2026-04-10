"""突破策略：Donchian 通道（海龟交易法）

信号逻辑：
  多头入场：收盘价突破过去 entry_period 根K线最高价（新高）
  多头出场：收盘价跌破过去 exit_period 根K线最低价（exit_period < entry_period）
  空头入场（仅合约）：收盘价跌破过去 entry_period 根K线最低价
  空头出场：收盘价涨破过去 exit_period 根K线最高价

止损：入场价 ± ATR × multiplier（兜底，通常由 exit 通道先触发）
适合行情：大趋势行情，日线/4小时级别效果最佳
"""
from typing import TYPE_CHECKING

from loguru import logger

from engine.base_strategy import BaseStrategy
from gateway.models import (
    Candle, InstType, Order, OrderSide, OrderStatus, OrderType, PosSide, Signal,
)
from strategies._base_state import PositionState, build_close_signal
from strategies._indicators import Donchian, RunningATR

if TYPE_CHECKING:
    from engine.portfolio import Portfolio
    from engine.risk_manager import RiskManager
    from gateway.okx_rest import OKXRestClient
    from storage.db import Database


class DonchianStrategy(BaseStrategy):
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

        entry_period = config.get("entry_period", 20)
        exit_period = config.get("exit_period", 10)
        atr_period = config.get("atr_period", 14)

        if exit_period >= entry_period:
            raise ValueError(f"exit_period({exit_period}) must be < entry_period({entry_period})")

        self._entry_ch = Donchian(entry_period)
        self._exit_ch = Donchian(exit_period)
        self._atr = RunningATR(atr_period)
        self._sl_mult: float = config.get("atr_sl_multiplier", 2.0)
        self._cooldown: int = config.get("cooldown_candles", 2)
        self._candles_since_trade: int = self._cooldown

        self.warm_up_period = max(entry_period * 3, atr_period) + 5
        self._state = PositionState()
        self._can_short = (inst_type == InstType.SWAP)

        # 上一根K线的通道值（"读-然后-更新"，避免当前K线污染信号）
        self._prev_entry_high: float | None = None
        self._prev_entry_low: float | None = None
        self._prev_exit_high: float | None = None
        self._prev_exit_low: float | None = None

    async def on_candle(self, candle: Candle) -> list[Signal]:
        # 先保存前一根K线的通道值，再更新
        prev_entry_high = self._prev_entry_high
        prev_entry_low = self._prev_entry_low
        prev_exit_high = self._prev_exit_high
        prev_exit_low = self._prev_exit_low

        self._entry_ch.update(candle.high, candle.low)
        self._exit_ch.update(candle.high, candle.low)
        self._atr.update(candle.high, candle.low, candle.close)

        # 更新供下一根K线读取
        if self._entry_ch.ready:
            self._prev_entry_high = self._entry_ch.highest
            self._prev_entry_low = self._entry_ch.lowest
        if self._exit_ch.ready:
            self._prev_exit_high = self._exit_ch.highest
            self._prev_exit_low = self._exit_ch.lowest

        if not (self._entry_ch.ready and self._exit_ch.ready and self._atr.ready):
            return []
        if prev_entry_high is None:  # 需要至少两批数据才能比较
            return []
        if not candle.confirmed:
            return []

        close = candle.close
        tf = self.config.get("timeframe", "?")
        atr = self._atr.value

        pos_str = "FLAT" if self._state.flat else (
            f"{self._state.pos_side.value.upper()} entry={self._state.entry_price:.4f} sl={self._state.stop_loss:.4f}"
        )
        logger.debug(
            f"[{self.name}] {candle.ts.strftime('%m-%d %H:%M')} [{tf}] C={close:.4f} | "
            f"EntryH={prev_entry_high:.4f} EntryL={prev_entry_low:.4f} "
            f"ExitH={prev_exit_high:.4f} ExitL={prev_exit_low:.4f} ATR={atr:.4f} | {pos_str}"
        )
        await self._db.save_candle(candle, self.symbol, tf)

        signals = []
        self._candles_since_trade += 1

        # ── 止损检查 ───────────────────────────────────────────────────────────
        if not self._state.flat:
            sl_hit = (
                (self._state.pos_side == PosSide.LONG and close <= self._state.stop_loss) or
                (self._state.pos_side == PosSide.SHORT and close >= self._state.stop_loss)
            )
            if sl_hit:
                logger.warning(f"[{self.name}] STOP LOSS hit @ {close:.4f}")
                sig = self._build_close(close, "Stop loss")
                if sig:
                    signals.append(sig)
                    self._state.close()
                    self._candles_since_trade = 0
                for s in signals:
                    await self._db.save_signal(s, self.name)
                return signals

        cooldown_ok = self._candles_since_trade >= self._cooldown

        # ── 入场：突破前 N 周期通道 ────────────────────────────────────────────
        if self._state.flat:
            if close > prev_entry_high and cooldown_ok:
                sl = close - self._sl_mult * atr
                signals.append(Signal(
                    inst_id=self.symbol,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    qty=0,
                    pos_side=PosSide.LONG if self._can_short else PosSide.NET,
                    stop_loss=sl,
                    reason=f"Donchian breakout HIGH {prev_entry_high:.4f} | SL={sl:.4f}",
                ))
                self._state.open(PosSide.LONG, close, sl)
                self._candles_since_trade = 0
                logger.info(f"[{self.name}] LONG ENTRY @ {close:.4f} > {prev_entry_high:.4f}")

            elif self._can_short and close < prev_entry_low and cooldown_ok:
                sl = close + self._sl_mult * atr
                signals.append(Signal(
                    inst_id=self.symbol,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    qty=0,
                    pos_side=PosSide.SHORT,
                    stop_loss=sl,
                    reason=f"Donchian breakout LOW {prev_entry_low:.4f} | SL={sl:.4f}",
                ))
                self._state.open(PosSide.SHORT, close, sl)
                self._candles_since_trade = 0
                logger.info(f"[{self.name}] SHORT ENTRY @ {close:.4f} < {prev_entry_low:.4f}")

        # ── 出场：回落到 exit 通道边界 ─────────────────────────────────────────
        else:
            if self._state.pos_side == PosSide.LONG and close < prev_exit_low:
                sig = self._build_close(close, f"Donchian exit LOW {prev_exit_low:.4f}")
                if sig:
                    signals.append(sig)
                    self._state.close()
                    self._candles_since_trade = 0
                    logger.info(f"[{self.name}] LONG EXIT @ {close:.4f} < {prev_exit_low:.4f}")

            elif self._state.pos_side == PosSide.SHORT and close > prev_exit_high:
                sig = self._build_close(close, f"Donchian exit HIGH {prev_exit_high:.4f}")
                if sig:
                    signals.append(sig)
                    self._state.close()
                    self._candles_since_trade = 0
                    logger.info(f"[{self.name}] SHORT EXIT @ {close:.4f} > {prev_exit_high:.4f}")

        for sig in signals:
            await self._db.save_signal(sig, self.name)
        return signals

    def _build_close(self, price: float, reason: str) -> Signal | None:
        return build_close_signal(
            self._state, self.symbol, self._portfolio,
            self._can_short, reason, self.name,
        )

    async def on_order_update(self, order: Order):
        if order.status == OrderStatus.FILLED:
            logger.info(
                f"[{self.name}] Order filled: {order.side.value} "
                f"{order.filled_qty}@{order.avg_fill_price:.4f}"
            )
            await self._db.save_order(order, self.name)

    async def on_stop(self):
        logger.info(f"[{self.name}] Stopped. {'flat' if self._state.flat else self._state.pos_side.value}")
