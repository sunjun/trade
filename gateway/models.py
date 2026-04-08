from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


class InstType(str, Enum):
    SPOT = "SPOT"
    SWAP = "SWAP"   # 永续合约
    FUTURES = "FUTURES"  # 交割合约


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    POST_ONLY = "post_only"


class OrderStatus(str, Enum):
    PENDING = "pending"          # 本地待发
    LIVE = "live"                # 已挂单
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PosSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    NET = "net"   # 单向持仓（现货）


@dataclass
class Candle:
    ts: datetime           # 开盘时间
    open: float
    high: float
    low: float
    close: float
    volume: float          # 基础货币成交量
    confirmed: bool = False  # True = 已收盘K线


@dataclass
class Ticker:
    inst_id: str
    last: float
    bid: float
    ask: float
    ts: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Order:
    inst_id: str
    side: OrderSide
    order_type: OrderType
    qty: float                   # 现货:基础货币量; 合约:张数
    price: float | None = None   # limit单价格，market单为None
    client_order_id: str = ""
    order_id: str = ""
    pos_side: PosSide = PosSide.NET
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    fee: float = 0.0
    ts: datetime = field(default_factory=datetime.utcnow)
    strategy_name: str = ""


@dataclass
class Position:
    inst_id: str
    pos_side: PosSide
    size: float          # 持仓量（基础货币或张数）
    entry_price: float
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0
    leverage: int = 1


@dataclass
class Balance:
    currency: str
    total: float
    available: float
    frozen: float = 0.0


@dataclass
class Signal:
    """策略生成的交易信号，经过风控后转为订单"""
    inst_id: str
    side: OrderSide
    order_type: OrderType
    qty: float
    price: float | None = None
    pos_side: PosSide = PosSide.NET   # 合约必填
    stop_loss: float | None = None
    reason: str = ""                  # 信号说明，用于日志


@dataclass
class InstrumentInfo:
    inst_id: str
    inst_type: InstType
    base_ccy: str       # e.g. BTC
    quote_ccy: str      # e.g. USDT
    lot_sz: float       # 最小下单量（step）
    min_sz: float       # 最小下单量
    ct_val: float = 1.0  # 合约面值（合约专用）
    tick_sz: float = 0.01
