"""策略通用持仓状态机 + 平仓信号构造工具"""
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from loguru import logger

from gateway.models import OrderSide, OrderType, PosSide, Signal

if TYPE_CHECKING:
    from engine.portfolio import Portfolio


@dataclass
class PositionState:
    """策略内部持仓状态机。
    flat = True 表示无仓；否则 pos_side 指明多/空方向，entry_price/stop_loss 为本地记录。
    """
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


def build_close_signal(
    state: PositionState,
    symbol: str,
    portfolio: "Portfolio",
    can_short: bool,
    reason: str,
    strategy_name: str = "",
) -> Optional[Signal]:
    """根据当前持仓方向构造市价平仓信号。若 portfolio 无该品种持仓则返回 None。
    注：现货无法做空，平多时 pos_side = NET。
    """
    if state.flat:
        return None

    if state.pos_side == PosSide.LONG:
        side = OrderSide.SELL
        pos_side = PosSide.LONG if can_short else PosSide.NET
    else:
        side = OrderSide.BUY
        pos_side = PosSide.SHORT

    pos = portfolio.get_position(symbol, state.pos_side.value)
    qty = pos.size if pos else 0.0
    if qty <= 0:
        logger.warning(f"[{strategy_name}] Close signal but no position found, skip")
        return None

    return Signal(
        inst_id=symbol,
        side=side,
        order_type=OrderType.MARKET,
        qty=qty,
        pos_side=pos_side,
        reason=reason,
    )
