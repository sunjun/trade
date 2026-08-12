"""下单精度处理——按交易所的 lotSz / tickSz 取整

单独一个模块是因为下单量的取整同时被策略侧（算仓位）和网关侧（发请求前
兜底）用到，两边必须给出完全一致的结果。

用 Decimal 而不是浮点：float 运算会产生 0.30000000000000004 这类值，
超出 lotSz 允许的小数位，OKX 会直接拒单（sCode 51121 等）。
"""
from decimal import ROUND_DOWN, Decimal


def _dec(value: float | str | Decimal) -> Decimal:
    # 经 str 转换，避免 Decimal(0.1) 带进二进制浮点的误差尾巴
    return value if isinstance(value, Decimal) else Decimal(str(value))


def round_to_step(value: float, step: float) -> float:
    """把 value 向下取整到 step 的整数倍，并对齐到 step 的小数位数。

    >>> round_to_step(1.23456, 0.001)
    1.234
    >>> round_to_step(0.1 + 0.2, 0.1)      # 浮点误差不会漏出来
    0.3
    >>> round_to_step(7.9, 1)
    7.0
    """
    if step <= 0:
        return value
    d_value, d_step = _dec(value), _dec(step)
    steps = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN)
    return float((steps * d_step).quantize(d_step, rounding=ROUND_DOWN))


def round_qty(qty: float, lot_sz: float, min_sz: float = 0.0) -> float:
    """按 lotSz 向下取整下单量；不足 minSz 返回 0（表示这笔单不该发出去）。"""
    rounded = round_to_step(qty, lot_sz)
    return rounded if rounded >= min_sz else 0.0
