# ==========================================================
# [main.py]
# ⚠️ 이 주석 및 파일명 표기는 절대 지우지 마세요.
# ==========================================================
import os
import json
import logging
import datetime
import pytz
import time
import math
import asyncio
import glob
import pandas_market_calendars as mcal
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from dotenv import load_dotenv

from config import ConfigManager
from broker import KoreaInvestmentBroker
from strategy import InfiniteStrategy
from telegram_bot import TelegramController

if not os.path.exists('data'):
    os.makedirs('data')
if not os.path.exists('logs'):
    os.makedirs('logs')

load_dotenv() 

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
try:
    ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID")) if os.getenv("ADMIN_CHAT_ID") else None
except ValueError:
    ADMIN_CHAT_ID = None

APP_KEY = os.getenv("APP_KEY")
APP_SECRET = os.getenv("APP_SECRET")
CANO = os.getenv("CANO")
ACNT_PRDT_CD = os.getenv("ACNT_PRDT_CD", "01")

if not all([TELEGRAM_TOKEN, APP_KEY, APP_SECRET, CANO]):
    print("❌ [치명적 오류] .env 파일에 봇 구동 필수 키(TELEGRAM_TOKEN, APP_KEY, APP_SECRET, CANO)가 누락되었습니다. 봇을 종료합니다.")
    exit(1)

log_filename = f"logs/bot_app_{datetime.datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO,
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

def is_dst_active():
    est = pytz.timezone('US/Eastern')
    return datetime.datetime.now(est).dst() != datetime.timedelta(0)

def get_target_hour():
    return (17, "🌞 서머타임 적용(여름)") if is_dst_active() else (18, "❄️ 서머타임 해제(겨울)")

def load_extra_closed_dates():
    """data/extra_closed_dates.json — 임시공휴일 등 수동 추가 휴장일 로드
    포맷: { "2026-05-25": "메모리얼데이(임시)", "2026-11-11": "재향군인의날" }
    """
    path = os.path.join("data", "extra_closed_dates.json")
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"extra_closed_dates.json 로드 실패: {e}")
    return {}

def get_market_status():
    """
    NYSE 거래 상태 반환.
    Returns:
        is_open (bool): True면 정상 거래일
        reason  (str):  텔레그램에 보낼 사유 문자열
        is_early_close (bool): 단축 거래일 여부
    """
    est = pytz.timezone('US/Eastern')
    today = datetime.datetime.now(est).date()
    today_str = today.strftime('%Y-%m-%d')
    weekday = today.weekday()  # 0=월 ... 6=일

    # ① 수동 임시공휴일 체크 (pandas_market_calendars가 모르는 날)
    extra = load_extra_closed_dates()
    if today_str in extra:
        label = extra[today_str]
        return False, f"🚫 임시/특별 휴장일 ({label})", False

    # ② pandas_market_calendars NYSE 캘린더 체크
    try:
        nyse = mcal.get_calendar('NYSE')
        schedule = nyse.schedule(start_date=today, end_date=today)

        if schedule.empty:
            # 주말인지 공휴일인지 구분
            if weekday == 5:
                return False, "🏖️ 주말 — 토요일 (NYSE 휴장)", False
            elif weekday == 6:
                return False, "🏖️ 주말 — 일요일 (NYSE 휴장)", False
            else:
                # NYSE 공식 공휴일 이름 조회 시도
                try:
                    holidays = nyse.holidays().holidays
                    ts = pandas_timestamp(today)
                    h_name = str(holidays.get(ts, "NYSE 공휴일"))
                except Exception:
                    h_name = "NYSE 공휴일"
                return False, f"🗓️ {h_name} — NYSE 공식 휴장일", False

        # ③ 단축 거래일(Early Close) 체크
        market_close = schedule.iloc[0]['market_close'].astimezone(est)
        is_early = market_close.hour < 16  # 정규 마감 16:00 기준
        if is_early:
            close_str = market_close.strftime('%H:%M')
            return True, f"⚠️ 단축 거래일 — 오늘 {close_str} EST 조기 마감", True

        return True, "✅ 정상 거래일", False

    except Exception as e:
        logging.warning(f"시장 캘린더 조회 오류: {e} — 거래일로 처리")
        return True, "✅ 정상 거래일 (캘린더 조회 실패, 기본값 적용)", False

def pandas_timestamp(d):
    """date → pandas Timestamp 변환 헬퍼"""
    try:
        import pandas as pd
        return pd.Timestamp(d)
    except Exception:
        return d

def is_market_open():
    """기존 코드 호환용 — 내부적으로 get_market_status() 사용"""
    is_open, _, _ = get_market_status()
    return is_open

def get_budget_allocation(cash, tickers, cfg):
    sorted_tickers = sorted(tickers, key=lambda x: 0 if x == "SOXL" else (1 if x == "TQQQ" else 2))
    allocated = {}
    force_turbo_off = False
    rem_cash = cash
    
    for tx in sorted_tickers:
        rev_state = cfg.get_reverse_state(tx)
        is_rev = rev_state.get("is_active", False)
        
        if is_rev:
            portion = 0.0
        else:
            split = cfg.get_split_count(tx)
            portion = cfg.get_seed(tx) / split if split > 0 else 0
            
        if rem_cash >= portion:
            allocated[tx] = rem_cash
            rem_cash -= portion
        else: 
            allocated[tx] = 0
            if not is_rev:
                force_turbo_off = True 
                
    return sorted_tickers, allocated, force_turbo_off

def get_actual_execution_price(execs, target_qty, side_cd):
    if not execs: return 0.0
    
    execs.sort(key=lambda x: x.get('ord_tmd', '000000'), reverse=True)
    matched_qty = 0
    total_amt = 0.0
    for ex in execs:
        if ex.get('sll_buy_dvsn_cd') == side_cd: 
            eqty = int(float(ex.get('ft_ccld_qty', '0')))
            eprice = float(ex.get('ft_ccld_unpr3', '0'))
            if matched_qty + eqty <= target_qty:
                total_amt += eqty * eprice
                matched_qty += eqty
            elif matched_qty < target_qty:
                rem = target_qty - matched_qty
                total_amt += rem * eprice
                matched_qty += rem
            
            if matched_qty >= target_qty:
                break
    
    if matched_qty > 0:
        return math.floor((total_amt / matched_qty) * 100) / 100.0
    return 0.0

def perform_self_cleaning():
    try:
        now = time.time()
        seven_days = 7 * 24 * 3600
        one_day = 24 * 3600
        
        for f in glob.glob("logs/*.log"):
            if os.path.isfile(f) and os.stat(f).st_mtime < now - seven_days:
                try: os.remove(f)
                except: pass
                
        for f in glob.glob("data/*.bak_*"):
            if os.path.isfile(f) and os.stat(f).st_mtime < now - seven_days:
                try: os.remove(f)
                except: pass
                
        for directory in ["data", "logs"]:
            for f in glob.glob(f"{directory}/tmp*"):
                if os.path.isfile(f) and os.stat(f).st_mtime < now - one_day:
                    try: os.remove(f)
                    except: pass
    except Exception as e:
        logging.error(f"🧹 자정(Self-Cleaning) 작업 중 오류 발생: {e}")

async def scheduled_self_cleaning(context):
    await asyncio.to_thread(perform_self_cleaning)
    logging.info("🧹 [시스템 자정 작업 완료] 7일 초과 로그/백업 및 24시간 초과 임시 파일 소각 완료")

async def scheduled_token_check(context):
    context.job.data['broker']._get_access_token(force=True)

async def scheduled_force_reset(context):
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.datetime.now(kst)
    target_hour, _ = get_target_hour()
    
    if now.hour != target_hour: return

    is_open, market_reason, is_early = get_market_status()
    if not is_open:
        await context.bot.send_message(
            chat_id=context.job.chat_id,
            text=(
                f"⛔ <b>[{target_hour}:00] 오늘은 거래 없는 날입니다</b>\n"
                f"{market_reason}\n\n"
                f"💤 매매 잠금 해제 및 스나이퍼 장전을 건너뜁니다."
            ),
            parse_mode='HTML'
        )
        return
    if is_early:
        await context.bot.send_message(
            chat_id=context.job.chat_id,
            text=f"⚠️ <b>[{target_hour}:00] 오늘은 단축 거래일입니다</b>\n{market_reason}\n\n계속 진행합니다.",
            parse_mode='HTML'
        )

    app_data = context.job.data
    app_data['cfg'].reset_locks()
    
    for t in app_data['cfg'].get_active_tickers():
        app_data['cfg'].increment_reverse_day(t)
        
    await context.bot.send_message(chat_id=context.job.chat_id, text=f"🔓 <b>[{target_hour}:00] 시스템 초기화 완료 (매매 잠금 해제 & 스나이퍼 장전 & 리버스 카운트 누적)</b>", parse_mode='HTML')

async def scheduled_premarket_monitor(context):
    if not is_market_open(): return
    app_data = context.job.data
    
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.datetime.now(kst)
    target_hour, _ = get_target_hour()
    
    if not (now.hour == target_hour and 0 <= now.minute < 30):
        return

    cfg, broker, strategy, tx_lock = app_data['cfg'], app_data['broker'], app_data['strategy'], app_data['tx_lock']

    async with tx_lock:
        _, holdings = broker.get_account_balance()
        if holdings is None: return 

        for t in cfg.get_active_tickers():
            h = holdings.get(t, {'qty': 0, 'avg': 0})
            if int(h['qty']) == 0: continue 

            curr_p = await asyncio.to_thread(broker.get_current_price, t)
            prev_c = await asyncio.to_thread(broker.get_previous_close, t)
            
            plan = strategy.get_plan(t, curr_p, float(h['avg']), int(h['qty']), prev_c, market_type="PRE_CHECK")
            
            if plan['orders']:
                msg = f"🌅 <b>[{t}] 대박! 프리마켓 목표 달성 🎉</b>\n⚡ 전량 익절 주문을 실행합니다!"
                broker.cancel_all_orders_safe(t)
                for o in plan['orders']:
                    res = broker.send_order(t, o['side'], o['qty'], o['price'], o['type'])
                    err_msg = res.get('msg1')
                    is_success = res.get('rt_cd') == '0'
                    msg += f"\n└ {o['desc']}: {'✅' if is_success else f'❌({err_msg})'}"
                    await asyncio.sleep(0.2) 
                await context.bot.send_message(chat_id=context.job.chat_id, text=msg, parse_mode='HTML')

async def scheduled_sniper_monitor(context):
    if not is_market_open(): return
    
    est = pytz.timezone('US/Eastern')
    now_est = datetime.datetime.now(est)
    nyse = mcal.get_calendar('NYSE')
    schedule = nyse.schedule(start_date=now_est.date(), end_date=now_est.date())
    if schedule.empty: return
    
    market_open = schedule.iloc[0]['market_open'].astimezone(est)
    market_close = schedule.iloc[0]['market_close'].astimezone(est)
    
    pre_start = market_open.replace(hour=4, minute=0)
    start_monitor = pre_start + datetime.timedelta(minutes=1)
    end_monitor = market_close - datetime.timedelta(minutes=15)
    
    if not (start_monitor <= now_est <= end_monitor):
        return

    app_data = context.job.data
    cfg, broker, strategy, tx_lock = app_data['cfg'], app_data['broker'], app_data['strategy'], app_data['tx_lock']
    chat_id = context.job.chat_id
    
    target_cache = app_data.setdefault('dynamic_targets', {})
    today_est_str = now_est.strftime('%Y%m%d')
    if target_cache.get('date') != today_est_str:
        target_cache.clear()
        target_cache['date'] = today_est_str
    
    async with tx_lock:
        cash, holdings = broker.get_account_balance()
        if holdings is None: return
        
        sorted_tickers, allocated_cash, force_turbo_off = get_budget_allocation(cash, cfg.get_active_tickers(), cfg)
        
        for t in cfg.get_active_tickers():
            if cfg.get_version(t) != "V17": continue
            
            lock_buy = cfg.check_lock(t, "SNIPER_BUY")
            lock_sell = cfg.check_lock(t, "SNIPER_SELL")
            
            if lock_buy and lock_sell:
                continue
            
            h = holdings.get(t, {'qty': 0, 'avg': 0})
            qty = int(h['qty'])
            avg_price = float(h['avg'])
            if qty == 0: continue
            
            curr_p = await asyncio.to_thread(broker.get_current_price, t)
            prev_c = await asyncio.to_thread(broker.get_previous_close, t)
            if curr_p <= 0: continue
            
            idx_ticker = "SOXX" if t == "SOXL" else "QQQ"
            if t not in target_cache:
                weight = cfg.get_sniper_multiplier(t)
                tgt = await asyncio.to_thread(broker.get_dynamic_sniper_target, idx_ticker, weight)
                target_cache[t] = tgt if tgt is not None else (9.0 if t=="SOXL" else 5.0) 

            sniper_pct = target_cache[t]
            raw_target_price = prev_c * (1 - (sniper_pct / 100.0))
            target_buy_price = math.floor(raw_target_price * 100) / 100.0
            
            is_sniper_armed = target_buy_price < avg_price
            trigger_reason = f"-{sniper_pct}% 동적 방어선"
            
            # =========================================================================
            # 1. 하방 스나이퍼 매수 (Intercept)
            # =========================================================================
            if not lock_buy and is_sniper_armed and target_buy_price > 0 and curr_p <= target_buy_price:
                if cfg.get_secret_mode():
                    is_rev = cfg.get_reverse_state(t).get("is_active", False)
                    if is_rev: sniper_budget = cfg.get_escrow_cash(t) / 4.0
                    else:
                        split = cfg.get_split_count(t)
                        sniper_budget = cfg.get_seed(t) / split if split > 0 else 0
                    
                    if sniper_budget < curr_p: continue 

                    await asyncio.to_thread(broker.cancel_targeted_orders, t, "BUY", "34")
                    await asyncio.sleep(1.0)
                    
                    ask_price = await asyncio.to_thread(broker.get_ask_price, t)
                    exec_price = ask_price if ask_price > 0 else curr_p
                    rem_qty = math.floor(sniper_budget / exec_price)
                    
                    hunt_success = False
                    actual_buy_price = exec_price
                    
                    for attempt in range(3):
                        if rem_qty <= 0:
                            hunt_success = True
                            break

                        ask_price = await asyncio.to_thread(broker.get_ask_price, t)
                        exec_price = ask_price if ask_price > 0 else curr_p
                        
                        res = broker.send_order(t, "BUY", rem_qty, exec_price, "LIMIT")
                        odno = res.get('odno', '')
                        
                        if res.get('rt_cd') == '0' and odno:
                            await asyncio.sleep(2.0)
                            unfilled = await asyncio.to_thread(broker.get_unfilled_orders_detail, t)
                            my_order = next((o for o in unfilled if o.get('odno') == odno), None)
                            
                            if not my_order:
                                rem_qty = 0
                                hunt_success = True
                                actual_buy_price = exec_price
                                break
                            else:
                                ord_qty = int(float(my_order.get('ord_qty', 0)))
                                tot_ccld_qty = int(float(my_order.get('tot_ccld_qty', 0)))
                                rem_qty = ord_qty - tot_ccld_qty
                                
                                await asyncio.to_thread(broker.cancel_order, t, odno)
                                await asyncio.sleep(1.0)
                        
                        await asyncio.sleep(0.5)
                    
                    if hunt_success:
                        cfg.set_lock(t, "SNIPER_BUY") 
                        msg = f"💥 <b>[{t}] V20.11 시크릿 모드 가로채기(Intercept) 명중!</b>\n"
                        msg += f"📉 실시간 현재가(${curr_p:.2f})가 <b>[{trigger_reason}]</b>(${target_buy_price:.2f})을 터치했습니다!\n"
                        msg += f"🎯 <b>매수 방어선을 해제하고 최적 단가 ${actual_buy_price:.2f}에 즉시 낚아채어 체결을 완료</b>했습니다!\n"
                        msg += "🔫 당일 하방(매수) 스나이퍼 활동만을 종료하며, 상방(익절) 감시는 계속됩니다."
                        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
                        continue

                    now_ts = time.time()
                    fail_history = app_data.setdefault('sniper_fail_ts', {})
                    if now_ts - fail_history.get(t, 0) > 3600:
                        msg = f"🛡️ <b>[{t}] V20.11 가로채기 덫 기습 실패 (1시간 쿨타임 진입)</b>\n"
                        msg += f"📉 동적 방어선(${target_buy_price:.2f})에 3회 지정가 덫을 던졌으나 잔량이 남았습니다.\n"
                        msg += f"🦇 매수 스나이퍼는 1시간 동안 숨을 죽이며, 취소했던 일반 방어 매수(LOC) 주문만 호가창에 정밀 복구합니다."
                        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
                        fail_history[t] = now_ts
                    
                    ma_5day = await asyncio.to_thread(broker.get_5day_ma, t)
                    plan = strategy.get_plan(t, curr_p, avg_price, qty, prev_c, ma_5day=ma_5day, market_type="REG", available_cash=allocated_cash[t], force_turbo_off=force_turbo_off)
                    
                    for o in plan.get('core_orders', []) + plan.get('bonus_orders', []):
                        if o['side'] == 'BUY':
                            broker.send_order(t, o['side'], o['qty'], o['price'], o['type'])
                            await asyncio.sleep(0.2)
                            
                    continue

            # =========================================================================
            # 2. 상방 스나이퍼 매도 (Jackpot)
            # =========================================================================
            target_pct_val = cfg.get_target_profit(t)
            target_price = math.ceil(avg_price * (1 + target_pct_val / 100.0) * 100) / 100.0
            
            split = cfg.get_split_count(t)
            t_val, _ = cfg.get_absolute_t_val(t, qty, avg_price)
            
            depreciation_factor = 2.0 / split if split > 0 else 0.1
            star_ratio = (target_pct_val / 100.0) - ((target_pct_val / 100.0) * depreciation_factor * t_val)
            star_price = math.ceil(avg_price * (1 + star_ratio) * 100) / 100.0
            
            if not lock_sell and curr_p >= target_price:
                await asyncio.to_thread(broker.cancel_all_orders_safe, t, side="SELL")
                await asyncio.sleep(1.0)
                
                rem_qty = qty
                hunt_success = False
                actual_sell_price = target_price
                
                for attempt in range(3):
                    if rem_qty <= 0:
                        hunt_success = True
                        break
                        
                    bid_price = await asyncio.to_thread(broker.get_bid_price, t)
                    
                    if bid_price > 0 and bid_price >= target_price:
                        res = broker.send_order(t, "SELL", rem_qty, bid_price, "LIMIT")
                        odno = res.get('odno', '')
                        
                        if res.get('rt_cd') == '0' and odno:
                            await asyncio.sleep(2.0)
                            unfilled = await asyncio.to_thread(broker.get_unfilled_orders_detail, t)
                            my_order = next((o for o in unfilled if o.get('odno') == odno), None)
                            
                            if not my_order:
                                rem_qty = 0
                                hunt_success = True
                                actual_sell_price = bid_price
                                break
                            else:
                                ord_qty = int(float(my_order.get('ord_qty', 0)))
                                tot_ccld_qty = int(float(my_order.get('tot_ccld_qty', 0)))
                                rem_qty = ord_qty - tot_ccld_qty
                                
                                await asyncio.to_thread(broker.cancel_order, t, odno)
                                await asyncio.sleep(1.0)
                                
                    await asyncio.sleep(0.5)
                    
                if hunt_success:
                    cfg.set_lock(t, "SNIPER_SELL")
                    msg = f"🔥 <b>[{t}] 스나이퍼 잭팟 터짐! (목표가 돌파)</b>\n"
                    msg += f"🎯 실시간 매수 1호가(${bid_price:.2f})가 목표가(${target_price:.2f})를 돌파하여 <b>실제 단가 ${actual_sell_price:.2f}에 전량 강제 익절</b> 처리했습니다.\n"
                    msg += "🔫 당일 상방(매도) 스나이퍼 활동을 완전 종료합니다."
                    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
                    continue
                    
                now_ts = time.time()
                fail_history_j = app_data.setdefault('sniper_j_fail_ts', {})
                if now_ts - fail_history_j.get(t, 0) > 3600:
                    msg = f"🛡️ <b>[{t}] 스나이퍼 잭팟 기습 실패 (방어선 복구)</b>\n"
                    msg += f"🎯 3회에 걸쳐 전량 익절을 시도했으나 체결되지 않았습니다.\n"
                    msg += f"🦇 취소했던 원래의 매도(SELL) 주문을 다시 호가창에 정밀 장전합니다."
                    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
                    fail_history_j[t] = now_ts
                
                ma_5day = await asyncio.to_thread(broker.get_5day_ma, t)
                plan = strategy.get_plan(t, curr_p, avg_price, qty, prev_c, ma_5day=ma_5day, market_type="REG", available_cash=allocated_cash[t], force_turbo_off=force_turbo_off)
                
                for o in plan.get('core_orders', []) + plan.get('bonus_orders', []):
                    if o['side'] == 'SELL':
                        broker.send_order(t, o['side'], o['qty'], o['price'], o['type'])
                        await asyncio.sleep(0.2)
                        
                continue
            
            # =========================================================================
            # 3. 상방 스나이퍼 매도 (Quarter / 리버스 / 별값 익절)
            # =========================================================================
            # 🚨 [V21.1 박제: 리버스 전용 스나이퍼 3단 기어 탑재]
            is_rev = cfg.get_reverse_state(t).get("is_active", False)
            
            if is_rev:
                # 리버스 모드: 내 평단가(수수료 포함)를 절대 방어선으로 설정 (1.0025)
                breakeven_price = math.ceil(avg_price * 1.0025 * 100) / 100.0
                trigger_price = breakeven_price
                # 리버스 모드의 탈출 수량 계산 (분할수에 따라 1/10 또는 1/20)
                sell_divisor = 10 if split <= 20 else 20
                q_qty = max(4, math.floor(qty / sell_divisor)) if qty >= 4 else qty
                phase = "리버스 부분 익절(본전+수수료 돌파)"
            else:
                # 일반 모드: 전반전은 별값, 후반전은 본전
                is_first_half = t_val < (split / 2)
                trigger_price = star_price if is_first_half else math.ceil(avg_price * 1.0025 * 100) / 100.0
                q_qty = math.ceil(qty / 4)
                phase = "전반전(별값 돌파)" if is_first_half else "후반전(본전+수수료 돌파)"
            
            if not lock_sell and curr_p >= trigger_price:
                unfilled = await asyncio.to_thread(broker.get_unfilled_orders_detail, t)
                target_odno = None
                
                for o in unfilled:
                    if o.get('sll_buy_dvsn_cd') == '01' and o.get('ord_dvsn_cd') == '34': 
                        target_odno = o.get('odno')
                        break
                        
                if target_odno:
                    await asyncio.to_thread(broker.cancel_order, t, target_odno)
                    await asyncio.sleep(1.0) 
                    
                    rem_qty = q_qty
                    hunt_success = False
                    actual_sell_price = trigger_price
                    
                    for attempt in range(3):
                        if rem_qty <= 0:
                            hunt_success = True
                            break
                            
                        bid_price = await asyncio.to_thread(broker.get_bid_price, t)
                        
                        if bid_price > 0 and bid_price >= trigger_price:
                            res = broker.send_order(t, "SELL", rem_qty, bid_price, "LIMIT")
                            odno = res.get('odno', '')
                            
                            if res.get('rt_cd') == '0' and odno:
                                await asyncio.sleep(2.0)
                                unfilled_check = await asyncio.to_thread(broker.get_unfilled_orders_detail, t)
                                my_order = next((o for o in unfilled_check if o.get('odno') == odno), None)
                                
                                if not my_order:
                                    rem_qty = 0
                                    hunt_success = True
                                    actual_sell_price = bid_price
                                    break
                                else:
                                    ord_qty = int(float(my_order.get('ord_qty', 0)))
                                    tot_ccld_qty = int(float(my_order.get('tot_ccld_qty', 0)))
                                    rem_qty = ord_qty - tot_ccld_qty
                                    
                                    await asyncio.to_thread(broker.cancel_order, t, odno)
                                    await asyncio.sleep(1.0)
                                    
                        await asyncio.sleep(0.5)
                        
                    if hunt_success:
                        cfg.set_lock(t, "SNIPER_SELL")
                        
                        msg = f"🔫 <b>[{t}] V17 시크릿 쿼터 익절 발동! ({phase})</b>\n"
                        msg += f"🎯 실시간 매수 1호가: ${bid_price:.2f} (트리거: ${trigger_price:.2f})\n"
                        msg += f"🛡️ 기존 쿼터 방어선만 해제하고 {q_qty}주를 <b>최적의 단가 ${actual_sell_price:.2f}에 즉시 낚아챘습니다!</b>\n"
                        
                        # 🚨 [V21.4 공수 완벽 분리 및 시퀀스 통제 로직 적용]
                        if not is_rev:
                            if is_first_half:
                                await asyncio.to_thread(broker.cancel_targeted_orders, t, "BUY", "34")
                                await asyncio.sleep(1.0)
                                
                                ma_5day = await asyncio.to_thread(broker.get_5day_ma, t)
                                plan = strategy.get_plan(t, curr_p, avg_price, qty, prev_c, ma_5day=ma_5day, market_type="REG", available_cash=allocated_cash[t], force_turbo_off=force_turbo_off)
                                
                                smart_cores = plan.get('smart_core_orders', [])
                                smart_bonus = plan.get('smart_bonus_orders', [])
                                
                                if len(smart_cores) == 0:
                                    msg += "\n🛑 <b>[스마트 밸런싱 발동]</b>\n└ 전반전 종가 관망 모드로 전환 (오늘 추가 매수 안 함)"
                                else:
                                    msg += "\n🦇 <b>[스마트 방어 매수 장전] (플랜 B 전환)</b>\n"
                                    for o in smart_cores:
                                        buy_res = broker.send_order(t, o['side'], o['qty'], o['price'], o['type'])
                                        err_msg = buy_res.get('msg1')
                                        msg += f"└ {o['desc']} {o['qty']}주: {'✅' if buy_res.get('rt_cd') == '0' else f'❌({err_msg})'}\n"
                                        await asyncio.sleep(0.2)
                                    for o in smart_bonus:
                                        buy_res = broker.send_order(t, o['side'], o['qty'], o['price'], o['type'])
                                        msg += f"└ {o['desc']} {o['qty']}주: {'✅' if buy_res.get('rt_cd') == '0' else '❌'}\n"
                                        await asyncio.sleep(0.2)
                            else:
                                msg += "\n🛡️ <b>[공수 완벽 분리 원칙 적용]</b>\n└ 쿼터 익절 성공(후반전)! 기존 하방 매수(LOC) 주문은 전혀 건드리지 않고 그대로 유지하며 스나이퍼 감시를 계속합니다."
                        else:
                            msg += "\n🛡️ <b>[공수 완벽 분리 원칙 적용]</b>\n└ 리버스 부분 익절 성공! 기존 하방 매수(LOC) 주문은 전혀 건드리지 않고 그대로 유지하며 스나이퍼 감시를 계속합니다."
                            
                        msg += "\n🔒 당일 상방(매도) 쿼터 스나이퍼 감시를 종료합니다."
                        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
                        continue
                        
                    now_ts = time.time()
                    fail_history_q = app_data.setdefault('sniper_q_fail_ts', {})
                    if now_ts - fail_history_q.get(t, 0) > 3600:
                        msg = f"🛡️ <b>[{t}] 스나이퍼 쿼터 기습 실패 (방어선 복구)</b>\n"
                        msg += f"🎯 3회에 걸쳐 쿼터 익절을 시도했으나 체결되지 않았습니다.\n"
                        msg += f"🦇 취소했던 원래의 쿼터 방어 주문(LOC 매도) 단 1개를 다시 장전합니다."
                        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
                        fail_history_q[t] = now_ts
                    
                    ma_5day = await asyncio.to_thread(broker.get_5day_ma, t)
                    plan = strategy.get_plan(t, curr_p, avg_price, qty, prev_c, ma_5day=ma_5day, market_type="REG", available_cash=allocated_cash[t], force_turbo_off=force_turbo_off)
                    
                    for o in plan.get('core_orders', []):
                        if o['side'] == 'SELL' and o['type'] == 'LOC':
                            broker.send_order(t, o['side'], o['qty'], o['price'], o['type'])
                            await asyncio.sleep(0.2)
                            
                    continue

async def scheduled_regular_trade(context):
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.datetime.now(kst)
    target_hour, _ = get_target_hour()
    
    if now.hour != target_hour: return

    chat_id = context.job.chat_id
    is_open, market_reason, is_early = get_market_status()
    if not is_open:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⛔ <b>[{target_hour}:30] 오늘은 거래 없는 날입니다</b>\n"
                f"{market_reason}\n\n"
                f"💤 정규장 자동매매를 건너뜁니다.\n"
                f"📅 내일 정상 거래일에 재개됩니다."
            ),
            parse_mode='HTML'
        )
        return
    if is_early:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ <b>[{target_hour}:30] 오늘은 단축 거래일입니다</b>\n{market_reason}",
            parse_mode='HTML'
        )

    app_data = context.job.data
    cfg, broker, strategy, tx_lock = app_data['cfg'], app_data['broker'], app_data['strategy'], app_data['tx_lock']
    
    latest_version = cfg.get_latest_version()
    await context.bot.send_message(chat_id=chat_id, text=f"🌃 <b>[{target_hour}:30] 다이내믹 스노우볼 {latest_version} 정규장 주문을 준비합니다.</b>", parse_mode='HTML')
    
    async with tx_lock:
        cash, holdings = broker.get_account_balance()
        if holdings is None:
            await context.bot.send_message(chat_id=chat_id, text="❌ 계좌 정보를 불러오지 못해 정규장 주문을 취소합니다.", parse_mode='HTML')
            return

        sorted_tickers, allocated_cash, force_turbo_off = get_budget_allocation(cash, cfg.get_active_tickers(), cfg)
        
        plans = {}
        msgs = {t: "" for t in sorted_tickers}
        all_success = {t: True for t in sorted_tickers}

        for t in sorted_tickers:
            if cfg.check_lock(t, "REG"): continue
            h = holdings.get(t, {'qty': 0, 'avg': 0})
            
            curr_p = await asyncio.to_thread(broker.get_current_price, t)
            prev_c = await asyncio.to_thread(broker.get_previous_close, t)
            ma_5day = await asyncio.to_thread(broker.get_5day_ma, t)
            
            plan = strategy.get_plan(t, curr_p, float(h['avg']), int(h['qty']), prev_c, ma_5day=ma_5day, market_type="REG", available_cash=allocated_cash[t], force_turbo_off=force_turbo_off)
            plans[t] = plan
            
            if plan['orders']:
                is_rev = plan.get('is_reverse', False)
                title = f"🔄 <b>[{t}] 리버스 주문 실행 (교차 전송)</b>\n" if is_rev else f"💎 <b>[{t}] 정규장 주문 실행 (교차 전송)</b>\n"
                msgs[t] += title

        for t in sorted_tickers:
            if t not in plans or not plans[t]['orders']: continue
            for o in plans[t].get('core_orders', []):
                res = broker.send_order(t, o['side'], o['qty'], o['price'], o['type'])
                is_success = res.get('rt_cd') == '0'
                if not is_success: all_success[t] = False
                err_msg = res.get('msg1')
                msgs[t] += f"└ 1차 필수: {o['desc']} {o['qty']}주: {'✅' if is_success else f'❌({err_msg})'}\n"
                await asyncio.sleep(0.2) 

        for t in sorted_tickers:
            if t not in plans or not plans[t]['orders']: continue
            for o in plans[t].get('bonus_orders', []):
                res = broker.send_order(t, o['side'], o['qty'], o['price'], o['type'])
                is_success = res.get('rt_cd') == '0'
                msgs[t] += f"└ 2차 보너스: {o['desc']} {o['qty']}주: {'✅' if is_success else '❌(잔금패스)'}\n"
                await asyncio.sleep(0.2) 

        for t in sorted_tickers:
            if t not in plans or not plans[t]['orders']: continue
            
            if all_success[t] and len(plans[t].get('core_orders', [])) > 0:
                cfg.set_lock(t, "REG")
                msgs[t] += "\n🔒 <b>필수 주문 정상 전송 완료 (잠금 설정됨)</b>"
            elif not all_success[t]:
                msgs[t] += "\n⚠️ <b>일부 필수 주문 실패 (매매 잠금 보류 - 다음 스케줄 재시도)</b>"
            else:
                cfg.set_lock(t, "REG")
                msgs[t] += "\n🔒 <b>보너스 줍줍 주문만 전송 완료 (잠금 설정됨)</b>"
                
            await context.bot.send_message(chat_id=chat_id, text=msgs[t], parse_mode='HTML')

async def scheduled_auto_sync_summer(context):
    if not is_dst_active(): return 
    await run_auto_sync(context, "08:30")

async def scheduled_auto_sync_winter(context):
    if is_dst_active(): return 
    await run_auto_sync(context, "09:30")

async def run_auto_sync(context, time_str):
    chat_id = context.job.chat_id
    bot = context.job.data['bot']
    status_msg = await context.bot.send_message(chat_id=chat_id, text=f"📝 <b>[{time_str}] 장부 자동 동기화(무결성 검증)를 시작합니다.</b>", parse_mode='HTML')
    
    success_tickers = []
    for t in context.job.data['cfg'].get_active_tickers():
        res = await bot.process_auto_sync(t, chat_id, context, silent_ledger=True)
        if res == "SUCCESS":
            success_tickers.append(t)
            
    if success_tickers:
        async with context.job.data['tx_lock']:
            _, holdings = context.job.data['broker'].get_account_balance()
        await bot._display_ledger(success_tickers[0], chat_id, context, message_obj=status_msg, pre_fetched_holdings=holdings)
    else:
        await status_msg.edit_text(f"📝 <b>[{time_str}] 장부 동기화 완료</b> (표시할 진행 중인 장부가 없습니다)", parse_mode='HTML')

async def scheduled_morning_check(context):
    """매일 09:00 KST — 오늘 시장 개장 여부 사전 알림"""
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.datetime.now(kst)
    if now.hour != 9: return

    is_open, market_reason, is_early = get_market_status()
    target_hour, season_icon = get_target_hour()

    if is_open:
        if is_early:
            msg = (
                f"🌅 <b>[09:00] 오늘의 시장 브리핑</b>\n"
                f"{market_reason}\n"
                f"{season_icon}\n"
                f"📋 정규장 주문 예정: <b>{target_hour}:30 KST</b>\n"
                f"⚠️ 단축 거래일이므로 주문 마감 시각에 유의하세요."
            )
        else:
            msg = (
                f"🌅 <b>[09:00] 오늘의 시장 브리핑</b>\n"
                f"✅ 정상 거래일입니다.\n"
                f"{season_icon}\n"
                f"📋 정규장 주문 예정: <b>{target_hour}:30 KST</b>"
            )
    else:
        msg = (
            f"🌅 <b>[09:00] 오늘의 시장 브리핑</b>\n"
            f"{market_reason}\n\n"
            f"💤 오늘은 자동매매가 없습니다.\n"
            f"📌 임시공휴일 추가: /holiday 명령어로 수동 등록 가능"
        )

    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=msg,
        parse_mode='HTML'
    )

def main():
    TARGET_HOUR, season_msg = get_target_hour()
    
    cfg = ConfigManager()
    latest_version = cfg.get_latest_version() 
    
    print("=" * 50)
    print(f"🚀 방치형 자동매매 {latest_version} (V21 아키텍처 탑재)")
    print(f"📅 날짜 정보: {season_msg}")
    print(f"⏰ 자동 동기화: 08:30(여름) / 09:30(겨울) 자동 변경")
    print("=" * 50)
    
    perform_self_cleaning()
    
    if ADMIN_CHAT_ID: cfg.set_chat_id(ADMIN_CHAT_ID)
    broker = KoreaInvestmentBroker(APP_KEY, APP_SECRET, CANO, ACNT_PRDT_CD)
    strategy = InfiniteStrategy(cfg)
    tx_lock = asyncio.Lock()
    bot = TelegramController(cfg, broker, strategy, tx_lock)
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    for cmd, handler in [
        ("start", bot.cmd_start), 
        ("record", bot.cmd_record), 
        ("history", bot.cmd_history), 
        ("sync", bot.cmd_sync), 
        ("settlement", bot.cmd_settlement), 
        ("seed", bot.cmd_seed), 
        ("ticker", bot.cmd_ticker), 
        ("mode", bot.cmd_mode), 
        ("reset", bot.cmd_reset), 
        ("version", bot.cmd_version),
        ("v17", bot.cmd_v17),
        ("v4", bot.cmd_v4),
        ("holiday", bot.cmd_holiday)
    ]:
        app.add_handler(CommandHandler(cmd, handler))
    app.add_handler(CallbackQueryHandler(bot.handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    
    if cfg.get_chat_id():
        jq = app.job_queue
        app_data = {'cfg': cfg, 'broker': broker, 'strategy': strategy, 'bot': bot, 'tx_lock': tx_lock}
        kst = pytz.timezone('Asia/Seoul')
        
        for tt in [datetime.time(7,0,tzinfo=kst), datetime.time(11,0,tzinfo=kst), datetime.time(16,30,tzinfo=kst), datetime.time(22,0,tzinfo=kst)]:
            jq.run_daily(scheduled_token_check, time=tt, days=tuple(range(7)), chat_id=cfg.get_chat_id(), data=app_data)
        
        jq.run_daily(scheduled_auto_sync_summer, time=datetime.time(8, 30, tzinfo=kst), days=tuple(range(7)), chat_id=cfg.get_chat_id(), data=app_data)
        jq.run_daily(scheduled_auto_sync_winter, time=datetime.time(9, 30, tzinfo=kst), days=tuple(range(7)), chat_id=cfg.get_chat_id(), data=app_data)
        
        for hour in [17, 18]:
            jq.run_daily(scheduled_force_reset, time=datetime.time(hour, 0, tzinfo=kst), days=(0,1,2,3,4), chat_id=cfg.get_chat_id(), data=app_data)
            jq.run_daily(scheduled_regular_trade, time=datetime.time(hour, 30, tzinfo=kst), days=(0,1,2,3,4), chat_id=cfg.get_chat_id(), data=app_data)

        jq.run_repeating(scheduled_premarket_monitor, interval=60, chat_id=cfg.get_chat_id(), data=app_data)
        jq.run_repeating(scheduled_sniper_monitor, interval=60, chat_id=cfg.get_chat_id(), data=app_data)
        
        jq.run_daily(scheduled_morning_check, time=datetime.time(9, 0, tzinfo=kst), days=tuple(range(7)), chat_id=cfg.get_chat_id(), data=app_data)
        jq.run_daily(scheduled_self_cleaning, time=datetime.time(6, 0, tzinfo=kst), days=tuple(range(7)), chat_id=cfg.get_chat_id(), data=app_data)
        
    app.run_polling()

if __name__ == "__main__":
    main()
