"""程序入口——启动策略引擎"""
import asyncio
import signal
import sys

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

    def _shutdown():
        logger.info("Shutdown signal received")
        asyncio.create_task(engine.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    try:
        await engine.start()
    except KeyboardInterrupt:
        await engine.stop()


if __name__ == "__main__":
    import os
    os.makedirs("logs", exist_ok=True)
    asyncio.run(main())
