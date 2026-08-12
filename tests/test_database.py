"""订单落库幂等 + 日内统计口径

旧实现写的是 ON CONFLICT(rowid)，而 rowid 由 AUTOINCREMENT 每次生成新值，
永不冲突——同一订单的每次状态推送都插了一行新记录。
"""
import sqlite3
from datetime import UTC, datetime

import pytest

from gateway.models import Order, OrderSide, OrderStatus, OrderType, PosSide
from storage.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    await d.init()
    yield d
    await d.close()


def order(oid, strategy="s1", status=OrderStatus.FILLED, pnl=None, fee=-1.5,
          side=OrderSide.SELL, filled=100.0, price=3000.0):
    return Order(inst_id="ETH-USDT-SWAP", side=side, order_type=OrderType.MARKET,
                 qty=filled, order_id=oid, client_order_id=f"tag{oid}",
                 pos_side=PosSide.LONG, status=status, filled_qty=filled,
                 avg_fill_price=price, fee=fee, realized_pnl=pnl)


async def test_upsert_converges_to_one_row(db):
    o = order("A1", status=OrderStatus.LIVE, filled=0.0)
    await db.save_order(o, "s1")
    o.status, o.filled_qty = OrderStatus.PARTIALLY_FILLED, 50.0
    await db.save_order(o, "s1")
    o.status, o.filled_qty = OrderStatus.FILLED, 100.0
    await db.save_order(o, "s1")

    rows = await db._fetch("SELECT * FROM orders WHERE order_id='A1'")
    assert len(rows) == 1
    assert rows[0]["status"] == "filled"
    assert rows[0]["filled_qty"] == 100.0


async def test_ws_echo_does_not_clobber(db):
    """WS 回推不带 realized_pnl / 策略归属，不能把已写入的值抹成空"""
    await db.save_order(order("B1", pnl=-42.0), "s1")
    await db.save_order(order("B1", pnl=None), "")

    row = (await db._fetch("SELECT * FROM orders WHERE order_id='B1'"))[0]
    assert row["realized_pnl"] == -42.0
    assert row["strategy"] == "s1"


async def test_empty_order_id_skipped(db):
    await db.save_order(order(""), "s1")
    assert not await db._fetch("SELECT * FROM orders")


# ── 迁移 ──────────────────────────────────────────────────────────────────────

LEGACY_SCHEMA = """
    CREATE TABLE orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL, client_oid TEXT, inst_id TEXT NOT NULL,
        strategy TEXT NOT NULL, side TEXT NOT NULL, order_type TEXT NOT NULL,
        qty REAL NOT NULL, price REAL, pos_side TEXT, status TEXT NOT NULL,
        filled_qty REAL DEFAULT 0, avg_price REAL DEFAULT 0, fee REAL DEFAULT 0,
        reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
"""


def _legacy_db(path, duplicates=3, orders=5):
    con = sqlite3.connect(path)
    con.executescript(LEGACY_SCHEMA)
    now = datetime.now(UTC).isoformat()
    for i in range(orders):
        for _ in range(duplicates):      # 旧 bug：同一订单被插多行
            con.execute(
                "INSERT INTO orders (order_id, inst_id, strategy, side, order_type,"
                " qty, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"O{i}", "ETH-USDT-SWAP", "s1", "buy", "market", 1.0,
                 "filled", now, now))
    con.commit()
    con.close()


async def test_migration_dedupes_and_backs_up(tmp_path):
    path = tmp_path / "legacy.db"
    _legacy_db(path, duplicates=3, orders=5)

    d = Database(str(path))
    await d.init()
    await d.close()

    con = sqlite3.connect(path)
    rows = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    ids = con.execute("SELECT COUNT(DISTINCT order_id) FROM orders").fetchone()[0]
    backups = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'orders_backup_%'")]
    backup_rows = con.execute(f"SELECT COUNT(*) FROM {backups[0]}").fetchone()[0]
    cols = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
    con.close()

    assert rows == ids == 5, "每个 order_id 应只剩一行"
    assert len(backups) == 1 and backup_rows == 15, "去重前必须完整备份"
    assert "realized_pnl" in cols


async def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    _legacy_db(path)
    for _ in range(3):
        d = Database(str(path))
        await d.init()
        await d.close()

    con = sqlite3.connect(path)
    backups = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'orders_backup_%'")]
    con.close()
    assert len(backups) == 1, "重复 init 不该反复备份"


async def test_migration_without_duplicates_skips_backup(tmp_path):
    path = tmp_path / "clean.db"
    _legacy_db(path, duplicates=1, orders=3)
    d = Database(str(path))
    await d.init()
    await d.close()

    con = sqlite3.connect(path)
    backups = list(con.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'orders_backup_%'"))
    con.close()
    assert not backups, "没有重复就不必备份"


# ── 日内统计 ──────────────────────────────────────────────────────────────────

async def test_daily_stats_counts_only_closing_legs(db):
    await db.save_order(order("O1", pnl=None, side=OrderSide.BUY), "s1")   # 开仓
    await db.save_order(order("C1", pnl=+200.0), "s1")
    await db.save_order(order("C2", pnl=-50.0), "s1")
    await db.save_order(order("C2", pnl=-50.0), "s1")                      # 重推

    rows = await db.get_daily_stats(days=7)
    assert len(rows) == 1
    r = rows[0]
    assert r["trades"] == 2, "开仓腿不计入交易次数；重推不重复累加"
    assert r["gross_pnl"] == pytest.approx(150.0)
    assert r["fees"] == pytest.approx(3.0)
    assert r["net_pnl"] == pytest.approx(147.0)


async def test_gross_pnl_is_not_turnover(db):
    """旧实现用 成交价 × 成交量 × ±1 当盈亏，那是带符号的成交额"""
    await db.save_order(order("C1", pnl=+200.0, filled=100.0, price=3000.0), "s1")
    r = (await db.get_daily_stats(days=7))[0]
    assert r["gross_pnl"] == pytest.approx(200.0)
    assert r["gross_pnl"] != pytest.approx(300_000.0)


async def test_get_daily_pnl(db):
    await db.save_order(order("C1", pnl=+200.0), "s1")
    await db.save_order(order("C2", pnl=-50.0), "s1")
    today = datetime.now(UTC).date().isoformat()
    assert await db.get_daily_pnl("s1", today) == pytest.approx(147.0)
    assert await db.get_daily_pnl("nobody", today) == 0.0


async def test_row_shape_stays_compatible(db):
    """cli.py pnl 与 gui.py 的图表直接消费这些字段"""
    await db.save_order(order("C1", pnl=1.0), "s1")
    row = (await db.get_daily_stats(days=7))[0]
    assert {"strategy", "date", "trades", "gross_pnl", "fees", "net_pnl"} <= set(row)
