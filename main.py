"""程序入口——启动策略引擎"""
import asyncio
import signal
import sys
import traceback

from loguru import logger

from config.settings import settings
from engine.strategy_engine import StrategyEngine


def _setup_logging():
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    )
    logger.add(
        "logs/trade_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="00:00",
        retention="30 days",
        compression="gz",
    )


async def main():
    _setup_logging()
    engine = StrategyEngine(settings)

    loop = asyncio.get_running_loop()

    # 捕获所有后台 Task 的未处理异常，防止静默失败
    def _task_exception_handler(loop, context):
        exc = context.get("exception")
        msg = context.get("message", "unknown")
        if exc:
            logger.error(f"Unhandled task exception: {msg} | {type(exc).__name__}: {exc}")
        else:
            logger.error(f"Asyncio error: {msg}")

    loop.set_exception_handler(_task_exception_handler)

    # 持有引用：悬空的 task 可能被 GC 回收，导致停机流程中途消失
    shutdown_tasks: set[asyncio.Task] = set()

    def _shutdown():
        logger.info("Shutdown signal received")
        task = asyncio.create_task(engine.stop())
        shutdown_tasks.add(task)
        task.add_done_callback(shutdown_tasks.discard)

    # add_signal_handler 仅 Unix 支持，Windows 降级为 signal.signal
    if sys.platform != 'win32':
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _shutdown)
    else:
        signal.signal(signal.SIGINT,  lambda *_: loop.call_soon_threadsafe(_shutdown))
        signal.signal(signal.SIGTERM, lambda *_: loop.call_soon_threadsafe(_shutdown))

    try:
        await engine.start()
    except KeyboardInterrupt:
        await engine.stop()


if __name__ == "__main__":
    import os
    os.makedirs("logs", exist_ok=True)
    code = 0
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except BaseException:
        # 必须在这里打完整栈：下面 finally 里的 os._exit 会直接终止进程，
        # 连解释器默认的 traceback 都来不及打，异常退出会表现为「静默退出码 0」
        traceback.print_exc()
        logger.opt(exception=True).critical("Engine crashed with an unhandled exception")
        code = 1
    finally:
        # aiohttp/websockets 的网络清理线程会阻塞正常退出，
        # 引擎已完成 stop() 清理，直接强制终止进程。
        logger.complete()          # 强杀前把日志 sink 的缓冲刷干净
        sys.stderr.flush()
        os._exit(code)
