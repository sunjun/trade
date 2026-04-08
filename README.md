# OKX 策略交易系统

基于 Python asyncio 的 OKX 量化交易框架，支持现货与永续合约，内置趋势跟踪策略。

## 功能特性

- **全异步架构**：基于 asyncio + WebSocket，低延迟实时行情处理
- **双品种支持**：现货（SPOT）和永续合约（SWAP）统一接口
- **趋势策略**：EMA 金叉死叉 + MACD 确认 + ATR 动态止损
- **多层风控**：下单频率限制、单策略日内亏损熔断、全局最大回撤紧急停止
- **指标预热**：启动时自动拉取历史 K 线完成指标初始化，避免冷启动信号失真
- **数据持久化**：SQLite 存储 K 线、信号、订单及每日盈亏统计
- **CLI 工具**：余额、持仓、行情、历史订单、信号记录一键查询

## 项目结构

```
trade/
├── main.py                  # 程序入口
├── cli.py                   # 命令行工具
├── config/
│   ├── settings.py          # 全局配置（API 密钥、风控参数）
│   └── strategies.yaml      # 策略配置文件
├── engine/
│   ├── strategy_engine.py   # 核心引擎：生命周期管理、行情路由
│   ├── base_strategy.py     # 策略基类
│   ├── risk_manager.py      # 风控模块
│   └── portfolio.py         # 持仓视图
├── gateway/
│   ├── models.py            # 数据模型（Candle、Signal、Order 等）
│   ├── okx_rest.py          # OKX REST API 客户端
│   └── okx_ws.py            # OKX WebSocket 客户端
├── strategies/
│   └── trend.py             # 趋势策略实现
└── storage/
    └── db.py                # SQLite 数据访问层
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API 密钥

在项目根目录创建 `.env` 文件：

```env
OKX__API_KEY=your_api_key
OKX__SECRET_KEY=your_secret_key
OKX__PASSPHRASE=your_passphrase
OKX__IS_DEMO=true    # true = 模拟盘，false = 实盘
```

### 3. 配置策略

编辑 `config/strategies.yaml`，按需启用/禁用策略并调整参数：

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
      position_size_pct: 0.1    # 单仓占账户权益比例
      require_spread_expand: true
      cooldown_candles: 3
```

### 4. 启动引擎

```bash
python main.py
```

## CLI 使用

```bash
python cli.py --help

python cli.py balance              # 查看账户余额
python cli.py positions            # 查看当前持仓
python cli.py ticker BTC-USDT      # 查看行情
python cli.py orders -s btc_trend_spot -n 50   # 查看历史订单
python cli.py signals -s btc_trend_spot        # 查看信号记录
python cli.py pnl -d 7             # 查看近 7 天盈亏统计
python cli.py candles BTC-USDT -t 5m -n 30    # 查看已保存K线
```

## 趋势策略说明

**入场条件（同时满足）：**
- EMA(fast) 上穿 EMA(slow)（金叉）
- MACD 柱体 > 0（多头动能确认）
- EMA 间距正在扩大（`require_spread_expand`，过滤假突破）
- 距上次交易间隔 ≥ `cooldown_candles` 根K线

**止损：** `入场价 - ATR × atr_sl_multiplier`

**出场：** EMA 死叉，或触及止损价

空头入场/出场逻辑对称，仅合约品种支持。

## 风控参数

在 `.env` 中配置（或使用默认值）：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `RISK__MAX_POSITION_PCT` | 0.1 | 单品种最大仓位占权益比例 |
| `RISK__MAX_DAILY_LOSS_PCT` | 0.02 | 策略日内最大亏损比例，超出则暂停该策略 |
| `RISK__MAX_DRAWDOWN_PCT` | 0.05 | 账户最大回撤比例，超出则紧急停止所有策略 |
| `RISK__ORDER_RATE_LIMIT` | 10 | 每秒最大下单次数 |

## 扩展策略

在 `strategies/` 目录下新建文件（如 `grid.py`），继承 `BaseStrategy` 并实现 `on_candle` 方法，在 `strategies.yaml` 中添加对应配置即可自动加载。

## 注意事项

- 实盘交易前请在模拟盘（`IS_DEMO=true`）充分测试
- 策略参数需根据市场行情定期回测调整
- 日志文件保存在 `logs/` 目录，自动按天轮转并压缩保留 30 天
