"""Portfolio — 账户资产与持仓的本地视图
通过 REST 定期刷新，通过 WS 推送实时更新
"""
import asyncio
from typing import TYPE_CHECKING

from loguru import logger

from gateway.models import Balance, Order, OrderStatus, Position

if TYPE_CHECKING:
    from gateway.okx_rest import OKXRestClient


class Portfolio:
    def __init__(self):
        self._balances: dict[str, Balance] = {}    # currency -> Balance
        self._positions: dict[str, Position] = {}  # inst_id:pos_side -> Position
        self._total_equity: float = 0.0
        self._lock = asyncio.Lock()

    # ── REST 全量刷新 ──────────────────────────────────────────────────────────

    async def refresh(self, rest: "OKXRestClient"):
        async with self._lock:
            try:
                # 刷新 USDT 余额
                bal = await rest.get_balance("USDT")
                self._balances["USDT"] = bal
                self._total_equity = bal.total

                # 刷新持仓（合约）
                positions = await rest.get_positions()
                self._positions = {
                    f"{p.inst_id}:{p.pos_side.value}": p for p in positions
                }
                logger.debug(f"Portfolio refreshed: equity={self._total_equity:.2f} USDT, "
                             f"positions={len(self._positions)}")
            except Exception as e:
                logger.warning(f"Portfolio refresh failed: {e}")

    # ── WS 实时更新 ────────────────────────────────────────────────────────────

    async def on_position_update(self, positions: list[Position]):
        async with self._lock:
            for p in positions:
                key = f"{p.inst_id}:{p.pos_side.value}"
                if p.size == 0:
                    self._positions.pop(key, None)
                else:
                    self._positions[key] = p

    async def on_order_filled(self, order: Order):
        """现货订单成交后更新 USDT 余额估算。
        注意：total 是账户权益（USDT 余额 + 持仓折算），买入时 USDT 转为 base 资产，
        权益只减少手续费；卖出时同理。available 是可动用 USDT，买入要扣掉花掉的 USDT，
        卖出则增加。下次 REST 全量刷新会用真实数据覆盖。"""
        if order.status != OrderStatus.FILLED:
            return
        async with self._lock:
            bal = self._balances.get("USDT")
            if not bal:
                return
            fill_value = order.filled_qty * order.avg_fill_price
            fee = abs(order.fee)  # OKX fee 为负数表示支出
            if order.side.value == "buy":
                bal.available = max(0.0, bal.available - fill_value - fee)
                bal.total = max(0.0, bal.total - fee)
            else:
                bal.available += fill_value - fee
                bal.total = max(0.0, bal.total - fee)

    # ── 查询接口 ───────────────────────────────────────────────────────────────

    def get_available(self, currency: str = "USDT") -> float:
        bal = self._balances.get(currency)
        return bal.available if bal else 0.0

    def get_total_equity(self) -> float:
        return self._total_equity

    def get_position(self, inst_id: str, pos_side: str = "long") -> Position | None:
        return self._positions.get(f"{inst_id}:{pos_side}")

    def has_position(self, inst_id: str) -> bool:
        return any(inst_id in k for k in self._positions)

    def summary(self) -> str:
        lines = [f"Equity: {self._total_equity:.2f} USDT",
                 f"Available: {self.get_available():.2f} USDT"]
        for key, pos in self._positions.items():
            lines.append(
                f"  {pos.inst_id} {pos.pos_side.value}: "
                f"sz={pos.size} entry={pos.entry_price:.4f} "
                f"uPnL={pos.unrealized_pnl:.4f}"
            )
        return "\n".join(lines)
