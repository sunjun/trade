"""共享技术指标（增量更新，无需重算历史）"""
import math
from collections import deque


class RunningEMA:
    """指数移动平均"""
    def __init__(self, period: int):
        self.period = period
        self.k = 2.0 / (period + 1)
        self.value: float | None = None
        self._buf: list[float] = []

    def update(self, price: float) -> float | None:
        if self.value is None:
            self._buf.append(price)
            if len(self._buf) >= self.period:
                self.value = sum(self._buf) / len(self._buf)
            return self.value
        self.value = price * self.k + self.value * (1 - self.k)
        return self.value

    @property
    def ready(self) -> bool:
        return self.value is not None

    def reset(self):
        self.value = None
        self._buf.clear()


class RunningATR:
    """真实波幅均值（EMA平滑）"""
    def __init__(self, period: int = 14):
        self._ema = RunningEMA(period)
        self._prev_close: float | None = None
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev_close is not None:
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )
            self.value = self._ema.update(tr)
        self._prev_close = close
        return self.value

    @property
    def ready(self) -> bool:
        return self.value is not None


class RunningRSI:
    """Wilder RSI（Wilder EMA平滑增益/损失）"""
    def __init__(self, period: int = 14):
        self.period = period
        self.value: float | None = None
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._prev_close: float | None = None
        self._buf: list[tuple[float, float]] = []  # (gain, loss)

    def update(self, price: float) -> float | None:
        if self._prev_close is None:
            self._prev_close = price
            return None
        change = price - self._prev_close
        self._prev_close = price
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0

        if self._avg_gain is None:
            self._buf.append((gain, loss))
            if len(self._buf) >= self.period:
                self._avg_gain = sum(g for g, _ in self._buf) / self.period
                self._avg_loss = sum(l for _, l in self._buf) / self.period
                self._set_value()
            return self.value

        self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
        self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period
        self._set_value()
        return self.value

    def _set_value(self):
        if self._avg_loss == 0:
            self.value = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            self.value = 100.0 - 100.0 / (1.0 + rs)

    @property
    def ready(self) -> bool:
        return self.value is not None


class RunningBB:
    """Bollinger Bands: SMA(N) ± std_mult * std(N)"""
    def __init__(self, period: int = 20, std_mult: float = 2.0):
        self.period = period
        self.std_mult = std_mult
        self._buf: deque[float] = deque(maxlen=period)
        self.upper: float | None = None
        self.middle: float | None = None
        self.lower: float | None = None

    def update(self, price: float):
        self._buf.append(price)
        if len(self._buf) < self.period:
            return
        mean = sum(self._buf) / self.period
        variance = sum((x - mean) ** 2 for x in self._buf) / self.period
        std = math.sqrt(variance)
        self.middle = mean
        self.upper = mean + self.std_mult * std
        self.lower = mean - self.std_mult * std

    @property
    def ready(self) -> bool:
        return self.middle is not None

    @property
    def width_pct(self) -> float | None:
        if not self.ready:
            return None
        return (self.upper - self.lower) / self.middle


class RunningVWAP:
    """当日 VWAP + 标准差波段（每日重置）"""
    def __init__(self, std_mult: float = 1.5):
        self.std_mult = std_mult
        self._cum_pv = 0.0   # Σ(price × volume)
        self._cum_pv2 = 0.0  # Σ(price² × volume)
        self._cum_v = 0.0    # Σ(volume)
        self.value: float | None = None
        self.upper: float | None = None
        self.lower: float | None = None

    def reset(self):
        self._cum_pv = 0.0
        self._cum_pv2 = 0.0
        self._cum_v = 0.0
        self.value = None
        self.upper = None
        self.lower = None

    def update(self, price: float, volume: float):
        self._cum_pv += price * volume
        self._cum_pv2 += price * price * volume
        self._cum_v += volume
        if self._cum_v == 0:
            return
        vwap = self._cum_pv / self._cum_v
        variance = max(self._cum_pv2 / self._cum_v - vwap * vwap, 0.0)
        std = math.sqrt(variance)
        self.value = vwap
        self.upper = vwap + self.std_mult * std
        self.lower = vwap - self.std_mult * std

    @property
    def ready(self) -> bool:
        return self.value is not None and self._cum_v > 0


class Donchian:
    """Donchian 通道：N 周期内的最高价/最低价"""
    def __init__(self, period: int):
        self.period = period
        self._highs: deque[float] = deque(maxlen=period)
        self._lows: deque[float] = deque(maxlen=period)

    def update(self, high: float, low: float):
        self._highs.append(high)
        self._lows.append(low)

    @property
    def ready(self) -> bool:
        return len(self._highs) >= self.period

    @property
    def highest(self) -> float:
        return max(self._highs)

    @property
    def lowest(self) -> float:
        return min(self._lows)
