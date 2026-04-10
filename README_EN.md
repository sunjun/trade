# OKX Trading & Backtesting System

An `asyncio`-based OKX quantitative trading, and multi-timeframe backtesting framework written in Python. Supports both Spot and Perpetual Swap trading with built-in multi-timeframe convergence strategies (e.g., MtfTrendStrategy).

## Features

- **Asynchronous Live Engine**: Low-latency execution reacting to WebSocket feeds and REST calls built purely on python `asyncio`.
- **Local Zero-code-change Backtesting**: Replay historical data using exact the same strategy code as live trading by mocking trading environments.
- **Multiple Timeframe Convergence**: Provides out-of-the-box trend-following models using EMA & MACD overlaps across timeframes (e.g. 4H, 1H, 15m), complete with trail stops via ATR bounds.
- **Data Caching & Pagination**: Backtester automatically queries and caches candlestick data seamlessly into CSV files, with incremental update capability for ultrafast iteration.
- **Strict Risk Control**: Multi-layered constraints restricting max trades per second, daily strategy drawdown limits, and global portfolio stop-loss blocks.
- **Indicator Warm-up**: Automatically fetches historical limit candles on startup to feed technical indicators, ensuring strategies never cold-start with false signals.
- **Visualization & Persistence**: Internal operations are tracked in a lightning-fast SQLite DB; whereas the backtest engine exports comprehensive CSV reports and plots equity/drawdown charts via `matplotlib`.

## Project Structure

```text
trade/
├── main.py                  # Live Trading Entrypoint
├── cli.py                   # Quick-check CLI tool
├── config/
│   ├── settings.py          # Global env config variables
│   └── strategies.yaml      # Strategy activation & configurations
├── backtest/                # 🚀 Backtesting Module
│   ├── run_backtest.py      # Run simulation for a single strategy
│   ├── run_all.py           # Batch process & compare all strategies
│   ├── engine.py            # Multi-TF backtest engine and Mock modules
│   ├── data_loader.py       # OKX REST fetching & CSV caching module
│   └── report.py            # Metrics calc & Matplotlib generators
├── engine/
│   ├── strategy_engine.py   # Live Engine: Routing market feeds & LC handling
│   ├── base_strategy.py     # Base interface for strategies
│   ├── risk_manager.py      # Risk constraints enforcer
│   └── portfolio.py         # Global balances & live position views
├── gateway/
│   ├── models.py            # Standard Dataclasses
│   ├── okx_rest.py          # OKX REST Client
│   └── okx_ws.py            # OKX WebSocket Client
├── strategies/
│   ├── mtftrend.py          # Example Multi-Timeframe Trend Strategy
│   ├── _base_state.py       # Helper functions for position finite-state machine
│   └── ...                  
└── storage/
    └── db.py                # Database API wrapping SQLite 
```

## Quick Start

### 1. Requirements

```bash
pip install -r requirements.txt
```

### 2. Configure Environment

Create a `.env` file at the root of the directory:

```env
OKX__API_KEY=your_api_key
OKX__SECRET_KEY=your_secret_key
OKX__PASSPHRASE=your_passphrase
OKX__IS_DEMO=true    # true = Paper Trading/Demo, false = Live Real Money
```

Adjust your strategy allocations in `config/strategies.yaml`:
```yaml
strategies:
  - name: eth_mtf_swap
    class: MtfTrendStrategy
    enabled: true
    inst_type: SWAP
    symbol: ETH-USDT-SWAP
    config:
      timeframe: "15m"
      # ... view yaml for more parameter tunings
```

---

## Backtesting

Without altering a single line of your operational strategy code, replay and validate through history:

**Single Strategy:**
```bash
# Executing this generates an equity chart and trades CSV inside /backtest_results directory
python -m backtest.run_backtest --strategy eth_mtf_swap --capital 10000 --max-bars 15000 --out-dir backtest_results
```

**Batch Strategy Comparisons:**
```bash
python -m backtest.run_all --capital 10000 --max-bars 20000 --out-dir backtest_results
```

> **Note**: During the first execution, historical Candles are downloaded and saved via OKX endpoints. Add `--force-download` to refresh local cache records.
 
---

## Live Trading

When you've safely validated within the Backtest and the Demo framework (`IS_DEMO=true`), engage the live system:

```bash
python main.py
```

### CLI Utilities
Check in on operations seamlessly using the script provided:
```bash
python cli.py balance              # Query usable funds
python cli.py positions            # List active positions
python cli.py ticker BTC-USDT      # Check symbol status
python cli.py orders -s eth_mtf_swap  # Check recent orders by strategy name
python cli.py signals -s eth_mtf_swap # Check strategy signal histories
```

## Disclaimer
- Trading algorithms present inherent risks. **ALWAYS** begin with Demo accounts in Paper trading mode for at least a week to verify integrations before live exposure!
- Output logs are strictly preserved in `/logs` with automated zipping rotation routines (kept for 30 days).
