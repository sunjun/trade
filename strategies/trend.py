"""趋势策略：EMA金叉死叉 + MACD确认 + ATR动态止损

信号逻辑：
  多头入场：快速EMA上穿慢速EMA，且MACD柱体 > 0
  多头出场：快速EMA下穿慢速EMA，或跌破止损价
  空头入场（仅合约）：快速EMA下穿慢速EMA，且MACD柱体 < 0
  空头出场：快速EMA上穿慢速EMA，或涨破止损价

止损：入场价 ± ATR * multiplier
"""
from collections import deque
from dataclasses import dataclass
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


# ──────────────────────────────────────────────────────────────────────────────
# 指标计算器（增量更新，无需重算历史）
# ──────────────────────────────────────────────────────────────────────────────

class _RunningEMA:
    """指数移动平均，增量计算"""
    def __init__(self, period: int):
        self.period = period
        self.k = 2.0 / (period + 1)
        self.value: float | None = None
        self._init_buf: list[float] = []

    def update(self, price: float) -> float | None:
        if self.value is None:
            self._init_buf.append(price)
            if len(self._init_buf) >= self.period:
                self.value = sum(self._init_buf) / len(self._init_buf)
            return self.value
        self.value = price * self.k + self.value * (1 - self.k)
        return self.value

    @property
    def ready(self) -> bool:
        return self.value is not None


class _MACD:
    """MACD = EMA(fast) - EMA(slow)；Signal = EMA(signal) of MACD；Hist = MACD - Signal"""
    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self._ema_fast = _RunningEMA(fast)
        self._ema_slow = _RunningEMA(slow)
        self._ema_signal = _RunningEMA(signal)
        self.macd: float | None = None
        self.signal_line: float | None = None
        self.hist: float | None = None

    def update(self, price: float) -> bool:
        ef = self._ema_fast.update(price)
        es = self._ema_slow.update(price)
        if ef is None or es is None:
            return False
        self.macd = ef - es
        sig = self._ema_signal.update(self.macd)
        if sig is None:
            return False
        self.signal_line = sig
        self.hist = self.macd - self.signal_line
        return True

    @property
    def ready(self) -> bool:
        return self.hist is not None


class _ATR:
    """真实波幅均值（EMA平滑）"""
    def __init__(self, period: int = 14):
        self._ema = _RunningEMA(period)
        self._prev_close: float | None = None
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev_close is not None:
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )
            self.value = self._ema.update(tr)
        self._prev_close = close
        return self.value

    @property
    def ready(self) -> bool:
        return self.value is not None


# ──────────────────────────────────────────────────────────────────────────────
# 持仓状态机
# ──────────────────────────────────────────────────────────────────────────────

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


# ──────────────────────────────────────────────────────────────────────────────
# 趋势策略
# ──────────────────────────────────────────────────────────────────────────────

class TrendStrategy(BaseStrategy):
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

        ema_fast = config.get("ema_fast", 9)
        ema_slow = config.get("ema_slow", 21)
        macd_fast = config.get("macd_fast", 12)
        macd_slow = config.get("macd_slow", 26)
        macd_signal = config.get("macd_signal", 9)
        atr_period = config.get("atr_period", 14)

        self._ema_fast = _RunningEMA(ema_fast)
        self._ema_slow = _RunningEMA(ema_slow)
        self._macd = _MACD(macd_fast, macd_slow, macd_signal)
        self._atr = _ATR(atr_period)
        self._sl_mult = config.get("atr_sl_multiplier", 2.0)

        # 预热所需K线数：最长 EMA 周期 × 3 保证收敛（初始SMA影响 < 3%），再加信号线缓冲
        self.warm_up_period = max(ema_slow * 3, macd_slow + macd_signal, atr_period) + 10

        # 上一根K线的EMA值（用于检测交叉）
        self._prev_ema_fast: float | None = None
        self._prev_ema_slow: float | None = None
        self._prev_hist: float | None = None

        # 策略内部持仓状态
        self._state = _State()
        self._can_short = (inst_type == InstType.SWAP)

        # ── 过滤器参数 ────────────────────────────────────────────────────────
        # EMA 动量过滤：要求交叉时 EMA 间距正在扩大（而非刚分开就收缩），避免假突破
        self._require_spread_expand: bool = config.get("require_spread_expand", True)
        # 冷却期：上次开/平仓后，至少等待 N 根K线才允许下一次开仓
        self._cooldown_candles: int = config.get("cooldown_candles", 3)
        self._candles_since_trade: int = self._cooldown_candles  # 初始值=已冷却

    # ── 核心逻辑 ───────────────────────────────────────────────────────────────

    async def on_candle(self, candle: Candle) -> list[Signal]:
        close = candle.close
        self._ema_fast.update(close)
        self._ema_slow.update(close)
        self._macd.update(close)
        self._atr.update(candle.high, candle.low, close)

        # 指标未就绪，继续预热
        if not self._indicators_ready():
            return []

        ef = self._ema_fast.value
        es = self._ema_slow.value
        hist = self._macd.hist
        atr = self._atr.value
        tf = self.config.get("timeframe", "?")

        # ── 所有K线都打印到终端 ───────────────────────────────────────────────
        pos_str = "FLAT"
        if not self._state.flat:
            pnl = (close - self._state.entry_price) * (
                1 if self._state.pos_side == PosSide.LONG else -1
            )
            pos_str = (f"{self._state.pos_side.value.upper()} "
                       f"entry={self._state.entry_price:.4f} "
                       f"sl={self._state.stop_loss:.4f} "
                       f"uPnL={pnl:+.4f}")

        if candle.confirmed:
            logger.debug(
                f"[{self.name}] {candle.ts.strftime('%m-%d %H:%M')} [{tf}] "
                f"O={candle.open:.4f} H={candle.high:.4f} L={candle.low:.4f} C={close:.4f} "
                f"V={candle.volume:.2f} | "
                f"EMA{self._ema_fast.period}={ef:.4f} EMA{self._ema_slow.period}={es:.4f} "
                f"hist={hist:+.6f} ATR={atr:.4f} | {pos_str}"
            )
            await self._db.save_candle(candle, self.symbol, tf)
        else:
            return []

        signals = []

        # ── 止损检查（优先级最高）─────────────────────────────────────────────
        if not self._state.flat:
            sl_triggered = (
                (self._state.pos_side == PosSide.LONG and close <= self._state.stop_loss) or
                (self._state.pos_side == PosSide.SHORT and close >= self._state.stop_loss)
            )
            if sl_triggered:
                logger.warning(
                    f"[{self.name}] *** STOP LOSS triggered *** "
                    f"close={close:.4f} sl={self._state.stop_loss:.4f}"
                )
                sig = self._close_signal(close, reason="Stop loss triggered")
                if sig:
                    signals.append(sig)
                    self._state.close()
                self._update_prev(ef, es, hist)
                return signals

        # ── 每根收盘K线更新冷却计数 ───────────────────────────────────────────
        self._candles_since_trade += 1

        # ── 交叉 & 过滤条件 ───────────────────────────────────────────────────
        golden_cross = self._cross_up(ef, es)
        death_cross  = self._cross_down(ef, es)
        macd_bull    = hist > 0
        macd_bear    = hist < 0
        cooldown_ok  = self._candles_since_trade >= self._cooldown_candles

        # EMA 间距动量：当前 spread 相对上根K线是否在向正确方向扩大
        # 金叉期望 ef-es 比上根更大（快线加速上穿），死叉期望 ef-es 比上根更小
        ema_spread = ef - es
        prev_spread = (
            (self._prev_ema_fast - self._prev_ema_slow)
            if self._prev_ema_fast is not None and self._prev_ema_slow is not None
            else 0.0
        )
        spread_ok_bull = (not self._require_spread_expand) or (ema_spread > prev_spread)
        spread_ok_bear = (not self._require_spread_expand) or (ema_spread < prev_spread)

        if golden_cross or death_cross:
            cross_type = "GOLDEN CROSS ▲" if golden_cross else "DEATH CROSS ▼"
            macd_ok = (golden_cross and macd_bull) or (death_cross and macd_bear)
            spread_ok = spread_ok_bull if golden_cross else spread_ok_bear
            reasons = []
            if not macd_ok:    reasons.append("MACD mismatch")
            if not spread_ok:  reasons.append(f"spread shrinking ({ema_spread:+.4f} vs prev {prev_spread:+.4f})")
            if not cooldown_ok: reasons.append(f"cooldown {self._candles_since_trade}/{self._cooldown_candles}K")
            confirm = "YES" if not reasons else f"NO ({', '.join(reasons)})"
            logger.info(
                f"[{self.name}] {cross_type} | "
                f"EMA_fast={ef:.4f} EMA_slow={es:.4f} spread={ema_spread:+.4f} "
                f"MACD={hist:+.6f} | {confirm}"
            )

        # ── 入场 / 出场逻辑 ───────────────────────────────────────────────────
        if self._state.flat:
            if golden_cross and macd_bull and spread_ok_bull and cooldown_ok:
                sl = close - self._sl_mult * atr
                sig = self._open_long_signal(close, sl)
                signals.append(sig)
                self._state.open(PosSide.LONG, close, sl)
                self._candles_since_trade = 0
                self._log_state("LONG ENTRY", close)

            elif self._can_short and death_cross and macd_bear and spread_ok_bear and cooldown_ok:
                sl = close + self._sl_mult * atr
                sig = self._open_short_signal(close, sl)
                signals.append(sig)
                self._state.open(PosSide.SHORT, close, sl)
                self._candles_since_trade = 0
                self._log_state("SHORT ENTRY", close)

        else:
            if self._state.pos_side == PosSide.LONG and death_cross:
                sig = self._close_signal(close, reason="EMA death cross")
                if sig:
                    signals.append(sig)
                    self._state.close()
                    self._candles_since_trade = 0
                    self._log_state("LONG EXIT", close)

            elif self._state.pos_side == PosSide.SHORT and golden_cross:
                sig = self._close_signal(close, reason="EMA golden cross")
                if sig:
                    signals.append(sig)
                    self._state.close()
                    self._candles_since_trade = 0
                    self._log_state("SHORT EXIT", close)

        # 保存产生的信号到数据库
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
        logger.info(f"[{self.name}] Strategy stopped. State: {'flat' if self._state.flat else self._state.pos_side.value}")

    # ── 信号构造 ───────────────────────────────────────────────────────────────

    def _open_long_signal(self, price: float, stop_loss: float) -> Signal:
        return Signal(
            inst_id=self.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            qty=0,  # 由引擎的 _calc_qty 填充
            pos_side=PosSide.LONG if self._can_short else PosSide.NET,
            stop_loss=stop_loss,
            reason=f"Long entry | EMA golden cross | SL={stop_loss:.4f}",
        )

    def _open_short_signal(self, price: float, stop_loss: float) -> Signal:
        return Signal(
            inst_id=self.symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            qty=0,
            pos_side=PosSide.SHORT,
            stop_loss=stop_loss,
            reason=f"Short entry | EMA death cross | SL={stop_loss:.4f}",
        )

    def _close_signal(self, price: float, reason: str) -> Signal | None:
        if self._state.flat:
            return None
        if self._state.pos_side == PosSide.LONG:
            side = OrderSide.SELL
            pos_side = PosSide.LONG if self._can_short else PosSide.NET
        else:
            side = OrderSide.BUY
            pos_side = PosSide.SHORT

        # 从 portfolio 获取持仓量
        pos = self._portfolio.get_position(self.symbol, self._state.pos_side.value)
        qty = pos.size if pos else 0.0

        if qty <= 0:
            logger.warning(f"[{self.name}] Close signal but no position found, skip")
            return None

        return Signal(
            inst_id=self.symbol,
            side=side,
            order_type=OrderType.MARKET,
            qty=qty,
            pos_side=pos_side,
            reason=reason,
        )

    # ── 工具方法 ───────────────────────────────────────────────────────────────

    def _indicators_ready(self) -> bool:
        return self._ema_fast.ready and self._ema_slow.ready and self._macd.ready and self._atr.ready

    def _cross_up(self, fast: float, slow: float) -> bool:
        """快线上穿慢线（本根在上，上根在下）"""
        if self._prev_ema_fast is None or self._prev_ema_slow is None:
            return False
        return self._prev_ema_fast <= self._prev_ema_slow and fast > slow

    def _cross_down(self, fast: float, slow: float) -> bool:
        """快线下穿慢线"""
        if self._prev_ema_fast is None or self._prev_ema_slow is None:
            return False
        return self._prev_ema_fast >= self._prev_ema_slow and fast < slow

    def _update_prev(self, ef: float, es: float, hist: float | None):
        self._prev_ema_fast = ef
        self._prev_ema_slow = es
        self._prev_hist = hist

    def _log_state(self, action: str, price: float):
        state_str = "FLAT" if self._state.flat else f"{self._state.pos_side.value.upper()} SL={self._state.stop_loss:.4f}"
        logger.info(f"[{self.name}] {action} @ {price:.4f} | {state_str}")
