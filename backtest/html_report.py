"""回测的可交互 HTML 报告

matplotlib 那张 PNG 只能看个大概轮廓：点密了糊成一片，看不出某个标记是哪一笔、
为什么开、赚了多少。这里把同一份数据渲染成自包含的 HTML——价格线上标出每次开平仓，
悬停看明细，下方按「回合」（从建仓到清仓）列出理由与盈亏，点一行就定位到图上。

产物不引用任何外部资源（数据以 JSON 内联），拷到哪都能打开。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from backtest.engine import TradeRecord
from config.tz import to_cn

# 动作分类：开仓 / 平仓，以及是否为止损离场
_OPEN_ACTIONS = ("open_long", "open_short")


def _is_open(action: str) -> bool:
    return action in _OPEN_ACTIONS


def _is_stop(action: str) -> bool:
    return "sl" in action


@dataclass
class Round:
    """一个完整回合：从空仓建仓，到再次回到空仓。"""
    idx: int
    side: str                 # long / short
    open_ts: datetime
    close_ts: datetime | None
    entry_price: float        # 加权平均开仓价
    exit_price: float | None  # 加权平均平仓价
    contracts: float          # 累计开出的张数
    pnl: float
    adds: int                 # 加码次数（不含首仓）
    exit_kind: str            # stop / signal / open（未平）
    open_reason: str
    close_reason: str


def group_rounds(trades: list[TradeRecord]) -> list[Round]:
    """把逐笔记录合并成回合。

    仓位可能分多次建、多次减，只有净张数回到 0 才算一个回合结束。
    未平仓的尾巴也会作为一个 exit_kind="open" 的回合返回，否则最后一段
    行情在报告里会凭空消失。
    """
    rounds: list[Round] = []
    opens: list[TradeRecord] = []
    closes: list[TradeRecord] = []
    net = 0.0

    def flush() -> None:
        if not opens:
            return
        oc = sum(t.contracts for t in opens)
        cc = sum(t.contracts for t in closes)
        entry = sum(t.price * t.contracts for t in opens) / oc if oc else 0.0
        exit_px = sum(t.price * t.contracts for t in closes) / cc if cc else None
        last = closes[-1] if closes else None
        rounds.append(Round(
            idx=len(rounds) + 1,
            side="long" if "long" in opens[0].action else "short",
            open_ts=opens[0].ts,
            close_ts=last.ts if last else None,
            entry_price=entry,
            exit_price=exit_px,
            contracts=oc,
            pnl=sum(t.pnl for t in closes),
            adds=len(opens) - 1,
            exit_kind=("open" if last is None
                       else "stop" if _is_stop(last.action) else "signal"),
            open_reason=opens[0].reason,
            close_reason=last.reason if last else "",
        ))

    for t in trades:
        if _is_open(t.action):
            if net <= 1e-12 and (opens or closes):
                flush()
                opens, closes = [], []
            opens.append(t)
            net += t.contracts
        else:
            closes.append(t)
            net -= t.contracts
            if net <= 1e-12:
                flush()
                opens, closes = [], []
                net = 0.0
    flush()
    return rounds


def _build_payload(
    candles: list,
    trades: list[TradeRecord],
    equity_curve: list[float],
    equity_ts: list[datetime],
    strategy_name: str,
    initial_capital: float,
    metrics: dict | None,
) -> dict:
    def ms(dt: datetime) -> int:
        """输出「东八区墙上时间」对应的 epoch。

        前端一律用 getUTC*() 读这个值，于是显示出来就是东八区，且不受打开
        报告的那台机器的本地时区影响——直接传真 epoch 会按浏览器时区渲染。
        """
        return int(to_cn(dt).replace(tzinfo=UTC).timestamp() * 1000)

    rounds = group_rounds(trades)
    # 权益曲线首元素是初始资金、没有对应的 K 线时间戳，长度差 1 是正常的
    eq_ts = list(equity_ts)
    eq = list(equity_curve)
    if len(eq) == len(eq_ts) + 1:
        eq = eq[1:]
    n = min(len(eq), len(eq_ts))

    return {
        "strategy": strategy_name,
        "initialCapital": initial_capital,
        "metrics": metrics or {},
        "price": {
            "t": [ms(c.ts) for c in candles],
            "c": [round(c.close, 4) for c in candles],
            "h": [round(c.high, 4) for c in candles],
            "l": [round(c.low, 4) for c in candles],
        },
        "equity": {
            "t": [ms(d) for d in eq_ts[:n]],
            "v": [round(v, 2) for v in eq[:n]],
        },
        "marks": [
            {
                "t": ms(t.ts),
                "p": round(t.price, 4),
                "open": _is_open(t.action),
                "stop": _is_stop(t.action),
                "side": "long" if "long" in t.action else "short",
                "qty": round(t.contracts, 4),
                "pnl": round(t.pnl, 2),
                "reason": t.reason,
            }
            for t in trades
        ],
        "rounds": [
            {
                "i": r.idx,
                "side": r.side,
                "t0": ms(r.open_ts),
                "t1": ms(r.close_ts) if r.close_ts else None,
                "entry": round(r.entry_price, 4),
                "exit": round(r.exit_price, 4) if r.exit_price is not None else None,
                "qty": round(r.contracts, 4),
                "pnl": round(r.pnl, 2),
                "adds": r.adds,
                "kind": r.exit_kind,
                "why": r.open_reason,
                "whyOut": r.close_reason,
            }
            for r in rounds
        ],
    }


def export_html_report(
    candles: list,
    trades: list[TradeRecord],
    equity_curve: list[float],
    equity_ts: list[datetime],
    strategy_name: str = "",
    initial_capital: float = 10_000.0,
    metrics: dict | None = None,
    output_path: str = "backtest_report.html",
) -> str:
    if not candles:
        logger.warning("没有 K 线数据，跳过 HTML 报告")
        return ""

    payload = _build_payload(candles, trades, equity_curve, equity_ts,
                             strategy_name, initial_capital, metrics)
    html = _TEMPLATE.replace(
        "__PAYLOAD__", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    ).replace("__TITLE__", strategy_name or "回测报告")

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    logger.info(f"HTML 报告 → {path}  （{len(payload['rounds'])} 个回合）")
    return str(path)



_TEMPLATE = r"""<title>__TITLE__</title>
<style>
/* 明色为基准，两处重定义只改 token：系统深色（未标记 data-theme）与显式深色 */
:root{
  --paper:#f3f4f5; --panel:#fbfbfc; --sunk:#eceef0;
  --ink:#17191c; --muted:#6c7076; --faint:#9aa0a6;
  --rule:#dfe2e5; --grid:#e8eaed;
  --up:#0f8a5f; --down:#c0392e; --accent:#a8701a; --accent-soft:#f0e2c9;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --paper:#101215; --panel:#181b1f; --sunk:#1e2126;
    --ink:#e6e8ea; --muted:#8d939a; --faint:#6a7077;
    --rule:#282c32; --grid:#22262b;
    --up:#35b483; --down:#e06a5c; --accent:#dfa445; --accent-soft:#3a2f1c;
  }
}
:root[data-theme="dark"]{
  --paper:#101215; --panel:#181b1f; --sunk:#1e2126;
  --ink:#e6e8ea; --muted:#8d939a; --faint:#6a7077;
  --rule:#282c32; --grid:#22262b;
  --up:#35b483; --down:#e06a5c; --accent:#dfa445; --accent-soft:#3a2f1c;
}

:root{
  --sans:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",
         "PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono","JetBrains Mono",
         Menlo,Consolas,"Liberation Mono",monospace;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.55 var(--sans)}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
.wrap{max-width:1200px;margin:0 auto;padding:34px 24px 72px;display:flex;
  flex-direction:column;gap:22px}

/* ── 报头 ─────────────────────────────────────────────────────────── */
.mast{display:flex;flex-wrap:wrap;align-items:baseline;gap:0 14px;
  padding-bottom:14px;border-bottom:2px solid var(--ink)}
.mast h1{margin:0;font-size:21px;font-weight:640;letter-spacing:-.01em;
  font-family:var(--mono)}
.mast .period{color:var(--muted);font-size:12.5px;font-family:var(--mono)}
.eyebrow{font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;
  color:var(--faint);font-weight:600}

/* ── 指标条：细线分隔，不用圆角卡片 ───────────────────────────────── */
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));
  border:1px solid var(--rule);border-radius:2px;background:var(--panel);
  overflow:hidden}
.cell{padding:12px 14px;border-right:1px solid var(--rule);
  border-bottom:1px solid var(--rule)}
.cell .k{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;
  color:var(--faint);font-weight:600}
.cell .v{font-size:19px;font-weight:560;margin-top:4px;font-family:var(--mono);
  font-variant-numeric:tabular-nums;letter-spacing:-.02em}

/* ── 面板 ─────────────────────────────────────────────────────────── */
.panel{background:var(--panel);border:1px solid var(--rule);border-radius:2px}
.phead{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;
  padding:10px 14px;border-bottom:1px solid var(--rule)}
.phead .eyebrow{margin-right:auto}
.hint{color:var(--muted);font-size:11.5px}
.legend{display:flex;gap:13px;align-items:center;color:var(--muted);font-size:11.5px}
.legend i{display:inline-block;width:9px;height:9px;margin-right:4px;
  vertical-align:-1px}
.legend .tri{width:0;height:0;border-left:5px solid transparent;
  border-right:5px solid transparent;border-bottom:8px solid var(--up)}
.legend .dot{border-radius:50%;border:1.6px solid var(--up);background:var(--panel)}
.legend .xx{border-radius:50%;border:2px solid var(--down);background:var(--panel)}
canvas{display:block;width:100%;cursor:crosshair;touch-action:none}

/* ── 工具提示 ─────────────────────────────────────────────────────── */
#tip{position:fixed;pointer-events:none;z-index:20;max-width:300px;
  background:var(--panel);color:var(--ink);border:1px solid var(--rule);
  border-left:3px solid var(--accent);border-radius:2px;padding:9px 11px;
  font-size:12px;line-height:1.5;box-shadow:0 8px 28px rgb(0 0 0 / .18);
  opacity:0;transition:opacity .09s linear}
#tip .t{font-family:var(--mono);color:var(--muted);font-size:11px}
#tip .why{color:var(--muted);display:block;margin-top:4px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;
  scroll-behavior:auto!important;animation:none!important}}

/* ── 过滤器 ───────────────────────────────────────────────────────── */
.filters{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
button{font:inherit;font-size:12.5px;background:transparent;color:var(--muted);
  border:1px solid var(--rule);border-radius:2px;padding:4px 11px;cursor:pointer}
button:hover{color:var(--ink);border-color:var(--faint)}
button[aria-pressed="true"]{background:var(--ink);color:var(--paper);
  border-color:var(--ink)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.tally{color:var(--muted);font-size:12px;font-family:var(--mono)}

/* ── 回合账 ───────────────────────────────────────────────────────── */
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:860px;font-size:13px}
th,td{padding:7px 12px;text-align:right;white-space:nowrap;
  border-bottom:1px solid var(--rule)}
th{position:sticky;top:0;z-index:2;background:var(--panel);cursor:pointer;
  user-select:none;font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--faint);font-weight:600;border-bottom:1px solid var(--ink)}
th:hover{color:var(--ink)}
th[aria-sort]{color:var(--accent)}
th.l,td.l{text-align:left}
td.why{white-space:normal;min-width:250px;color:var(--muted);font-size:12px;
  font-family:var(--mono)}
tbody tr{cursor:pointer}
tbody tr:hover{background:var(--sunk)}
tbody tr.on{background:var(--accent-soft)}
tbody tr.on td:first-child{box-shadow:inset 3px 0 0 var(--accent)}
/* 盈亏列自带轻重条：颜色之外再给一层形，扫读时不必逐个读数 */
td.pnl{position:relative}
td.pnl span{position:relative;z-index:1}
td.pnl::before{content:"";position:absolute;right:0;top:3px;bottom:3px;
  width:var(--w,0);background:currentColor;opacity:.13}
.pos{color:var(--up)} .neg{color:var(--down)}
.kind{font-size:11px;color:var(--muted);font-family:var(--mono)}
.kind.stop{color:var(--down)}
</style>

<div class="wrap">
  <header class="mast">
    <h1 id="ttl"></h1>
    <span class="period num" id="sub"></span>
  </header>

  <section class="strip" id="strip"></section>

  <section class="panel">
    <div class="phead">
      <span class="eyebrow">价格与成交</span>
      <span class="legend">
        <span><i class="tri"></i>开仓</span>
        <span><i class="dot"></i>平仓</span>
        <span><i class="xx"></i>止损</span>
      </span>
      <span class="hint">滚轮缩放 · 拖拽平移 · 双击复位</span>
    </div>
    <canvas id="px" height="390"></canvas>
  </section>

  <section class="panel">
    <div class="phead">
      <span class="eyebrow">权益曲线</span>
      <span class="hint">阴影为距前高的回撤，虚线为初始资金</span>
    </div>
    <canvas id="eq" height="176"></canvas>
  </section>

  <section class="panel">
    <div class="phead">
      <span class="eyebrow">交易回合</span>
      <span class="filters">
        <button id="f-all" aria-pressed="true">全部</button>
        <button id="f-win">盈利</button>
        <button id="f-loss">亏损</button>
        <button id="f-stop">止损离场</button>
      </span>
      <span class="tally" id="cnt"></span>
    </div>
    <div class="tablewrap">
      <table>
        <thead><tr>
          <th class="l" data-k="i">#</th>
          <th class="l" data-k="t0">开仓时间</th>
          <th class="l" data-k="side">方向</th>
          <th data-k="entry">开仓价</th>
          <th data-k="exit">平仓价</th>
          <th data-k="qty">张数</th>
          <th data-k="adds">加码</th>
          <th data-k="pnl">盈亏 USDT</th>
          <th class="l" data-k="kind">离场</th>
          <th class="l">开仓理由</th>
        </tr></thead>
        <tbody id="tb"></tbody>
      </table>
    </div>
  </section>
</div>
<div id="tip" role="status"></div>

<script>
const D = __PAYLOAD__;
const tip = document.getElementById('tip');
const css = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
const fmt = (n, d = 2) => n == null ? '—'
  : n.toLocaleString('zh-CN', {minimumFractionDigits: d, maximumFractionDigits: d});
const sgn = (n, d = 2) => (n >= 0 ? '+' : '') + fmt(n, d);
// 时间戳已带 +8 偏移，用 getUTC* 读出来就是东八区，与打开报告的机器时区无关
const dt = ms => { const d = new Date(ms), p = x => String(x).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} `
       + `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`; };
const day = ms => dt(ms).slice(0, 10);

const T = D.price.t;
document.getElementById('ttl').textContent = D.strategy;
document.getElementById('sub').textContent =
  `${day(T[0])} → ${day(T[T.length - 1])}   ${T.length} 根 K 线   ${D.rounds.length} 个回合   UTC+8`;

/* ── 指标条 ───────────────────────────────────────────────────────── */
const wins = D.rounds.filter(r => r.pnl > 0);
const losses = D.rounds.filter(r => r.pnl <= 0 && r.kind !== 'open');
const sum = a => a.reduce((s, r) => s + r.pnl, 0);
const m = D.metrics || {};
const eqV = D.equity.v;
const ret = m.total_return_pct ?? ((eqV[eqV.length - 1] ?? D.initialCapital) / D.initialCapital - 1) * 100;
const ann = m.annual_return_pct;

document.getElementById('strip').innerHTML = [
  ['总收益', sgn(ret) + '%', ret >= 0 ? 'pos' : 'neg'],
  ['年化', ann != null ? sgn(ann) + '%' : '—', (ann ?? 0) >= 0 ? 'pos' : 'neg'],
  ['最大回撤', m.max_drawdown_pct != null ? fmt(m.max_drawdown_pct) + '%' : '—', 'neg'],
  ['Sharpe', m.sharpe != null ? fmt(m.sharpe) : '—', ''],
  ['盈亏比', fmt(m.profit_factor ?? sum(wins) / Math.abs(sum(losses) || 1)), ''],
  ['回合胜率', fmt(wins.length / Math.max(1, wins.length + losses.length) * 100, 1) + '%', ''],
  ['平均盈利', sgn(wins.length ? sum(wins) / wins.length : 0), 'pos'],
  ['平均亏损', fmt(losses.length ? sum(losses) / losses.length : 0), 'neg'],
].map(([k, v, c]) => `<div class="cell"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join('');

/* ── 视图状态（两图共享 x 轴）─────────────────────────────────────── */
let lo = 0, hi = T.length - 1, focus = null, vis = [];
const pxc = document.getElementById('px'), pxx = pxc.getContext('2d');
const eqc = document.getElementById('eq'), eqx = eqc.getContext('2d');
const PAD = {l: 10, r: 66, t: 12, b: 24};
const MONO = () => '11px ' + css('--mono');

function setup(cv, ctx){
  const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = +cv.getAttribute('height');
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)){
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    cv.style.height = h + 'px';
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return {w, h};
}
const xOf = (i, w) => PAD.l + (i - lo) / Math.max(1, hi - lo) * (w - PAD.l - PAD.r);
const iOf = (x, w) => Math.round(lo + (x - PAD.l) / (w - PAD.l - PAD.r) * (hi - lo));
function bisect(ms){
  let a = 0, b = T.length - 1;
  while (a < b){ const k = (a + b) >> 1; T[k] < ms ? a = k + 1 : b = k; }
  return a;
}

function axes(ctx, w, h, min, max, dec){
  ctx.font = MONO(); ctx.lineWidth = 1;
  ctx.strokeStyle = css('--grid'); ctx.fillStyle = css('--faint');
  ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  for (let g = 0; g <= 4; g++){
    const y = Math.round(PAD.t + g * (h - PAD.t - PAD.b) / 4) + .5;
    ctx.beginPath(); ctx.moveTo(PAD.l, y); ctx.lineTo(w - PAD.r, y); ctx.stroke();
    ctx.fillText(fmt(max - (max - min) * g / 4, dec), w - PAD.r + 8, y);
  }
  ctx.textBaseline = 'top';
  for (let g = 0; g <= 4; g++){
    const i = Math.round(lo + (hi - lo) * g / 4);
    // 首尾贴边对齐，居中会被面板裁掉半截日期
    ctx.textAlign = g === 0 ? 'left' : g === 4 ? 'right' : 'center';
    ctx.fillText(day(T[i]), xOf(i, w), h - PAD.b + 6);
  }
}

function drawPrice(){
  const {w, h} = setup(pxc, pxx);
  let min = Infinity, max = -Infinity;
  for (let i = lo; i <= hi; i++){
    if (D.price.l[i] < min) min = D.price.l[i];
    if (D.price.h[i] > max) max = D.price.h[i];
  }
  const pad = (max - min) * 0.045 || 1; min -= pad; max += pad;
  const yOf = p => PAD.t + (max - p) / (max - min) * (h - PAD.t - PAD.b);
  axes(pxx, w, h, min, max, 0);

  pxx.strokeStyle = css('--ink'); pxx.globalAlpha = .5; pxx.lineWidth = 1.1;
  pxx.lineJoin = 'round'; pxx.beginPath();
  const step = Math.max(1, Math.floor((hi - lo) / 2400));
  for (let i = lo; i <= hi; i += step){
    const x = xOf(i, w), y = yOf(D.price.c[i]);
    i === lo ? pxx.moveTo(x, y) : pxx.lineTo(x, y);
  }
  pxx.stroke(); pxx.globalAlpha = 1;

  vis = [];
  const t0 = T[lo], t1 = T[hi], panel = css('--panel');
  for (const k of D.marks){
    if (k.t < t0 || k.t > t1) continue;
    const x = xOf(bisect(k.t), w), y = yOf(k.p);
    vis.push({x, y, k});
    pxx.globalAlpha = (focus && (k.t < focus.t0 || k.t > (focus.t1 ?? Infinity))) ? .16 : 1;
    if (k.open){
      pxx.fillStyle = k.side === 'long' ? css('--up') : css('--down');
      const s = 5, d = k.side === 'long' ? -1 : 1;
      pxx.beginPath();
      pxx.moveTo(x, y + d * s * 1.1);
      pxx.lineTo(x - s, y - d * s * .75);
      pxx.lineTo(x + s, y - d * s * .75);
      pxx.closePath(); pxx.fill();
    } else {
      const col = k.pnl >= 0 ? css('--up') : css('--down');
      pxx.strokeStyle = col; pxx.fillStyle = panel; pxx.lineWidth = k.stop ? 2 : 1.5;
      pxx.beginPath(); pxx.arc(x, y, 4.2, 0, 6.2832); pxx.fill(); pxx.stroke();
      if (k.stop){
        pxx.lineWidth = 1.3; pxx.beginPath();
        pxx.moveTo(x - 2.1, y - 2.1); pxx.lineTo(x + 2.1, y + 2.1);
        pxx.moveTo(x + 2.1, y - 2.1); pxx.lineTo(x - 2.1, y + 2.1);
        pxx.stroke();
      }
    }
    pxx.globalAlpha = 1;
  }
}

function drawEquity(){
  const {w, h} = setup(eqc, eqx);
  const E = D.equity, t0 = T[lo], t1 = T[hi];
  let a = 0, b = E.t.length - 1;
  while (a < E.t.length && E.t[a] < t0) a++;
  while (b > 0 && E.t[b] > t1) b--;
  if (b <= a) return;
  let min = Infinity, max = -Infinity;
  for (let i = a; i <= b; i++){ if (E.v[i] < min) min = E.v[i]; if (E.v[i] > max) max = E.v[i]; }
  const pad = (max - min) * 0.06 || 1; min -= pad; max += pad;
  const yOf = v => PAD.t + (max - v) / (max - min) * (h - PAD.t - PAD.b);
  const xE = i => PAD.l + (E.t[i] - t0) / Math.max(1, t1 - t0) * (w - PAD.l - PAD.r);
  axes(eqx, w, h, min, max, 0);

  // 回撤带：前高与当前之间填色，回撤深浅一眼可见
  let peak = -Infinity; const pk = [];
  for (let i = a; i <= b; i++){ peak = Math.max(peak, E.v[i]); pk.push(peak); }
  eqx.fillStyle = css('--down'); eqx.globalAlpha = .12; eqx.beginPath();
  for (let i = a; i <= b; i++){ const x = xE(i), y = yOf(E.v[i]); i === a ? eqx.moveTo(x, y) : eqx.lineTo(x, y); }
  for (let i = b; i >= a; i--) eqx.lineTo(xE(i), yOf(Math.min(pk[i - a], max)));
  eqx.closePath(); eqx.fill(); eqx.globalAlpha = 1;

  if (D.initialCapital > min && D.initialCapital < max){
    eqx.strokeStyle = css('--faint'); eqx.setLineDash([3, 4]); eqx.lineWidth = 1;
    const y = Math.round(yOf(D.initialCapital)) + .5;
    eqx.beginPath(); eqx.moveTo(PAD.l, y); eqx.lineTo(w - PAD.r, y); eqx.stroke();
    eqx.setLineDash([]);
  }
  eqx.strokeStyle = css('--accent'); eqx.lineWidth = 1.7; eqx.lineJoin = 'round';
  eqx.beginPath();
  for (let i = a; i <= b; i++){ const x = xE(i), y = yOf(E.v[i]); i === a ? eqx.moveTo(x, y) : eqx.lineTo(x, y); }
  eqx.stroke();
  // 收尾点：曲线终值是读者最先找的数
  eqx.fillStyle = css('--accent');
  eqx.beginPath(); eqx.arc(xE(b), yOf(E.v[b]), 3, 0, 6.2832); eqx.fill();
}

const draw = () => { drawPrice(); drawEquity(); };
draw();
addEventListener('resize', draw);
matchMedia('(prefers-color-scheme:dark)').addEventListener('change', draw);

/* ── 缩放 / 平移 ──────────────────────────────────────────────────── */
pxc.addEventListener('wheel', e => {
  e.preventDefault();
  const w = pxc.clientWidth, c = iOf(e.offsetX, w);
  const span = Math.round(Math.max(30, Math.min(T.length - 1, (hi - lo) * (e.deltaY > 0 ? 1.25 : .8))));
  const r = (c - lo) / Math.max(1, hi - lo);
  lo = Math.max(0, Math.min(T.length - 1 - span, Math.round(c - span * r)));
  hi = lo + span;
  draw();
}, {passive: false});

let drag = null;
pxc.addEventListener('pointerdown', e => {
  drag = {x: e.offsetX, lo, hi}; pxc.setPointerCapture(e.pointerId);
});
pxc.addEventListener('pointerup', () => drag = null);
pxc.addEventListener('pointercancel', () => drag = null);
pxc.addEventListener('pointerleave', () => tip.style.opacity = 0);
pxc.addEventListener('dblclick', () => {
  lo = 0; hi = T.length - 1; focus = null;
  document.querySelectorAll('tbody tr.on').forEach(r => r.classList.remove('on'));
  draw();
});
pxc.addEventListener('pointermove', e => {
  const w = pxc.clientWidth;
  if (drag){
    const span = drag.hi - drag.lo;
    const d = Math.round((drag.x - e.offsetX) / (w - PAD.l - PAD.r) * span);
    lo = Math.max(0, Math.min(T.length - 1 - span, drag.lo + d)); hi = lo + span;
    draw(); return;
  }
  let best = null, bd = 1e9;
  for (const v of vis){
    const d = Math.hypot(v.x - e.offsetX, v.y - e.offsetY);
    if (d < bd){ bd = d; best = v; }
  }
  if (best && bd < 15){
    const k = best.k;
    const head = k.open ? (k.side === 'long' ? '开多' : '开空')
                        : (k.stop ? '止损离场' : '信号离场');
    tip.innerHTML = `<strong>${head}</strong> <span class="t">${dt(k.t)}</span><br>`
      + `<span class="t">${fmt(k.p)} × ${k.qty} 张</span>`
      + (k.open ? '' : ` <span class="${k.pnl >= 0 ? 'pos' : 'neg'}">${sgn(k.pnl)} USDT</span>`)
      + `<span class="why">${k.reason || '—'}</span>`;
    tip.style.opacity = 1;
    tip.style.left = Math.min(innerWidth - 310, e.clientX + 15) + 'px';
    tip.style.top = Math.max(8, e.clientY - 14) + 'px';
  } else tip.style.opacity = 0;
});

/* ── 回合账 ───────────────────────────────────────────────────────── */
const KIND = {stop: '止损', signal: '信号', open: '未平仓'};
let filter = 'all', sortK = 'i', sortAsc = true;
const maxAbs = Math.max(1, ...D.rounds.map(r => Math.abs(r.pnl)));

function rows(){
  const f = D.rounds.filter(x =>
    filter === 'all' ? true :
    filter === 'win' ? x.pnl > 0 :
    filter === 'loss' ? (x.pnl <= 0 && x.kind !== 'open') : x.kind === 'stop');
  return [...f].sort((a, b) => {
    const va = a[sortK], vb = b[sortK];
    const c = (va == null) - (vb == null)
      || (typeof va === 'string' ? va.localeCompare(vb) : va - vb);
    return sortAsc ? c : -c;
  });
}
function render(){
  const r = rows(), tot = r.reduce((s, x) => s + x.pnl, 0);
  document.getElementById('cnt').textContent = `${r.length} 个 · 合计 ${sgn(tot)} USDT`;
  document.getElementById('tb').innerHTML = r.map(x => `
    <tr data-i="${x.i}">
      <td class="l num">${x.i}</td>
      <td class="l num">${dt(x.t0)}</td>
      <td class="l">${x.side === 'long' ? '多' : '空'}</td>
      <td class="num">${fmt(x.entry)}</td>
      <td class="num">${fmt(x.exit)}</td>
      <td class="num">${x.qty}</td>
      <td class="num">${x.adds || '—'}</td>
      <td class="num pnl ${x.pnl >= 0 ? 'pos' : 'neg'}"
          style="--w:${(Math.abs(x.pnl) / maxAbs * 58).toFixed(1)}px"><span>${sgn(x.pnl)}</span></td>
      <td class="l"><span class="kind ${x.kind}">${KIND[x.kind]}</span></td>
      <td class="l why">${x.why || '—'}</td>
    </tr>`).join('');
}
render();

document.querySelectorAll('th[data-k]').forEach(th => th.onclick = () => {
  const k = th.dataset.k;
  sortAsc = sortK === k ? !sortAsc : true; sortK = k;
  document.querySelectorAll('th[data-k]').forEach(o => o.removeAttribute('aria-sort'));
  th.setAttribute('aria-sort', sortAsc ? 'ascending' : 'descending');
  render();
});
for (const [id, f] of [['f-all','all'],['f-win','win'],['f-loss','loss'],['f-stop','stop']]){
  document.getElementById(id).onclick = e => {
    filter = f; render();
    document.querySelectorAll('.filters button')
      .forEach(b => b.setAttribute('aria-pressed', String(b === e.currentTarget)));
  };
}
// 点一行：图表缩放到该回合，并把区间外的标记压暗
document.getElementById('tb').addEventListener('click', e => {
  const tr = e.target.closest('tr'); if (!tr) return;
  const r = D.rounds.find(x => x.i === +tr.dataset.i); if (!r) return;
  document.querySelectorAll('tbody tr.on').forEach(x => x.classList.remove('on'));
  tr.classList.add('on');
  focus = {t0: r.t0, t1: r.t1};
  const a = bisect(r.t0), b = r.t1 ? bisect(r.t1) : a + 30;
  const pad = Math.max(24, Math.round((b - a) * .8));
  lo = Math.max(0, a - pad); hi = Math.min(T.length - 1, b + pad);
  draw();
  scrollTo({top: 0, behavior: 'smooth'});
});
</script>
"""
