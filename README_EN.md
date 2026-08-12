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
└── tests/                   # pytest suite (134 tests)
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
pytest                # 134 tests
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

### Improvements made

The sketch had three structural problems; all are fixed here.

**1. Stop strength ran opposite to exposure → structural stop + risk-first sizing**

The original hard stop was a fixed USDT amount (15% of committed capital). In price terms:

| Step | Notional | Price drop needed to trigger |
|---|---|---|
| 1 | 1960 | **38.3%** (effectively no stop) |
| 6 | 25000 | **3.0%** (one or two days of normal noise) |

Now the stop price comes from market structure (`lowest support − stop_atr_mult × ATR`
— breaking it invalidates the "supports will hold" premise), and tranche sizes are
solved backwards from it:

```
Σ wᵢ·Q·(entryᵢ − stop_price)·ct_val = equity × risk_pct
```

The planned add prices *are* the support levels, so this has a closed form. The
**worst case is therefore known and bounded before the first order**, and every
earlier step loses strictly less than budget — protection now scales monotonically
with exposure. If the implied full-load leverage exceeds `max_leverage`, the round
is skipped entirely.

**2. Rising take-profit targets → decreasing + partial trims + time stop**

Targets are relative to average entry, which always sits above current price when
averaging down — and the gap widens with each add:

| | Price now | Avg entry | Target | Rally needed |
|---|---|---|---|---|
| Step 1 | 100 | 100 | 101.5 | +1.5% |
| Step 6 (old rising schedule) | 80 | 86.6 | 90.9 | **+13.7%** |

Three changes: `tp_schedule` now decreases (deep steps just want out); `partial_tp`
trims the *deepest* tranche on a bounce instead of waiting for one big all-or-nothing
target; and `max_hold_bars` forces an exit after prolonged full-load stagnation —
being trapped is what actually kills martingales, so exposure duration now has a bound.

**3. Unconditional first entry → trend filter + wait for the pullback**

`trend_filter` requires the higher-timeframe EMAs stacked bullish before a new round,
removing the "load up fully during a sustained decline" scenario. `first_entry_at_support`
makes the base position wait for the first Fibonacci retracement, so it rests on the
same structural logic as the adds.

### Remaining risks

1. **Still a martingale** — high win rate, fat left tail. The changes make the worst
   case computable; they do not and cannot change the shape of that distribution.
2. **Long only.** The trend filter blocks most declines but cannot guarantee the
   trend holds after entry.
3. **Fees and funding**: up to 6 entries plus several trims per round, and multi-day
   perpetual funding erodes the already-thin deep-step targets.
4. **Filters cut trade frequency sharply**, shrinking the sample and weakening the
   statistical weight of any backtest.

### Measured Result

ETH-USDT-SWAP, 20065 × 15m candles (2026-01-15 → 2026-08-12, ~7 months),
10000 USDT start, default params:

```
Total return  +1.78%       Max drawdown   -1.38%
Closing legs  34 (19 trims)          Win rate  94.12%
Avg win       9.06 USDT    Avg loss      58.31 USDT
```

Versus the pre-redesign run (BTC, 2 months): max drawdown improved from
**−11.45% to −1.38%**, and the loss/win ratio from **11× to 6.4×**. The cost is
position sizes an order of magnitude smaller, and correspondingly smaller absolute
returns — **that is precisely the trade: returns exchanged for a computable worst case**.

> Note: this window still contains no genuine trending decline. OKX's
> `history-candles` endpoint only reaches back to around 2026-01 for 15m bars; to
> test bear behaviour, switch `timeframe` to `1H`/`4H` to buy a longer span.
> Also note that once a cache file exists, raising `--max-bars` does not backfill
> older history — pass `--force-download`.

---

## Setting Maximum Capital Usage

Three layers, outermost first:

### 1. Global hard gate (shared by all strategies)

`RISK__MAX_POSITION_PCT` in `.env` caps **notional value per instrument**:

```env
RISK__MAX_POSITION_PCT=0.3    # per-symbol notional <= 30% of equity
```

Whatever size a strategy computes, entry legs are truncated to this cap (existing
position included); if the allowance is exhausted the order is skipped. Closing legs
are **never** capped — otherwise you would leave an unmanaged remainder. Set `0` to
disable.

⚠️ The default is `0.1`. A strategy configured with `position_size_pct: 0.2` and
`leverage: 3` (intending 60% notional) gets truncated to 10%, so **live behaviour
will diverge from backtests**. The engine warns at startup; either raise the cap or
lower the strategy's sizing.

### 2. Per-strategy sizing (trend strategies)

`position_size_pct × leverage` is the notional fraction of equity per entry —
e.g. `0.2 × 3 = 60%`.

### 3. Per-strategy risk budget (PyramidStrategy)

The pyramid is configured by "how much may I lose", not "how much may I spend":

```yaml
risk_pct: 0.02       # max acceptable loss per round = 2% of equity
max_leverage: 5      # cap on the implied full-load notional leverage
```

Capital deployed is an *output*, not an input — a nearer stop buys more size for the
same risk, a distant stop automatically shrinks it. `max_leverage` bounds that output.

> The layers compose: a strategy sizes by its own rules, then gets truncated by
> `max_position_pct`. The simplest way to control exposure overall is to set
> `RISK__MAX_POSITION_PCT` to the largest per-symbol exposure you can stomach and
> let strategies operate freely inside it.

### ⚠️ `leverage` means different things in different strategies

This is the easiest trap to fall into:

| | Trend strategies | PyramidStrategy |
|---|---|---|
| Effect of `leverage: 5` | **5× larger position** | **Position unchanged**, only less margin locked |
| Size determined by | `position_size_pct × leverage` | `risk_pct` and stop distance |
| To size up, change | `position_size_pct` or `leverage` | `risk_pct` (and raise `max_leverage` / `RISK__MAX_POSITION_PCT` to match) |

Trend strategies go through `BaseStrategy._calc_qty`, where
`qty = balance × pct × leverage / (ctVal × price)` — leverage is a plain multiplier.
`PyramidStrategy` overrides `_calc_qty` and solves backwards from risk instead:

```
qty = (equity × risk_pct) / (ctVal × Σ wᵢ(entryᵢ − stop_price))
```

There is no `leverage` in that formula. The yaml `leverage` key then has exactly one
remaining job: `StrategyEngine._setup_strategy` passes it to the exchange's
`set-leverage` endpoint, so it only changes **how much margin is locked**.

Example (800 USDT account, `risk_pct: 0.02`, full-load notional 191 USDT):

| Leverage | Notional | Margin locked | Worst-case loss |
|---|---|---|---|
| 1x | 191U | 191U | 15.6U |
| 3x | 191U | 64U | 15.6U |
| 5x | 191U | 38U | 15.6U |

Position, PnL and stop price are identical. Under cross margin, liquidation is
evaluated against total account equity versus maintenance margin — 800 equity
carrying 191 notional stays far from liquidation at any leverage setting. So
changing leverage here **amplifies neither returns nor risk**; it merely frees up
margin that was otherwise reserved.

---

## Disclaimer

- Trading carries risk. Always run at least a week on OKX paper trading
  (`IS_DEMO=true`) before going live.
- Logs live in `logs/`, rotated daily and kept compressed for 30 days.
- The database schema upgrades itself on first start. If duplicate order rows from
  an older version are detected, the whole table is backed up as
  `orders_backup_<timestamp>` before deduplication.
