"""右侧趋势策略：顺势、控损、耐心

入场逻辑（右侧确认，量价齐升）：
  多头：EMA 金叉 + MACD 柱体 > 0 + 成交量放大
  空头（仅合约）：EMA 死叉 + MACD 柱体 < 0 + 成交量放大

出场逻辑（三道防线）：
  1. 止损（铁律）：入场价 ± sl_pct（默认 10%），无论盈亏立即执行
  2. 0 轴上方死叉 → 减仓：EMA 死叉 且 MACD 柱体 > 0，先锁 reduce_ratio（默认 50%）仓位
  3. 均线拐头 → 清仓：EMA 死叉 且 MACD 柱体 <= 0（或已减仓后 EMA 继续下行），果断离场

六字箴言：顺势、控损、耐心
  顺势：只做趋势走出来后的右侧交易
  控损：仓位 20%（position_size_pct=0.2），止损 10%（sl_pct=0.1）铁律
  耐心：宁可错过诱多，也要等待量价齐升的确定性
"""
from typing import TYPE_CHECKING

from loguru import logger

from engine.base_strategy import BaseStrategy
from gateway.models import (
    Candle, InstType, Order, OrderSide, OrderStatus, OrderType, PosSide, Signal,
)
from strategies._base_state import PositionState, build_close_signal
from strategies._indicators import RunningATR, RunningEMA, RunningMACD

if TYPE_CHECKING:
    from engine.portfolio import Portfolio
    from engine.risk_manager import RiskManager
    from gateway.okx_rest import OKXRestClient
    from storage.db import Database


class RightSideStrategy(BaseStrategy):
    """右侧趋势策略：顺势、控损、耐心"""

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

        ema_fast    = config.get("ema_fast", 9)
        ema_slow    = config.get("ema_slow", 21)
        macd_fast   = config.get("macd_fast", 12)
        macd_slow   = config.get("macd_slow", 26)
        macd_signal = config.get("macd_signal", 9)
        atr_period  = config.get("atr_period", 14)
        vol_period  = config.get("vol_period", 20)

        self._ema_fast  = RunningEMA(ema_fast)
        self._ema_slow  = RunningEMA(ema_slow)
        self._macd      = RunningMACD(macd_fast, macd_slow, macd_signal)
        self._atr       = RunningATR(atr_period)
        self._vol_ma    = RunningEMA(vol_period)   # 量能均值，用于量价确认

        # 止损比例（10% 铁律），减仓比例，成交量阈值
        self._sl_pct: float        = config.get("sl_pct", 0.10)
        self._reduce_ratio: float  = config.get("reduce_ratio", 0.5)
        self._vol_threshold: float = config.get("vol_threshold", 1.5)

        # 预热所需 K 线：取各指标中最慢的，再留 10 根缓冲
        self.warm_up_period = max(
            ema_slow * 3,
            (macd_slow + macd_signal) * 3,
            atr_period * 3,
            vol_period * 3,
        ) + 10

        # 上一根 K 线的指标值（用于检测交叉 & 拐头）
        self._prev_ema_fast: float | None = None
        self._prev_ema_slow: float | None = None
        self._prev_hist: float | None     = None

        # 策略内部持仓状态
        self._state       = PositionState()
        self._can_short   = (inst_type == InstType.SWAP)

        # 减仓标记：True 表示已执行过一次减仓，持有剩余半仓
        self._half_reduced: bool = False

        # 冷却期参数
        self._cooldown_candles: int  = config.get("cooldown_candles", 3)
        self._candles_since_trade: int = self._cooldown_candles

    # ── 核心逻辑 ───────────────────────────────────────────────────────────────

    async def on_candle(self, candle: Candle) -> list[Signal]:
        close  = candle.close
        volume = candle.volume

        self._ema_fast.update(close)
        self._ema_slow.update(close)
        self._macd.update(close)
        self._atr.update(candle.high, candle.low, close)
        self._vol_ma.update(volume)

        if not self._indicators_ready():
            return []

        ef   = self._ema_fast.value
        es   = self._ema_slow.value
        hist = self._macd.hist
        atr  = self._atr.value
        vmа  = self._vol_ma.value
        tf   = self.config.get("timeframe", "?")

        # ── 调试日志 ─────────────────────────────────────────────────────────
        if candle.confirmed:
            pos_str = "FLAT"
            if not self._state.flat:
                pnl = (close - self._state.entry_price) * (
                    1 if self._state.pos_side == PosSide.LONG else -1
                )
                reduced_tag = " [HALF]" if self._half_reduced else " [FULL]"
                pos_str = (
                    f"{self._state.pos_side.value.upper()}{reduced_tag} "
                    f"entry={self._state.entry_price:.4f} "
                    f"sl={self._state.stop_loss:.4f} "
                    f"uPnL={pnl:+.4f}"
                )
            logger.debug(
                f"[{self.name}] {candle.ts.strftime('%m-%d %H:%M')} [{tf}] "
                f"O={candle.open:.4f} H={candle.high:.4f} L={candle.low:.4f} C={close:.4f} "
                f"V={volume:.2f}(avg={vmа:.2f}) | "
                f"EMA{self._ema_fast.period}={ef:.4f} EMA{self._ema_slow.period}={es:.4f} "
                f"hist={hist:+.6f} ATR={atr:.4f} | {pos_str}"
            )
            await self._db.save_candle(candle, self.symbol, tf)
        else:
            return []

        signals: list[Signal] = []

        # ── 防线1：固定比例止损（优先级最高）────────────────────────────────
        if not self._state.flat:
            sl_triggered = (
                (self._state.pos_side == PosSide.LONG  and close <= self._state.stop_loss) or
                (self._state.pos_side == PosSide.SHORT and close >= self._state.stop_loss)
            )
            if sl_triggered:
                logger.warning(
                    f"[{self.name}] *** STOP LOSS triggered *** "
                    f"close={close:.4f} sl={self._state.stop_loss:.4f} "
                    f"(entry={self._state.entry_price:.4f}, sl_pct={self._sl_pct:.1%})"
                )
                sig = self._close_signal(close, reason="Stop loss (10% iron rule)")
                if sig:
                    signals.append(sig)
                    self._state.close()
                    self._half_reduced = False
                self._update_prev(ef, es, hist)
                for s in signals:
                    await self._db.save_signal(s, self.name)
                return signals

        # ── 冷却计数 ──────────────────────────────────────────────────────────
        self._candles_since_trade += 1

        # ── 交叉信号检测 ──────────────────────────────────────────────────────
        golden_cross = self._cross_up(ef, es)
        death_cross  = self._cross_down(ef, es)
        macd_bull    = hist > 0
        macd_bear    = hist < 0
        cooldown_ok  = self._candles_since_trade >= self._cooldown_candles

        # 量价齐升确认：当前成交量 >= 均量 × 阈值
        vol_surge    = (volume >= vmа * self._vol_threshold)

        if golden_cross or death_cross:
            cross_type = "GOLDEN ▲" if golden_cross else "DEATH ▼"
            logger.info(
                f"[{self.name}] {cross_type} | "
                f"EMA_fast={ef:.4f} EMA_slow={es:.4f} hist={hist:+.6f} "
                f"vol={volume:.2f}/avg={vmа:.2f}({volume/vmа:.1f}x) "
                f"vol_surge={'YES' if vol_surge else 'NO'} "
                f"cooldown={'OK' if cooldown_ok else f'wait {self._cooldown_candles - self._candles_since_trade}K'}"
            )

        # ── 入场 / 出场逻辑 ───────────────────────────────────────────────────
        if self._state.flat:
            # 多头入场：金叉 + MACD>0 + 量价齐升 + 冷却完毕
            if golden_cross and macd_bull and vol_surge and cooldown_ok:
                sl = close * (1 - self._sl_pct)
                sig = self._open_long_signal(close, sl)
                signals.append(sig)
                self._state.open(PosSide.LONG, close, sl)
                self._half_reduced = False
                self._candles_since_trade = 0
                logger.info(
                    f"[{self.name}] LONG ENTRY @ {close:.4f} | "
                    f"SL={sl:.4f} ({self._sl_pct:.1%}) vol={volume:.2f}({volume/vmа:.1f}x)"
                )

            # 空头入场（仅合约）：死叉 + MACD<0 + 量价放大 + 冷却完毕
            elif self._can_short and death_cross and macd_bear and vol_surge and cooldown_ok:
                sl = close * (1 + self._sl_pct)
                sig = self._open_short_signal(close, sl)
                signals.append(sig)
                self._state.open(PosSide.SHORT, close, sl)
                self._half_reduced = False
                self._candles_since_trade = 0
                logger.info(
                    f"[{self.name}] SHORT ENTRY @ {close:.4f} | "
                    f"SL={sl:.4f} ({self._sl_pct:.1%}) vol={volume:.2f}({volume/vmа:.1f}x)"
                )

        else:
            # ── 多头出场逻辑 ──────────────────────────────────────────────────
            if self._state.pos_side == PosSide.LONG:

                if death_cross:
                    if macd_bull and not self._half_reduced:
                        # 防线2：0轴上方出现死叉 → 减仓50%，不带感情色彩
                        sig = self._reduce_signal(
                            reason=f"MACD>0 death cross: reduce {self._reduce_ratio:.0%}"
                        )
                        if sig:
                            signals.append(sig)
                            self._half_reduced = True
                            self._candles_since_trade = 0
                            logger.info(
                                f"[{self.name}] LONG REDUCE {self._reduce_ratio:.0%} @ {close:.4f} | "
                                f"MACD above zero, locking partial profit"
                            )
                    else:
                        # 防线3：均线拐头向下 → 清仓
                        reason = (
                            "MACD<=0 death cross: trend reversal, full exit"
                            if not self._half_reduced
                            else "EMA continues down after reduce: full exit"
                        )
                        sig = self._close_signal(close, reason=reason)
                        if sig:
                            signals.append(sig)
                            self._state.close()
                            self._half_reduced = False
                            self._candles_since_trade = 0
                            logger.info(
                                f"[{self.name}] LONG EXIT @ {close:.4f} | {reason}"
                            )

                elif self._half_reduced:
                    # 已减仓状态：EMA 快线持续下行（拐头向下确认），清仓剩余
                    ema_turning_down = (
                        self._prev_ema_fast is not None and
                        ef < self._prev_ema_fast and
                        ef < es
                    )
                    if ema_turning_down:
                        sig = self._close_signal(
                            close, reason="EMA fast turning down after reduce: clear remaining"
                        )
                        if sig:
                            signals.append(sig)
                            self._state.close()
                            self._half_reduced = False
                            self._candles_since_trade = 0
                            logger.info(
                                f"[{self.name}] LONG CLEAR (remaining) @ {close:.4f} | "
                                f"EMA fast={ef:.4f} < prev={self._prev_ema_fast:.4f}, trend reversing"
                            )

            # ── 空头出场逻辑 ──────────────────────────────────────────────────
            elif self._state.pos_side == PosSide.SHORT:

                if golden_cross:
                    if macd_bear and not self._half_reduced:
                        # 0轴下方出现金叉 → 减仓50%
                        sig = self._reduce_signal(
                            reason=f"MACD<0 golden cross: reduce {self._reduce_ratio:.0%}"
                        )
                        if sig:
                            signals.append(sig)
                            self._half_reduced = True
                            self._candles_since_trade = 0
                            logger.info(
                                f"[{self.name}] SHORT REDUCE {self._reduce_ratio:.0%} @ {close:.4f} | "
                                f"MACD below zero, locking partial profit"
                            )
                    else:
                        # 均线拐头向上 → 清仓
                        reason = (
                            "MACD>=0 golden cross: trend reversal, full exit"
                            if not self._half_reduced
                            else "EMA continues up after reduce: full exit"
                        )
                        sig = self._close_signal(close, reason=reason)
                        if sig:
                            signals.append(sig)
                            self._state.close()
                            self._half_reduced = False
                            self._candles_since_trade = 0
                            logger.info(
                                f"[{self.name}] SHORT EXIT @ {close:.4f} | {reason}"
                            )

                elif self._half_reduced:
                    # 已减仓状态：EMA 快线持续上行，清仓剩余
                    ema_turning_up = (
                        self._prev_ema_fast is not None and
                        ef > self._prev_ema_fast and
                        ef > es
                    )
                    if ema_turning_up:
                        sig = self._close_signal(
                            close, reason="EMA fast turning up after reduce: clear remaining"
                        )
                        if sig:
                            signals.append(sig)
                            self._state.close()
                            self._half_reduced = False
                            self._candles_since_trade = 0
                            logger.info(
                                f"[{self.name}] SHORT CLEAR (remaining) @ {close:.4f} | "
                                f"EMA fast={ef:.4f} > prev={self._prev_ema_fast:.4f}, trend reversing"
                            )

        for sig in signals:
            await self._db.save_signal(sig, self.name)

        self._update_prev(ef, es, hist)
        return signals

    async def on_order_update(self, order: Order):
        if order.status == OrderStatus.FILLED:
            logger.info(
                f"[{self.name}] Order filled: {order.order_id} "
                f"{order.side.value} {order.filled_qty}@{order.avg_fill_price:.4f}"
            )
            await self._db.save_order(order, self.name)

    async def on_stop(self):
        half_tag = " [HALF]" if self._half_reduced else ""
        state_str = "FLAT" if self._state.flat else f"{self._state.pos_side.value.upper()}{half_tag}"
        logger.info(f"[{self.name}] Strategy stopped. State: {state_str}")

    def reset_position_state(self):
        super().reset_position_state()
        self._half_reduced = False

    # ── 信号构造 ───────────────────────────────────────────────────────────────

    def _open_long_signal(self, price: float, stop_loss: float) -> Signal:
        return Signal(
            inst_id=self.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            qty=0,  # 由引擎 _calc_qty 填充
            pos_side=PosSide.LONG if self._can_short else PosSide.NET,
            stop_loss=stop_loss,
            reason=f"Right-side LONG | EMA golden cross + MACD>0 + vol surge | SL={stop_loss:.4f} ({self._sl_pct:.1%})",
        )

    def _open_short_signal(self, price: float, stop_loss: float) -> Signal:
        return Signal(
            inst_id=self.symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            qty=0,
            pos_side=PosSide.SHORT,
            stop_loss=stop_loss,
            reason=f"Right-side SHORT | EMA death cross + MACD<0 + vol surge | SL={stop_loss:.4f} ({self._sl_pct:.1%})",
        )

    def _reduce_signal(self, reason: str) -> Signal | None:
        """减仓：平掉 reduce_ratio 比例的持仓"""
        if self._state.flat:
            return None
        pos = self._portfolio.get_position(self.symbol, self._state.pos_side.value)
        if not pos or pos.size <= 0:
            logger.warning(f"[{self.name}] Reduce signal but no position found, skip")
            return None
        reduce_qty = pos.size * self._reduce_ratio
        if reduce_qty <= 0:
            return None
        if self._state.pos_side == PosSide.LONG:
            side = OrderSide.SELL
            pos_side = PosSide.LONG if self._can_short else PosSide.NET
        else:
            side = OrderSide.BUY
            pos_side = PosSide.SHORT
        return Signal(
            inst_id=self.symbol,
            side=side,
            order_type=OrderType.MARKET,
            qty=reduce_qty,
            pos_side=pos_side,
            reason=reason,
        )

    def _close_signal(self, price: float, reason: str) -> Signal | None:
        return build_close_signal(
            self._state, self.symbol, self._portfolio,
            self._can_short, reason, self.name,
        )

    # ── 工具方法 ───────────────────────────────────────────────────────────────

    def _indicators_ready(self) -> bool:
        return (
            self._ema_fast.ready and
            self._ema_slow.ready and
            self._macd.ready and
            self._atr.ready and
            self._vol_ma.ready
        )

    def _cross_up(self, fast: float, slow: float) -> bool:
        if self._prev_ema_fast is None or self._prev_ema_slow is None:
            return False
        return self._prev_ema_fast <= self._prev_ema_slow and fast > slow

    def _cross_down(self, fast: float, slow: float) -> bool:
        if self._prev_ema_fast is None or self._prev_ema_slow is None:
            return False
        return self._prev_ema_fast >= self._prev_ema_slow and fast < slow

    def _update_prev(self, ef: float, es: float, hist: float | None):
        self._prev_ema_fast = ef
        self._prev_ema_slow = es
        self._prev_hist = hist
