"""CLI 工具——查看状态、管理策略、查询历史"""
import asyncio
import sys
from datetime import date

import click

from config.settings import settings
from gateway.okx_rest import OKXRestClient
from storage.db import Database


def _get_rest() -> OKXRestClient:
    okx = settings.okx
    return OKXRestClient(okx.api_key, okx.secret_key, okx.passphrase, okx.is_demo)


@click.group()
def cli():
    """OKX 策略交易系统 CLI"""


# ── 账户状态 ──────────────────────────────────────────────────────────────────

@cli.command()
def balance():
    """查看账户余额"""
    async def _run():
        async with _get_rest() as rest:
            bal = await rest.get_balance("USDT")
            click.echo(f"USDT 总资产: {bal.total:.4f}")
            click.echo(f"USDT 可用:   {bal.available:.4f}")
            click.echo(f"USDT 冻结:   {bal.frozen:.4f}")
    asyncio.run(_run())


@cli.command()
def positions():
    """查看当前持仓"""
    async def _run():
        async with _get_rest() as rest:
            pos_list = await rest.get_positions()
            if not pos_list:
                click.echo("当前无持仓")
                return
            for p in pos_list:
                click.echo(
                    f"{p.inst_id:20s} {p.pos_side.value:6s} "
                    f"size={p.size:10.4f}  entry={p.entry_price:12.4f}  "
                    f"mark={p.mark_price:12.4f}  uPnL={p.unrealized_pnl:+.4f}"
                )
    asyncio.run(_run())


@cli.command()
@click.argument("symbol")
def ticker(symbol):
    """查看行情（示例: btc-usdt）"""
    async def _run():
        async with _get_rest() as rest:
            t = await rest.get_ticker(symbol.upper())
            click.echo(f"{t.inst_id}: last={t.last:.4f}  bid={t.bid:.4f}  ask={t.ask:.4f}")
    asyncio.run(_run())


# ── 历史记录 ──────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--strategy", "-s", default=None, help="按策略过滤")
@click.option("--limit", "-n", default=20, help="显示条数")
def orders(strategy, limit):
    """查看历史订单"""
    async def _run():
        db = Database(settings.db_path)
        await db.init()
        rows = await db.get_orders(strategy=strategy, limit=limit)
        if not rows:
            click.echo("暂无订单记录")
            return
        header = f"{'ID':>6}  {'时间':19}  {'品种':16}  {'策略':20}  {'方向':5}  {'状态':12}  {'成交量':>10}  {'均价':>12}"
        click.echo(header)
        click.echo("-" * len(header))
        for r in rows:
            click.echo(
                f"{r['id']:>6}  {r['created_at'][:19]}  {r['inst_id']:16}  "
                f"{r['strategy']:20}  {r['side']:5}  {r['status']:12}  "
                f"{r['filled_qty']:>10.4f}  {r['avg_price']:>12.4f}"
            )
        await db.close()
    asyncio.run(_run())


@cli.command()
@click.option("--days", "-d", default=7, help="查看天数")
def pnl(days):
    """查看每日盈亏统计"""
    async def _run():
        db = Database(settings.db_path)
        await db.init()
        rows = await db.get_daily_stats(days=days)
        if not rows:
            click.echo("暂无统计数据")
            return
        header = f"{'策略':20}  {'日期':12}  {'交易次数':>8}  {'毛利润':>12}  {'手续费':>10}  {'净利润':>12}"
        click.echo(header)
        click.echo("-" * len(header))
        for r in rows:
            net = r.get("net_pnl", r.get("gross_pnl", 0) - r.get("fees", 0))
            click.echo(
                f"{r['strategy']:20}  {r['date']:12}  {r['trades']:>8}  "
                f"{r['gross_pnl']:>12.4f}  {r['fees']:>10.4f}  {net:>12.4f}"
            )
        await db.close()
    asyncio.run(_run())


@cli.command()
@click.option("--strategy", "-s", default=None, help="按策略过滤")
@click.option("--limit", "-n", default=20, help="显示条数")
def signals(strategy, limit):
    """查看策略产生的信号记录"""
    async def _run():
        db = Database(settings.db_path)
        await db.init()
        rows = await db.get_signals(strategy=strategy, limit=limit)
        if not rows:
            click.echo("暂无信号记录")
            return
        header = f"{'ID':>5}  {'时间':19}  {'策略':20}  {'品种':16}  {'方向':5}  {'类型':8}  {'止损':>12}  原因"
        click.echo(header)
        click.echo("-" * 90)
        for r in rows:
            click.echo(
                f"{r['id']:>5}  {r['created_at'][:19]}  {r['strategy']:20}  "
                f"{r['inst_id']:16}  {r['side']:5}  {r['order_type']:8}  "
                f"{str(r['stop_loss'] or '-'):>12}  {r['reason'] or ''}"
            )
        await db.close()
    asyncio.run(_run())


@cli.command()
@click.argument("symbol")
@click.option("--timeframe", "-t", default="15m", help="K线周期")
@click.option("--limit", "-n", default=20, help="显示条数")
def candles(symbol, timeframe, limit):
    """查看已保存的K线数据（示例: btc-usdt -t 15m）"""
    async def _run():
        db = Database(settings.db_path)
        await db.init()
        rows = await db.get_candles(symbol.upper(), timeframe, limit=limit)
        if not rows:
            click.echo(f"暂无 {symbol.upper()} [{timeframe}] K线数据")
            return
        click.echo(f"{'时间':19}  {'开盘':>10}  {'最高':>10}  {'最低':>10}  {'收盘':>10}  {'成交量':>12}")
        click.echo("-" * 80)
        for r in rows:
            click.echo(
                f"{r['ts'][:19]}  {r['open']:>10.4f}  {r['high']:>10.4f}  "
                f"{r['low']:>10.4f}  {r['close']:>10.4f}  {r['volume']:>12.4f}"
            )
        await db.close()
    asyncio.run(_run())


# ── 引擎控制 ──────────────────────────────────────────────────────────────────

@cli.command()
def run():
    """启动交易引擎（等同于 python main.py）"""
    import subprocess
    subprocess.run([sys.executable, "main.py"])


if __name__ == "__main__":
    cli()
