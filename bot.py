import os
import sys
import json
import time
import queue
import datetime
import threading
import requests
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import perry_memory as pm
import budget_manager as bm

# Config pour le nouveau job de Perry

TELEGRAM_TOKEN = "8803422841:AAGQklfLQfkbFkqrVkrnBVYWwT9VCGCFR6k"
TELEGRAM_CHAT_ID = "6552751822"
FMP_API_KEY = "c4MfYeznxUSNi4F8doGCoXw7VCb3xvzo"

SYMBOL = "XAUUSD"
TIMEFRAME = mt5.TIMEFRAME_M30

# Stratégie Pullback validée
ATR_SL_MULTIPLIER = 1.5
RR_RATIO = 2.5
ADX_MIN = 22.0
ADX_PERIOD = 14
ATR_RATIO_MIN = 0.9
RSI_BUY_MIN, RSI_BUY_MAX = 40, 58
RSI_SELL_MIN, RSI_SELL_MAX = 42, 60
SESSION_START_UTC = 7
SESSION_END_UTC = 21

# Trailing / BE
ATR_TRAILING_MULTIPLIER = 2.0
ATR_BE_TRIGGER = 0.5
PARTIAL_TP_R = 1.0
PARTIAL_TP_PCT = 0.5
TIME_STOP_HOURS = 12

# Risque
MIN_BALANCE_STOP = 20.0
MAX_SPREAD_DOLLARS = 0.60
MAX_CONCURRENT_TRADES = 1
COOLDOWN_MINUTES = 30
MAGIC_NUMBER = 998877

STATE_FILE = "perry_state.json"
CYCLE_SLEEP_SEC = 5.0

# Etat global

is_running = True
pull_requested = False
VALID_FILLING_MODES = {}
ACTIVE_TRACKED_POSITIONS = {}
SYMBOL_COOLDOWNS = {}
cooldown_lock = threading.Lock()
state_lock = threading.Lock()

session_stats = {
    "start_time": None, "initial_balance": 0.0,
    "trades_executed": 0, "closed_wins": 0, "closed_losses": 0
}

budget_mgr = bm.BudgetManager()

# Thread Telegram

_tg_queue = queue.Queue(maxsize=300)


def _telegram_worker():
    while is_running:
        try:
            msg = _tg_queue.get(timeout=1)
        except queue.Empty:
            continue
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
                timeout=5,
            )
        except Exception as e:
            print(f"TG err : {e}")


def send_telegram_message(message):
    try:
        _tg_queue.put_nowait(message)
        if _tg_queue.qsize() > 50:
            print(f"⚠️ TG queue : {_tg_queue.qsize()}")
    except queue.Full:
        print("🚨 TG queue saturée")


def flush_telegram(timeout=3.0):
    deadline = time.time() + timeout
    while not _tg_queue.empty() and time.time() < deadline:
        time.sleep(0.1)


# Nouvelles du journal 🐻

_news_state = {"blocked": False, "last_check": 0.0, "last_event": ""}
_news_lock = threading.Lock()
NEWS_REFRESH_SEC = 180
NEWS_BLOCK_WINDOW = 30


def _fetch_news_blocking_state():
    if not FMP_API_KEY or FMP_API_KEY == "YOUR_FMP_KEY_HERE":
        return False, ""
    try:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        url = (f"https://financialmodelingprep.com/api/v3/economic_calendar"
               f"?from={today}&to={today}&apikey={FMP_API_KEY}")
        res = requests.get(url, timeout=3)
        if res.status_code != 200:
            return False, ""
        now = datetime.datetime.utcnow()
        for ev in res.json():
            if ev.get("impact") != "High":
                continue
            if ev.get("currency") not in ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD"]:
                continue
            ds = ev.get("date")
            if not ds:
                continue
            et = datetime.datetime.strptime(ds, "%Y-%m-%d %H:%M:%S")
            if abs((et - now).total_seconds()) / 60.0 <= NEWS_BLOCK_WINDOW:
                return True, ev.get("event", "?")
    except Exception as e:
        print(f"News fetch err : {e}")
    return False, ""


def news_refresher_thread():
    global _news_state
    while is_running:
        try:
            blocked, evt = _fetch_news_blocking_state()
            with _news_lock:
                prev = _news_state.get("blocked", False)
                _news_state = {"blocked": blocked, "last_check": time.time(), "last_event": evt}
            if blocked and not prev:
                send_telegram_message(f"⚠️ **NEWS ÉCO** : *{evt}*\nEntrées suspendues ±30 min.")
        except Exception as e:
            print(f"News refresher err : {e}")
        time.sleep(NEWS_REFRESH_SEC)


def is_economic_news_coming():
    with _news_lock:
        return _news_state.get("blocked", False)


# Persistance

def save_state():
    try:
        with state_lock:
            data = {
                "session_stats": session_stats,
                "symbol_cooldowns": {s: t.isoformat() for s, t in SYMBOL_COOLDOWNS.items()},
            }
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, default=str)
    except Exception as e:
        print(f"⚠️ save_state : {e}")


def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        session_stats.update(data.get("session_stats", {}))
        if session_stats.get("start_time"):
            session_stats["start_time"] = datetime.datetime.fromisoformat(session_stats["start_time"])
        for s, t in data.get("symbol_cooldowns", {}).items():
            SYMBOL_COOLDOWNS[s] = datetime.datetime.fromisoformat(t)
    except Exception as e:
        print(f"⚠️ load_state : {e}")


# Indicateurs

def compute_indicators(df):
    df = df.copy()
    df['EMA20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['EMA50'] = df['close'].ewm(span=50, adjust=False).mean()

    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))

    high, low, close = df['high'], df['low'], df['close']
    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(ADX_PERIOD).mean()
    plus_di = 100 * (plus_dm.rolling(ADX_PERIOD).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(ADX_PERIOD).mean() / atr)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    df['ADX'] = dx.rolling(ADX_PERIOD).mean()
    df['ATR'] = atr
    df['ATR_AVG'] = atr.rolling(50).mean()
    df['ATR_RATIO'] = df['ATR'] / df['ATR_AVG']
    return df


# Signal Pullback

def check_signal():
    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, 200)
    if rates is None or len(rates) < 60:
        return None, 50.0, 0.0, None

    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df = compute_indicators(df)

    c = df.iloc[-2]           # dernière barre clôturée
    p = df.iloc[-3]

    # Validations NaN
    if pd.isna(c['EMA50']) or pd.isna(c['ADX']) or pd.isna(c['ATR_RATIO']) or pd.isna(c['RSI']):
        return None, 50.0, 0.0, df

    # Filtre jour / session
    t = df['time'].iloc[-2]
    if t.weekday() >= 5:
        return None, c['RSI'], c['ATR'], df
    if not (SESSION_START_UTC <= t.hour < SESSION_END_UTC):
        return None, c['RSI'], c['ATR'], df

    # Filtres régime
    if c['ATR_RATIO'] < ATR_RATIO_MIN:
        return None, c['RSI'], c['ATR'], df
    if c['ADX'] < ADX_MIN:
        return None, c['RSI'], c['ATR'], df

    # Tendance EMA avec pente
    ema50_3ago = df.iloc[-5]['EMA50']
    trend_up = c['EMA20'] > c['EMA50'] and c['EMA50'] > ema50_3ago
    trend_dn = c['EMA20'] < c['EMA50'] and c['EMA50'] < ema50_3ago

    signal = None
    if (trend_up and c['low'] <= c['EMA20'] and c['close'] > c['EMA20']
            and c['close'] > c['open'] and RSI_BUY_MIN < c['RSI'] < RSI_BUY_MAX):
        signal = "BUY"
    elif (trend_dn and c['high'] >= c['EMA20'] and c['close'] < c['EMA20']
          and c['close'] < c['open'] and RSI_SELL_MIN < c['RSI'] < RSI_SELL_MAX):
        signal = "SELL"

    return signal, float(c['RSI']), float(c['ATR']), df


# Soutient MT5

def get_account_balance():
    ai = mt5.account_info()
    return ai.balance if ai else 0.0


def initialize_mt5():
    if not mt5.initialize():
        send_telegram_message(f"❌ Init MT5 échouée ({mt5.last_error()})")
        sys.exit()
    init_filling_modes()
    if not mt5.symbol_select(SYMBOL, True):
        send_telegram_message(f"⚠️ Impossible de sélectionner {SYMBOL}")
    print(f"✅ MT5 connecté — {SYMBOL} M30")


def init_filling_modes():
    info = mt5.symbol_info(SYMBOL)
    if info is None:
        VALID_FILLING_MODES[SYMBOL] = mt5.ORDER_FILLING_IOC
        return
    if info.filling_mode & mt5.SYMBOL_FILLING_IOC:
        VALID_FILLING_MODES[SYMBOL] = mt5.ORDER_FILLING_IOC
    elif info.filling_mode & mt5.SYMBOL_FILLING_FOK:
        VALID_FILLING_MODES[SYMBOL] = mt5.ORDER_FILLING_FOK
    else:
        VALID_FILLING_MODES[SYMBOL] = mt5.ORDER_FILLING_RETURN


def ensure_mt5_connection():
    if not mt5.terminal_info():
        send_telegram_message("⚠️ Perte MT5, reconnexion...")
        for _ in range(10):
            time.sleep(5)
            if mt5.initialize():
                init_filling_modes()
                send_telegram_message("✅ MT5 rétabli.")
                return True
        send_telegram_message("🚨 Impossible de reconnecter MT5.")
        return False
    return True


def safe_order_send(request, max_retries=3):
    last = None
    for _ in range(max_retries):
        result = mt5.order_send(request)
        last = result
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return result
        if result and result.retcode in (mt5.TRADE_RETCODE_REQUOTE,
                                          mt5.TRADE_RETCODE_PRICE_CHANGED,
                                          mt5.TRADE_RETCODE_PRICE_OFF):
            time.sleep(0.5)
            t = mt5.symbol_info_tick(request["symbol"])
            if t:
                request["price"] = t.ask if request["type"] == mt5.ORDER_TYPE_BUY else t.bid
            continue
        return result
    return last


def get_current_spread_dollars():
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return None
    return tick.ask - tick.bid


# Calcul de la poso sur l'or

def calculate_position_size(sl_distance, risk_percent):
    ai = mt5.account_info()
    si = mt5.symbol_info(SYMBOL)
    if ai is None or si is None or sl_distance <= 0:
        return 0.01, 0, 0

    balance = ai.balance
    risk_amount_target = balance * (risk_percent / 100.0)

    tv = si.trade_tick_value
    ts = si.trade_tick_size
    if tv == 0 or ts == 0:
        return 0.01, 0, 0

    value_per_unit = tv / ts
    raw_lot = risk_amount_target / (sl_distance * value_per_unit)
    step = si.volume_step
    min_lot = si.volume_min
    lots = round(raw_lot / step) * step
    lots = max(min_lot, min(lots, si.volume_max))
    lots = round(lots, 2)

    # Perte réelle
    risky_dollars = lots * sl_distance * value_per_unit
    risky_pct = (risky_dollars / balance * 100) if balance > 0 else 0

    return lots, risky_dollars, risky_pct


# Ordre

def send_order(order_type, rsi_val, atr_val, df):
    if not ensure_mt5_connection():
        return
    if is_economic_news_coming():
        print("🛑 Bloqué par news éco")
        return

    balance = get_account_balance()

    can_trade, reason = budget_mgr.can_open_trade(SYMBOL, balance)
    if not can_trade:
        print(f"[{SYMBOL}] Budget refuse : {reason}")
        return

    # Spread check
    spread = get_current_spread_dollars()
    if spread is None:
        return
    if spread > MAX_SPREAD_DOLLARS:
        print(f"[{SYMBOL}] Spread trop élevé : {spread:.2f}$")
        return

    # Vérifier positions actives
    active = mt5.positions_get(group=f"*{MAGIC_NUMBER}*")
    if active and len(active) >= MAX_CONCURRENT_TRADES:
        return

    # Cooldown
    with cooldown_lock:
        if SYMBOL in SYMBOL_COOLDOWNS:
            if datetime.datetime.now() < SYMBOL_COOLDOWNS[SYMBOL] + datetime.timedelta(minutes=COOLDOWN_MINUTES):
                return

    # Confidence mémoire
    confidence, risk_factor, rr_ratio = pm.evaluate_trade_confidence(
        SYMBOL, order_type, rsi_val, atr_val)
    if risk_factor <= 0:
        send_telegram_message(f"🧠 Perry hésite ({confidence:.1f}%) sur {SYMBOL}.")
        return

    # ATR + SL/TP
    sl_distance = atr_val * ATR_SL_MULTIPLIER
    tp_distance = sl_distance * RR_RATIO

    # Risk %
    risk_pct = budget_mgr.get_risk_percent(SYMBOL, balance)
    if risk_pct <= 0:
        return

    # Position size
    lots, risky_dollars, risky_pct = calculate_position_size(sl_distance, risk_pct)

    # Alerte si le lot minimum dépasse le budget
    if risky_pct > 1.5:
        send_telegram_message(
            f"⚠️ **RISQUE ÉLEVÉ** : lot 0.01 sur {SYMBOL} = {risky_dollars:.2f}$ "
            f"({risky_pct:.2f}% du capital)\n"
            f"Capital actuel : {balance:.2f}$"
        )
    if risky_pct > 3.0:
        send_telegram_message(f"🛑 Lot minimum dépasse 3% — trade annulé")
        return

    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return

    if order_type == "BUY":
        price = tick.ask
        sl = price - sl_distance
        tp = price + tp_distance
        type_op = mt5.ORDER_TYPE_BUY
    else:
        price = tick.bid
        sl = price + sl_distance
        tp = price - tp_distance
        type_op = mt5.ORDER_TYPE_SELL

    fill = VALID_FILLING_MODES.get(SYMBOL, mt5.ORDER_FILLING_IOC)
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL, "volume": lots, "type": type_op,
        "price": price, "sl": sl, "tp": tp,
        "magic": MAGIC_NUMBER,
        "comment": f"Perry v3 {order_type}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": fill,
    }
    result = safe_order_send(request)

    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        session_stats["trades_executed"] += 1
        budget_mgr.register_trade_open(SYMBOL)
        time.sleep(0.3)
        for p in mt5.positions_get(symbol=SYMBOL) or []:
            if p.magic == MAGIC_NUMBER:
                ACTIVE_TRACKED_POSITIONS[p.ticket] = {
                    "symbol": SYMBOL, "type": order_type,
                    "rsi": rsi_val, "macd": atr_val,
                    "spread": spread * 100,
                    "entry_price": price,
                    "sl_distance": sl_distance,
                    "be_applied": False,
                    "partial_done": False,
                    "open_time": datetime.datetime.now(),
                }
        send_telegram_message(
            f"🚀 **ORDRE {SYMBOL}** {order_type}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• {lots} lot(s) | Risque {risky_dollars:.2f}$ ({risky_pct:.2f}%)\n"
            f"• Confidence {confidence:.0f}%\n"
            f"• Spread {spread:.2f}$\n"
            f"• Prix {price:.2f} | SL {sl:.2f} | TP {tp:.2f}"
        )
        save_state()


# Fermeture / Suivi

def close_position(position, reason="Close"):
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        return False
    type_op = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if position.type == mt5.ORDER_TYPE_BUY else tick.ask
    fill = VALID_FILLING_MODES.get(position.symbol, mt5.ORDER_FILLING_IOC)
    result = safe_order_send({
        "action": mt5.TRADE_ACTION_DEAL,
        "position": position.ticket, "symbol": position.symbol,
        "volume": position.volume, "type": type_op, "price": price,
        "magic": MAGIC_NUMBER, "comment": f"Perry {reason}",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": fill,
    })
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        with cooldown_lock:
            SYMBOL_COOLDOWNS[position.symbol] = datetime.datetime.now()
        send_telegram_message(f"🔄 **CLÔTURE {position.symbol}** ({reason})")
        return True
    return False


def close_partial(position, fraction=PARTIAL_TP_PCT):
    si = mt5.symbol_info(position.symbol)
    if si is None:
        return False
    vol = round((position.volume * fraction) / si.volume_step) * si.volume_step
    vol = round(vol, 2)
    if vol < si.volume_min or vol >= position.volume:
        return False
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        return False
    type_op = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if position.type == mt5.ORDER_TYPE_BUY else tick.ask
    fill = VALID_FILLING_MODES.get(position.symbol, mt5.ORDER_FILLING_IOC)
    result = safe_order_send({
        "action": mt5.TRADE_ACTION_DEAL,
        "position": position.ticket, "symbol": position.symbol,
        "volume": vol, "type": type_op, "price": price,
        "magic": MAGIC_NUMBER, "comment": "Perry PartialTP",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": fill,
    })
    return bool(result and result.retcode == mt5.TRADE_RETCODE_DONE)


def close_all_open_positions():
    for p in mt5.positions_get(group=f"*{MAGIC_NUMBER}*") or []:
        close_position(p, "Shutdown")


def check_closed_positions():
    if not ACTIVE_TRACKED_POSITIONS:
        return
    current = mt5.positions_get(group=f"*{MAGIC_NUMBER}*")
    curr_t = [p.ticket for p in current] if current else []
    closed = [t for t in list(ACTIVE_TRACKED_POSITIONS.keys()) if t not in curr_t]
    if not closed:
        return

    bal_now = get_account_balance()

    for ticket in closed:
        info = ACTIVE_TRACKED_POSITIONS.pop(ticket)
        with cooldown_lock:
            SYMBOL_COOLDOWNS[info["symbol"]] = datetime.datetime.now()

        hist = mt5.history_deals_get(
            datetime.datetime.now() - datetime.timedelta(days=1),
            datetime.datetime.now() + datetime.timedelta(hours=1))
        profit, comment = 0.0, ""
        if hist:
            for d in hist:
                if d.position_id == ticket and d.entry == mt5.DEAL_ENTRY_OUT:
                    profit = d.profit
                    comment = d.comment
                    break

        exit_type = "STOP_LOSS" if "sl" in comment.lower() else \
                    "TAKE_PROFIT" if "tp" in comment.lower() else "OTHER"
        status = "WIN" if profit > 0 else "LOSS"

        pm.record_experience(
            symbol=info['symbol'], order_type=info['type'],
            rsi_val=info['rsi'], macd_val=info['macd'],
            spread_pips=info['spread'],
            exit_type=exit_type, result_status=status, profit=profit,
            balance_at_trade=bal_now, signal_quality=0)

        budget_mgr.register_trade_close(info["symbol"], profit, bal_now)
        budget_mgr.sync(bal_now)

        emoji = "🎯" if profit > 0 else "💥" if profit < 0 else "⚖️"
        send_telegram_message(
            f"{emoji} **{info['symbol']}** {profit:+.2f} $ ({exit_type})"
        )
        save_state()


def manage_open_positions():
    now = datetime.datetime.now()
    is_fri_eve = now.weekday() == 4 and now.hour >= 20

    positions = mt5.positions_get(group=f"*{MAGIC_NUMBER}*")
    if not positions:
        return

    for pos in positions:
        if is_fri_eve:
            send_telegram_message(f"⚠️ Week-end : clôture {pos.symbol}")
            close_position(pos, "Weekend")
            continue

        t = pos.ticket
        if t not in ACTIVE_TRACKED_POSITIONS:
            continue
        info = ACTIVE_TRACKED_POSITIONS[t]
        si = mt5.symbol_info(pos.symbol)
        tick = mt5.symbol_info_tick(pos.symbol)
        atr = pm.get_atr_value(pos.symbol)
        if not (si and tick and atr):
            continue

        entry = info["entry_price"]
        sl_dist = info["sl_distance"]

        # Time-stop
        op_time = info.get("open_time")
        if op_time and (now - op_time).total_seconds() > TIME_STOP_HOURS * 3600:
            profit_dist = (tick.bid - entry) if pos.type == mt5.ORDER_TYPE_BUY else (entry - tick.ask)
            if profit_dist < sl_dist * 0.3:
                close_position(pos, "TimeStop")
                continue

        if pos.type == mt5.ORDER_TYPE_BUY:
            prof = tick.bid - entry
            if not info.get("partial_done") and prof >= sl_dist * PARTIAL_TP_R:
                if close_partial(pos):
                    info["partial_done"] = True
                    send_telegram_message(f"💰 TP partiel 50% sur {pos.symbol}")
            if not info["be_applied"] and prof >= sl_dist * ATR_BE_TRIGGER:
                new_sl = entry + (si.point * 10)
                min_dist = si.trade_stops_level * si.point
                if tick.bid - new_sl < min_dist:
                    new_sl = tick.bid - min_dist
                r = safe_order_send({"action": mt5.TRADE_ACTION_SLTP,
                                     "position": t, "sl": new_sl, "tp": pos.tp})
                if r and r.retcode == mt5.TRADE_RETCODE_DONE:
                    info["be_applied"] = True
                    send_telegram_message(f"🛡️ BE {pos.symbol} → {new_sl:.2f}")
            if info["be_applied"]:
                trail = tick.bid - atr * ATR_TRAILING_MULTIPLIER
                if trail > pos.sl:
                    safe_order_send({"action": mt5.TRADE_ACTION_SLTP,
                                     "position": t, "sl": trail, "tp": pos.tp})

        elif pos.type == mt5.ORDER_TYPE_SELL:
            prof = entry - tick.ask
            if not info.get("partial_done") and prof >= sl_dist * PARTIAL_TP_R:
                if close_partial(pos):
                    info["partial_done"] = True
                    send_telegram_message(f"💰 TP partiel 50% sur {pos.symbol}")
            if not info["be_applied"] and prof >= sl_dist * ATR_BE_TRIGGER:
                new_sl = entry - (si.point * 10)
                min_dist = si.trade_stops_level * si.point
                if new_sl - tick.ask < min_dist:
                    new_sl = tick.ask + min_dist
                r = safe_order_send({"action": mt5.TRADE_ACTION_SLTP,
                                     "position": t, "sl": new_sl, "tp": pos.tp})
                if r and r.retcode == mt5.TRADE_RETCODE_DONE:
                    info["be_applied"] = True
                    send_telegram_message(f"🛡️ BE {pos.symbol} → {new_sl:.2f}")
            if info["be_applied"]:
                trail = tick.ask + atr * ATR_TRAILING_MULTIPLIER
                if pos.sl == 0 or trail < pos.sl:
                    safe_order_send({"action": mt5.TRADE_ACTION_SLTP,
                                     "position": t, "sl": trail, "tp": pos.tp})


# Rapport
def update_closed_trades_stats():
    from_date = session_stats.get("start_time", datetime.datetime.now())
    to_date = datetime.datetime.now() + datetime.timedelta(hours=1)
    hist = mt5.history_deals_get(from_date, to_date)
    if hist is None:
        return
    w = l = 0
    for d in hist:
        if d.entry == mt5.DEAL_ENTRY_OUT and d.magic == MAGIC_NUMBER:
            if d.profit > 0: w += 1
            elif d.profit < 0: l += 1
    session_stats["closed_wins"] = w
    session_stats["closed_losses"] = l


def send_session_summary(header="📊 **BILAN**"):
    update_closed_trades_stats()
    cur = get_account_balance()
    init = session_stats["initial_balance"]
    pnl = cur - init
    pct = (pnl / init * 100) if init > 0 else 0
    w = session_stats.get("closed_wins", 0)
    l = session_stats.get("closed_losses", 0)
    t = w + l
    wr = (w / t * 100) if t > 0 else 0

    send_telegram_message(
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Initial : {init:.2f} $\n"
        f"💵 Actuel  : {cur:.2f} $\n"
        f"📈 PnL    : {pnl:+.2f} $ ({pct:+.2f}%)\n"
        f"✅ Trades : {t} (W:{w} | L:{l})\n"
        f"🎯 Win Rate : {wr:.1f} %"
    )


def send_budget_status():
    cur = get_account_balance()
    s = budget_mgr.get_status(cur)
    pause = "⏸️ PAUSE" if s["paused"] else "🟢 Actif"
    if s["paused"]:
        pause += f" jusqu'à {s['pause_until']}"
    msg = (
        f"💼 **BUDGET**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Solde : {s['balance']:.2f} $\n"
        f"📈 Pic   : {s['peak']:.2f} $\n"
        f"📉 DD    : {s['drawdown_pct']:.2f} %\n"
        f"🎯 Risque base : {s['base_risk_pct']:.2f} %\n"
        f"⚙️ Risque eff. : {s['effective_risk_pct']:.2f} %\n"
        f"📊 Trades jour : {s['day_trades']} / {s['day_max']}\n"
        f"🔥 W:{s['consec_wins']} | L:{s['consec_losses']}\n"
    )
    if s["recovery_mode"]:
        msg += f"♻️ Récupération : {s['recovery_left']} trades\n"
    msg += f"🚦 {pause}"
    send_telegram_message(msg)


def send_telegram_state_status():
    conn = "✅ Connecté" if mt5.terminal_info() else "🚨 Déconnecté"
    positions = mt5.positions_get(group=f"*{MAGIC_NUMBER}*")
    nb = len(positions) if positions else 0
    spread = get_current_spread_dollars()
    spread_str = f"{spread:.2f} $" if spread else "?"
    up = session_stats['start_time'].strftime('%d/%m %H:%M') if session_stats.get('start_time') else "?"
    send_telegram_message(
        f"🖥🐻 **ÉTAT PERRY v3**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔌 MT5 : {conn}\n"
        f"💱 Symbole : {SYMBOL} M30\n"
        f"📊 Position : {nb} / {MAX_CONCURRENT_TRADES}\n"
        f"📏 Spread : {spread_str}\n"
        f"⏱️ Session : {up}\n"
        f"💰 Solde : {get_account_balance():.2f} $"
    )


def send_technical_status():
    _, rsi, atr, df = check_signal()
    if df is None:
        send_telegram_message("⚠️ Pas de données")
        return
    c = df.iloc[-2]
    t = df['time'].iloc[-2]
    msg = (
        f"🔬🐻 **ANALYSE TECHNIQUE {SYMBOL}**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ Barre : {t.strftime('%d/%m %H:%M')} UTC\n"
        f"💵 Prix : {c['close']:.2f}\n"
        f"📈 EMA20 : {c['EMA20']:.2f}\n"
        f"📈 EMA50 : {c['EMA50']:.2f}\n"
        f"📊 RSI : {c['RSI']:.1f}\n"
        f"📊 ADX : {c['ADX']:.1f}\n"
        f"📊 ATR : {c['ATR']:.2f}$\n"
        f"📊 ATR ratio : {c['ATR_RATIO']:.2f}\n"
        f"Tendance : {'UP' if c['EMA20'] > c['EMA50'] else 'DOWN'}"
    )
    send_telegram_message(msg)


# Suiveur de Telegram

def listen_telegram_commands():
    global is_running, pull_requested
    last_id = 0
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", timeout=5).json()
        if "result" in r and r["result"]:
            last_id = r["result"][-1]["update_id"]
    except Exception:
        pass

    while is_running:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_id+1}&timeout=10"
            r = requests.get(url, timeout=12).json()
            if "result" not in r:
                continue

            for upd in r["result"]:
                last_id = upd["update_id"]
                if "message" not in upd or "text" not in upd["message"]:
                    continue
                text = upd["message"]["text"].strip().lower()
                chat = str(upd["message"]["chat"]["id"])
                if chat != str(TELEGRAM_CHAT_ID):
                    continue

                if text == "/start":
                    bal = get_account_balance()
                    session_stats["start_time"] = datetime.datetime.now()
                    session_stats["initial_balance"] = bal
                    session_stats["trades_executed"] = 0
                    session_stats["closed_wins"] = 0
                    session_stats["closed_losses"] = 0
                    budget_mgr.reset_day(bal)
                    budget_mgr.sync(bal)
                    save_state()
                    send_telegram_message(
                        f"🚀 **SESSION PERRY v3**\n"
                        f"• {SYMBOL} M30 Pullback\n"
                        f"• Capital : {bal:.2f} $\n\n"
                        f"/budget /state /status /tech /stop /pull"
                    )
                elif text == "/budget": send_budget_status()
                elif text == "/state":  send_telegram_state_status()
                elif text == "/status": send_session_summary()
                elif text == "/tech":   send_technical_status()
                elif text == "/pull":
                    send_telegram_message("🔄 git pull. Arrêt sécurisé...")
                    pull_requested = True
                    is_running = False
                elif text == "/stop":
                    send_telegram_message("🛑 Arrêt demandé.")
                    is_running = False
        except Exception:
            time.sleep(2)


# Lancement des OPs Agent P
if __name__ == "__main__":
    initialize_mt5()
    pm.init_db()
    load_state()
    restore_tracked_positions = None  # désactivé en v3 (mono-actif)

    # Reprendre positions existantes
    existing = mt5.positions_get(group=f"*{MAGIC_NUMBER}*")
    if existing:
        for p in existing:
            si = mt5.symbol_info(p.symbol)
            point = si.point if si else 0.01
            sl_dist = abs(p.price_open - p.sl) if p.sl > 0 else (300 * point)
            be_done = (p.type == mt5.ORDER_TYPE_BUY and p.sl >= p.price_open) or \
                      (p.type == mt5.ORDER_TYPE_SELL and p.sl <= p.price_open)
            ACTIVE_TRACKED_POSITIONS[p.ticket] = {
                "symbol": p.symbol,
                "type": "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
                "rsi": 50.0, "macd": 0.0, "spread": 30,
                "entry_price": p.price_open, "sl_distance": sl_dist,
                "be_applied": be_done, "partial_done": False,
                "open_time": datetime.datetime.now(),
            }
        print(f"🔄 {len(existing)} position(s) reprise(s).")

    bal = get_account_balance()
    budget_mgr.sync(bal)

    if not session_stats.get("initial_balance"):
        session_stats["start_time"] = datetime.datetime.now()
        session_stats["initial_balance"] = bal
    save_state()

    threading.Thread(target=_telegram_worker, daemon=True).start()
    threading.Thread(target=news_refresher_thread, daemon=True).start()
    threading.Thread(target=listen_telegram_commands, daemon=True).start()

    spread = get_current_spread_dollars()
    send_telegram_message(
        f"🐻 **PERRY v3 DÉMARRÉ**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• Actif : **{SYMBOL}** M30\n"
        f"• Stratégie : Pullback EMA20 + RSI + ADX + ATR\n"
        f"• RR : 1:{RR_RATIO}\n"
        f"• Solde : {bal:.2f} $\n"
        f"• Spread : {spread:.2f} $\n\n"
        f"/budget /state /status /tech /stop /pull"
    )

    try:
        while is_running:
            t0 = time.time()

            if not ensure_mt5_connection():
                time.sleep(10)
                continue

            bal = get_account_balance()
            budget_mgr.sync(bal)

            if bal < MIN_BALANCE_STOP:
                send_telegram_message(f"⚠️ Solde {bal:.2f}$ < {MIN_BALANCE_STOP}$ → arrêt.")
                is_running = False
                break

            check_closed_positions()
            manage_open_positions()
            budget_mgr.flush_pending()

            can_trade, reason = budget_mgr.can_open_trade(SYMBOL, bal)
            if can_trade:
                signal, rsi_v, atr_v, df = check_signal()
                positions = mt5.positions_get(symbol=SYMBOL)

                if positions and len(positions) > 0:
                    for pos in positions:
                        if pos.magic != MAGIC_NUMBER:
                            continue
                        # Signal opposé → on clôture
                        pos_type = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
                        if signal and signal != pos_type:
                            if close_position(pos, "Reversal"):
                                time.sleep(1)
                                send_order(signal, rsi_v, atr_v, df)
                else:
                    if signal in ["BUY", "SELL"]:
                        send_order(signal, rsi_v, atr_v, df)

            elapsed = time.time() - t0
            if elapsed > 5.0:
                print(f"⚠️ Cycle lent : {elapsed:.2f}s")

            time.sleep(CYCLE_SLEEP_SEC)

    except KeyboardInterrupt:
        print("Arrêt manuel.")

    # Cleanup
    print("🧹 Cleanup...")
    if pull_requested:
        save_state()
        budget_mgr.flush_pending()
        flush_telegram(3.0)
        mt5.shutdown()
        sys.exit(99)
    else:
        try:
            close_all_open_positions()
        except Exception as e:
            print(f"Cleanup : {e}")
        send_session_summary("📊 **BILAN FINAL**")
        save_state()
        budget_mgr.flush_pending()
        flush_telegram(3.0)
        mt5.shutdown()
        sys.exit(0)