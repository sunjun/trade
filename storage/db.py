"""SQLite 持久化层（使用 aiosqlite 异步操作）"""
from datetime import date, datetime, timezone

import aiosqlite
from loguru import logger

from gateway.models import Candle, Order, OrderStatus, Signal


class Database:
    def __init__(self, db_path: str = "trade.db"):
        self._path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()
        logger.info(f"Database initialized: {self._path}")

    async def close(self):
        if self._db:
            await self._db.close()

    # ── 建表 ──────────────────────────────────────────────────────────────────

    async def _create_tables(self):
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id    TEXT NOT NULL,
                client_oid  TEXT,
                inst_id     TEXT NOT NULL,
                strategy    TEXT NOT NULL,
                side        TEXT NOT NULL,
                order_type  TEXT NOT NULL,
                qty         REAL NOT NULL,
                price       REAL,
                pos_side    TEXT,
                status      TEXT NOT NULL,
                filled_qty  REAL DEFAULT 0,
                avg_price   REAL DEFAULT 0,
                fee         REAL DEFAULT 0,
                reason      TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_orders_strategy ON orders(strategy);
            CREATE INDEX IF NOT EXISTS idx_orders_inst ON orders(inst_id);
            CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(created_at);

            CREATE TABLE IF NOT EXISTS candles (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                inst_id   TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                ts        TEXT NOT NULL,
                open      REAL NOT NULL,
                high      REAL NOT NULL,
                low       REAL NOT NULL,
                close     REAL NOT NULL,
                volume    REAL NOT NULL,
                UNIQUE(inst_id, timeframe, ts)
            );

            CREATE INDEX IF NOT EXISTS idx_candles_inst_tf ON candles(inst_id, timeframe);
            CREATE INDEX IF NOT EXISTS idx_candles_ts ON candles(ts);

            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy    TEXT NOT NULL,
                inst_id     TEXT NOT NULL,
                side        TEXT NOT NULL,
                order_type  TEXT NOT NULL,
                qty         REAL NOT NULL,
                price       REAL,
                pos_side    TEXT,
                stop_loss   REAL,
                reason      TEXT,
                created_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy);

            CREATE TABLE IF NOT EXISTS daily_stats (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy    TEXT NOT NULL,
                date        TEXT NOT NULL,
                trades      INTEGER DEFAULT 0,
                gross_pnl   REAL DEFAULT 0,
                fees        REAL DEFAULT 0,
                UNIQUE(strategy, date)
            );
        """)
        await self._db.commit()

    # ── 订单 ──────────────────────────────────────────────────────────────────

    async def save_order(self, order: Order, strategy: str):
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute("""
            INSERT INTO orders
              (order_id, client_oid, inst_id, strategy, side, order_type, qty, price,
               pos_side, status, filled_qty, avg_price, fee, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(rowid) DO UPDATE SET
              status=excluded.status,
              filled_qty=excluded.filled_qty,
              avg_price=excluded.avg_price,
              fee=excluded.fee,
              updated_at=excluded.updated_at
        """, (
            order.order_id, order.client_order_id, order.inst_id, strategy,
            order.side.value, order.order_type.value, order.qty, order.price,
            order.pos_side.value, order.status.value,
            order.filled_qty, order.avg_fill_price, order.fee,
            now, now,
        ))
        await self._db.commit()

        if order.status == OrderStatus.FILLED and order.filled_qty > 0:
            await self._update_daily_stats(strategy, order)

    async def _update_daily_stats(self, strategy: str, order: Order):
        today = date.today().isoformat()
        pnl = order.avg_fill_price * order.filled_qty * (
            1 if order.side.value == "sell" else -1
        )
        await self._db.execute("""
            INSERT INTO daily_stats (strategy, date, trades, gross_pnl, fees)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(strategy, date) DO UPDATE SET
              trades = trades + 1,
              gross_pnl = gross_pnl + excluded.gross_pnl,
              fees = fees + excluded.fees
        """, (strategy, today, pnl, abs(order.fee)))
        await self._db.commit()

    # ── K线 ───────────────────────────────────────────────────────────────────

    async def save_candle(self, candle: Candle, inst_id: str, timeframe: str):
        """仅保存已收盘的K线（confirmed=True），避免重复写入实时推送"""
        if not candle.confirmed:
            return
        await self._db.execute("""
            INSERT OR IGNORE INTO candles (inst_id, timeframe, ts, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            inst_id, timeframe,
            candle.ts.isoformat(),
            candle.open, candle.high, candle.low, candle.close, candle.volume,
        ))
        await self._db.commit()

    async def get_candles(
        self, inst_id: str, timeframe: str, limit: int = 200
    ) -> list[dict]:
        async with self._db.execute("""
            SELECT * FROM candles
            WHERE inst_id=? AND timeframe=?
            ORDER BY ts DESC LIMIT ?
        """, (inst_id, timeframe, limit)) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in reversed(rows)]  # 返回升序

    # ── 信号 ──────────────────────────────────────────────────────────────────

    async def save_signal(self, signal: Signal, strategy: str):
        await self._db.execute("""
            INSERT INTO signals
              (strategy, inst_id, side, order_type, qty, price, pos_side, stop_loss, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            strategy, signal.inst_id, signal.side.value,
            signal.order_type.value, signal.qty,
            signal.price, signal.pos_side.value,
            signal.stop_loss, signal.reason,
            datetime.now(timezone.utc).isoformat(),
        ))
        await self._db.commit()

    async def get_signals(self, strategy: str | None = None, limit: int = 50) -> list[dict]:
        query = "SELECT * FROM signals"
        params: list = []
        if strategy:
            query += " WHERE strategy=?"
            params.append(strategy)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── 订单 ──────────────────────────────────────────────────────────────────

    async def get_orders(
        self, strategy: str | None = None, limit: int = 50
    ) -> list[dict]:
        query = "SELECT * FROM orders"
        params: list = []
        if strategy:
            query += " WHERE strategy = ?"
            params.append(strategy)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_daily_stats(self, days: int = 7) -> list[dict]:
        async with self._db.execute("""
            SELECT strategy, date, trades, gross_pnl, fees,
                   (gross_pnl - fees) AS net_pnl
            FROM daily_stats
            ORDER BY date DESC
            LIMIT ?
        """, (days * 10,)) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_daily_pnl(self, strategy: str, target_date: str) -> float:
        async with self._db.execute(
            "SELECT net_pnl FROM daily_stats WHERE strategy=? AND date=?",
            (strategy, target_date)
        ) as cursor:
            row = await cursor.fetchone()
        return float(row["net_pnl"]) if row else 0.0
