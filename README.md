# OKX 策略交易与回测系统

基于 Python `asyncio` 的 OKX 量化交易与多时框回测框架，支持现货与永续合约，内置三重共振趋势跟踪策略（MtfTrendStrategy）。

## 功能特性

- **全异步实盘架构**：基于 `asyncio` + WebSocket，低延迟实时行情处理。
- **本地回测引擎**：支持多时框对齐回放引擎。策略代码**零修改**即可在实盘与回测间无缝切换。
- **三重共振策略**：支持跨时框（如 4H, 1H, 15m）EMA + MACD 趋势共振诊断，结合 ATR 动态止损。
- **数据本地缓存**：回测时自动拉取 OKX 历史 K 线并缓存为 CSV，支持断点增量更新拉取，极速验证策略。
- **多层风控机制**：下单频率限制、单策略日内亏损熔断、全局最大回撤紧急停止。
- **指标自动预热**：启动时自动拉取历史极限 K 线完成指标初始化，避免冷启动信号失真。
- **可视化与数据持久化**：SQLite 存储实时订单与信号；回测输出自动生成净值图表及详细交易 CSV。

## 项目结构

```text
trade/
├── main.py                  # 实盘系统入口
├── cli.py                   # 命令行快捷查询工具
├── config/
│   ├── settings.py          # 全局配置（环境变量解析）
│   └── strategies.yaml      # 策略与参数配置文件
├── backtest/                # 🚀 回测模块
│   ├── run_backtest.py      # 单策略回测 CLI
│   ├── run_all.py           # 批量并列回测所有策略 CLI
│   ├── engine.py            # 多时框回测引擎与 Mock 对象
│   ├── data_loader.py       # OKX 历史数据拉取与本地 CSV 缓存
│   └── report.py            # 量化指标计算与 matplotlib 绘图
├── engine/
│   ├── strategy_engine.py   # 实盘引擎核心：生命周期与行情路由
│   ├── base_strategy.py     # 策略基类
│   ├── risk_manager.py      # 实盘风控模块
│   └── portfolio.py         # 账户资产与持仓视图
├── gateway/
│   ├── models.py            # 数据模型
│   ├── okx_rest.py          # OKX REST API 客户端
│   └── okx_ws.py            # OKX WebSocket 客户端
├── strategies/
│   ├── mtftrend.py          # 多时框共振趋势策略
│   ├── _base_state.py       # 状态机辅助
│   └── ...                  # 其他策略
└── storage/
    └── db.py                # SQLite 数据访问
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境与策略

在项目根目录创建 `.env` 文件：

```env
OKX__API_KEY=your_api_key
OKX__SECRET_KEY=your_secret_key
OKX__PASSPHRASE=your_passphrase
OKX__IS_DEMO=true    # true = 模拟盘，false = 实盘
```

编辑 `config/strategies.yaml` 按需启用并配置实盘/回测策略：
```yaml
strategies:
  - name: eth_mtf_swap
    class: MtfTrendStrategy
    enabled: true
    inst_type: SWAP
    symbol: ETH-USDT-SWAP
    config:
      timeframe: "15m"
      # ... 详见策略 yaml 示例
```

---

## 回测 (Backtesting)

无需修改任何策略运行代码，直接使用历史数据模拟撮合：

**单策略回测：**
```bash
# 执行完毕会自动在当前目录下/backtest_results 生成走势图与 CSV 交易记录
python -m backtest.run_backtest --strategy eth_mtf_swap --capital 10000 --max-bars 15000 --out-dir backtest_results
```

**批量测试所有策略：**
```bash
python -m backtest.run_all --capital 10000 --max-bars 20000 --out-dir backtest_results
```

> **提示**：首次回测会自动从 OKX 获取历史 K 线并缓存。如果需要强制拉取最新的修复数据，可添加 `--force-download`。

---

## 实盘运行 (Live Trading)

确认你的 `.env` 中 `IS_DEMO` 无误后即可启动：

```bash
python main.py
```

### CLI 实用工具
运行过程中可以使用系统自带 CLI 查询本地数据库或交易所情况：
```bash
python cli.py balance              # 账户可用资金
python cli.py positions            # 仓位全览
python cli.py ticker BTC-USDT      # 最新行情
python cli.py orders -s eth_mtf_swap  # 指定策略历史订单
python cli.py signals -s eth_mtf_swap # 信号日志
```

## 数据声明 / 免责
- 投资有风险，实盘前**必须**在 OKX 模拟盘 (`IS_DEMO=true`) 中运行至少一周进行观察！
- 日志文件保存在 `logs/` 目录，自动按天轮转并压缩保留 30 天。
