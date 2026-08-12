# OKX 策略交易与回测系统

基于 Python `asyncio` 的 OKX 量化交易与多时框回测框架，支持现货与永续合约，内置 8 个可插拔策略。

## 功能特性

- **全异步实盘架构**：基于 `asyncio` + WebSocket，低延迟实时行情处理。WS 读取循环与策略回调分离，慢策略不会阻塞收包。
- **本地回测引擎**：多时框对齐回放，策略代码**零修改**即可在实盘与回测间切换。支持部分平仓与加仓，回测行为与实盘一致。
- **多层风控机制**：下单频率限制、单策略日内亏损熔断、全局最大回撤紧急停止。
- **崩溃/重启恢复**：启动时自动接管交易所已有持仓并重建止损，避免重复开仓；开仓单同时挂交易所侧止损，进程挂掉仍受保护。
- **指标自动预热**：启动时拉取历史 K 线完成指标初始化；只用已收盘 K 线驱动指标，实盘与回测口径一致。
- **数据本地缓存**：回测自动拉取 OKX 历史 K 线并缓存 CSV，支持增量更新。
- **可视化与持久化**：SQLite 存储订单与信号（按 `order_id` 幂等），回测输出净值图表与交易 CSV。

## 项目结构

```text
trade/
├── main.py                  # 实盘系统入口
├── cli.py                   # 命令行快捷查询工具
├── gui.py / chart.py        # 桌面看板与实时 K 线图（tkinter + matplotlib）
├── pyproject.toml           # pytest / ruff 配置
├── config/
│   ├── settings.py          # 全局配置（环境变量解析）
│   └── strategies.yaml      # 策略与参数配置文件
├── backtest/                # 回测模块
│   ├── run_backtest.py      # 单策略回测 CLI
│   ├── run_all.py           # 批量并列回测所有策略 CLI
│   ├── engine.py            # 多时框回测引擎与 Mock 对象
│   ├── data_loader.py       # OKX 历史数据拉取与本地 CSV 缓存
│   └── report.py            # 量化指标计算与 matplotlib 绘图
├── engine/
│   ├── strategy_engine.py   # 实盘引擎核心：生命周期、行情路由、订单归属
│   ├── base_strategy.py     # 策略基类：信号执行、仓位计算、持仓接管
│   ├── risk_manager.py      # 风控：频率限制 / 日内亏损 / 回撤熔断
│   └── portfolio.py         # 账户资产与持仓视图
├── gateway/
│   ├── models.py            # 数据模型
│   ├── okx_rest.py          # REST 客户端（超时 / 重试 / 统一异常）
│   ├── okx_ws.py            # WebSocket 客户端（自动重连 / 队列化回调）
│   └── precision.py         # 下单精度：按 lotSz 的 Decimal 取整
├── strategies/              # 策略实现，见下表
├── storage/db.py            # SQLite 数据访问
└── tests/                   # pytest 测试（115 项）
```

## 内置策略

| 策略类 | 模块 | 思路 | 方向 |
|---|---|---|---|
| `RightSideStrategy` | `rightside.py` | EMA 金叉 + MACD>0 + 放量确认；0 轴上方死叉减仓 50%，均线拐头清仓 | 多/空 |
| `MtfTrendStrategy` | `mtftrend.py` | 4H 定方向 + 1H 定偏向 + 15m 找入场的三重共振 | 多/空 |
| `TrendStrategy` | `trend.py` | EMA 金叉死叉 + MACD 确认 + ATR 动态止损 | 多/空 |
| `BbRsiStrategy` | `bbrsi.py` | 布林带 + RSI 均值回归，适合震荡 | 多/空 |
| `DonchianStrategy` | `donchian.py` | 唐奇安通道突破（海龟式） | 多/空 |
| `VwapStrategy` | `vwap.py` | 日内 VWAP 偏离 + RSI 过滤 | 多/空 |
| `GridStrategy` | `grid.py` | 固定区间等分网格，支持合约双向 | 多/空 |
| `PyramidStrategy` | `pyramid.py` | 斐波那契支撑 + ATR 间隔双确认的金字塔加仓 | **仅多** |

> `PyramidStrategy` 是马丁格尔型策略，风险特征与其余策略完全不同，默认关闭。
> 启用前请读下面的[策略优缺点分析](#pyramidstrategy-优缺点分析)。

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt          # 实盘运行
pip install -r requirements-dev.txt      # 开发：额外装 pytest / ruff
```

> 实盘部署的机器不必装 `requirements-dev.txt`，也可以不装 `matplotlib`（仅回测出图和 GUI 需要），能省约 120MB。

### 2. 配置环境与策略

在项目根目录创建 `.env`：

```env
OKX__API_KEY=your_api_key
OKX__SECRET_KEY=your_secret_key
OKX__PASSPHRASE=your_passphrase
OKX__IS_DEMO=true    # true = 模拟盘，false = 实盘

RISK__MAX_DAILY_LOSS_PCT=0.02    # 单策略日内亏损熔断
RISK__MAX_DRAWDOWN_PCT=0.05      # 全局回撤紧急停止
```

⚠️ **风控阈值必须与策略仓位自洽**。单笔止损打满的损失约等于
`position_size_pct × leverage × 止损比例`。例如 `20% × 3x × 10% = 6%`，
若日内亏损上限设为 2%，一次止损就会暂停该策略并触发全局熔断。
启动时若检测到这种不匹配会打印告警。

编辑 `config/strategies.yaml` 启用策略：

```yaml
strategies:
  - name: eth_rightside_swap
    class: RightSideStrategy
    enabled: true
    inst_type: SWAP
    symbol: ETH-USDT-SWAP
    config:
      timeframe: "1H"
      position_size_pct: 0.2
      leverage: 3
      # ... 详见 yaml 内注释
```

---

## 回测 (Backtesting)

无需修改策略代码，直接用历史数据模拟撮合：

```bash
# 单策略；结束后在 --out-dir 生成走势图与 CSV 交易记录
python -m backtest.run_backtest --strategy eth_rightside_swap --capital 10000 --max-bars 15000 --out-dir backtest_results

# 批量对比所有策略
python -m backtest.run_all --capital 10000 --max-bars 20000 --out-dir backtest_results
```

> 首次回测会自动从 OKX 拉取并缓存历史 K 线，加 `--force-download` 可强制刷新。

报告里的「平仓腿数」统计的是产生已实现盈亏的腿（清仓 / 减仓 / 止损），
开仓不计入；括号内标注其中有多少是减仓。

---

## 实盘运行 (Live Trading)

```bash
python main.py
```

### CLI 实用工具

```bash
python cli.py balance                     # 账户可用资金
python cli.py positions                   # 仓位全览
python cli.py ticker BTC-USDT             # 最新行情
python cli.py orders -s eth_rightside_swap   # 指定策略历史订单
python cli.py signals -s eth_rightside_swap  # 信号日志
python cli.py pnl --days 7                # 每日盈亏统计
```

### 部署资源

实盘进程实测约 **50~80MB 内存**、CPU 基本空闲（1 核 1G 的机器绰绰有余）。
回测峰值约 87MB（不出图）/ 160MB（出图），建议放开发机跑。

---

## 开发

```bash
pytest                # 115 项测试
ruff check .          # 静态检查
ruff check . --fix
```

测试覆盖的是容易出错、且出错代价高的地方：指标只吃收盘 K 线、平仓数量、
风控熔断、订单幂等落库、REST 重试策略、WS 不阻塞读取循环等。
`tests/test_gateway.py` 会起一个本地 aiohttp 服务端测真实的超时与重试行为。

---

<a id="pyramidstrategy-优缺点分析"></a>
## PyramidStrategy 优缺点分析

该策略移植自 `gemini.py` 草稿。核心逻辑：先建底仓，价格下跌时在
「触及斐波那契支撑位」且「距上次成交 ≥ 1.2×ATR」双条件满足时加仓，
每档保证金按 1.3 倍递增，出场只有阶梯止盈或全局硬止损两种。

### 优点

1. **ATR 最小间隔是真正有价值的设计**。纯固定网格 DCA 在急跌时会把所有子弹
   打在很窄的价格区间里，加权平均成本几乎没被拉低。要求两次加仓至少间隔
   1.2 个 ATR，能让加仓点随波动率自适应地摊开。
2. **双重确认比单一条件更克制**。只在技术支撑位附近加仓，避免了在无支撑的
   自由落体段不断接刀。
3. **保证金表预先算好且总和恰好为 1**。`w1 = (1-r)/(1-rⁿ)` 保证加满 6 档正好
   用尽预算，不会像很多马丁实现那样出现"最后一档钱不够"。
4. **有全局硬止损**。多数马丁格尔策略压根不设止损，这一点已经好过平均水平。
5. **阶梯止盈承认了不同仓位状态需要不同目标**。

### 缺点与风险

1. **本质是马丁格尔：胜率高、单次亏损大**。收益曲线会长期呈现"稳步小赚"，
   然后在一次单边下跌中回吐掉几十次盈利。回测若不覆盖一轮真正的熊市，
   结果会严重高估。
2. **止损的有效性随档位反向变化**。硬止损按「动用资金的 15%」计，是个固定
   USDT 金额。建仓初期仓位小，需要价格跌非常多才触发（几乎等于没有止损）；
   加满后仓位大，很小的波动就触发。风险控制的强度恰好和暴露程度相反。
3. **止盈目标随档位递增，与逃生需求相反**。`tp_schedule` 从 1.5% 递增到 5.0%，
   意味着仓位越重、越被套，反而要求价格反弹得越多才肯离场。多数 DCA 实现
   在这里是递减的。
4. **首次建仓无条件市价买入**。支撑位、ATR 这些约束只作用于加仓，而决定
   整轮成本的底仓完全不看位置。这是原始草稿的设计，本实现保持一致——
   如果要改进，第一步应该是给首仓加趋势过滤（如 4H EMA 排列向上才开仓）。
5. **只做多，没有趋势过滤**。在持续下行的市场里，它会用尽 6 档然后满仓等待。
6. **手续费与资金费不可忽视**。一轮完整周期是 6 次开仓 + 1 次平仓，taker 单
   在递增的名义价值上累积；永续合约持仓数天的资金费也会侵蚀本就不高的
   止盈目标（首档只有 1.5%）。

### 本实现相对草稿做的改动

原草稿是 `while True` + `time.sleep(3)` 的轮询脚本，状态全在局部变量里。
移植到本框架时修正了几处会直接导致事故的问题：

| 问题 | 草稿行为 | 本实现 |
|---|---|---|
| 支撑位只算一次 | 建仓时算好后永不刷新，行情走远后阶梯失效 | 空仓时随 4H 收盘持续刷新；建仓后冻结，避免阶梯在脚下移动 |
| 重启丢失状态 | `current_step` 归零，会在已有持仓上再开一轮 | 启动时接管持仓，档位未知则按已加满处理，不再加仓 |
| 止损检查频率 | 依赖 3 秒轮询 ticker | 主时框（15m）每根收盘 K 线检查，高时框只负责算指标 |
| 加仓预算受浮亏影响 | 未涉及 | 建仓时快照动用资金，后续档位预算不随权益缩水 |
| 异常处理 | `except Exception: sleep(5)` 无限吞异常 | 网关层统一 `OKXError`，下单失败回滚本地状态 |

### 一段实测数据

BTC-USDT-SWAP，5000 根 15m（约 2 个月），初始 10000 USDT，默认参数：

```
总收益率    +1.25%        最大回撤   -11.45%
胜率        92.86%        Sharpe      0.31
平均盈利    93.23 USDT    平均亏损   1029.20 USDT
```

**一次亏损约等于 11 次盈利**——这就是上面第 1 条说的形态，而且这段区间还没
包含真正的单边下跌。看总收益率是赚的，看盈亏结构就知道这个正收益有多脆弱。

> 建议：先用 `capital_pct` 限制本策略只动用一部分权益（默认 0.5），
> 并且**务必跑一段包含 2022 年那种单边下跌的回测**再考虑实盘。

---

## 数据声明 / 免责

- 投资有风险，实盘前**必须**在 OKX 模拟盘 (`IS_DEMO=true`) 中运行至少一周观察。
- 日志保存在 `logs/`，按天轮转、压缩保留 30 天。
- 首次启动会自动升级数据库结构；若检测到旧版本遗留的重复订单记录，
  会先把整表备份为 `orders_backup_<时间戳>` 再去重。
