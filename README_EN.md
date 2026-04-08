# OKX Algorithmic Trading System

An asyncio-based quantitative trading framework for OKX, supporting both spot and perpetual swap markets with a built-in trend-following strategy.

## Features

- **Fully async**: Built on asyncio + WebSocket for low-latency real-time market data
- **Spot & Swap**: Unified interface for both SPOT and perpetual SWAP instruments
- **Trend strategy**: EMA crossover + MACD confirmation + ATR-based dynamic stop-loss
- **Multi-layer risk control**: Order rate limiting, per-strategy daily loss circuit breaker, global max drawdown emergency stop
- **Indicator warm-up**: Fetches historical candles on startup to initialize indicators, preventing cold-start signal distortion
- **Persistent storage**: SQLite stores candles, signals, orders, and daily P&L statistics
- **CLI tool**: One-command access to balance, positions, tickers, order history, and signal logs

## Project Structure

```
trade/
├── main.py                  # Entry point
├── cli.py                   # Command-line tool
├── config/
│   ├── settings.py          # Global config (API keys, risk parameters)
│   └── strategies.yaml      # Strategy configuration
├── engine/
│   ├── strategy_engine.py   # Core engine: lifecycle management, data routing
│   ├── base_strategy.py     # Strategy base class
│   ├── risk_manager.py      # Risk control module
│   └── portfolio.py         # Position tracker
├── gateway/
│   ├── models.py            # Data models (Candle, Signal, Order, etc.)
│   ├── okx_rest.py          # OKX REST API client
│   └── okx_ws.py            # OKX WebSocket client
├── strategies/
│   └── trend.py             # Trend strategy implementation
└── storage/
    └── db.py                # SQLite data access layer
```

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure API credentials

Create a `.env` file in the project root:

```env
OKX__API_KEY=your_api_key
OKX__SECRET_KEY=your_secret_key
OKX__PASSPHRASE=your_passphrase
OKX__IS_DEMO=true    # true = paper trading, false = live trading
```

### 3. Configure strategies

Edit `config/strategies.yaml` to enable/disable strategies and tune parameters:

```yaml
strategies:
  - name: btc_trend_spot
    class: TrendStrategy
    enabled: true
    inst_type: SPOT
    symbol: BTC-USDT
    config:
      timeframe: "5m"
      ema_fast: 9
      ema_slow: 21
      macd_fast: 5
      macd_slow: 13
      macd_signal: 3
      atr_sl_multiplier: 2.0
      position_size_pct: 0.1    # position size as a fraction of account equity
      require_spread_expand: true
      cooldown_candles: 3
```

### 4. Start the engine

```bash
python main.py
```

## CLI Usage

```bash
python cli.py --help

python cli.py balance                              # Account balance
python cli.py positions                            # Current positions
python cli.py ticker BTC-USDT                      # Live ticker
python cli.py orders -s btc_trend_spot -n 50       # Order history
python cli.py signals -s btc_trend_spot            # Signal log
python cli.py pnl -d 7                             # Last 7 days P&L
python cli.py candles BTC-USDT -t 5m -n 30         # Saved candles
```

## Trend Strategy

**Entry conditions (all must be met):**
- EMA(fast) crosses above EMA(slow) — golden cross
- MACD histogram > 0 — bullish momentum confirmation
- EMA spread is widening (`require_spread_expand`) — filters false breakouts
- Candles since last trade ≥ `cooldown_candles`

**Stop-loss:** `entry_price - ATR × atr_sl_multiplier`

**Exit:** EMA death cross, or stop-loss price hit

Short entry/exit logic is symmetric and only available for SWAP instruments.

## Risk Control Parameters

Configure via `.env` (or use defaults):

| Parameter | Default | Description |
|-----------|---------|-------------|
| `RISK__MAX_POSITION_PCT` | 0.1 | Max position size per instrument as fraction of equity |
| `RISK__MAX_DAILY_LOSS_PCT` | 0.02 | Per-strategy daily loss limit; strategy is paused when exceeded |
| `RISK__MAX_DRAWDOWN_PCT` | 0.05 | Account drawdown limit; all strategies emergency-stopped when exceeded |
| `RISK__ORDER_RATE_LIMIT` | 10 | Max orders per second across all strategies |

## Adding Custom Strategies

Create a new file under `strategies/` (e.g., `grid.py`), subclass `BaseStrategy`, and implement the `on_candle` method. Add the corresponding entry to `strategies.yaml` — the engine will auto-discover and load it.

## Notes

- Always paper trade (`IS_DEMO=true`) thoroughly before going live
- Strategy parameters should be backtested and adjusted periodically
- Logs are stored in `logs/`, rotated daily, compressed, and retained for 30 days
