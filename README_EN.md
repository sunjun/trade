# OKX Trading & Backtesting System

An `asyncio`-based OKX quantitative trading and multi-timeframe backtesting framework written in Python. Supports both Spot and Perpetual Swap trading, with 8 pluggable strategies included.

## Features

- **Asynchronous Live Engine**: Low-latency execution over WebSocket feeds. Callback processing is decoupled from the socket read loop, so a slow strategy never stalls message intake.
- **Zero-code-change Backtesting**: Replay historical data with the exact same strategy code as live trading. Partial closes and pyramiding behave identically in both.
- **Layered Risk Control**: Order rate limiting, per-strategy daily loss circuit breaker, and global drawdown emergency stop.
- **Crash & Restart Recovery**: On startup the engine adopts any existing exchange position and rebuilds its stop-loss instead of opening a duplicate. Entry orders also attach an exchange-side stop, which survives process death.
- **Indicator Warm-up**: Historical candles are fetched at startup to prime indicators. Only *closed* candles drive indicators, so live and backtest stay consistent.
- **Data Caching**: Backtests fetch and cache OKX candles as CSV with incremental updates.
- **Visualization & Persistence**: Orders and signals are stored in SQLite (idempotent by `order_id`); backtests export equity charts and trade CSVs.

## Project Structure

```text
trade/
├── main.py                  # Live trading entrypoint
├── cli.py                   # Quick-check CLI tool
├── gui.py / chart.py        # Desktop dashboard & live candlestick chart
├── pyproject.toml           # pytest / ruff configuration
├── config/
│   ├── settings.py          # Global env config
│   └── strategies.yaml      # Strategy activation & parameters
├── backtest/                # Backtesting module
│   ├── run_backtest.py      # Single-strategy backtest CLI
│   ├── run_all.py           # Batch compare all strategies
│   ├── engine.py            # Multi-TF backtest engine and mocks
│   ├── data_loader.py       # OKX fetching & CSV caching
│   └── report.py            # Metrics & matplotlib plots
├── engine/
│   ├── strategy_engine.py   # Lifecycle, market routing, order attribution
│   ├── base_strategy.py     # Base class: execution, sizing, position adoption
│   ├── risk_manager.py      # Rate limit / daily loss / drawdown breaker
│   └── portfolio.py         # Balance & position view
├── gateway/
│   ├── models.py            # Data models
│   ├── okx_rest.py          # REST client (timeout / retry / unified errors)
│   ├── okx_ws.py            # WebSocket client (auto-reconnect / queued callbacks)
│   └── precision.py         # Order sizing: Decimal rounding to lotSz
├── strategies/              # Strategy implementations, see table below
├── storage/db.py            # SQLite data access
└── tests/                   # pytest suite (115 tests)
```

## Built-in Strategies

| Class | Module | Idea | Side |
|---|---|---|---|
| `RightSideStrategy` | `rightside.py` | EMA golden cross + MACD>0 + volume surge; trim 50% on death cross above zero, exit on EMA rollover | Long/Short |
| `MtfTrendStrategy` | `mtftrend.py` | 4H direction + 1H bias + 15m entry convergence | Long/Short |
| `TrendStrategy` | `trend.py` | EMA crossover + MACD confirmation + ATR stop | Long/Short |
| `BbRsiStrategy` | `bbrsi.py` | Bollinger + RSI mean reversion, for ranging markets | Long/Short |
| `DonchianStrategy` | `donchian.py` | Donchian channel breakout (turtle-style) | Long/Short |
| `VwapStrategy` | `vwap.py` | Intraday VWAP deviation + RSI filter | Long/Short |
| `GridStrategy` | `grid.py` | Fixed-range grid, dual-side on swaps | Long/Short |
| `PyramidStrategy` | `pyramid.py` | Fibonacci support + ATR spacing dual-confirmed pyramiding | **Long only** |

> `PyramidStrategy` is a martingale-style strategy with a completely different risk
> profile from the others, and is disabled by default. Read the
> [strategy analysis](#pyramidstrategy-analysis) before enabling it.

## Quick Start

### 1. Install

```bash
pip install -r requirements.txt          # live trading
pip install -r requirements-dev.txt      # development: adds pytest / ruff
```

> A live-trading box does not need `requirements-dev.txt`, and can skip `matplotlib`
> entirely (only backtest charts and the GUI use it) — saves roughly 120MB.

### 2. Configure

Create `.env` in the project root:

```env
OKX__API_KEY=your_api_key
OKX__SECRET_KEY=your_secret_key
OKX__PASSPHRASE=your_passphrase
OKX__IS_DEMO=true    # true = paper trading, false = live

RISK__MAX_DAILY_LOSS_PCT=0.02    # per-strategy daily loss breaker
RISK__MAX_DRAWDOWN_PCT=0.05      # global drawdown emergency stop
```

⚠️ **Risk thresholds must be consistent with strategy sizing.** A single stop-out
costs roughly `position_size_pct × leverage × stop_loss_pct` of equity — e.g.
`20% × 3x × 10% = 6%`. With a 2% daily loss limit, one stop-out pauses the
strategy and trips the global breaker. The engine warns about this at startup.

Then edit `config/strategies.yaml` to enable strategies.

---

## Backtesting

```bash
# Single strategy; writes charts and trade CSV into --out-dir
python -m backtest.run_backtest --strategy eth_rightside_swap --capital 10000 --max-bars 15000 --out-dir backtest_results

# Compare all strategies
python -m backtest.run_all --capital 10000 --max-bars 20000 --out-dir backtest_results
```

> The first run fetches and caches candles from OKX; `--force-download` refreshes.

The report counts *closing legs* — exits, partial trims and stop-outs, i.e. anything
that realizes PnL. Entries are not counted; the parenthetical shows how many legs
were partial trims.

---

## Live Trading

```bash
python main.py
```

### CLI Tools

```bash
python cli.py balance                        # available funds
python cli.py positions                      # open positions
python cli.py ticker BTC-USDT                # latest price
python cli.py orders -s eth_rightside_swap   # order history per strategy
python cli.py signals -s eth_rightside_swap  # signal log
python cli.py pnl --days 7                   # daily PnL
```

### Deployment Footprint

The live process measures at roughly **50–80MB RSS** with CPU essentially idle —
a 1-core / 1GB box is more than enough. Backtests peak around 87MB (no charts)
or 160MB (with charts); run those on a dev machine.

---

## Development

```bash
pytest                # 115 tests
ruff check .          # lint
ruff check . --fix
```

Tests concentrate on the places where mistakes are both easy to make and
expensive: indicators consuming only closed candles, close-order sizing, risk
breakers, idempotent order persistence, REST retry policy, and the WebSocket read
loop staying unblocked. `tests/test_gateway.py` spins up a local aiohttp server to
exercise real timeout and retry behaviour rather than mocking the socket layer.

---

<a id="pyramidstrategy-analysis"></a>
## PyramidStrategy Analysis

Ported from the `gemini.py` sketch. It opens a base position, then adds on the way
down whenever price both touches a Fibonacci support level and sits at least
1.2×ATR below the previous fill. Margin per step grows by 1.3×. Exits are either a
tiered take-profit or a global hard stop.

### Strengths

1. **ATR minimum spacing is the genuinely good idea here.** A plain fixed-grid DCA
   dumps its whole budget into a narrow band during a fast drop, barely improving
   the average entry. Requiring 1.2 ATR between adds makes the ladder adapt to
   realized volatility.
2. **Dual confirmation is more disciplined than pure DCA** — it only adds near
   technical support instead of catching a free fall continuously.
3. **The margin schedule is precomputed and sums exactly to 1.** `w1 = (1-r)/(1-rⁿ)`
   guarantees all 6 steps consume exactly the budget, avoiding the classic
   "not enough left for the final step" bug.
4. **It has a global hard stop at all**, which already puts it ahead of most
   martingale implementations.
5. **Tiered take-profit acknowledges** that different exposure levels warrant
   different targets.

### Weaknesses and risks

1. **It is fundamentally a martingale: high win rate, large tail loss.** The equity
   curve grinds upward and then gives back dozens of wins in a single trending
   decline. A backtest that does not span a real bear leg will badly overstate it.
2. **Stop effectiveness runs opposite to exposure.** The hard stop is a fixed USDT
   amount (15% of committed capital). Early on, with a small position, price must
   fall enormously to trigger it — effectively no stop. Fully loaded, a small move
   trips it. Protection is weakest exactly when it matters most.
3. **Take-profit targets rise with step count, which is backwards for escaping.**
   `tp_schedule` grows 1.5% → 5.0%, so the deeper underwater you are, the further
   price must rally before you are allowed out. Most DCA designs decrease this.
4. **The first entry is an unconditional market buy.** Support levels and ATR only
   gate the *adds*; the base position — which dominates the cycle's average cost —
   ignores location entirely. This mirrors the original sketch. The first
   improvement to make would be a trend filter on the initial entry (e.g. require
   4H EMAs stacked bullish).
5. **Long only, no trend filter.** In a sustained downtrend it exhausts all 6 steps
   and then sits fully loaded.
6. **Fees and funding are not negligible.** A full cycle is 6 entries plus 1 exit of
   taker fills on growing notional, and multi-day perpetual funding erodes an
   already thin first-step target of 1.5%.

### Changes made versus the sketch

The original is a `while True` + `time.sleep(3)` polling script holding all state in
local variables. Porting it fixed several issues that would cause real incidents:

| Issue | Sketch behaviour | This implementation |
|---|---|---|
| Supports computed once | Frozen forever after entry; the ladder goes stale | Refreshed on every 4H close while flat, frozen once in position so the ladder does not move underfoot |
| State lost on restart | `current_step` resets to 0 and it opens a fresh cycle on top of the existing position | Adopts the position at startup; step unknown so it assumes fully loaded and stops adding |
| Stop-loss check cadence | Depends on a 3-second ticker poll | Checked on every closed 15m candle; the higher timeframe only computes indicators |
| Add budget vs. drawdown | Not addressed | Committed capital is snapshotted at entry, so later steps do not shrink with equity |
| Error handling | `except Exception: sleep(5)` swallows everything forever | Unified `OKXError` at the gateway; failed entries roll back local state |

### Measured Result

BTC-USDT-SWAP, 5000 × 15m candles (~2 months), 10000 USDT start, default params:

```
Total return  +1.25%        Max drawdown   -11.45%
Win rate      92.86%        Sharpe           0.31
Avg win       93.23 USDT    Avg loss     1029.20 USDT
```

**One loss ≈ eleven wins** — exactly the shape described in point 1, and this window
does not even contain a real trending decline. The headline return is positive; the
win/loss structure shows how fragile that positive is.

> Recommendation: keep `capital_pct` well below 1.0 (default 0.5) and **backtest
> across a genuinely trending decline** before considering live deployment.

---

## Disclaimer

- Trading carries risk. Always run at least a week on OKX paper trading
  (`IS_DEMO=true`) before going live.
- Logs live in `logs/`, rotated daily and kept compressed for 30 days.
- The database schema upgrades itself on first start. If duplicate order rows from
  an older version are detected, the whole table is backed up as
  `orders_backup_<timestamp>` before deduplication.
