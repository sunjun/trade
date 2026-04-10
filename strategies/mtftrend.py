"""多时框趋势策略：4H 定方向 → 1H 做决策 → 15M 找买卖点

框架逻辑：
  4H  宏观趋势  EMA20/50 判断大方向 + 成交量趋势确认
  1H  中期偏向  EMA9/21 + MACD + 成交量放量确认偏多/偏空
  15M 精确入场  EMA9/21 金叉/死叉 + 成交量放量作为触发信号

多头入场（三重共振）：
  1. 4H : EMA20 > EMA50（大趋势向上）且近期成交量均线走升
  2. 1H : EMA9 > EMA21 且 MACD hist > 0 且当前成交量 > vol_ma × threshold
  3. 15M: EMA9 上穿 EMA21（金叉）且当根成交量 > vol_ma × threshold

空头入场（仅合约，三重共振）：
  1. 4H : EMA20 < EMA50 且近期成交量均线走升（下跌放量）
  2. 1H : EMA9 < EMA21 且 MACD hist < 0 且成交量 > vol_ma × threshold
  3. 15M: EMA9 下穿 EMA21（死叉）且成交量 > vol_ma × threshold

止损：入场价 ± ATR(15M) × atr_sl_multiplier
出场：15M 反向交叉 或 止损触发 或 1H 偏向翻转

关键参数（strategies.yaml）：
  h4_timeframe      : "4H"
  h4_ema_fast       : 20
  h4_ema_slow       : 50
  h4_vol_period     : 20
  h1_timeframe      : "1H"
  h1_ema_fast       : 9
  h1_ema_slow       : 21
  h1_macd_fast      : 12
  h1_macd_slow      : 26
  h1_macd_signal    : 9
  h1_vol_period     : 20
  timeframe         : "15m"     # 执行时框
  m15_ema_fast      : 9
  m15_ema_slow      : 21
  m15_vol_period    : 20
  atr_period        : 14
  atr_sl_multiplier : 2.0
  vol_threshold     : 1.2       # 入场要求成交量 ≥ 均量 × 此倍数
  position_size_pct : 0.1
  cooldown_candles  : 2
"""
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from loguru import logger

from engine.base_strategy import BaseStrategy
from gateway.models import Candle, InstType, Order, OrderSide, OrderStatus, OrderType, PosSide, Signal
from strategies._indicators import RunningATR, RunningEMA

if TYPE_CHECKING:
    from engine.portfolio import Portfolio
    from engine.risk_manager import RiskManager
    from gateway.okx_rest import OKXRestClient
    from storage.db import Database


# ── 指标 ──────────────────────────────────────────────────────────────────────

class _MACD:
    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self._ef  = RunningEMA(fast)
        self._es  = RunningEMA(slow)
        self._sig = RunningEMA(signal)
        self.hist: Optional[float] = None

    def update(self, price: float) -> bool:
        ef = self._ef.update(price)
        es = self._es.update(price)
        if ef is None or es is None:
            return False
        sig = self._sig.update(ef - es)
        if sig is None:
            return False
        self.hist = (ef - es) - sig
        return True

    @property
    def ready(self) -> bool:
        return self.hist is not None


class _VolMA:
    """成交量简单移动平均（用 deque 保证 O(1)）"""
    def __init__(self, period: int = 20):
        self.period = period
        self._buf: list[float] = []
        self.value: Optional[float] = None

    def update(self, vol: float):
        self._buf.append(vol)
        if len(self._buf) > self.period:
            self._buf.pop(0)
        if len(self._buf) >= self.period:
            self.value = sum(self._buf) / self.period

    @property
    def ready(self) -> bool:
        return self.value is not None


# ── 单时框上下文（封装该时框全部指标 + 交叉记忆）────────────────────────────

@dataclass
class _TfCtx:
    ema_fast : RunningEMA
    ema_slow : RunningEMA
    vol_ma   : _VolMA
    macd     : Optional[_MACD] = None      # 4H/1H 有，15M 可不用
    atr      : Optional[RunningATR] = None # 15M 用于止损

    # 上一根K线 EMA 值，用于检测交叉
    prev_ef  : Optional[float] = None
    prev_es  : Optional[float] = None

    def cross_up(self) -> bool:
        """快线上穿慢线"""
        ef, es = self.ema_fast.value, self.ema_slow.value
        if ef is None or es is None or self.prev_ef is None:
            return False
        return self.prev_ef <= self.prev_es and ef > es

    def cross_down(self) -> bool:
        """快线下穿慢线"""
        ef, es = self.ema_fast.value, self.ema_slow.value
        if ef is None or es is None or self.prev_ef is None:
            return False
        return self.prev_ef >= self.prev_es and ef < es

    def remember(self):
        """保存本根K线 EMA 值供下根判断"""
        self.prev_ef = self.ema_fast.value
        self.prev_es = self.ema_slow.value

    @property
    def ready(self) -> bool:
        macd_ok = (self.macd is None or self.macd.ready)
        atr_ok  = (self.atr  is None or self.atr.ready)
        return (self.ema_fast.ready and self.ema_slow.ready
                and self.vol_ma.ready and macd_ok and atr_ok)


# ── 持仓状态机 ────────────────────────────────────────────────────────────────

@dataclass
class _State:
    flat       : bool     = True
    pos_side   : PosSide  = PosSide.NET
    entry_price: float    = 0.0
    stop_loss  : float    = 0.0

    def open(self, side: PosSide, price: float, sl: float):
        self.flat = False
        self.pos_side = side
        self.entry_price = price
        self.stop_loss = sl

    def close(self):
        self.flat = True
        self.pos_side = PosSide.NET
        self.entry_price = 0.0
        self.stop_loss = 0.0


# ── 多时框趋势策略 ────────────────────────────────────────────────────────────

class MtfTrendStrategy(BaseStrategy):
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

        # ── 时框配置 ──────────────────────────────────────────────────────────
        self._tf_h4  = config.get("h4_timeframe",  "4H")
        self._tf_h1  = config.get("h1_timeframe",  "1H")
        self._tf_m15 = config.get("timeframe",     "15m")   # 执行时框（主周期）

        # ── 4H 指标 ───────────────────────────────────────────────────────────
        h4f = config.get("h4_ema_fast",   20)
        h4s = config.get("h4_ema_slow",   50)
        self._h4 = _TfCtx(
            ema_fast = RunningEMA(h4f),
            ema_slow = RunningEMA(h4s),
            vol_ma   = _VolMA(config.get("h4_vol_period", 20)),
        )
        self._h4_trend: int = 0   # +1=Bull, -1=Bear, 0=Neutral
        self._h4_warmed: bool = False

        # ── 1H 指标 ───────────────────────────────────────────────────────────
        h1f = config.get("h1_ema_fast",   9)
        h1s = config.get("h1_ema_slow",   21)
        self._h1 = _TfCtx(
            ema_fast = RunningEMA(h1f),
            ema_slow = RunningEMA(h1s),
            vol_ma   = _VolMA(config.get("h1_vol_period", 20)),
            macd     = _MACD(
                config.get("h1_macd_fast",   12),
                config.get("h1_macd_slow",   26),
                config.get("h1_macd_signal",  9),
            ),
        )
        self._h1_bias: int = 0    # +1=Long bias, -1=Short bias, 0=Neutral
        self._h1_warmed: bool = False

        # ── 15M 指标（执行层）────────────────────────────────────────────────
        atr_period = config.get("atr_period", 14)
        self._m15 = _TfCtx(
            ema_fast = RunningEMA(config.get("m15_ema_fast", 9)),
            ema_slow = RunningEMA(config.get("m15_ema_slow", 21)),
            vol_ma   = _VolMA(config.get("m15_vol_period", 20)),
            atr      = RunningATR(atr_period),
        )

        # ── 参数 ──────────────────────────────────────────────────────────────
        self._sl_mult      : float = config.get("atr_sl_multiplier", 2.0)
        self._vol_threshold: float = config.get("vol_threshold", 1.2)
        self._cooldown     : int   = config.get("cooldown_candles", 2)
        self._candles_since : int  = self._cooldown   # 初始为已冷却

        # 预热所需K线数（主周期 15M）
        m15_slow = config.get("m15_ema_slow", 21)
        self.warm_up_period = max(m15_slow * 3, atr_period,
                                  config.get("m15_vol_period", 20)) + 10

        # ── 状态机 ────────────────────────────────────────────────────────────
        self._state    = _State()
        self._can_short = (inst_type == InstType.SWAP)

    # ── engine 识别的多时框配置 ───────────────────────────────────────────────
    @property
    def extra_tf_configs(self) -> list[tuple[str, int, object]]:
        """返回 [(timeframe, warm_up_candles, async_handler), ...] 供引擎订阅。"""
        h4f = self.config.get("h4_ema_slow", 50)
        h4_warm = max(h4f * 3, self.config.get("h4_vol_period", 20)) + 10

        h1s = self.config.get("h1_ema_slow", 21)
        h1_macd_slow = self.config.get("h1_macd_slow", 26)
        h1_macd_sig  = self.config.get("h1_macd_signal", 9)
        h1_warm = max(h1s * 3, h1_macd_slow + h1_macd_sig,
                      self.config.get("h1_vol_period", 20)) + 10

        return [
            (self._tf_h4, h4_warm, self._handle_h4),
            (self._tf_h1, h1_warm, self._handle_h1),
        ]

    def on_extra_tf_warmed(self, tf: str):
        """引擎预热完成后回调，标记该时框为已就绪。"""
        if tf == self._tf_h4:
            self._h4_warmed = True
            logger.info(f"[{self.name}] 4H warm-up done, trend={self._h4_trend:+d}")
        elif tf == self._tf_h1:
            self._h1_warmed = True
            logger.info(f"[{self.name}] 1H warm-up done, bias={self._h1_bias:+d}")

    # ── 4H 回调：更新宏观趋势方向 ─────────────────────────────────────────────

    async def _handle_h4(self, candles: list[Candle]):
        for c in candles:
            ef = self._h4.ema_fast.update(c.close)
            es = self._h4.ema_slow.update(c.close)
            self._h4.vol_ma.update(c.volume)

            if not c.confirmed:
                continue
            if ef is None or es is None:
                self._h4.remember()
                continue

            # 宏观方向：EMA 排列 + 斜率（当前 vs 上根）
            if self._h4.prev_ef is not None:
                ef_rising = ef > self._h4.prev_ef
                es_rising = es > self._h4.prev_es if self._h4.prev_es else True
                if ef > es and ef_rising:
                    new_trend = 1
                elif ef < es and not ef_rising:
                    new_trend = -1
                else:
                    new_trend = 0

                if new_trend != self._h4_trend and self._h4_warmed:
                    arrow = "▲Bull" if new_trend == 1 else ("▼Bear" if new_trend == -1 else "—Neutral")
                    logger.info(
                        f"[{self.name}][4H] Trend → {arrow}  "
                        f"EMA{self._h4.ema_fast.period}={ef:.2f}  "
                        f"EMA{self._h4.ema_slow.period}={es:.2f}"
                    )
                self._h4_trend = new_trend

            self._h4.remember()

    # ── 1H 回调：更新中期偏向 ─────────────────────────────────────────────────

    async def _handle_h1(self, candles: list[Candle]):
        for c in candles:
            ef = self._h1.ema_fast.update(c.close)
            es = self._h1.ema_slow.update(c.close)
            self._h1.vol_ma.update(c.volume)
            self._h1.macd.update(c.close)

            if not c.confirmed:
                continue
            if not self._h1.ready:
                self._h1.remember()
                continue

            hist = self._h1.macd.hist
            vol_ok = self._h1.vol_ma.value and (
                c.volume >= self._h1.vol_ma.value * self._vol_threshold
            )

            if ef > es and hist > 0 and vol_ok:
                new_bias = 1
            elif ef < es and hist < 0 and vol_ok:
                new_bias = -1
            else:
                new_bias = 0

            if new_bias != self._h1_bias and self._h1_warmed:
                arrow = "Long↑" if new_bias == 1 else ("Short↓" if new_bias == -1 else "Neutral")
                logger.info(
                    f"[{self.name}][1H] Bias → {arrow}  "
                    f"EMA{self._h1.ema_fast.period}={ef:.2f}  "
                    f"EMA{self._h1.ema_slow.period}={es:.2f}  "
                    f"MACD_hist={hist:+.4f}  vol={c.volume:.0f}/"
                    f"vol_ma={self._h1.vol_ma.value:.0f}"
                )
                # 1H 偏向翻转时，若有仓位且方向相反，立即平仓
                if (self._state.pos_side == PosSide.LONG  and new_bias == -1) or \
                   (self._state.pos_side == PosSide.SHORT and new_bias == 1):
                    logger.warning(f"[{self.name}][1H] Bias flip → force close position")
                    sig = self._close_signal(c.close, "1H bias flip")
                    if sig:
                        self._state.close()
                        self._candles_since = 0
                        await self._execute_signal(sig)

            self._h1_bias = new_bias
            self._h1.remember()

    # ── 15M 主逻辑：精确入场信号 ─────────────────────────────────────────────

    async def on_candle(self, candle: Candle) -> list[Signal]:
        close = candle.close

        self._m15.ema_fast.update(close)
        self._m15.ema_slow.update(close)
        self._m15.vol_ma.update(candle.volume)
        self._m15.atr.update(candle.high, candle.low, close)

        if not candle.confirmed:
            self._m15.remember()
            return []

        if not self._m15.ready:
            self._m15.remember()
            return []

        ef  = self._m15.ema_fast.value
        es  = self._m15.ema_slow.value
        atr = self._m15.atr.value
        vol = candle.volume
        vol_ma = self._m15.vol_ma.value

        # ── 日志 ──────────────────────────────────────────────────────────────
        pos_str = "FLAT"
        if not self._state.flat:
            pnl = (close - self._state.entry_price) * (
                1 if self._state.pos_side == PosSide.LONG else -1
            )
            pos_str = (f"{self._state.pos_side.value.upper()} "
                       f"entry={self._state.entry_price:.4f} "
                       f"sl={self._state.stop_loss:.4f} uPnL={pnl:+.4f}")

        logger.debug(
            f"[{self.name}][15M] {candle.ts.strftime('%m-%d %H:%M')} "
            f"C={close:.4f} V={vol:.0f}(ma={vol_ma:.0f})  "
            f"EMA{self._m15.ema_fast.period}={ef:.4f} "
            f"EMA{self._m15.ema_slow.period}={es:.4f}  "
            f"4H={'+Bull' if self._h4_trend>0 else ('-Bear' if self._h4_trend<0 else '=Neutral')}  "
            f"1H={'+Long' if self._h1_bias>0  else ('-Short' if self._h1_bias<0 else '=Neutral')}  "
            f"| {pos_str}"
        )

        await self._db.save_candle(candle, self.symbol, self._tf_m15)

        signals: list[Signal] = []
        self._candles_since += 1

        # ── 止损检查（最高优先级）─────────────────────────────────────────────
        if not self._state.flat:
            sl_hit = (
                (self._state.pos_side == PosSide.LONG  and close <= self._state.stop_loss) or
                (self._state.pos_side == PosSide.SHORT and close >= self._state.stop_loss)
            )
            if sl_hit:
                logger.warning(
                    f"[{self.name}] STOP LOSS hit  close={close:.4f}  sl={self._state.stop_loss:.4f}")
                sig = self._close_signal(close, "Stop loss triggered")
                if sig:
                    signals.append(sig)
                    self._state.close()
                self._m15.remember()
                return signals

        # ── 过滤条件 ──────────────────────────────────────────────────────────
        golden_cross  = self._m15.cross_up()
        death_cross   = self._m15.cross_down()
        vol_spike     = vol_ma and (vol >= vol_ma * self._vol_threshold)
        cooldown_ok   = self._candles_since >= self._cooldown
        all_warmed    = self._h4_warmed and self._h1_warmed

        # ── 入场 ──────────────────────────────────────────────────────────────
        if self._state.flat and all_warmed and cooldown_ok:

            # 三重共振多头入场：4H 必须明确向上定方向，1H 至少不反对，15M 金叉+放量触发
            if golden_cross and vol_spike and self._h4_trend > 0 and self._h1_bias >= 0:
                sl = close - self._sl_mult * atr
                sig = Signal(
                    inst_id=self.symbol,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    qty=0,
                    pos_side=PosSide.LONG if self._can_short else PosSide.NET,
                    stop_loss=sl,
                    reason=(f"LONG entry | 4H={self._h4_trend:+d} 1H={self._h1_bias:+d} "
                            f"15M golden-cross | vol={vol:.0f}/{vol_ma:.0f} "
                            f"SL={sl:.4f}"),
                )
                signals.append(sig)
                self._state.open(PosSide.LONG, close, sl)
                self._candles_since = 0
                logger.info(
                    f"[{self.name}] ▲ LONG ENTRY @ {close:.4f}  SL={sl:.4f}  "
                    f"4H={self._h4_trend:+d}  1H={self._h1_bias:+d}  "
                    f"vol={vol:.0f}/{vol_ma:.0f}×{self._vol_threshold}"
                )

            elif (self._can_short and death_cross and vol_spike
                  and self._h4_trend < 0 and self._h1_bias <= 0):
                sl = close + self._sl_mult * atr
                sig = Signal(
                    inst_id=self.symbol,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    qty=0,
                    pos_side=PosSide.SHORT,
                    stop_loss=sl,
                    reason=(f"SHORT entry | 4H={self._h4_trend:+d} 1H={self._h1_bias:+d} "
                            f"15M death-cross | vol={vol:.0f}/{vol_ma:.0f} "
                            f"SL={sl:.4f}"),
                )
                signals.append(sig)
                self._state.open(PosSide.SHORT, close, sl)
                self._candles_since = 0
                logger.info(
                    f"[{self.name}] ▼ SHORT ENTRY @ {close:.4f}  SL={sl:.4f}  "
                    f"4H={self._h4_trend:+d}  1H={self._h1_bias:+d}  "
                    f"vol={vol:.0f}/{vol_ma:.0f}×{self._vol_threshold}"
                )

        # ── 出场：15M 反向交叉 ─────────────────────────────────────────────────
        elif not self._state.flat:
            should_close = (
                (self._state.pos_side == PosSide.LONG  and death_cross) or
                (self._state.pos_side == PosSide.SHORT and golden_cross)
            )
            if should_close:
                reason = "15M EMA reverse cross"
                sig = self._close_signal(close, reason)
                if sig:
                    signals.append(sig)
                    self._state.close()
                    self._candles_since = 0
                    logger.info(f"[{self.name}] EXIT @ {close:.4f}  ({reason})")

        for sig in signals:
            await self._db.save_signal(sig, self.name)

        self._m15.remember()
        return signals

    async def on_order_update(self, order: Order):
        if order.status == OrderStatus.FILLED:
            logger.info(
                f"[{self.name}] Filled: {order.side.value} "
                f"{order.filled_qty}@{order.avg_fill_price:.4f}"
            )
            await self._db.save_order(order, self.name)

    async def on_stop(self):
        logger.info(
            f"[{self.name}] Stopped. State: "
            f"{'flat' if self._state.flat else self._state.pos_side.value}  "
            f"4H={self._h4_trend:+d}  1H={self._h1_bias:+d}"
        )

    # ── 工具 ──────────────────────────────────────────────────────────────────

    def _close_signal(self, price: float, reason: str) -> Optional[Signal]:
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
        return Signal(
            inst_id=self.symbol,
            side=side,
            order_type=OrderType.MARKET,
            qty=qty,
            pos_side=pos_side,
            reason=reason,
        )
