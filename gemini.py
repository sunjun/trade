import math
import time

# --- 1. 参数与资金配置 ---
TOTAL_CAPITAL = 1000.0       # 总本金 (USDT)
LEVERAGE = 5                 # 5x 杠杆
MAX_STEPS = 6                # 6 次金字塔加仓
PYRAMID_RATIO = 1.3          # 正金字塔递增系数

STOP_LOSS_PCT = 0.15         # 全局 15% 总本金硬止损
TP_SCHEDULE = [0.015, 0.020, 0.025, 0.030, 0.038, 0.050] # 动态阶梯止盈表

ATR_TIMEFRAME = '4h'         # 采用 4小时 ATR 识别大级别波动
ATR_PERIOD = 14
ATR_MULTIPLIER = 1.2         # 最小加仓安全间隔 (1.2 * ATR)

# --- 2. 预计算金字塔保证金表 ---
W1 = TOTAL_CAPITAL * (1 - PYRAMID_RATIO) / (1 - math.pow(PYRAMID_RATIO, MAX_STEPS))
margin_schedule = [round(W1 * math.pow(PYRAMID_RATIO, i), 2) for i in range(MAX_STEPS)]


def calculate_fibonacci_supports(symbol, timeframe='4h', lookback=100):
    """ 获取近 N 根 K 线的极值并计算斐波那契支撑位列表 """
    klines = get_okx_klines(symbol, timeframe, limit=lookback)
    highs = [float(k['high']) for k in klines]
    lows = [float(k['low']) for k in klines]
    
    recent_high = max(highs)
    recent_low = min(lows)
    diff = recent_high - recent_low
    
    # 计算斐波那契回调关键支撑位
    supports = [
        recent_high - diff * 0.236,
        recent_high - diff * 0.382,
        recent_high - diff * 0.500,
        recent_high - diff * 0.618,
        recent_high - diff * 0.786,
        recent_low  # 前低强支撑
    ]
    return sorted(supports, reverse=True) # 从高到低排序


def run_smart_pyramid_bot():
    current_step = 0            # 当前处于第几次加仓 (0 ~ MAX_STEPS-1)
    last_entry_price = 0.0      # 上一次成交价
    avg_entry_price = 0.0       # 持仓均价
    total_position_qty = 0.0    # 总持仓量
    support_levels = []         # 支撑位缓存
    
    set_okx_leverage("BTC-USDT-SWAP", LEVERAGE)

    while True:
        try:
            ticker = get_okx_ticker("BTC-USDT-SWAP")
            current_price = float(ticker['last'])

            # -------------------------------------------------------------
            # A. 风控与动态止盈检查
            # -------------------------------------------------------------
            if current_step > 0:
                unrealized_pnl = calculate_pnl(current_price, avg_entry_price, total_position_qty)
                
                # 1. 触发全局 15% 总本金硬止损
                if unrealized_pnl <= -1 * (TOTAL_CAPITAL * STOP_LOSS_PCT):
                    print("🚨 触发全局硬止损！市价全平出局。")
                    close_all_positions("BTC-USDT-SWAP")
                    break
                
                # 2. 阶梯止盈检查
                current_tp_rate = TP_SCHEDULE[current_step - 1]
                target_tp_price = avg_entry_price * (1 + current_tp_rate)
                
                if current_price >= target_tp_price:
                    print(f"🎉 触发第 {current_step} 阶动态止盈 (目标: +{current_tp_rate*100}%)，全平获利出局！")
                    close_all_positions("BTC-USDT-SWAP")
                    current_step = 0
                    continue

            # -------------------------------------------------------------
            # B. 支撑位 + ATR 双重确认加仓逻辑
            # -------------------------------------------------------------
            # 1. 首次建仓
            if current_step == 0:
                # 初始化支撑位列表
                support_levels = calculate_fibonacci_supports("BTC-USDT-SWAP", timeframe='4h')
                print(f"🎯 预计算 4小时 斐波那契支撑位: {[round(s, 2) for s in support_levels]}")
                
                margin = margin_schedule[0]
                order = place_okx_order("BTC-USDT-SWAP", side="buy", margin=margin, leverage=LEVERAGE)
                
                current_step = 1
                last_entry_price = float(order['avgPrice'])
                avg_entry_price, total_position_qty = sync_okx_position_status()
                print(f"🚀 [首次建仓完成] 均价: {avg_entry_price:.2f}")

            # 2. 深度加仓 (Step 1 ~ MAX_STEPS-1)
            elif current_step < MAX_STEPS:
                current_atr = calculate_atr("BTC-USDT-SWAP", timeframe=ATR_TIMEFRAME, period=ATR_PERIOD)
                min_safe_distance = current_atr * ATR_MULTIPLIER
                
                # 匹配下一个支撑位 target_support
                target_support = support_levels[min(current_step, len(support_levels)-1)]
                
                # 【双重判定条件】：
                # 条件 1：价格跌破了既定的关键技术支撑位 (Price <= Target Support)
                # 条件 2：距离上一次加仓的位置达到了 ATR 最小安全间隔 (Price <= Last Price - ATR Distance)
                atr_trigger_price = last_entry_price - min_safe_distance
                
                if current_price <= target_support and current_price <= atr_trigger_price:
                    margin = margin_schedule[current_step]
                    print(f"📉 【双重确认触发加仓】第 {current_step + 1} 次加仓!")
                    print(f"   ├─ 触达支撑位: {target_support:.2f}")
                    print(f"   └─ 满足 ATR 安全间隔 ({min_safe_distance:.2f} 点)")
                    print(f"👉 砸入正金字塔保证金: {margin} USDT (占比 {margin/TOTAL_CAPITAL*100:.1f}%)")
                    
                    order = place_okx_order("BTC-USDT-SWAP", side="buy", margin=margin, leverage=LEVERAGE)
                    
                    current_step += 1
                    last_entry_price = float(order['avgPrice'])
                    avg_entry_price, total_position_qty = sync_okx_position_status()

            time.sleep(3)

        except Exception as e:
            print(f"⚠️ 运行异常: {e}")
            time.sleep(5)