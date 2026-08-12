"""金字塔加仓策略：斐波那契支撑 + ATR 间隔 双重确认

思路（移植自 gemini.py 草稿）：
  建仓后若价格下跌，在「触及斐波那契支撑位」且「距上次成交至少 1.2×ATR」
  两个条件同时满足时加仓，且每次加仓的保证金按 1.3 倍递增（正金字塔）。
  出场只有两个：阶梯止盈（按当前档位取目标涨幅）或全局硬止损。

  高时框（默认 4H）负责算支撑位与 ATR，主时框（默认 15m）负责检查触发，
  这样止损检查频率不受 4H 限制。

  只做多。

⚠️ 这是一个「越跌越买」的马丁格尔型策略，风险特征与本项目其他趋势策略
   完全不同：胜率很高、单次亏损很大。上线前务必读 README 中的策略分析一节。

配置项（config）:
  timeframe          主执行时框，默认 15m
  higher_timeframe   支撑位/ATR 所用时框，默认 4H
  max_steps          最大加仓次数，默认 6
  pyramid_ratio      保证金递增系数，默认 1.3
  capital_pct        本策略动用的账户权益比例，默认 0.5
  leverage           杠杆，默认 5
  stop_loss_pct      硬止损：亏损达到「动用资金」的该比例即全平，默认 0.15
  tp_schedule        各档位的止盈目标（相对持仓均价），长度需 >= max_steps
  atr_period         ATR 周期，默认 14
  atr_multiplier     加仓最小间隔 = ATR × 该系数，默认 1.2
  fib_lookback       计算支撑位回看的高时框K线数，默认 100
"""
import math
from typing import TYPE_CHECKING

from loguru import logger

from engine.base_strategy import BaseStrategy
from gateway.models import (
    Candle,
    InstType,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PosSide,
    Signal,
)
from strategies._base_state import PositionState, build_close_signal
from strategies._indicators import RunningATR

if TYPE_CHECKING:
    from engine.portfolio import Portfolio
    from engine.risk_manager import RiskManager
    from gateway.okx_rest import OKXRestClient
    from storage.db import Database

# 斐波那契回调比例，最后一档用前低本身作为强支撑
FIB_RATIOS = (0.236, 0.382, 0.500, 0.618, 0.786)


def pyramid_margin_ratios(max_steps: int, ratio: float) -> list[float]:
    """各档保证金占「动用资金」的比例，等比递增且总和为 1。

    首档 w1 = (1-r) / (1-r^n)，第 i 档 = w1 × r^i。
    这样无论 max_steps / ratio 怎么配，全部加满恰好用尽预算，不会超额。
    """
    if ratio == 1.0:
        return [1.0 / max_steps] * max_steps
    w1 = (1 - ratio) / (1 - math.pow(ratio, max_steps))
    return [w1 * math.pow(ratio, i) for i in range(max_steps)]


class PyramidStrategy(BaseStrategy):
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

        self._max_steps: int = config.get("max_steps", 6)
        self._ratio: float = config.get("pyramid_ratio", 1.3)
        self._capital_pct: float = config.get("capital_pct", 0.5)
        self._leverage: int = config.get("leverage", 5)
        self._sl_pct: float = config.get("stop_loss_pct", 0.15)
        self._atr_mult: float = config.get("atr_multiplier", 1.2)
        self._fib_lookback: int = config.get("fib_lookback", 100)

        self._tp_schedule: list[float] = config.get(
            "tp_schedule", [0.015, 0.020, 0.025, 0.030, 0.038, 0.050]
        )
        if len(self._tp_schedule) < self._max_steps:
            raise ValueError(
                f"tp_schedule 需要至少 {self._max_steps} 个档位，"
                f"当前只有 {len(self._tp_schedule)} 个"
            )

        self._margin_ratios = pyramid_margin_ratios(self._max_steps, self._ratio)

        # 高时框：支撑位 + ATR
        self._higher_tf: str = config.get("higher_timeframe", "4H")
        self._atr = RunningATR(config.get("atr_period", 14))
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._higher_warmed = False

        # 主时框只做触发检查，不需要指标预热
        self.warm_up_period = 5

        # ── 金字塔状态 ────────────────────────────────────────────────────────
        self._state = PositionState()      # 供引擎 reconcile / adopt 使用
        self._step = 0                     # 已完成的建仓次数（0 = 空仓）
        self._avg_entry = 0.0
        self._total_qty = 0.0              # 张数
        self._last_entry_price = 0.0
        self._committed = 0.0              # 本轮动用的资金，建仓时快照
        self._supports: list[float] = []   # 从高到低
        self._ct_val: float | None = None

    # ── 高时框：支撑位与 ATR ───────────────────────────────────────────────────

    @property
    def extra_tf_configs(self):
        warm = max(self._fib_lookback, self._atr.period * 3) + 10
        return [(self._higher_tf, warm, self._handle_higher_tf)]

    def on_extra_tf_warmed(self, tf: str):
        self._higher_warmed = True
        self._refresh_supports()
        logger.info(
            f"[{self.name}] {tf} warm-up done, ATR={self._atr.value:.4f} "
            f"supports={[round(s, 2) for s in self._supports]}"
        )

    async def _handle_higher_tf(self, candles: list[Candle]):
        for c in candles:
            if not c.confirmed:
                continue
            self._atr.update(c.high, c.low, c.close)
            self._highs.append(c.high)
            self._lows.append(c.low)
            if len(self._highs) > self._fib_lookback:
                self._highs = self._highs[-self._fib_lookback:]
                self._lows = self._lows[-self._fib_lookback:]

            # 空仓时持续刷新支撑位；一旦建仓就冻结，避免加仓阶梯在脚下移动
            if self._step == 0 and self._higher_warmed:
                self._refresh_supports()

    def _refresh_supports(self):
        """由近 N 根高时框K线的极值算斐波那契回调支撑，从高到低排列。"""
        if not self._highs:
            return
        high, low = max(self._highs), min(self._lows)
        diff = high - low
        self._supports = sorted(
            [high - diff * r for r in FIB_RATIOS] + [low], reverse=True
        )

    # ── 主时框：触发检查 ───────────────────────────────────────────────────────

    async def on_candle(self, candle: Candle) -> list[Signal]:
        if not candle.confirmed:
            return []
        if not (self._higher_warmed and self._atr.ready and self._supports):
            return []

        close = candle.close
        await self._db.save_candle(candle, self.symbol, self.config.get("timeframe", "?"))

        signals: list[Signal] = []

        if self._step > 0:
            # 兜底：正常路径下 _calc_qty 会在首次建仓时快照 _committed。
            # 若因故为 0，止损阈值会退化成 0 —— 任何浮亏都立刻触发硬止损。
            if self._committed <= 0:
                self._committed = self._portfolio.get_total_equity() * self._capital_pct
                logger.warning(
                    f"[{self.name}] _committed 未初始化，按当前权益重算为 "
                    f"{self._committed:.2f} USDT"
                )

            pnl = await self._unrealized_pnl(close)

            # 防线1：硬止损（相对本轮动用资金）
            if pnl <= -self._committed * self._sl_pct:
                logger.warning(
                    f"[{self.name}] *** HARD STOP *** 浮亏 {pnl:.2f} USDT "
                    f"达到动用资金 {self._committed:.2f} 的 {self._sl_pct:.0%}，全平"
                )
                sig = self._close_signal("Hard stop loss")
                if sig:
                    signals.append(sig)
                    self._reset_cycle()
                await self._save(signals)
                return signals

            # 防线2：阶梯止盈
            tp_rate = self._tp_schedule[self._step - 1]
            target = self._avg_entry * (1 + tp_rate)
            if close >= target:
                logger.info(
                    f"[{self.name}] 第 {self._step} 档止盈触发 "
                    f"(目标 +{tp_rate:.1%}, 均价 {self._avg_entry:.4f} → {close:.4f})，全平"
                )
                sig = self._close_signal(f"Tiered TP step={self._step} (+{tp_rate:.1%})")
                if sig:
                    signals.append(sig)
                    self._reset_cycle()
                await self._save(signals)
                return signals

        # 首次建仓：无条件市价买入（沿用参考实现，见 README 中的缺点分析）
        if self._step == 0:
            logger.info(
                f"[{self.name}] 首次建仓 @ {close:.4f} | "
                f"支撑阶梯 {[round(s, 2) for s in self._supports]}"
            )
            signals.append(self._entry_signal(step=0, reason="Pyramid first entry"))

        # 深度加仓：支撑位 + ATR 间隔 双重确认
        elif self._step < self._max_steps:
            target_support = self._supports[min(self._step, len(self._supports) - 1)]
            min_gap = self._atr.value * self._atr_mult
            atr_trigger = self._last_entry_price - min_gap

            if close <= target_support and close <= atr_trigger:
                logger.info(
                    f"[{self.name}] 双重确认加仓 第 {self._step + 1}/{self._max_steps} 档 "
                    f"@ {close:.4f} | 支撑 {target_support:.4f} | "
                    f"距上次成交 {self._last_entry_price - close:.4f} ≥ {min_gap:.4f}"
                )
                signals.append(self._entry_signal(
                    step=self._step,
                    reason=(f"Pyramid add step={self._step + 1} "
                            f"support={target_support:.4f} gap>={min_gap:.4f}"),
                ))

        await self._save(signals)
        return signals

    # ── 信号与仓位 ─────────────────────────────────────────────────────────────

    def _entry_signal(self, step: int, reason: str) -> Signal:
        return Signal(
            inst_id=self.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            qty=0,                       # 由 _calc_qty 按当档保证金填充
            pos_side=PosSide.LONG if self.inst_type == InstType.SWAP else PosSide.NET,
            # 加仓腿不挂独立的交易所止损：本策略的止损是「整体浮亏达阈值」，
            # 逐笔挂 SL 会各自按自己那一份大小平仓，变成阶梯式部分止损
            reduce_only=False,
            reason=reason,
        )

    def _close_signal(self, reason: str) -> Signal | None:
        return build_close_signal(
            self._state, self.symbol, self._portfolio,
            can_short=False, reason=reason, strategy_name=self.name,
        )

    async def _calc_qty(self, signal: Signal) -> float:
        """按当前档位的保证金预算算张数。

        基数是建仓时快照的 `_committed`，不是实时可用余额——否则浮亏会让
        后面几档的预算随权益一起缩水，正金字塔就名存实亡了。
        """
        if signal.qty > 0:
            return signal.qty

        if self._step == 0:
            self._committed = self._portfolio.get_total_equity() * self._capital_pct

        info = await self._rest.get_instrument(signal.inst_id, self.inst_type)
        self._ct_val = info.ct_val
        ticker = await self._rest.get_ticker(signal.inst_id)

        margin = self._committed * self._margin_ratios[self._step]
        notional = margin * self._leverage
        qty = notional / (info.ct_val * ticker.last)

        from gateway.precision import round_qty
        return round_qty(qty, info.lot_sz, info.min_sz)

    async def _unrealized_pnl(self, price: float) -> float:
        if self._ct_val is None:
            info = await self._rest.get_instrument(self.symbol, self.inst_type)
            self._ct_val = info.ct_val
        return (price - self._avg_entry) * self._total_qty * self._ct_val

    # ── 成交回调：维护金字塔状态 ───────────────────────────────────────────────

    async def on_order_update(self, order: Order):
        if order.status != OrderStatus.FILLED or order.filled_qty <= 0:
            return

        if order.side == OrderSide.BUY:
            filled_value = order.avg_fill_price * order.filled_qty
            total_value = self._avg_entry * self._total_qty + filled_value
            self._total_qty += order.filled_qty
            self._avg_entry = total_value / self._total_qty if self._total_qty else 0.0
            self._last_entry_price = order.avg_fill_price
            self._step += 1
            self._state.open(
                PosSide.LONG if self.inst_type == InstType.SWAP else PosSide.NET,
                self._avg_entry,
                0.0,   # 止损是浮亏比例判定，不是价格判定
            )
            logger.info(
                f"[{self.name}] 第 {self._step}/{self._max_steps} 档成交 "
                f"{order.filled_qty}@{order.avg_fill_price:.4f} → "
                f"均价 {self._avg_entry:.4f} 总量 {self._total_qty}"
            )
        else:
            logger.info(
                f"[{self.name}] 平仓成交 {order.filled_qty}@{order.avg_fill_price:.4f}"
            )

    def _reset_cycle(self):
        self._step = 0
        self._avg_entry = 0.0
        self._total_qty = 0.0
        self._last_entry_price = 0.0
        self._committed = 0.0
        self._state.close()

    def reset_position_state(self):
        self._reset_cycle()

    def adopt_position(self, position) -> bool:
        """重启接管：无从得知重启前处于第几档，一律按已加满处理。

        宁可少赚（不再加仓、等止盈或止损）也不要在未知档位上继续加仓——
        真实档位若比猜测的低，继续加仓会超出原定的资金预算。
        """
        self._total_qty = position.size
        self._avg_entry = position.entry_price
        self._last_entry_price = position.entry_price
        self._step = self._max_steps
        self._committed = self._portfolio.get_total_equity() * self._capital_pct
        self._state.open(position.pos_side, position.entry_price, 0.0)
        logger.warning(
            f"[{self.name}] 接管已有持仓 size={position.size} "
            f"entry={position.entry_price:.4f}；档位未知，按已加满 "
            f"({self._max_steps}/{self._max_steps}) 处理，不再加仓"
        )
        return True

    async def _save(self, signals: list[Signal]):
        for sig in signals:
            await self._db.save_signal(sig, self.name)

    async def on_stop(self):
        logger.info(
            f"[{self.name}] Stopped. step={self._step}/{self._max_steps} "
            f"qty={self._total_qty} avg={self._avg_entry:.4f}"
        )
