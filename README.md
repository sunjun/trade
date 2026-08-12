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
└── tests/                   # pytest 测试（132 项）
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
pytest                # 132 项测试
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

### 已做的改进

原草稿有三处结构性问题，都已在本实现中修正：

**1. 止损强度与暴露程度相反 → 改为「结构失效价 + 风险反推仓位」**

原设计的硬止损是固定 USDT 金额（动用资金的 15%），换算成价格：

| 档位 | 名义价值 | 触发止损所需跌幅 |
|---|---|---|
| 第 1 档 | 1960 | **38.3%**（等于没有止损） |
| 第 6 档 | 25000 | **3.0%**（一两天的正常波动就打掉） |

现在改为：止损价由市场结构决定（`最低支撑 − stop_atr_mult × ATR`，跌破即
「支撑会撑住」这个前提失效），再由「加满档且打到止损时恰好亏掉 `risk_pct`」
反解出各档张数：

```
Σ wᵢ·Q·(entryᵢ − stop_price)·ct_val = 权益 × risk_pct
```

计划中的加仓价就是支撑位本身，所以有闭式解。于是**最坏情况在开仓前就是
已知且有上界的**，且任何更早的档位亏损都严格小于预算——保护强度随暴露单调。
若反解出的满档名义杠杆超过 `max_leverage`，说明止损离得太远，这一轮直接放弃。

**2. 止盈目标递增 → 改为递减 + 分批减仓 + 时间止损**

目标是相对持仓均价的，而均价始终在当前价之上，越加越明显：

| | 当前价 | 均价 | 目标价 | 需要反弹 |
|---|---|---|---|---|
| 第 1 档 | 100 | 100 | 101.5 | +1.5% |
| 第 6 档（原递增梯度） | 80 | 86.6 | 90.9 | **+13.7%** |

三处改动：`tp_schedule` 改为递减（深档只求逃出来）；`partial_tp` 默认开启，
反弹时先平**最深一档**立刻降暴露，而不是死等一个大目标全平；新增
`max_hold_bars`，满档滞留超时强制离场——马丁的真正死法是被困住，
这给暴露时间加了上界。

**3. 首仓无条件市价买入 → 趋势过滤 + 等回调**

`trend_filter`：高时框均线向上才允许开新一轮，把「在单边下跌里加满档」这个
最坏场景排除掉；`first_entry_at_support`：首仓也要等价格回调到第一道支撑，
让它和加仓用同一套结构依据。

### 仍然存在的风险

1. **本质仍是马丁格尔**：胜率高、左尾厚。上述改造让最坏情况可计算，
   但没有、也无法改变这个分布形状。
2. **只做多**。趋势过滤会挡掉大部分下跌行情，但不能保证开仓后趋势不反转。
3. **手续费与资金费**：一轮最多 6 次开仓 + 若干次减仓，永续持仓数天的
   资金费会侵蚀本就不高的深档止盈目标。
4. **过滤器让交易频率大幅下降**，样本变少，回测结论的统计意义随之减弱。

### 实测数据

ETH-USDT-SWAP，20065 根 15m（2026-01-15 → 2026-08-12，约 7 个月），
初始 10000 USDT，默认参数：

```
总收益率    +1.78%        最大回撤    -1.38%
平仓腿数    34（含 19 次减仓）        胜率  94.12%
平均盈利    9.06 USDT     平均亏损   58.31 USDT
```

与改造前（BTC 2 个月）对比：最大回撤从 **−11.45% 降到 −1.38%**，
单次亏损/单次盈利从 **11 倍降到 6.4 倍**。代价是仓位小了一个量级，
绝对收益也随之变小——**这正是「用收益换可计算的最坏情况」的取舍**。

> 提醒：这段区间仍不含真正的单边下跌。OKX 的 `history-candles` 接口对
> 15m 只能回溯到 2026-01 左右；要验证熊市表现，需要用更高时框
> （把 `timeframe` 调成 `1H`/`4H`）来换取更长的历史跨度。
> 另注意：缓存存在时 `--max-bars` 调大不会向历史回填，需加 `--force-download`。

---

## 最大使用资金怎么设

三个层次，从外到内：

### 1. 全局硬闸门（所有策略共用）

`.env` 里的 `RISK__MAX_POSITION_PCT` 限制**单个品种的名义价值**上限：

```env
RISK__MAX_POSITION_PCT=0.3    # 单品种名义价值 <= 权益 × 30%
```

不管策略自己算出多大的仓位，开仓腿都会按这个上限截断（含已有持仓）；
额度用尽则跳过该单。平仓腿**不受限**——否则会平不干净留下残仓。
设为 `0` 表示关闭该闸门。

⚠️ 默认值是 `0.1`。如果策略配的是 `position_size_pct: 0.2` + `leverage: 3`
（意图 60% 名义），会被截断成 10%，**实盘表现将与回测不一致**。
启动时会打印告警，要么调高上限，要么调低策略仓位。

### 2. 策略级仓位（趋势类策略）

`position_size_pct × leverage` = 单次开仓占权益的名义比例。
例如 `0.2 × 3 = 60%`。

### 3. 策略级风险预算（PyramidStrategy）

金字塔不按「用多少钱」配置，而按「最多亏多少」配置：

```yaml
risk_pct: 0.02       # 单轮最大可接受亏损 = 权益的 2%
max_leverage: 5      # 反解出的满档名义杠杆上限，超过则放弃该轮
```

实际用多少资金是**结果**而不是输入——止损越近，同样的风险预算能买越多；
止损越远，仓位自动越小。`max_leverage` 是这个结果的上限。

> 这三层是叠加的：策略先按自己的规则算，再被 `max_position_pct` 截断。
> 想彻底控制敞口，最简单的做法是把 `RISK__MAX_POSITION_PCT` 设成你能接受的
> 单品种最大敞口，然后让各策略在这个框内自由发挥。

---

## 数据声明 / 免责

- 投资有风险，实盘前**必须**在 OKX 模拟盘 (`IS_DEMO=true`) 中运行至少一周观察。
- 日志保存在 `logs/`，按天轮转、压缩保留 30 天。
- 首次启动会自动升级数据库结构；若检测到旧版本遗留的重复订单记录，
  会先把整表备份为 `orders_backup_<时间戳>` 再去重。
