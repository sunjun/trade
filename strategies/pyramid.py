"""金字塔加仓策略：斐波那契支撑 + ATR 间隔 双重确认

思路（移植自 gemini.py 草稿，并做了风险结构上的重设计）：
  在上升趋势的回调里分批建仓。价格每触及一道斐波那契支撑、且距上次成交
  至少 1.2×ATR 时加一档，档位越深投入越大（正金字塔）。

  与原草稿最大的区别是**先定风险、再反推仓位**：
  止损价由市场结构决定（跌破前低 = "支撑会撑住"这个前提失效），
  各档张数则由「加满档且打到止损时恰好亏掉风险预算」反解出来。
  于是最坏情况在开仓前就是已知且有上界的，而不是仓位定死之后听天由命。

  高时框（默认 4H）负责趋势方向、支撑位与 ATR；
  主时框（默认 15m）负责止损、止盈与加仓的触发检查。

  只做多。

⚠️ 这仍然是马丁格尔型策略：胜率高、左尾厚。上述改造的目标不是让它更赚钱，
   而是让最坏情况可计算。上线前务必读 README 的策略分析并跑一段包含
   单边下跌的回测。

配置项（config）:
  timeframe          主执行时框，默认 15m
  higher_timeframe   趋势/支撑/ATR 所用时框，默认 4H
  max_steps          最大档位数，默认 6
  pyramid_ratio      各档张数递增系数，默认 1.3
  risk_pct           单轮最大可接受亏损占权益比例，默认 0.02
  max_leverage       反推出的名义杠杆上限，超过则放弃这一轮，默认 5
  stop_atr_mult      止损价 = 最低支撑 − 该系数 × ATR，默认 1.5
  tp_schedule        各档止盈目标（相对持仓均价），**递减**，长度 >= max_steps
  partial_tp         止盈时是否只平最深一档（默认 true），false = 全平
  max_hold_bars      满档后最多滞留多少根主时框K线，超时离场，默认 200
  trend_filter       是否要求高时框均线向上才开新一轮，默认 true
  ema_fast/ema_slow  趋势判定用的高时框均线周期，默认 21 / 55
  trend_max_margin   快线领先慢线的**上限**（如 0.02 = 2%），默认 None 不设限。
                     领先太多说明价格刚跑完一段，此时接回调接的是动能衰竭
  first_entry_at_support  首仓是否也要等回调到第一道支撑，默认 true
  drop_atr_min       首仓门槛：距近期高点至少跌够几个 ATR（如 2.0），默认 None 不设限。
                     用波动率归一化，固定百分比在不同波动率时期含义完全不同
  require_reclaim    首仓是否要求右侧确认（本根收阳且收在上一根高点之上），默认 false。
                     超跌判据本身不含「跌势停没停」的信息，单用会在下跌全程接飞刀
  ladder_spread      加仓档位是否均匀铺满整个支撑区间（末档落在前低、紧挨止损），
                     默认 false = 只用最浅的几道支撑。false 时加仓射程远短于
                     止损射程，中间一段满仓无摊薄
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
from gateway.precision import round_qty
from strategies._base_state import PositionState, build_close_signal
from strategies._indicators import RunningATR, RunningEMA

if TYPE_CHECKING:
    from engine.portfolio import Portfolio
    from engine.risk_manager import RiskManager
    from gateway.okx_rest import OKXRestClient
    from storage.db import Database

# 斐波那契回调比例，最后一档用前低本身作为强支撑
FIB_RATIOS = (0.236, 0.382, 0.500, 0.618, 0.786)


def pyramid_weights(max_steps: int, ratio: float) -> list[float]:
    """各档的相对张数权重，等比递增且总和为 1。

    首档 w1 = (1-r)/(1-rⁿ)，第 i 档 = w1 × rⁱ。
    """
    if ratio == 1.0:
        return [1.0 / max_steps] * max_steps
    w1 = (1 - ratio) / (1 - math.pow(ratio, max_steps))
    return [w1 * math.pow(ratio, i) for i in range(max_steps)]


def plan_ladder(
    weights: list[float],
    entries: list[float],
    stop_price: float,
    risk_budget: float,
    ct_val: float,
) -> list[float]:
    """由风险预算反推各档张数。

    求总张数 Q 使得「加满所有档、价格跌到 stop_price」时的总亏损等于预算：
        Σ wᵢ·Q·(entryᵢ − stop_price)·ct_val = risk_budget

    于是任何更早的档位，亏损都严格小于预算——保护强度随暴露单调，
    而不是像固定金额止损那样在浅档形同虚设、在满档一碰就停。

    公式成立的前提是**每一档都在止损之上**。只校验加权总和为正是不够的：
    支撑位过期时（建仓期间不刷新支撑，价格已经走远）可能出现「首仓在止损
    下方、而更深的几档在止损上方」，正负相抵后 denom 仍为正，于是规划通过、
    挂出一个高于入场价的止损——实盘会被交易所拒单或瞬间触发。
    """
    if any(e <= stop_price for e in entries):
        return []
    denom = sum(w * (e - stop_price) for w, e in zip(weights, entries, strict=True))
    if denom <= 0:
        return []
    total_qty = risk_budget / (ct_val * denom)
    return [w * total_qty for w in weights]


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
        self._risk_pct: float = config.get("risk_pct", 0.02)
        self._max_leverage: float = config.get("max_leverage", 5)
        self._stop_atr_mult: float = config.get("stop_atr_mult", 1.5)
        self._atr_mult: float = config.get("atr_multiplier", 1.2)
        self._fib_lookback: int = config.get("fib_lookback", 100)
        self._partial_tp: bool = config.get("partial_tp", True)
        self._max_hold_bars: int = config.get("max_hold_bars", 200)
        self._trend_filter: bool = config.get("trend_filter", True)
        self._first_at_support: bool = config.get("first_entry_at_support", True)
        self._ladder_spread: bool = config.get("ladder_spread", False)
        self._trend_max_margin: float | None = config.get("trend_max_margin")
        self._drop_atr_min: float | None = config.get("drop_atr_min")
        self._require_reclaim: bool = config.get("require_reclaim", False)
        self._prev_candle: Candle | None = None   # 上一根主时框K线，用于收复确认

        # 止盈梯度**递减**：浅档子弹多、暴露低，扛得起等一个大波段；
        # 深档的目标是逃出来并重置，不是把利润最大化。
        self._tp_schedule: list[float] = config.get(
            "tp_schedule", [0.050, 0.038, 0.030, 0.025, 0.020, 0.015]
        )
        if len(self._tp_schedule) < self._max_steps:
            raise ValueError(
                f"tp_schedule 需要至少 {self._max_steps} 个档位，"
                f"当前只有 {len(self._tp_schedule)} 个"
            )

        self._weights = pyramid_weights(self._max_steps, self._ratio)

        # 高时框指标
        self._higher_tf: str = config.get("higher_timeframe", "4H")
        self._atr = RunningATR(config.get("atr_period", 14))
        self._ema_fast = RunningEMA(config.get("ema_fast", 21))
        self._ema_slow = RunningEMA(config.get("ema_slow", 55))
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._higher_warmed = False

        self.warm_up_period = 5   # 主时框只做触发检查，不需要指标预热

        # ── 本轮状态 ──────────────────────────────────────────────────────────
        self._state = PositionState()      # 供引擎 reconcile / adopt 使用
        self._step = 0                     # 已完成的建仓次数（0 = 空仓）
        self._avg_entry = 0.0
        self._total_qty = 0.0
        self._last_entry_price = 0.0
        self._supports: list[float] = []   # 从高到低
        self._step_qty: list[float] = []   # 本轮各档计划张数
        self._stop_price = 0.0             # 本轮结构止损价
        self._bars_at_max = 0
        self._ct_val: float | None = None
        self._note = "等待高时框预热完成"   # 「为什么没下单」，由各道闸门填写

    # ── 高时框：趋势、支撑位与 ATR ─────────────────────────────────────────────

    @property
    def extra_tf_configs(self):
        warm = max(self._fib_lookback, self._ema_slow.period * 3,
                   self._atr.period * 3) + 10
        return [(self._higher_tf, warm, self._handle_higher_tf)]

    def on_extra_tf_warmed(self, tf: str):
        self._higher_warmed = True
        self._refresh_supports()
        logger.info(
            f"[{self.name}] {tf} warm-up done | ATR={self._atr.value:.4f} "
            f"trend={'UP' if self._trend_ok() else 'DOWN/FLAT'} "
            f"supports={[round(s, 2) for s in self._supports]}"
        )

    async def _handle_higher_tf(self, candles: list[Candle]):
        for c in candles:
            if not c.confirmed:
                continue
            self._atr.update(c.high, c.low, c.close)
            self._ema_fast.update(c.close)
            self._ema_slow.update(c.close)
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

    def trend_margin(self) -> float:
        """快线高于慢线的相对幅度。负数 = 快线在下方。"""
        if not self._ema_slow.value:
            return 0.0
        return self._ema_fast.value / self._ema_slow.value - 1

    def _trend_ok(self) -> bool:
        """高时框均线向上才允许开新一轮。

        马丁最大的死法就是在下跌趋势里启动一整轮金字塔——这道过滤会让
        牛市里也错过一些机会，但它把「在单边下跌中加满档」这个最坏场景排除掉。

        trend_max_margin 给这道闸门加一个**上界**：快线领先太多说明价格刚跑完
        一段，此时「回调到第一道支撑」买的是动能衰竭而不是回调。本策略的结构
        （跌到支撑买、涨够就卖）本质是均值回归，单边趋势的末端正是它的弱区。
        """
        if not self._trend_filter:
            return True
        if not (self._ema_fast.ready and self._ema_slow.ready):
            return False
        if self._trend_max_margin is not None:
            return 0 < self.trend_margin() <= self._trend_max_margin
        return self._ema_fast.value > self._ema_slow.value

    # ── 风险预算 → 仓位 ────────────────────────────────────────────────────────

    def _invalidation_price(self) -> float:
        """论证失效价：跌破最低支撑说明"支撑会撑住"这个前提不成立了。

        缓冲用 ATR 而非固定百分比，随波动率自适应。
        """
        return min(self._supports) - self._stop_atr_mult * self._atr.value

    def _planned_entries(self, first_price: float) -> list[float]:
        """本轮计划的各档入场价：首仓在当前价，之后依次落在各道支撑上。

        ladder_spread=False（默认，原行为）取最浅的 max_steps-1 道支撑，
        阶梯只覆盖前几档斐波那契回撤——而止损始终在最低支撑下方。
        max_steps 越小这个错配越明显：加仓射程 −5%，止损射程 −15%，
        中间 10 个百分点满仓无摊薄，回测里的滞留超时和止损都出在这一段。

        ladder_spread=True 把 max_steps-1 道入场**均匀铺满整个支撑区间**，
        末档落在最低支撑（前低）上，紧挨止损，让两个射程对齐。
        """
        rest = self._supports[1:]
        if not rest:
            return [first_price]
        n = self._max_steps - 1
        if n <= 0:
            return [first_price]
        if not self._ladder_spread or n >= len(rest):
            return [first_price, *rest[:n]]
        # 在 rest 上等距取 n 个点，末点固定为最低支撑
        idx = [round(i * (len(rest) - 1) / (n - 1)) if n > 1 else len(rest) - 1
               for i in range(n)]
        return [first_price, *(rest[i] for i in dict.fromkeys(idx))]

    async def _plan_round(self, first_price: float) -> bool:
        """开新一轮前规划整个阶梯。返回 False 表示这轮不该开。"""
        info = await self._rest.get_instrument(self.symbol, self.inst_type)
        self._ct_val = info.ct_val

        stop_price = self._invalidation_price()
        entries = self._planned_entries(first_price)
        equity = self._portfolio.get_total_equity()
        budget = equity * self._risk_pct

        qty = plan_ladder(self._weights, entries, stop_price, budget, info.ct_val)
        if not qty:
            logger.warning(f"[{self.name}] 阶梯规划失败（止损价高于入场价？），跳过")
            return False

        # 满档名义杠杆校验：止损太远会要求过大的仓位，此时应放弃而不是硬上
        notional = sum(q * info.ct_val * e for q, e in zip(qty, entries, strict=True))
        lev = notional / equity if equity > 0 else float("inf")
        if lev > self._max_leverage:
            logger.info(
                f"[{self.name}] 放弃本轮：止损距离要求满档名义杠杆 {lev:.2f}x "
                f"> 上限 {self._max_leverage}x（止损价 {stop_price:.4f} 距首仓过远）"
            )
            return False

        self._step_qty = qty
        self._stop_price = stop_price
        logger.info(
            f"[{self.name}] 本轮规划 | 风险预算 {budget:.2f} USDT ({self._risk_pct:.1%}) "
            f"| 止损 {stop_price:.4f} | 满档杠杆 {lev:.2f}x | "
            f"各档张数 {[round(q, 2) for q in qty]}"
        )
        return True

    async def _calc_qty(self, signal: Signal) -> float:
        if signal.qty > 0:          # 平仓/减仓腿已算好
            return signal.qty
        if not self._step_qty:
            return 0.0
        info = await self._rest.get_instrument(signal.inst_id, self.inst_type)
        return round_qty(self._step_qty[self._step], info.lot_sz, info.min_sz)

    # ── 主时框：触发检查 ───────────────────────────────────────────────────────

    def decision_note(self) -> str:
        return self._note

    def _pos_note(self, close: float) -> str:
        """持仓状态摘要：档位 / 均价 / 浮盈 / 本档止盈目标 / 结构止损。"""
        if self._avg_entry <= 0:
            return f"持仓 {self._step}/{self._max_steps} 档（均价未知）"
        tp_rate = self._tp_schedule[max(self._step - 1, 0)]
        tp = self._avg_entry * (1 + tp_rate)
        return (
            f"持仓 {self._step}/{self._max_steps} 档 {self._total_qty:.2f} 张"
            f"｜均价 {self._avg_entry:.4f} 现价 {close:.4f} "
            f"({close / self._avg_entry - 1:+.2%})"
            f"｜止盈 {tp:.4f} (+{tp_rate:.1%}) 止损 {self._stop_price:.4f}"
        )

    async def on_candle(self, candle: Candle) -> list[Signal]:
        if not candle.confirmed:
            return []
        if not (self._higher_warmed and self._atr.ready and self._supports):
            self._note = (
                f"高时框({self._higher_tf})尚未就绪："
                f"warmed={self._higher_warmed} atr_ready={self._atr.ready} "
                f"支撑位数={len(self._supports)}"
            )
            return []

        close = candle.close
        await self._db.save_candle(candle, self.symbol, self.config.get("timeframe", "?"))
        signals: list[Signal] = []

        if self._step > 0:
            exit_sig = self._check_exits(close)
            if exit_sig:
                signals.append(exit_sig)
                await self._save(signals)
                return signals

        if self._step == 0:
            entry = await self._check_first_entry(candle)
            if entry:
                signals.append(entry)
        elif self._step < self._max_steps:
            add = self._check_add(close)
            if add:
                signals.append(add)
        else:
            self._note = (
                f"{self._pos_note(close)}"
                f"｜已满档，只等止盈/止损"
                f"（滞留 {self._bars_at_max}/{self._max_hold_bars} 根K线）"
            )

        # 收复确认要比对上一根，所以放在全部检查之后再更新
        self._prev_candle = candle
        await self._save(signals)
        return signals

    def _check_exits(self, close: float) -> Signal | None:
        """按优先级检查三道出场：结构止损 → 滞留超时 → 阶梯止盈。"""
        # 防线1：结构止损（跌破失效价）
        if close <= self._stop_price:
            logger.warning(
                f"[{self.name}] *** 结构止损 *** {close:.4f} 跌破失效价 "
                f"{self._stop_price:.4f}（第 {self._step}/{self._max_steps} 档），全平"
            )
            sig = self._close_signal("Structural stop: below invalidation price")
            if sig:
                self._reset_cycle()
            return sig

        # 防线2：满档滞留超时——马丁的真正死法是被困住，给暴露时间加个上界
        if self._step >= self._max_steps:
            self._bars_at_max += 1
            if self._bars_at_max >= self._max_hold_bars:
                logger.warning(
                    f"[{self.name}] *** 滞留超时 *** 满档已 {self._bars_at_max} 根K线"
                    f"仍未回到目标，主动离场"
                )
                sig = self._close_signal("Max-step hold timeout")
                if sig:
                    self._reset_cycle()
                return sig

        # 防线3：阶梯止盈
        tp_rate = self._tp_schedule[self._step - 1]
        target = self._avg_entry * (1 + tp_rate)
        if close < target:
            return None

        if self._partial_tp and self._step > 1:
            # 只平最深一档：立刻降暴露，剩余仓位继续等下一档目标。
            # 直接打击"在最大暴露上停留的时间"这个真正的风险源。
            qty = min(self._step_qty[self._step - 1], self._total_qty)
            sig = self._reduce_signal(
                qty, f"Tiered TP step={self._step} (+{tp_rate:.1%}), trim deepest tranche"
            )
            if sig:
                logger.info(
                    f"[{self.name}] 第 {self._step} 档止盈减仓 {qty:.2f} 张 @ {close:.4f} "
                    f"(目标 +{tp_rate:.1%}, 均价 {self._avg_entry:.4f})"
                )
            return sig

        logger.info(
            f"[{self.name}] 第 {self._step} 档止盈全平 @ {close:.4f} "
            f"(目标 +{tp_rate:.1%}, 均价 {self._avg_entry:.4f})"
        )
        sig = self._close_signal(f"Tiered TP step={self._step} (+{tp_rate:.1%})")
        if sig:
            self._reset_cycle()
        return sig

    def drop_atr(self, close: float) -> float:
        """距近期高点跌了几个 ATR。用波动率归一化，固定百分比在不同波动率
        时期含义完全不同（ATR=2% 和 ATR=6% 时跌 8% 是两回事）。

        锚点和 ATR 都取自高时框，与支撑位、结构止损、加仓间距同源——
        再引入一套标准差口径会让几个模块用两种波动率定义，参数之间没法互推。
        """
        if not self._highs or not self._atr.value:
            return 0.0
        return (max(self._highs) - close) / self._atr.value

    def _reclaimed(self, candle: Candle) -> bool:
        """右侧确认：本根收阳且收在上一根高点之上。

        超跌判据本身不含「跌势停没停」的信息——下跌趋势里它在全程反复满足，
        只会让你更早更频繁地接飞刀（回测里 2025-09~11 三轮连续止损，每轮
        入场时"已回调至支撑"都成立）。这道确认用最少的信息换掉最低点。
        """
        if self._prev_candle is None:
            return False
        return candle.close > candle.open and candle.close > self._prev_candle.high

    async def _check_first_entry(self, candle: Candle) -> Signal | None:
        """首仓：趋势向上 + 已回调到第一道支撑，才开。

        原草稿在这里是无条件市价买入——而首仓决定了整轮的成本基准，
        却是唯一不看位置的一笔。

        drop_atr_min / require_reclaim 是可选的第三、四道闸门：前者要求
        「跌得够深（按波动率归一化）」，后者要求「跌势已经出现停止的迹象」。
        """
        close = candle.close
        if not self._trend_ok():
            if not (self._ema_fast.ready and self._ema_slow.ready):
                self._note = (
                    f"空仓等待｜趋势过滤：{self._higher_tf} 均线未就绪 "
                    f"(EMA{self._ema_fast.period}/EMA{self._ema_slow.period})"
                )
            elif (self._trend_max_margin is not None
                    and self.trend_margin() > self._trend_max_margin):
                self._note = (
                    f"空仓等待｜趋势过热：{self._higher_tf} 快线领先慢线 "
                    f"{self.trend_margin():.2%} > 上限 {self._trend_max_margin:.2%}"
                    f"（EMA{self._ema_fast.period}={self._ema_fast.value:.4f} / "
                    f"EMA{self._ema_slow.period}={self._ema_slow.value:.4f}）"
                    f"，此时接回调多半是接动能衰竭"
                )
            else:
                self._note = (
                    f"空仓等待｜趋势过滤未通过：{self._higher_tf} "
                    f"EMA{self._ema_fast.period}={self._ema_fast.value:.4f} ≤ "
                    f"EMA{self._ema_slow.period}={self._ema_slow.value:.4f}"
                    f"（差 {self._ema_slow.value - self._ema_fast.value:.4f}，需快线上穿）"
                )
            return None
        if self._first_at_support and close > self._supports[0]:
            self._note = (
                f"空仓等待｜趋势向上，但现价 {close:.4f} 高于第一道支撑 "
                f"{self._supports[0]:.4f}，还需回调 "
                f"{close / self._supports[0] - 1:.2%} 才建首仓"
            )
            return None
        if self._drop_atr_min is not None and self.drop_atr(close) < self._drop_atr_min:
            self._note = (
                f"空仓等待｜跌幅不够：距近期高点 {max(self._highs):.4f} 跌了 "
                f"{self.drop_atr(close):.2f} 个 ATR < 门槛 {self._drop_atr_min:.2f}"
                f"（ATR={self._atr.value:.4f}，还需再跌 "
                f"{(self._drop_atr_min - self.drop_atr(close)) * self._atr.value:.4f}）"
            )
            return None
        if self._require_reclaim and not self._reclaimed(candle):
            prev_high = self._prev_candle.high if self._prev_candle else None
            self._note = (
                f"空仓等待｜已超跌但跌势未止：本根 O={candle.open:.4f} "
                f"C={candle.close:.4f}"
                + (f"，需收阳且收上前一根高点 {prev_high:.4f}"
                   if prev_high is not None else "，等待上一根K线")
            )
            return None
        if not await self._plan_round(close):
            self._note = (
                f"空仓等待｜已到支撑 {self._supports[0]:.4f}，"
                f"但本轮阶梯规划被否决（详见上一条日志）"
            )
            return None

        logger.info(
            f"[{self.name}] 首仓 @ {close:.4f} | 趋势向上 | "
            f"已回调至第一道支撑 {self._supports[0]:.4f} | "
            f"止损 {self._stop_price:.4f}"
        )
        return self._entry_signal("Pyramid first entry (trend up + at support)")

    def _check_add(self, close: float) -> Signal | None:
        """加仓：触及下一道支撑 + 距上次成交至少一个 ATR 间隔。"""
        target_support = self._supports[min(self._step, len(self._supports) - 1)]
        min_gap = self._atr.value * self._atr_mult
        atr_trigger = self._last_entry_price - min_gap

        if close > target_support:
            self._note = (
                f"{self._pos_note(close)}"
                f"｜下一档({self._step + 1}/{self._max_steps})需先跌至支撑 "
                f"{target_support:.4f}（还差 {close / target_support - 1:.2%}）"
            )
            return None
        if close > atr_trigger:
            self._note = (
                f"{self._pos_note(close)}"
                f"｜已触及支撑 {target_support:.4f}，但距上次成交 "
                f"{self._last_entry_price:.4f} 只回落 {self._last_entry_price - close:.4f}"
                f" < 最小间隔 {min_gap:.4f} (ATR {self._atr.value:.4f} × {self._atr_mult})"
                f"，跌到 {atr_trigger:.4f} 才加仓"
            )
            return None

        logger.info(
            f"[{self.name}] 双重确认加仓 第 {self._step + 1}/{self._max_steps} 档 "
            f"@ {close:.4f} | 支撑 {target_support:.4f} | "
            f"距上次成交 {self._last_entry_price - close:.4f} ≥ {min_gap:.4f}"
        )
        return self._entry_signal(
            f"Pyramid add step={self._step + 1} support={target_support:.4f}"
        )

    # ── 信号构造 ───────────────────────────────────────────────────────────────

    def _entry_signal(self, reason: str) -> Signal:
        return Signal(
            inst_id=self.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            qty=0,                       # 由 _calc_qty 取本档计划张数
            pos_side=PosSide.LONG if self.inst_type == InstType.SWAP else PosSide.NET,
            # 各档共用同一个结构止损价，所以交易所侧的附加止损会一起触发，
            # 不会变成阶梯式的部分止损。也补上了"本地检查只在收盘跑、
            # 快速插针会穿过去"以及"进程挂掉就没保护"这两个洞。
            stop_loss=self._stop_price,
            reduce_only=False,
            reason=reason,
        )

    def _reduce_signal(self, qty: float, reason: str) -> Signal | None:
        if qty <= 0:
            return None
        return Signal(
            inst_id=self.symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            qty=qty,
            pos_side=PosSide.LONG if self.inst_type == InstType.SWAP else PosSide.NET,
            reduce_only=True,
            reason=reason,
        )

    def _close_signal(self, reason: str) -> Signal | None:
        return build_close_signal(
            self._state, self.symbol, self._portfolio,
            is_swap=self.inst_type == InstType.SWAP,
            reason=reason, strategy_name=self.name,
            max_qty=self._total_qty,   # 绝不碰手动仓/其他策略的仓位
            mgn_mode=self.td_mode,
        )

    # ── 成交回调：维护金字塔状态 ───────────────────────────────────────────────

    async def on_order_update(self, order: Order):
        if order.status != OrderStatus.FILLED or order.filled_qty <= 0:
            return

        if order.side == OrderSide.BUY:
            total_value = self._avg_entry * self._total_qty + \
                order.avg_fill_price * order.filled_qty
            self._total_qty += order.filled_qty
            self._avg_entry = total_value / self._total_qty if self._total_qty else 0.0
            self._last_entry_price = order.avg_fill_price
            self._step += 1
            self._bars_at_max = 0
            self._state.open(
                PosSide.LONG if self.inst_type == InstType.SWAP else PosSide.NET,
                self._avg_entry,
                self._stop_price,
            )
            logger.info(
                f"[{self.name}] 第 {self._step}/{self._max_steps} 档成交 "
                f"{order.filled_qty}@{order.avg_fill_price:.4f} → "
                f"均价 {self._avg_entry:.4f} 总量 {self._total_qty:.2f}"
            )
        else:
            # 减仓：回退一档，均价不变（平的是最深那一笔，剩余仓位成本不受影响）
            self._total_qty = max(0.0, self._total_qty - order.filled_qty)
            if self._total_qty <= 1e-9:
                self._reset_cycle()
            else:
                self._step = max(1, self._step - 1)
                self._bars_at_max = 0
                self._state.open(self._state.pos_side, self._avg_entry, self._stop_price)
            logger.info(
                f"[{self.name}] 平仓成交 {order.filled_qty}@{order.avg_fill_price:.4f} "
                f"→ 剩余 {self._total_qty:.2f} 张，回到第 {self._step} 档"
            )

    def _reset_cycle(self):
        self._step = 0
        self._avg_entry = 0.0
        self._total_qty = 0.0
        self._last_entry_price = 0.0
        self._step_qty = []
        self._stop_price = 0.0
        self._bars_at_max = 0
        self._state.close()

    def reset_position_state(self):
        self._reset_cycle()

    def _recompute_stop_loss(self, entry_price: float, pos_side) -> float | None:
        # 本策略只做多，失效价是「跌破最低支撑」——对空头没有任何意义
        if pos_side == PosSide.SHORT:
            return None
        if not (self._supports and self._atr.ready):
            return None
        return self._invalidation_price()

    def adopt_position(self, position) -> bool:
        """重启接管：无从得知重启前处于第几档，一律按已加满处理。

        宁可少赚（不再加仓、等止盈或止损）也不要在未知档位上继续加仓——
        真实档位若比猜测的低，继续加仓会超出原定的风险预算。
        """
        if position.pos_side == PosSide.SHORT:
            logger.critical(
                f"[{self.name}] 交易所上是 {position.pos_side.value} 持仓，"
                f"而本策略只做多，拒绝接管——若按多头接管，之后每一次止盈/止损都会"
                f"发出 posSide=long 的平仓单并被交易所拒绝 (sCode 51169)，仓位裸奔"
            )
            return False

        stop = self._recompute_stop_loss(position.entry_price, position.pos_side)
        if stop is None:
            logger.critical(f"[{self.name}] 支撑位/ATR 未就绪，无法重建止损，拒绝接管")
            return False

        self._total_qty = position.size
        self._avg_entry = position.entry_price
        self._last_entry_price = position.entry_price
        self._step = self._max_steps
        self._stop_price = stop
        self._step_qty = [position.size / self._max_steps] * self._max_steps
        self._bars_at_max = 0
        self._state.open(position.pos_side, position.entry_price, stop)
        logger.warning(
            f"[{self.name}] 接管已有持仓 {position.pos_side.value} size={position.size} "
            f"entry={position.entry_price:.4f} 止损重建为 {stop:.4f}；"
            f"档位未知，按已加满 ({self._max_steps}/{self._max_steps}) 处理，不再加仓"
        )
        return True

    async def _save(self, signals: list[Signal]):
        for sig in signals:
            await self._db.save_signal(sig, self.name)

    async def on_stop(self):
        logger.info(
            f"[{self.name}] Stopped. step={self._step}/{self._max_steps} "
            f"qty={self._total_qty:.2f} avg={self._avg_entry:.4f}"
        )

