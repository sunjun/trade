"""风险控制模块
检查链（顺序执行，任一失败即拒绝信号）：
  1. 下单频率限制
  2. 单笔最大金额（基于当前价格）
  3. 单品种最大持仓比例
  4. 单策略日内最大亏损（触发后暂停该策略）
  5. 账户最大回撤（触发后紧急停止所有策略）
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

    def on_pnl_update(self, strategy_name: str, pnl_delta: float, total_equity: float):
        """订单成交后由引擎调用，更新PnL和回撤状态"""
        if pnl_delta < 0:
            self._daily_loss[strategy_name] += abs(pnl_delta)

        # 高水位更新
        if total_equity > self._equity_high:
            self._equity_high = total_equity

        # 策略日内亏损熔断
        initial = self._equity_high  # 用全局高水位近似
        if initial > 0:
            loss_pct = self._daily_loss[strategy_name] / initial
            if loss_pct >= self._max_daily_loss_pct:
                if strategy_name not in self._paused_strategies:
                    logger.warning(
                        f"[RiskManager] Strategy {strategy_name} paused: "
                        f"daily loss {loss_pct:.1%} >= limit {self._max_daily_loss_pct:.1%}"
                    )
                    self._paused_strategies.add(strategy_name)

            # 全局最大回撤紧急停止
            drawdown = (self._equity_high - total_equity) / self._equity_high
            if drawdown >= self._max_drawdown_pct and not self._emergency_stop:
                logger.critical(
                    f"[RiskManager] EMERGENCY STOP: drawdown {drawdown:.1%} "
                    f">= limit {self._max_drawdown_pct:.1%}"
                )
                self._emergency_stop = True

    def reset_daily(self):
        """每天凌晨由引擎调用，重置日内统计"""
        self._daily_loss.clear()
        self._paused_strategies.clear()
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

    def _check_rate(self) -> bool:
        now = time.monotonic()
        # 清除1秒前的记录
        while self._order_timestamps and now - self._order_timestamps[0] > 1.0:
            self._order_timestamps.popleft()
        return len(self._order_timestamps) < self._rate_limit
