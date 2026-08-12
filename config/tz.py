"""日志展示用的本地时区（东八区）。

内部所有时间戳一律存 UTC（见 gateway.models），只在打日志时转成东八区，
避免「存的是 UTC、看的是本地」两套语义混在一起。
交易所签名用的时间戳必须保持 UTC，不要用这里的函数。
"""
from datetime import UTC, datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8), "UTC+8")


def to_cn(dt: datetime) -> datetime:
    """把（带时区的）时间转成东八区。naive 时间视为 UTC。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(CN_TZ)


def fmt_ts(dt: datetime, fmt: str = "%m-%d %H:%M") -> str:
    """日志里显示 K 线/订单时间用，输出东八区。"""
    return to_cn(dt).strftime(fmt)
