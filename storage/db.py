"""SQLite 持久化层（使用 aiosqlite 异步操作）"""
from datetime import datetime, timezone

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
        await self._migrate()
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
                realized_pnl REAL,
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
        """)
        await self._db.commit()
        # 注：日内统计不再单独建表维护，改为从 orders 实时聚合（见 get_daily_stats）。
        # 增量维护的 daily_stats 表会被同一订单的多次推送重复累加，无法保证幂等。

    # ── 迁移 ──────────────────────────────────────────────────────────────────

    async def _migrate(self):
        """把旧库升级到当前结构：补 realized_pnl 列 + order_id 唯一索引。

        唯一索引是 save_order 的 upsert 前提——旧代码写的是
        `ON CONFLICT(rowid)`，而 rowid 由 AUTOINCREMENT 每次生成新值，
        永远不冲突，于是同一笔订单的每次状态推送都插了一行新记录。
        """
        cols = {r["name"] for r in await self._fetch("PRAGMA table_info(orders)")}
        if "realized_pnl" not in cols:
            await self._db.execute("ALTER TABLE orders ADD COLUMN realized_pnl REAL")
            await self._db.commit()
            logger.info("Migration: added orders.realized_pnl")

        idx = {r["name"] for r in await self._fetch("PRAGMA index_list(orders)")}
        if "idx_orders_order_id" in idx:
            return

        dups = await self._fetch("""
            SELECT order_id, COUNT(*) AS n FROM orders
            WHERE order_id != '' GROUP BY order_id HAVING n > 1
        """)
        if dups:
            total = sum(r["n"] - 1 for r in dups)
            backup = f"orders_backup_{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
            logger.warning(
                f"Migration: found {len(dups)} order(s) duplicated into {total} extra "
                f"row(s) by the old broken upsert; backing up to `{backup}` then deduping"
            )
            await self._db.execute(f"CREATE TABLE {backup} AS SELECT * FROM orders")
            # 每个 order_id 只保留 rowid 最大的一行（即最后写入的最新状态）
            await self._db.execute("""
                DELETE FROM orders WHERE order_id != '' AND rowid NOT IN (
                    SELECT MAX(rowid) FROM orders WHERE order_id != '' GROUP BY order_id
                )
            """)
            await self._db.commit()

        await self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_order_id "
            "ON orders(order_id) WHERE order_id != ''"
        )
        await self._db.commit()
        logger.info("Migration: orders(order_id) unique index created, upsert now works")

    async def _fetch(self, query: str, params: tuple = ()) -> list[dict]:
        async with self._db.execute(query, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    # ── 订单 ──────────────────────────────────────────────────────────────────

    async def save_order(self, order: Order, strategy: str):
        """按 order_id 幂等落库。同一笔订单会被写多次（下单成功时一次、
        每次 WS 状态推送各一次），靠 order_id 唯一索引收敛成一行。"""
        if not order.order_id:
            logger.warning(f"save_order skipped: empty order_id ({order.inst_id})")
            return

        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute("""
            INSERT INTO orders
              (order_id, client_oid, inst_id, strategy, side, order_type, qty, price,
               pos_side, status, filled_qty, avg_price, fee, realized_pnl,
               created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            -- WHERE 子句必须与 idx_orders_order_id 这个部分索引的谓词一致，
            -- 否则 SQLite 认为 ON CONFLICT 没有匹配的唯一约束而直接报错
            ON CONFLICT(order_id) WHERE order_id != '' DO UPDATE SET
              status=excluded.status,
              filled_qty=excluded.filled_qty,
              avg_price=excluded.avg_price,
              fee=excluded.fee,
              -- 已实现盈亏只在下单时算得出，WS 推送带不上，不能被 NULL 覆盖
              realized_pnl=COALESCE(excluded.realized_pnl, orders.realized_pnl),
              -- 策略归属同理：WS 侧解析不出时不要把已有的归属抹掉
              strategy=CASE WHEN excluded.strategy != '' THEN excluded.strategy
                            ELSE orders.strategy END,
              updated_at=excluded.updated_at
        """, (
            order.order_id, order.client_order_id, order.inst_id, strategy,
            order.side.value, order.order_type.value, order.qty, order.price,
            order.pos_side.value, order.status.value,
            order.filled_qty, order.avg_fill_price, order.fee, order.realized_pnl,
            now, now,
        ))
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

    # ── 日内统计（从 orders 实时聚合）──────────────────────────────────────────
    #
    # 只统计平仓腿（realized_pnl 非空）：开仓不产生已实现盈亏，把开仓也算成
    # 一"笔"会让交易次数翻倍。gross_pnl 是真正的已实现盈亏，不是成交额——
    # 旧实现用 `成交价 × 成交量 × ±1` 当盈亏，那是带符号的成交额。

    _DAILY_AGG = """
        SELECT strategy,
               date(created_at)      AS date,
               COUNT(*)              AS trades,
               SUM(realized_pnl)     AS gross_pnl,
               SUM(ABS(fee))         AS fees,
               SUM(realized_pnl) - SUM(ABS(fee)) AS net_pnl
        FROM orders
        WHERE status = ? AND realized_pnl IS NOT NULL
    """

    async def get_daily_stats(self, days: int = 7) -> list[dict]:
        return await self._fetch(
            self._DAILY_AGG + """
              AND date(created_at) >= date('now', ?)
            GROUP BY strategy, date(created_at)
            ORDER BY date DESC
            """,
            (OrderStatus.FILLED.value, f"-{days} days"),
        )

    async def get_daily_pnl(self, strategy: str, target_date: str) -> float:
        rows = await self._fetch(
            self._DAILY_AGG + """
              AND strategy = ? AND date(created_at) = ?
            GROUP BY strategy, date(created_at)
            """,
            (OrderStatus.FILLED.value, strategy, target_date),
        )
        return float(rows[0]["net_pnl"]) if rows else 0.0
