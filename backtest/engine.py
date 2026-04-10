"""回测引擎

设计：
  - 策略代码零修改，通过 Mock 对象替代真实 REST / Portfolio / DB / RiskManager
  - 多时框对齐：以 15m 为基准推进，每根 15m 蜡烛前先喂已闭合的 1H / 4H
  - 成交模型：收盘价立即成交（市价单）
  - 止损检查：每根 K 线末尾，若 high/low 触及止损则按止损价平仓
  - 手续费：合约 taker 0.05%
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from engine.risk_manager import RiskManager
from gateway.models import (
    Balance, Candle, InstrumentInfo, InstType, Order, OrderSide,
    OrderStatus, OrderType, PosSide, Position, Signal, Ticker,
)

FEE_RATE = 0.0005  # taker 手续费率 0.05%


# ── Mock Portfolio ─────────────────────────────────────────────────────────────

class BacktestPortfolio:
    """模拟账户，精确跟踪保证金与资金变化。"""

    def __init__(self, initial_capital: float, ct_val: float, leverage: int):
        self.initial_capital = initial_capital
        self.ct_val = ct_val          # 合约面值（ETH-USDT-SWAP = 0.01 ETH）
        self.leverage = leverage

        self._cash = initial_capital  # 可用保证金（USDT）
        self._position: dict | None = None  # 当前持仓，None=无仓
        self.equity_curve: list[float] = [initial_capital]

    # ── Portfolio 接口（策略调用）─────────────────────────────────────────────

    def get_available(self, currency: str = "USDT") -> float:
        return self._cash if currency == "USDT" else 0.0

    def get_position(self, inst_id: str, pos_side: str) -> Position | None:
        if self._position and self._position["pos_side"] == pos_side:
            p = self._position
            return Position(
                inst_id=inst_id,
                pos_side=PosSide(pos_side),
                size=p["contracts"],
                entry_price=p["entry_price"],
            )
        return None

    def has_position(self, inst_id: str) -> bool:
        return self._position is not None

    async def refresh(self, rest) -> None:
        pass  # no-op

    async def on_order_filled(self, order: Order) -> None:
        pass  # 由 BacktestRest.place_order 处理

    async def on_position_update(self, positions: list) -> None:
        pass

    async def reconcile_position(self, position) -> None:
        pass

    # ── 内部交易操作 ──────────────────────────────────────────────────────────

    def open_position(self, pos_side: str, contracts: float, price: float) -> float:
        """开仓，返回手续费（USDT）。"""
        margin = contracts * self.ct_val * price / self.leverage
        fee = contracts * self.ct_val * price * FEE_RATE
        self._cash -= margin + fee
        self._position = {
            "pos_side": pos_side,
            "contracts": contracts,
            "entry_price": price,
            "margin": margin,
        }
        return fee

    def close_position(self, price: float) -> tuple[float, float]:
        """平仓，返回 (pnl_net_usdt, fee_usdt)。"""
        if not self._position:
            return 0.0, 0.0
        p = self._position
        contracts = p["contracts"]
        entry = p["entry_price"]
        margin = p["margin"]

        if p["pos_side"] == "long":
            gross_pnl = (price - entry) * contracts * self.ct_val
        else:
            gross_pnl = (entry - price) * contracts * self.ct_val

        fee = contracts * self.ct_val * price * FEE_RATE
        net_pnl = gross_pnl - fee
        self._cash += margin + net_pnl
        self._position = None
        return net_pnl, fee

    def current_equity(self, mark_price: float) -> float:
        """返回当前权益（含未实现盈亏）。"""
        equity = self._cash
        if self._position:
            p = self._position
            if p["pos_side"] == "long":
                unrealized = (mark_price - p["entry_price"]) * p["contracts"] * self.ct_val
            else:
                unrealized = (p["entry_price"] - mark_price) * p["contracts"] * self.ct_val
            equity += p["margin"] + unrealized
        return equity


# ── Mock REST ─────────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    ts: datetime
    action: str               # "open_long" / "open_short" / "close_long" / "close_short" / "sl_long" / "sl_short"
    price: float
    contracts: float
    pnl: float = 0.0          # 本笔净盈亏（仅关仓有效）
    reason: str = ""


class BacktestRest:
    """模拟 REST 客户端，截获下单请求，记录交易。"""

    def __init__(self, portfolio: BacktestPortfolio, inst_info: InstrumentInfo):
        self._portfolio = portfolio
        self._inst_info = inst_info
        self._current_candle: Candle | None = None
        self.trades: list[TradeRecord] = []
        self._order_seq = 0

    def set_current_candle(self, candle: Candle) -> None:
        self._current_candle = candle

    def _price(self) -> float:
        return self._current_candle.close if self._current_candle else 0.0

    # ── 策略调用的接口 ────────────────────────────────────────────────────────

    async def get_ticker(self, inst_id: str) -> Ticker:
        p = self._price()
        return Ticker(inst_id=inst_id, last=p, bid=p, ask=p)

    async def get_instrument(self, inst_id: str, inst_type: InstType) -> InstrumentInfo:
        return self._inst_info

    async def place_order(self, order: Order, inst_type: InstType) -> Order:
        price = self._price()
        contracts = order.qty
        ts = self._current_candle.ts if self._current_candle else datetime.now(timezone.utc)

        is_open = order.stop_loss is not None  # 开仓信号带 stop_loss

        if is_open:
            pos_side = order.pos_side.value  # "long" / "short"
            self._portfolio.open_position(pos_side, contracts, price)
            action = f"open_{pos_side}"
            self.trades.append(TradeRecord(
                ts=ts, action=action, price=price, contracts=contracts,
            ))
        else:
            # 平仓：从持仓方向判断
            pos = self._portfolio._position
            pos_side = pos["pos_side"] if pos else "long"
            net_pnl, _ = self._portfolio.close_position(price)
            action = f"close_{pos_side}"
            self.trades.append(TradeRecord(
                ts=ts, action=action, price=price, contracts=contracts,
                pnl=net_pnl, reason=getattr(order, "_reason", ""),
            ))

        self._order_seq += 1
        order.order_id = f"bt_{self._order_seq}"
        order.status = OrderStatus.FILLED
        order.filled_qty = contracts
        order.avg_fill_price = price
        return order

    async def get_balance(self, currency: str = "USDT") -> Balance:
        avail = self._portfolio.get_available(currency)
        return Balance(currency=currency, total=avail, available=avail)

    async def get_positions(self, inst_id: str | None = None) -> list[Position]:
        return []

    async def set_leverage(self, *args, **kwargs) -> None:
        pass

    async def cancel_order(self, *args, **kwargs) -> bool:
        return True

    # context manager 支持（引擎调用 __aenter__/__aexit__）
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


# ── Mock DB ───────────────────────────────────────────────────────────────────

class BacktestDB:
    async def init(self) -> None: pass
    async def save_candle(self, *a, **kw) -> None: pass
    async def save_signal(self, *a, **kw) -> None: pass
    async def save_order(self, *a, **kw) -> None: pass


# ── 多时框回测引擎 ─────────────────────────────────────────────────────────────

class BacktestEngine:
    def __init__(
        self,
        strategy_cls,
        strategy_name: str,
        strategy_config: dict,
        inst_id: str,
        inst_info: InstrumentInfo,
        initial_capital: float = 10_000.0,
        inst_type: InstType = InstType.SWAP,
    ):
        leverage = strategy_config.get("leverage", 1)
        self._portfolio = BacktestPortfolio(initial_capital, inst_info.ct_val, leverage)
        self._rest = BacktestRest(self._portfolio, inst_info)
        self._db = BacktestDB()
        self._risk = RiskManager(
            max_position_pct=1.0,   # 回测不做风控拦截
            max_daily_loss_pct=1.0,
            max_drawdown_pct=1.0,
            order_rate_limit=9999,
        )

        self._strategy = strategy_cls(
            name=strategy_name,
            inst_type=inst_type,
            symbol=inst_id,
            config=strategy_config,
            rest=self._rest,
            risk=self._risk,
            portfolio=self._portfolio,
            db=self._db,
        )
        self._strategy._warm_up_done = False

        self._inst_id = inst_id
        self._inst_info = inst_info
        self._equity_ts: list[datetime] = []
        self._sl_trades: list[TradeRecord] = []  # 止损触发记录

    async def run(
        self,
        candles_m15: list[Candle],
        candles_h1: list[Candle],
        candles_h4: list[Candle],
        warm_up_m15: int = 0,
        warm_up_h1: int = 0,
        warm_up_h4: int = 0,
    ) -> None:
        """执行回测主循环。

        warm_up_* 参数指定各时框用于预热的根数（不计入回测期）。
        若为 0 则自动使用策略的 warm_up_period（15m）及 extra_tf_configs（1H/4H）计算默认值。
        """
        strategy = self._strategy

        # ── 计算预热根数 ──────────────────────────────────────────────────────
        if warm_up_m15 == 0:
            warm_up_m15 = strategy.warm_up_period + 10

        if warm_up_h4 == 0 or warm_up_h1 == 0:
            if hasattr(strategy, "extra_tf_configs"):
                for tf, tf_warm, _ in strategy.extra_tf_configs:
                    if "4H" in tf and warm_up_h4 == 0:
                        warm_up_h4 = tf_warm + 5
                    elif "1H" in tf and warm_up_h1 == 0:
                        warm_up_h1 = tf_warm + 5

        logger.info(
            f"Warm-up: 4H={warm_up_h4}, 1H={warm_up_h1}, 15m={warm_up_m15}"
        )

        # ── 预热 4H ───────────────────────────────────────────────────────────
        h4_warm = candles_h4[:warm_up_h4]
        logger.info(f"Warming up {len(h4_warm)} x 4H candles...")
        for c in h4_warm:
            c.confirmed = True
            if hasattr(strategy, "_handle_h4"):
                await strategy._handle_h4([c])
        if hasattr(strategy, "on_extra_tf_warmed"):
            strategy.on_extra_tf_warmed(strategy._tf_h4)

        # ── 预热 1H ───────────────────────────────────────────────────────────
        h1_warm = candles_h1[:warm_up_h1]
        logger.info(f"Warming up {len(h1_warm)} x 1H candles...")
        for c in h1_warm:
            c.confirmed = True
            if hasattr(strategy, "_handle_h1"):
                await strategy._handle_h1([c])
        if hasattr(strategy, "on_extra_tf_warmed"):
            strategy.on_extra_tf_warmed(strategy._tf_h1)

        # ── 预热 15m ──────────────────────────────────────────────────────────
        m15_warm = candles_m15[:warm_up_m15]
        logger.info(f"Warming up {len(m15_warm)} x 15m candles...")
        for c in m15_warm:
            c.confirmed = True
            await strategy.on_candle(c)
        strategy._warm_up_done = True
        strategy.reset_position_state()

        # ── 正式回测 ──────────────────────────────────────────────────────────
        m15_bt = candles_m15[warm_up_m15:]
        h1_bt  = candles_h1[warm_up_h1:]
        h4_bt  = candles_h4[warm_up_h4:]

        # 构建 1H / 4H 的 ts 索引（用于判断某 15m 时刻之前有没有新闭合的高时框 K 线）
        h1_idx = 0
        h4_idx = 0

        logger.info(
            f"Backtesting {len(m15_bt)} x 15m candles "
            f"({m15_bt[0].ts.strftime('%Y-%m-%d')} → {m15_bt[-1].ts.strftime('%Y-%m-%d')})"
        )

        for i, candle in enumerate(m15_bt):
            # 更新当前价格（供 MockRest 使用）
            self._rest.set_current_candle(candle)

            # 先喂已闭合的 4H K 线（ts <= 当前 15m 开盘时间）
            while h4_idx < len(h4_bt) and h4_bt[h4_idx].ts <= candle.ts:
                h4_bt[h4_idx].confirmed = True
                if hasattr(strategy, "_handle_h4"):
                    await strategy._handle_h4([h4_bt[h4_idx]])
                h4_idx += 1

            # 先喂已闭合的 1H K 线
            while h1_idx < len(h1_bt) and h1_bt[h1_idx].ts <= candle.ts:
                h1_bt[h1_idx].confirmed = True
                if hasattr(strategy, "_handle_h1"):
                    await strategy._handle_h1([h1_bt[h1_idx]])
                h1_idx += 1

            # 止损检查：在喂 K 线前先判断本根是否触及止损
            await self._check_stop_loss(candle)

            # 喂 15m K 线给策略（策略内部会调用 _execute_signal → place_order）
            candle.confirmed = True
            signals = await strategy.on_candle(candle)
            for sig in signals:
                await strategy._execute_signal(sig)

            # 记录权益曲线
            equity = self._portfolio.current_equity(candle.close)
            self._portfolio.equity_curve.append(equity)
            self._equity_ts.append(candle.ts)

            if i % 500 == 0 and i > 0:
                logger.debug(
                    f"  [{candle.ts.strftime('%Y-%m-%d %H:%M')}] "
                    f"equity={equity:.2f} USDT  trades={len(self._rest.trades)}"
                )

        logger.info(
            f"Backtest done. "
            f"Trades={len(self._rest.trades)}, "
            f"Final equity={self._portfolio.equity_curve[-1]:.2f} USDT"
        )

    async def _check_stop_loss(self, candle: Candle) -> None:
        """检查本根 K 线是否触及止损，若触及则按止损价平仓。"""
        pos = self._portfolio._position
        strategy = self._strategy
        state = getattr(strategy, "_state", None)
        if not pos or not state or state.flat:
            return

        sl = state.stop_loss
        hit = False
        sl_price = sl

        if pos["pos_side"] == "long" and candle.low <= sl:
            hit = True
        elif pos["pos_side"] == "short" and candle.high >= sl:
            hit = True

        if not hit:
            return

        # 以止损价平仓
        net_pnl, _ = self._portfolio.close_position(sl_price)
        action = f"sl_{pos['pos_side']}"
        self._rest.trades.append(TradeRecord(
            ts=candle.ts,
            action=action,
            price=sl_price,
            contracts=pos["contracts"],
            pnl=net_pnl,
            reason="stop_loss",
        ))
        self._sl_trades.append(self._rest.trades[-1])
        state.close()
        strategy._candles_since = 0
        logger.debug(
            f"  SL hit @ {sl_price:.4f}  pnl={net_pnl:+.2f} USDT  "
            f"({candle.ts.strftime('%Y-%m-%d %H:%M')})"
        )

    # ── 结果访问 ──────────────────────────────────────────────────────────────

    @property
    def trades(self) -> list[TradeRecord]:
        return self._rest.trades

    @property
    def equity_curve(self) -> list[float]:
        return self._portfolio.equity_curve

    @property
    def equity_timestamps(self) -> list[datetime]:
        return self._equity_ts

    @property
    def initial_capital(self) -> float:
        return self._portfolio.initial_capital
