"""利弗摩尔式突破加码（反马丁格尔）

与 PyramidStrategy **方向相反**，这是本仓库里唯一一个向盈利加码的策略：

  PyramidStrategy   价格向不利方向走时加码，越深越重 → 最大仓位出现在判断最错的时刻
  LivermoreStrategy 价格向有利方向走时加码           → 最大仓位出现在判断已被验证的时刻

后者的最坏情况由**构造**保证有上界，不靠参数调优：只有当持仓已经浮盈时才可能
持有大仓位，而每次加码都会把止损一起上移。收益分布也因此相反——胜率低、
右尾厚，靠少数几笔大趋势覆盖大量小亏。

规则（把利弗摩尔的"关键点 + 试探 + 向盈利加码 + 快速认错"规则化，
即海龟法则的形式）：
  入场   收盘价突破前 entry_period 根K线极值（关键点）
  加码   价格每朝有利方向再走 add_atr_step × N，加一个单位，最多 max_units 个
  止损   每次加码后，全部持仓的止损上移到「最新一笔成交价 ∓ stop_atr_mult × N」
  出场   收盘价回落穿越 exit_period 通道，或触及止损

N = 建仓那一刻的 ATR，整轮冻结（海龟原版做法）。不冻结的话止损会随波动率
收缩而抬高，在震荡里被自己扫出去。

仓位：每单位按「触及自身初始止损时恰好亏掉 unit_risk_pct 权益」反推，与
PyramidStrategy 的风险预算反推同源。加满 max_units 个单位后的最坏亏损
（价格从最高一笔直接跌回止损）约为 unit_risk_pct × max_units × (1+…)/2，
仍然有界——具体值见 worst_case_risk_pct()。

配置项（config）:
  timeframe          执行时框，默认 4H
  entry_period       入场通道周期（关键点回看根数），默认 20
  exit_period        出场通道周期，必须 < entry_period，默认 10
                     周期太短会退化成噪声突破：ETH 4H 上 8~10 根 PF 只有 1.00，
                     15 根才转正，20~55 根是一整片有效区间。
  atr_period         ATR 周期，默认 20
  unit_risk_pct      单个单位的风险占权益比例，默认 0.005
  max_units          最多加到几个单位，默认 4
  add_atr_step       每走多少个 N 加一个单位，默认 0.5
  stop_atr_mult      止损距最新成交价几个 N，默认 2.0
  unit_ratio         各单位张数的递减系数（1.0=等量，海龟原版；<1 = 越晚越轻），默认 1.0
  allow_short        是否做空（仅合约），默认 true
  cooldown_candles   平仓后冷却根数，默认 1

仓位基数取 self.strategy_equity()（账户权益 × equity_pct，再受 max_equity 封顶），
不是账户全额——见 BaseStrategy。
"""
from typing import TYPE_CHECKING

from loguru import logger

from config.tz import fmt_ts
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
from gateway.precision import round_qty
from strategies._base_state import PositionState, build_close_signal
from strategies._indicators import Donchian, RunningATR

if TYPE_CHECKING:
    from engine.portfolio import Portfolio
    from engine.risk_manager import RiskManager
    from gateway.okx_rest import OKXRestClient
    from storage.db import Database


class LivermoreStrategy(BaseStrategy):
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
        atr_period = config.get("atr_period", 20)
        if exit_period >= entry_period:
            raise ValueError(
                f"exit_period({exit_period}) 必须 < entry_period({entry_period})，"
                f"否则出场通道比入场通道还宽，开仓即出场"
            )

        self._entry_ch = Donchian(entry_period)
        self._exit_ch = Donchian(exit_period)
        self._atr = RunningATR(atr_period)

        self._unit_risk_pct: float = config.get("unit_risk_pct", 0.005)
        self._max_units: int = config.get("max_units", 4)
        self._add_step: float = config.get("add_atr_step", 0.5)
        self._sl_mult: float = config.get("stop_atr_mult", 2.0)
        self._unit_ratio: float = config.get("unit_ratio", 1.0)
        self._cooldown: int = config.get("cooldown_candles", 1)
        self._can_short = (inst_type == InstType.SWAP
                           and config.get("allow_short", True))

        self.warm_up_period = max(entry_period * 3, atr_period * 3) + 5
        self._state = PositionState()

        # ── 本轮状态 ──────────────────────────────────────────────────────────
        self._units = 0                   # 已建立的单位数（0 = 空仓）
        self._unit_qty = 0.0              # 单位基准张数，建仓时定死
        self._round_atr = 0.0             # 本轮冻结的 N
        self._last_entry = 0.0            # 最近一笔成交价，加码间距的基准
        self._avg_entry = 0.0
        self._total_qty = 0.0
        self._stop_price = 0.0
        self._pending_side: PosSide | None = None   # 已发出但未成交的开仓方向
        self._candles_since_trade = self._cooldown
        self._note = "等待通道与 ATR 预热"

        # 上一根K线的通道值（"读-然后-更新"，避免当前K线污染信号）
        self._prev_entry_high: float | None = None
        self._prev_entry_low: float | None = None
        self._prev_exit_high: float | None = None
        self._prev_exit_low: float | None = None

    # ── 风险 ──────────────────────────────────────────────────────────────────

    def worst_case_risk_pct(self) -> float:
        """整轮最大可能亏损占权益的比例（对所有可能的中途单位数取最大）。

        建到第 k 个单位时止损位于 entry + (k-1)·step·N − sl·N，此时第 i 个
        单位（0 起，成交在 entry + i·step·N）亏 ((i-k+1)·step + sl)·N。

        **必须对 k 取最大值，不能只算加满那一种**：单位越多，早期单位在最终
        止损位上的浮盈越大、抵消越多，加满反而不是最坏情况。比如 step=0.5、
        sl=2、等量单位时，max_units=8 加满只亏 0.5%，而中途 4~5 个单位时
        亏 1.25% —— 只报加满会把风险上界说小 2.5 倍。
        """
        step, sl = self._add_step, self._sl_mult
        if sl <= 0:
            return 0.0
        worst = 0.0
        for k in range(1, self._max_units + 1):
            loss = sum(
                (self._unit_ratio ** i) * ((i - k + 1) * step + sl)
                for i in range(k)
            )
            worst = max(worst, loss)
        return self._unit_risk_pct * worst / sl

    def _unit_size(self, equity: float, atr: float, ct_val: float) -> float:
        """单位张数：触及自身初始止损（sl_mult × N）时恰好亏掉 unit_risk_pct 权益。"""
        risk_per_contract = self._sl_mult * atr * ct_val
        if risk_per_contract <= 0:
            return 0.0
        return equity * self._unit_risk_pct / risk_per_contract

    async def _calc_qty(self, signal: Signal) -> float:
        if signal.qty > 0:            # 平仓腿已算好张数
            return signal.qty
        info = await self._rest.get_instrument(signal.inst_id, self.inst_type)
        qty = self._unit_qty * (self._unit_ratio ** self._units)
        return round_qty(qty, info.lot_sz, info.min_sz)

    # ── 主循环 ────────────────────────────────────────────────────────────────

    def decision_note(self) -> str:
        return self._note

    async def on_candle(self, candle: Candle) -> list[Signal]:
        # 只用已收盘K线驱动指标：盘中推送会把同一根K线反复压入通道队列，
        # 使 N 周期通道退化成"最近 N 次推送"的极值
        if not candle.confirmed:
            return []

        prev_entry_high, prev_entry_low = self._prev_entry_high, self._prev_entry_low
        prev_exit_high, prev_exit_low = self._prev_exit_high, self._prev_exit_low

        self._entry_ch.update(candle.high, candle.low)
        self._exit_ch.update(candle.high, candle.low)
        self._atr.update(candle.high, candle.low, candle.close)

        if self._entry_ch.ready:
            self._prev_entry_high = self._entry_ch.highest
            self._prev_entry_low = self._entry_ch.lowest
        if self._exit_ch.ready:
            self._prev_exit_high = self._exit_ch.highest
            self._prev_exit_low = self._exit_ch.lowest

        if not (self._entry_ch.ready and self._exit_ch.ready and self._atr.ready):
            self._note = (
                f"指标未就绪：通道 entry={self._entry_ch.ready} "
                f"exit={self._exit_ch.ready} atr={self._atr.ready}"
            )
            return []
        if prev_entry_high is None:      # 需要至少两批数据才能比较
            return []

        close = candle.close
        tf = self.config.get("timeframe", "?")
        await self._db.save_candle(candle, self.symbol, tf)
        logger.debug(
            f"[{self.name}] {fmt_ts(candle.ts)} [{tf}] C={close:.4f} | "
            f"EntryH={prev_entry_high:.4f} EntryL={prev_entry_low:.4f} "
            f"ExitL={prev_exit_low:.4f} ATR={self._atr.value:.4f} | "
            f"units={self._units}/{self._max_units}"
        )

        self._candles_since_trade += 1
        signals: list[Signal] = []

        if self._units > 0:
            exit_sig = self._check_exits(close, prev_exit_high, prev_exit_low)
            if exit_sig:
                signals.append(exit_sig)
                await self._save(signals)
                return signals
            add = await self._check_add(close)
            if add:
                signals.append(add)
        else:
            entry = await self._check_entry(close, prev_entry_high, prev_entry_low)
            if entry:
                signals.append(entry)

        await self._save(signals)
        return signals

    # ── 出场 ──────────────────────────────────────────────────────────────────

    def _check_exits(
        self, close: float, prev_exit_high: float, prev_exit_low: float
    ) -> Signal | None:
        long = self._state.pos_side == PosSide.LONG
        hit_stop = close <= self._stop_price if long else close >= self._stop_price
        if hit_stop:
            logger.warning(
                f"[{self.name}] *** 止损 *** {close:.4f} 触及 {self._stop_price:.4f}"
                f"（{self._units}/{self._max_units} 单位，均价 {self._avg_entry:.4f}）"
            )
            return self._close("Stop loss")

        crossed = close < prev_exit_low if long else close > prev_exit_high
        if crossed:
            band = prev_exit_low if long else prev_exit_high
            logger.info(
                f"[{self.name}] 通道出场 @ {close:.4f} "
                f"{'<' if long else '>'} {band:.4f}"
                f"（{self._units}/{self._max_units} 单位，均价 {self._avg_entry:.4f}）"
            )
            return self._close(f"Donchian exit {band:.4f}")

        self._note = self._pos_note(close)
        return None

    def _close(self, reason: str) -> Signal | None:
        sig = build_close_signal(
            self._state, self.symbol, self._portfolio,
            self.inst_type == InstType.SWAP, reason, self.name,
            max_qty=self._total_qty, mgn_mode=self.td_mode,
        )
        if sig:
            self._reset_cycle()
        return sig

    # ── 入场与加码 ────────────────────────────────────────────────────────────

    async def _check_entry(
        self, close: float, prev_entry_high: float, prev_entry_low: float
    ) -> Signal | None:
        if self._candles_since_trade < self._cooldown:
            self._note = (
                f"空仓等待｜冷却中 "
                f"{self._candles_since_trade}/{self._cooldown} 根K线"
            )
            return None

        if close > prev_entry_high:
            side, pos_side, band = OrderSide.BUY, PosSide.LONG, prev_entry_high
        elif self._can_short and close < prev_entry_low:
            side, pos_side, band = OrderSide.SELL, PosSide.SHORT, prev_entry_low
        else:
            self._note = (
                f"空仓等待｜未突破关键点：现价 {close:.4f}，"
                f"上破需 > {prev_entry_high:.4f}（差 {prev_entry_high - close:+.4f}）"
                + (f"，下破需 < {prev_entry_low:.4f}" if self._can_short else "")
            )
            return None

        atr = self._atr.value
        info = await self._rest.get_instrument(self.symbol, self.inst_type)
        equity = self.strategy_equity()
        unit_qty = self._unit_size(equity, atr, info.ct_val)
        if round_qty(unit_qty, info.lot_sz, info.min_sz) <= 0:
            self._note = (
                f"空仓等待｜已突破 {band:.4f}，但单位张数 {unit_qty:.4f} "
                f"低于交易所最小下单量 minSz={info.min_sz}"
                f"（权益 {equity:.2f}，单位风险 {self._unit_risk_pct:.2%}）"
            )
            return None

        # N 整轮冻结：不冻结的话止损会随波动率收缩而抬高，在震荡里自己扫自己
        self._round_atr = atr
        self._unit_qty = unit_qty
        self._pending_side = pos_side
        stop = (close - self._sl_mult * atr if pos_side == PosSide.LONG
                else close + self._sl_mult * atr)
        logger.info(
            f"[{self.name}] 突破关键点 {band:.4f} → {side.value.upper()} 试探首仓 "
            f"@ {close:.4f} | N={atr:.4f} 止损 {stop:.4f} | "
            f"计划最多 {self._max_units} 个单位，满仓最坏亏损 "
            f"{self.worst_case_risk_pct():.2%} 权益"
        )
        return Signal(
            inst_id=self.symbol,
            side=side,
            order_type=OrderType.MARKET,
            qty=0,                       # 由 _calc_qty 填本单位张数
            pos_side=pos_side if self.inst_type == InstType.SWAP else PosSide.NET,
            stop_loss=stop,
            reason=f"Livermore breakout {band:.4f} unit=1/{self._max_units}",
        )

    async def _check_add(self, close: float) -> Signal | None:
        if self._units >= self._max_units:
            self._note = f"{self._pos_note(close)}｜已满仓，只等通道出场或止损"
            return None

        long = self._state.pos_side == PosSide.LONG
        gap = self._add_step * self._round_atr
        trigger = self._last_entry + gap if long else self._last_entry - gap
        advanced = close >= trigger if long else close <= trigger
        if not advanced:
            self._note = (
                f"{self._pos_note(close)}｜下一单位需价格再走到 {trigger:.4f}"
                f"（还差 {abs(trigger - close):.4f} = "
                f"{abs(trigger - close) / self._round_atr:.2f}N）"
            )
            return None

        side = OrderSide.BUY if long else OrderSide.SELL
        logger.info(
            f"[{self.name}] 向盈利加码 第 {self._units + 1}/{self._max_units} 单位 "
            f"@ {close:.4f}（距上笔 {abs(close - self._last_entry) / self._round_atr:.2f}N "
            f"≥ {self._add_step}N）"
        )
        return Signal(
            inst_id=self.symbol,
            side=side,
            order_type=OrderType.MARKET,
            qty=0,
            pos_side=self._state.pos_side if self.inst_type == InstType.SWAP
            else PosSide.NET,
            reason=f"Livermore add unit={self._units + 1}/{self._max_units}",
        )

    # ── 成交回调 ──────────────────────────────────────────────────────────────

    async def on_order_update(self, order: Order):
        if order.status != OrderStatus.FILLED or order.filled_qty <= 0:
            return

        # 这笔成交是在建仓还是在平仓？
        # 空仓时看它是否与已发出的开仓方向一致——开空是 SELL，早先这里写成
        # 「BUY 才算开仓」，于是开空成交被当成平仓：策略把自己重置回空仓，
        # 空单却已挂在交易所上，接着又去开下一笔，空头仓位只增不减。
        # 另一半：空仓且没有 _pending_side，说明这是上一轮平仓信号的迟到回报
        # ——_close() 在发出信号那一刻就重置了状态，成交回报晚一步才到。平多是
        # SELL、平空是 BUY，只看方向会把平空的 BUY 认成开多。
        if self._units == 0:
            expect = (OrderSide.SELL if self._pending_side == PosSide.SHORT
                      else OrderSide.BUY)
            opening = self._pending_side is not None and order.side == expect
        else:
            opening = (order.side == OrderSide.BUY) == (self._state.pos_side == PosSide.LONG)
        if not opening:
            self._total_qty = max(0.0, self._total_qty - order.filled_qty)
            if self._total_qty <= 1e-9:
                self._reset_cycle()
            logger.info(
                f"[{self.name}] 平仓成交 {order.filled_qty}@{order.avg_fill_price:.4f}"
                f" → 剩余 {self._total_qty:.4f} 张"
            )
            return

        pos_side = self._pending_side if self._units == 0 else self._state.pos_side
        total_value = self._avg_entry * self._total_qty + \
            order.avg_fill_price * order.filled_qty
        self._total_qty += order.filled_qty
        self._avg_entry = total_value / self._total_qty if self._total_qty else 0.0
        self._last_entry = order.avg_fill_price
        self._units += 1

        # 每次加码后，全部持仓的止损一并上移到「最新成交价 ∓ sl_mult × N」。
        # 这正是反马丁的保护机制：仓位变大的同时，风险敞口反而收窄。
        self._stop_price = (
            self._last_entry - self._sl_mult * self._round_atr
            if pos_side == PosSide.LONG
            else self._last_entry + self._sl_mult * self._round_atr
        )
        self._state.open(pos_side, self._avg_entry, self._stop_price)
        self._pending_side = None
        self._candles_since_trade = 0
        logger.info(
            f"[{self.name}] 第 {self._units}/{self._max_units} 单位成交 "
            f"{order.filled_qty}@{order.avg_fill_price:.4f} → 均价 {self._avg_entry:.4f} "
            f"总量 {self._total_qty:.4f}｜止损上移至 {self._stop_price:.4f}"
        )

    # ── 状态 ──────────────────────────────────────────────────────────────────

    def _pos_note(self, close: float) -> str:
        if self._avg_entry <= 0:
            return f"持仓 {self._units}/{self._max_units} 单位（均价未知）"
        long = self._state.pos_side == PosSide.LONG
        pnl = (close / self._avg_entry - 1) * (1 if long else -1)
        return (
            f"持仓 {self._units}/{self._max_units} 单位 {self._total_qty:.4f} 张"
            f"｜均价 {self._avg_entry:.4f} 现价 {close:.4f} ({pnl:+.2%})"
            f"｜止损 {self._stop_price:.4f}"
        )

    def _reset_cycle(self):
        self._units = 0
        self._unit_qty = 0.0
        self._round_atr = 0.0
        self._last_entry = 0.0
        self._avg_entry = 0.0
        self._total_qty = 0.0
        self._stop_price = 0.0
        self._pending_side = None
        self._candles_since_trade = 0
        self._state.close()

    def reset_position_state(self):
        self._reset_cycle()

    def _recompute_stop_loss(self, entry_price: float, pos_side) -> float | None:
        if not self._atr.ready:
            return None
        delta = self._sl_mult * self._atr.value
        return (entry_price + delta if pos_side == PosSide.SHORT
                else entry_price - delta)

    def adopt_position(self, position) -> bool:
        """重启接管：无从得知重启前加到第几个单位，一律按已满仓处理。

        与金字塔同理——真实单位数若比猜测的低，继续加码会超出原定风险预算。
        """
        if not super().adopt_position(position):
            return False
        self._units = self._max_units
        self._total_qty = position.size
        self._avg_entry = position.entry_price
        self._last_entry = position.entry_price
        self._round_atr = self._atr.value
        self._stop_price = self._state.stop_loss
        return True

    async def _save(self, signals: list[Signal]):
        for sig in signals:
            await self._db.save_signal(sig, self.name)

    async def on_stop(self):
        logger.info(
            f"[{self.name}] Stopped. units={self._units}/{self._max_units} "
            f"qty={self._total_qty:.4f} avg={self._avg_entry:.4f}"
        )
