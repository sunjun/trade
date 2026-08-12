"""策略通用持仓状态机 + 平仓信号构造工具"""
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
    is_swap: bool,
    reason: str,
    strategy_name: str = "",
    max_qty: float | None = None,
    mgn_mode: str | None = None,
) -> Signal | None:
    """根据当前持仓方向构造市价平仓信号。若 portfolio 无该品种持仓则返回 None。

    `is_swap` 问的是**品种**，不是「这个策略会不会做空」——OKX 双向持仓模式下
    平多必须发 posSide=long，发 net 会被拒（sCode 51169）。只做多的合约策略
    照样要传 True；只有现货才是 NET。
    """
    if state.flat:
        return None

    if state.pos_side == PosSide.LONG:
        side = OrderSide.SELL
        pos_side = PosSide.LONG if is_swap else PosSide.NET
    else:
        side = OrderSide.BUY
        pos_side = PosSide.SHORT

    pos = portfolio.get_position(symbol, state.pos_side.value, mgn_mode)
    qty = pos.size if pos else 0.0
    if qty <= 0:
        logger.warning(f"[{strategy_name}] Close signal but no position found, skip")
        return None

    # 交易所把同方向同保证金模式的仓位合并成一笔，里面可能掺着手动开的仓
    # （on_existing_position=ignore）。策略只该平掉自己开出来的那部分。
    if max_qty is not None and qty > max_qty:
        logger.warning(
            f"[{strategy_name}] 交易所持仓 {qty} 张 > 本策略持有 {max_qty} 张，"
            f"只平自己的部分（其余为手动仓或其他策略的仓位）"
        )
        qty = max_qty

    return Signal(
        inst_id=symbol,
        side=side,
        order_type=OrderType.MARKET,
        qty=qty,
        pos_side=pos_side,
        reduce_only=True,
        reason=reason,
    )
