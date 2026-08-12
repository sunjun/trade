"""风险控制模块
检查链（顺序执行，任一失败即拒绝信号）：
  1. 紧急停止（全局回撤触发后）
  2. 策略暂停（日内亏损触发后）
  3. 下单频率限制

备注：单笔最大金额、单品种最大持仓比例由 BaseStrategy._calc_qty 通过
position_size_pct 隐式控制。

日内亏损与回撤由两个入口驱动：
  - on_realized_pnl : 策略平仓/减仓下单成功后调用（BaseStrategy._execute_signal）
  - on_equity_update: 账户权益刷新后调用（StrategyEngine 启动时 + 每 60 秒刷新循环）
两者缺一，对应的熔断就不会生效。
"""
import time
from collections import defaultdict, deque
from typing import TYPE_CHECKING

from loguru import logger

from gateway.models import Signal

if TYPE_CHECKING:
    from engine.portfolio import Portfolio


class RiskManager:
    def __init__(
        self,
        max_position_pct: float = 0.1,
        max_daily_loss_pct: float = 0.02,
        max_drawdown_pct: float = 0.05,
        order_rate_limit: int = 10,
    ):
        self._max_position_pct = max_position_pct
        self._max_daily_loss_pct = max_daily_loss_pct
        self._max_drawdown_pct = max_drawdown_pct
        self._rate_limit = order_rate_limit

        # 频率窗口：记录最近1秒内的下单时间戳
        self._order_timestamps: deque[float] = deque()

        # 策略维度的日内亏损跟踪
        self._daily_loss: dict[str, float] = defaultdict(float)  # strategy -> usdt loss
        self._paused_strategies: set[str] = set()

        # 账户维度的高水位（用于回撤计算）
        self._equity_high: float = 0.0
        # 当日起始权益（日内亏损比例的分母，每日重置后由下一次权益刷新重新播种）
        self._day_start_equity: float = 0.0
        self._emergency_stop = False

    # ── 主检查入口 ─────────────────────────────────────────────────────────────

    def check_signal(
        self, signal: Signal, portfolio: "Portfolio", strategy_name: str
    ) -> tuple[bool, str]:
        """返回 (allowed, reason)"""
        if self._emergency_stop:
            return False, "Emergency stop: max drawdown exceeded"

        if strategy_name in self._paused_strategies:
            return False, f"Strategy {strategy_name} paused: daily loss limit hit"

        if not self._check_rate():
            return False, f"Order rate limit exceeded (>{self._rate_limit}/s)"

        return True, "ok"

    def on_order_sent(self, strategy_name: str):
        """下单成功后调用，更新频率计数"""
        self._order_timestamps.append(time.monotonic())

    def cap_notional(
        self,
        existing_notional: float,
        requested_notional: float,
        equity: float,
        strategy_name: str = "",
    ) -> float:
        """按 max_position_pct 限制单品种的名义价值上限，返回允许的新增额度。

        这是「最大使用资金」的全局闸门：不管策略自己算出多大的仓位，
        单个品种的名义价值（含已有持仓）都不会超过 权益 × max_position_pct。
        返回值可能为 0，表示这一单该被跳过。
        """
        if equity <= 0 or self._max_position_pct <= 0:
            return requested_notional

        cap = equity * self._max_position_pct
        room = cap - existing_notional
        if room <= 0:
            logger.warning(
                f"[RiskManager] {strategy_name} 已达单品种名义上限 "
                f"{cap:.2f} USDT（当前 {existing_notional:.2f}），跳过本单"
            )
            return 0.0
        if requested_notional <= room:
            return requested_notional

        logger.warning(
            f"[RiskManager] {strategy_name} 请求名义 {requested_notional:.2f} USDT "
            f"超出剩余额度 {room:.2f}（上限 {cap:.2f} = 权益 {equity:.2f} × "
            f"{self._max_position_pct:.0%}），按额度截断"
        )
        return room

    @property
    def max_position_pct(self) -> float:
        return self._max_position_pct

    def on_realized_pnl(self, strategy_name: str, pnl: float):
        """策略平仓/减仓下单成功后调用，累计日内亏损并在超限时暂停该策略。

        pnl 为本次平仓腿的已实现盈亏（USDT，亏损为负）。
        """
        if pnl < 0:
            self._daily_loss[strategy_name] += abs(pnl)
            logger.info(
                f"[RiskManager] {strategy_name} realized {pnl:+.2f} USDT, "
                f"daily loss now {self._daily_loss[strategy_name]:.2f} USDT"
            )
        self._check_daily_loss(strategy_name)

    def on_equity_update(self, total_equity: float):
        """账户权益刷新后调用，维护高水位并在回撤超限时紧急停止。"""
        if total_equity <= 0:
            return

        if self._day_start_equity <= 0:
            self._day_start_equity = total_equity
            logger.info(f"[RiskManager] Day start equity = {total_equity:.2f} USDT")

        if total_equity > self._equity_high:
            self._equity_high = total_equity

        drawdown = (self._equity_high - total_equity) / self._equity_high
        if drawdown >= self._max_drawdown_pct and not self._emergency_stop:
            logger.critical(
                f"[RiskManager] EMERGENCY STOP: drawdown {drawdown:.1%} "
                f">= limit {self._max_drawdown_pct:.1%} "
                f"(high={self._equity_high:.2f}, now={total_equity:.2f})"
            )
            self._emergency_stop = True

        # 权益基准变了，重新评估各策略的日亏损占比
        for name in list(self._daily_loss):
            self._check_daily_loss(name)

    def reset_daily(self):
        """每天凌晨由引擎调用，重置日内统计。
        账户高水位不重置——它是跨日的回撤基准。"""
        self._daily_loss.clear()
        self._paused_strategies.clear()
        self._day_start_equity = 0.0  # 由下一次权益刷新重新播种
        logger.info("[RiskManager] Daily stats reset")

    def resume_strategy(self, strategy_name: str):
        """手动恢复被暂停的策略"""
        self._paused_strategies.discard(strategy_name)
        logger.info(f"[RiskManager] Strategy {strategy_name} resumed")

    def clear_emergency(self):
        """手动清除紧急停止（需人工确认）"""
        self._emergency_stop = False
        logger.warning("[RiskManager] Emergency stop cleared manually")

    @property
    def is_emergency(self) -> bool:
        return self._emergency_stop

    @property
    def paused_strategies(self) -> set[str]:
        return set(self._paused_strategies)

    # ── 内部工具 ──────────────────────────────────────────────────────────────

    def _check_daily_loss(self, strategy_name: str):
        """日内亏损达到上限则暂停该策略，直到 reset_daily 或人工 resume。"""
        if strategy_name in self._paused_strategies:
            return
        base = self._day_start_equity or self._equity_high
        if base <= 0:
            return  # 权益尚未播种，无法判断比例
        loss_pct = self._daily_loss[strategy_name] / base
        if loss_pct >= self._max_daily_loss_pct:
            logger.warning(
                f"[RiskManager] Strategy {strategy_name} PAUSED: "
                f"daily loss {loss_pct:.1%} >= limit {self._max_daily_loss_pct:.1%} "
                f"({self._daily_loss[strategy_name]:.2f} / {base:.2f} USDT)"
            )
            self._paused_strategies.add(strategy_name)

    def _check_rate(self) -> bool:
        now = time.monotonic()
        # 清除1秒前的记录
        while self._order_timestamps and now - self._order_timestamps[0] > 1.0:
            self._order_timestamps.popleft()
        return len(self._order_timestamps) < self._rate_limit
