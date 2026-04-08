"""均值回归策略：Bollinger Bands + RSI

信号逻辑：
  多头入场：收盘价跌破布林下轨 且 RSI < oversold_threshold
  多头出场：收盘价回到布林中轨（SMA）或触及止损
  空头入场（仅合约）：收盘价突破布林上轨 且 RSI > overbought_threshold
  空头出场：收盘价回到布林中轨或触及止损

止损：入场价 ± ATR × multiplier
适合行情：横盘震荡，布林带宽较窄
"""
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

from engine.base_strategy import BaseStrategy
from gateway.models import (
    Candle, InstType, Order, OrderSide, OrderStatus, OrderType, PosSide, Signal,
)
from strategies._indicators import RunningATR, RunningBB, RunningRSI

if TYPE_CHECKING:
    from engine.portfolio import Portfolio
    from engine.risk_manager import RiskManager
    from gateway.okx_rest import OKXRestClient
    from storage.db import Database


@dataclass
class _State:
    flat: bool = True
    pos_side: PosSide = PosSide.NET
    entry_price: float = 0.0
    stop_loss: float = 0.0

    def open(self, pos_side: PosSide, entry_price: float, stop_loss: float):
        self.flat = False
        self.pos_side = pos_side
        self.entry_price = entry_price
        self.stop_loss = stop_loss

    def close(self):
        self.flat = True
        self.pos_side = PosSide.NET
        self.entry_price = 0.0
        self.stop_loss = 0.0


class BbRsiStrategy(BaseStrategy):
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

        bb_period = config.get("bb_period", 20)
        bb_std = config.get("bb_std", 2.0)
        rsi_period = config.get("rsi_period", 14)
        atr_period = config.get("atr_period", 14)

        self._bb = RunningBB(bb_period, bb_std)
        self._rsi = RunningRSI(rsi_period)
        self._atr = RunningATR(atr_period)
        self._sl_mult: float = config.get("atr_sl_multiplier", 1.5)
        self._oversold: float = config.get("rsi_oversold", 30)
        self._overbought: float = config.get("rsi_overbought", 70)
        self._cooldown: int = config.get("cooldown_candles", 3)
        self._candles_since_trade: int = self._cooldown

        self.warm_up_period = max(bb_period, rsi_period, atr_period) + 5
        self._state = _State()
        self._can_short = (inst_type == InstType.SWAP)

    async def on_candle(self, candle: Candle) -> list[Signal]:
        close = candle.close
        self._bb.update(close)
        self._rsi.update(close)
        self._atr.update(candle.high, candle.low, close)

        if not (self._bb.ready and self._rsi.ready and self._atr.ready):
            return []
        if not candle.confirmed:
            return []

        tf = self.config.get("timeframe", "?")
        rsi = self._rsi.value
        atr = self._atr.value

        pos_str = "FLAT" if self._state.flat else (
            f"{self._state.pos_side.value.upper()} entry={self._state.entry_price:.4f} sl={self._state.stop_loss:.4f}"
        )
        logger.info(
            f"[{self.name}] {candle.ts.strftime('%m-%d %H:%M')} [{tf}] C={close:.4f} | "
            f"BB=[{self._bb.lower:.4f},{self._bb.middle:.4f},{self._bb.upper:.4f}] "
            f"RSI={rsi:.1f} ATR={atr:.4f} | {pos_str}"
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

        # ── 入场 ───────────────────────────────────────────────────────────────
        if self._state.flat:
            if close < self._bb.lower and rsi < self._oversold and cooldown_ok:
                sl = close - self._sl_mult * atr
                signals.append(Signal(
                    inst_id=self.symbol,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    qty=0,
                    pos_side=PosSide.LONG if self._can_short else PosSide.NET,
                    stop_loss=sl,
                    reason=f"BB lower breach + RSI={rsi:.1f}<{self._oversold} | SL={sl:.4f}",
                ))
                self._state.open(PosSide.LONG, close, sl)
                self._candles_since_trade = 0
                logger.info(f"[{self.name}] LONG ENTRY @ {close:.4f}")

            elif self._can_short and close > self._bb.upper and rsi > self._overbought and cooldown_ok:
                sl = close + self._sl_mult * atr
                signals.append(Signal(
                    inst_id=self.symbol,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    qty=0,
                    pos_side=PosSide.SHORT,
                    stop_loss=sl,
                    reason=f"BB upper breach + RSI={rsi:.1f}>{self._overbought} | SL={sl:.4f}",
                ))
                self._state.open(PosSide.SHORT, close, sl)
                self._candles_since_trade = 0
                logger.info(f"[{self.name}] SHORT ENTRY @ {close:.4f}")

        # ── 出场：回到中轨 ─────────────────────────────────────────────────────
        else:
            if self._state.pos_side == PosSide.LONG and close >= self._bb.middle:
                sig = self._build_close(close, "Price returned to BB middle")
                if sig:
                    signals.append(sig)
                    self._state.close()
                    self._candles_since_trade = 0
                    logger.info(f"[{self.name}] LONG EXIT @ {close:.4f}")

            elif self._state.pos_side == PosSide.SHORT and close <= self._bb.middle:
                sig = self._build_close(close, "Price returned to BB middle")
                if sig:
                    signals.append(sig)
                    self._state.close()
                    self._candles_since_trade = 0
                    logger.info(f"[{self.name}] SHORT EXIT @ {close:.4f}")

        for sig in signals:
            await self._db.save_signal(sig, self.name)
        return signals

    def _build_close(self, price: float, reason: str) -> Signal | None:
        if self._state.flat:
            return None
        if self._state.pos_side == PosSide.LONG:
            side = OrderSide.SELL
            pos_side = PosSide.LONG if self._can_short else PosSide.NET
        else:
            side = OrderSide.BUY
            pos_side = PosSide.SHORT
        pos = self._portfolio.get_position(self.symbol, self._state.pos_side.value)
        qty = pos.size if pos else 0.0
        if qty <= 0:
            logger.warning(f"[{self.name}] Close signal but no position found, skip")
            return None
        return Signal(inst_id=self.symbol, side=side, order_type=OrderType.MARKET,
                      qty=qty, pos_side=pos_side, reason=reason)

    async def on_order_update(self, order: Order):
        if order.status == OrderStatus.FILLED:
            logger.info(
                f"[{self.name}] Order filled: {order.side.value} "
                f"{order.filled_qty}@{order.avg_fill_price:.4f}"
            )
            await self._db.save_order(order, self.name)

    async def on_stop(self):
        logger.info(f"[{self.name}] Stopped. {'flat' if self._state.flat else self._state.pos_side.value}")
