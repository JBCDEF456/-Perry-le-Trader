import time
import sys
import datetime
import threading
import requests
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import perry_memory as pm

# Params Telegram et Tactiques de Jeu
TELEGRAM_TOKEN = "8803422841:AAGQklfLQfkbFkqrVkrnBVYWwT9VCGCFR6k"
TELEGRAM_CHAT_ID = "6552751822"
FMP_API_KEY = "c4MfYeznxUSNi4F8doGCoXw7VCb3xvzo"

SYMBOLS_DAY = ["EURUSD", "GBPUSD", "USDCHF"]
SYMBOLS_NIGHT = ["USDJPY", "AUDUSD", "NZDUSD", "AUDJPY"]
SYMBOLS = SYMBOLS_DAY + SYMBOLS_NIGHT

TIMEFRAME = mt5.TIMEFRAME_M5

# Gestion du risque & Sécurités
RISK_PERCENT = 1.0
MIN_BALANCE_STOP = 5.0
MAX_DAILY_LOSS_PCT = 10.0
MAX_CONSECUTIVE_LOSSES = 3
PAUSE_HOURS_AFTER_LOSSES = 2

# Paramètres ATR
ATR_SL_MULTIPLIER = 1.5
ATR_TRAILING_MULTIPLIER = 1.2

MAX_SPREAD_PIPS = 2.0
MAX_CONCURRENT_TRADES = 3
COOLDOWN_MINUTES = 15
MAGIC_NUMBER = 998877

is_running = True

# Mémoires locales
VALID_FILLING_MODES = {}
ACTIVE_TRACKED_POSITIONS = {}
SYMBOL_COOLDOWNS = {}
consecutive_losses = 0
bot_pause_until = None

session_stats = {
    "start_time": None,
    "initial_balance": 0.0,
    "trades_executed": 0,
    "closed_wins": 0,
    "closed_losses": 0
}


# Filtre avancés dans les ordres

def calculate_adx(high, low, close, period=14):
    plus_dm = np.diff(high)
    minus_dm = -np.diff(low)

    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0

    plus_dm[plus_dm <= minus_dm] = 0
    minus_dm[minus_dm <= plus_dm] = 0

    tr1 = high[1:] - low[1:]
    tr2 = np.abs(high[1:] - close[:-1])
    tr3 = np.abs(low[1:] - close[:-1])
    tr = np.maximum(tr1, np.maximum(tr2, tr3))

    def wilder_smoothing(arr, n):
        smoothed = np.zeros_like(arr)
        if len(arr) < n:
            return smoothed
        smoothed[n - 1] = np.sum(arr[:n])
        for i in range(n, len(arr)):
            smoothed[i] = smoothed[i - 1] - (smoothed[i - 1] / n) + arr[i]
        return smoothed

    if len(tr) < period * 2:
        return 25.0

    tr_smoothed = wilder_smoothing(tr, period)
    plus_di = 100 * wilder_smoothing(plus_dm, period) / (tr_smoothed + 1e-9)
    minus_di = 100 * wilder_smoothing(minus_dm, period) / (tr_smoothed + 1e-9)

    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-9)
    adx = np.mean(dx[-period:])

    return float(adx)


def check_strict_trend_and_adx(symbol, timeframe_h1=mt5.TIMEFRAME_H1, period=14, adx_threshold=22.0):
    rates = mt5.copy_rates_from_pos(symbol, timeframe_h1, 0, period * 3)
    if rates is None or len(rates) < period * 2:
        return False, "Données H1 insuffisantes"

    close_prices = rates['close']
    high_prices = rates['high']
    low_prices = rates['low']

    sma_20 = np.mean(close_prices[-20:])
    sma_50 = np.mean(close_prices[-50:]) if len(close_prices) >= 50 else np.mean(close_prices)
    current_price = close_prices[-1]

    is_bullish_trend = (current_price > sma_20) and (sma_20 > sma_50)
    is_bearish_trend = (current_price < sma_20) and (sma_20 < sma_50)

    if not is_bullish_trend and not is_bearish_trend:
        return False, "Marché en consolidation en H1"

    adx_value = calculate_adx(high_prices, low_prices, close_prices, period)
    if adx_value < adx_threshold:
        return False, f"Tendance H1 trop faible (ADX: {adx_value:.1f})"

    trend_direction = "BUY" if is_bullish_trend else "SELL"
    return True, trend_direction


# Gestion notification telegram

def update_closed_trades_stats():
    from_date = session_stats.get("start_time", datetime.datetime.now())
    to_date = datetime.datetime.now() + datetime.timedelta(hours=1)
    history = mt5.history_deals_get(from_date, to_date)

    if history is None or len(history) == 0:
        return

    closed_wins = 0
    closed_losses = 0

    for deal in history:
        if deal.entry == mt5.DEAL_ENTRY_OUT and deal.magic == MAGIC_NUMBER:
            if deal.profit > 0:
                closed_wins += 1
            elif deal.profit < 0:
                closed_losses += 1

    session_stats["closed_wins"] = closed_wins
    session_stats["closed_losses"] = closed_losses


def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Erreur d'envoi Telegram : {e}")


def send_telegram_kpi_report():
    try:
        import sqlite3

        now = datetime.datetime.now()
        start_of_week = (now - datetime.timedelta(days=now.weekday())).strftime("%Y-%m-%d 00:00:00")

        conn = sqlite3.connect(pm.DB_FILE)
        query = f"""
            SELECT profit, result, timestamp 
            FROM trade_experience 
            WHERE profit IS NOT NULL AND profit != 0 
              AND timestamp >= '{start_of_week}'
        """
        df = pd.read_sql_query(query, conn)
        conn.close()

        if df.empty:
            send_telegram_message("📊 **KPI SEMAINE EN COURS**\n\n⚠️ Aucun trade clôturé enregistré depuis ce lundi.")
            return

        profits = df['profit'].values
        gains = profits[profits > 0]
        pertes = profits[profits < 0]

        gross_profits = np.sum(gains) if len(gains) > 0 else 0.0
        gross_losses = abs(np.sum(pertes)) if len(pertes) > 0 else 1e-6
        profit_factor = gross_profits / gross_losses

        initial_cap = session_stats.get("initial_balance", 1000.0)
        if initial_cap <= 0:
            initial_cap = get_account_balance()
            if initial_cap <= 0:
                initial_cap = 1000.0

        equity_curve = np.cumsum(profits) + initial_cap
        peak = np.maximum.accumulate(equity_curve)
        drawdown = (equity_curve - peak) / peak
        max_drawdown_pct = abs(np.min(drawdown)) * 100.0 if len(drawdown) > 0 else 0.0

        returns = profits / initial_cap
        mean_return = np.mean(returns)
        std_return = np.std(returns, ddof=1) if len(returns) > 1 else 0.0

        downside_returns = returns[returns < 0]
        downside_std = np.std(downside_returns, ddof=1) if len(downside_returns) > 1 else 0.0

        rf_per_trade = 0.0
        sharpe_ratio = (mean_return - rf_per_trade) / std_return if std_return > 1e-8 else 0.0
        sortino_ratio = (mean_return - rf_per_trade) / downside_std if downside_std > 1e-8 else 0.0

        sharpe_ratio = max(min(sharpe_ratio, 100.0), -100.0)
        sortino_ratio = max(min(sortino_ratio, 100.0), -100.0)
        win_rate = (len(gains) / len(profits)) * 100.0

        msg = (
            f"📊 **PERFORMANCES KPI (SEMAINE EN COURS)**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱️ **Période** : Depuis le {start_of_week[:10]}\n"
            f"📈 **Trades exécutés** : {len(profits)}\n"
            f"🎯 **Win Rate** : {win_rate:.1f} %\n"
            f"💰 **Profit Factor** : {profit_factor:.2f}  ({'✅' if profit_factor >= 1.5 else '⚠️'})\n"
            f"📉 **Max Drawdown** : {max_drawdown_pct:.2f} %\n"
            f"📐 **Sharpe Ratio** : {sharpe_ratio:.2f}\n"
            f"📊 **Sortino Ratio** : {sortino_ratio:.2f}\n"
            f"💵 **PnL Semaine** : {np.sum(profits):+.2f} $"
        )
        send_telegram_message(msg)

    except Exception as e:
        send_telegram_message(f"❌ **Erreur lors du calcul des KPI** : {e}")


def send_telegram_state_status():
    mt5_conn = "✅ Connecté" if mt5.terminal_info() else "🚨 Déconnecté"

    if bot_pause_until and datetime.datetime.now() < bot_pause_until:
        bot_state = f"⏸️ En pause jusqu'à {bot_pause_until.strftime('%H:%M:%S')}"
    else:
        bot_state = "🟢 Actif & En recherche"

    active_positions = mt5.positions_get(group=f"*{MAGIC_NUMBER}*")
    nb_open = len(active_positions) if active_positions else 0
    uptime_str = session_stats['start_time'].strftime('%d/%m à %H:%M:%S') if session_stats.get(
        'start_time') else "Inconnu"

    msg = (
        f"🖥️ **SANTÉ SYSTÈME & ÉTAT DE PERRY**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🐻 **État de Perry** : {bot_state}\n"
        f"🔌 **Connexion MT5** : {mt5_conn}\n"
        f"📊 **Positions Ouvertes** : {nb_open} / {MAX_CONCURRENT_TRADES}\n"
        f"⏱️ **Lancement Session** : {uptime_str}\n"
        f"💰 **Solde Compte** : {get_account_balance():.2f} $"
    )
    send_telegram_message(msg)


def send_session_summary(header_title="📊 **BILAN DE SESSION**"):
    update_closed_trades_stats()

    current_balance = get_account_balance()
    initial_balance = session_stats["initial_balance"]
    pnl = current_balance - initial_balance
    pnl_pct = ((current_balance - initial_balance) / initial_balance) * 100 if initial_balance > 0 else 0

    wins = session_stats.get("closed_wins", 0)
    losses = session_stats.get("closed_losses", 0)
    total_closed = wins + losses
    win_rate = (wins / total_closed * 100) if total_closed > 0 else 0.0

    msg = (
        f"{header_title}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 **Capital initial** : {initial_balance:.2f} $\n"
        f"💵 **Capital actuel** : {current_balance:.2f} $\n"
        f"📈 **PnL** : {pnl:+.2f} $ ({pnl_pct:+.4f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ **Trades clôturés** : {total_closed} (Gagnants: {wins} | Perdants: {losses})\n"
        f"🎯 **Taux de réussite** : {win_rate:.1f} %\n"
    )
    send_telegram_message(msg)


def listen_telegram_commands():
    global is_running
    last_update_id = 0

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        response = requests.get(url, timeout=5).json()
        if "result" in response and len(response["result"]) > 0:
            last_update_id = response["result"][-1]["update_id"]
    except Exception as e:
        print(f"Erreur vidange Telegram : {e}")

    while is_running:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=10"
            response = requests.get(url, timeout=12).json()

            if "result" in response:
                for update in response["result"]:
                    last_update_id = update["update_id"]

                    if "message" in update and "text" in update["message"]:
                        text = update["message"]["text"].strip().lower()
                        chat_id = str(update["message"]["chat"]["id"])

                        if chat_id == str(TELEGRAM_CHAT_ID):
                            if text == "/start":
                                session_stats["start_time"] = datetime.datetime.now()
                                session_stats["initial_balance"] = get_account_balance()
                                session_stats["trades_executed"] = 0

                                pairs_str = ", ".join(SYMBOLS)
                                start_msg = (
                                    f"🚀 **SESSION DE TRADING INITIALISÉE**\n"
                                    f"━━━━━━━━━━━━━━━━━━━━\n"
                                    f"• **Paires surveillées** : {pairs_str}\n"
                                    f"• **Timeframe** : M5\n"
                                    f"• **Solde Initial** : {session_stats['initial_balance']:.2f} $\n\n"
                                    f"📱 **Commandes disponibles :**\n"
                                    f"• `/status` : Bilan en direct de la session\n"
                                    f"• `/start` : Réinitialiser la session à zéro\n"
                                    f"• `/stop` : Arrêter le bot et afficher le bilan final"
                                )
                                send_telegram_message(start_msg)

                            elif text == "/kpi":
                                send_telegram_kpi_report()

                            elif text == "/state":
                                send_telegram_state_status()

                            elif text == "/status":
                                send_session_summary("ℹ️ **STATUT EN DIRECT (SESSION EN COURS)**")

                            elif text == "/stop":
                                send_telegram_message("🛑 **Le patron veut que je m'arrête !!!** Arrêt en cours...")
                                is_running = False
                                send_session_summary("📊 **BILAN FINAL DE SESSION**")
                                time.sleep(1.5)
                                mt5.shutdown()
                                print("Bot arrêté avec succès.")
                                break

        except Exception:
            time.sleep(2)


# Filtre economique et la securite

def is_economic_news_coming():
    if not FMP_API_KEY or FMP_API_KEY == "c4MfYeznxUSNi4F8doGCoXw7VCb3xyzo":
        return False

    try:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        url = f"https://financialmodelingprep.com/api/v3/economic_calendar?from={today}&to={today}&apikey={FMP_API_KEY}"
        res = requests.get(url, timeout=5)
        if res.status_code != 200:
            return False

        events = res.json()
        now = datetime.datetime.utcnow()

        for event in events:
            if event.get("impact") == "High" and event.get("currency") in ["USD", "EUR", "GBP", "JPY", "AUD", "NZD",
                                                                           "CAD"]:
                event_date_str = event.get("date")
                if event_date_str:
                    event_time = datetime.datetime.strptime(event_date_str, "%Y-%m-%d %H:%M:%S")
                    diff_minutes = abs((event_time - now).total_seconds()) / 60.0
                    if diff_minutes <= 30:
                        event_title = event.get("event", "Impact Majeur")
                        send_telegram_message(
                            f"⚠️ **FILTRE ÉCO ACTIF**\n\nAnnonce imminente : *{event_title}* dans {int(diff_minutes)} min.\nPrise de position suspendue.")
                        return True
    except Exception as e:
        print(f"Erreur vérification calendrier éco : {e}")

    return False


def is_trading_session_active(symbol):
    current_hour = datetime.datetime.now().hour
    if current_hour == 22:
        return False

    if symbol in SYMBOLS_DAY:
        return 8 <= current_hour < 18
    elif symbol in SYMBOLS_NIGHT:
        return current_hour >= 23 or current_hour < 8

    return True


def is_in_cooldown(symbol):
    if symbol in SYMBOL_COOLDOWNS:
        last_closed_time = SYMBOL_COOLDOWNS[symbol]
        if datetime.datetime.now() < last_closed_time + datetime.timedelta(minutes=COOLDOWN_MINUTES):
            return True
        else:
            del SYMBOL_COOLDOWNS[symbol]
    return False


def check_funds_and_loss_safety():
    global is_running
    account_info = mt5.account_info()
    if account_info is None:
        return True

    balance = account_info.balance
    equity = account_info.equity
    initial_balance = session_stats["initial_balance"]

    if balance <= MIN_BALANCE_STOP or equity <= MIN_BALANCE_STOP:
        msg = (
            f"⚠️ **ARRÊT D'URGENCE - FONDS ÉPUISÉS / INSUFFISANTS** ⚠️\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• **Solde actuel** : {balance:.2f} $\n"
            f"• **Équité actuelle** : {equity:.2f} $\n"
            f"• **Seuil critique** : {MIN_BALANCE_STOP:.2f} $\n\n"
            f"🔴 *Perry s'arrête pour protéger votre compte.*"
        )
        send_telegram_message(msg)
        close_all_open_positions()
        send_session_summary("📊 **BILAN FINAL DE SESSION (SOLDE INSUFFISANT)**")
        is_running = False
        mt5.shutdown()
        return False

    if initial_balance > 0:
        loss_pct = ((initial_balance - balance) / initial_balance) * 100
        if loss_pct >= MAX_DAILY_LOSS_PCT:
            msg = (
                f"🚨 **ARRÊT DE SÉCURITÉ - PERTE SEUIL ATTEINTE ({MAX_DAILY_LOSS_PCT:.1f}%)** 🚨\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• **Perte accumulée** : -{loss_pct:.2f}%\n"
                f"• **Solde initial** : {initial_balance:.2f} $\n"
                f"• **Solde actuel** : {balance:.2f} $\n\n"
                f"⏸ *Perry s'arrête définitivement pour aujourd'hui.*"
            )
            send_telegram_message(msg)
            close_all_open_positions()
            send_session_summary("📊 **BILAN FINAL DE SESSION (PERTE MAX DÉPASSÉE)**")
            is_running = False
            mt5.shutdown()
            return False

    return True


def close_all_open_positions():
    positions = mt5.positions_get(group=f"*{MAGIC_NUMBER}*")
    if positions:
        for pos in positions:
            close_position(pos)


# Moteur de trading de Perry

def initialize_mt5():
    if not mt5.initialize():
        msg = f"❌ **Échec d'initialisation MT5** (code {mt5.last_error()})"
        print(msg)
        send_telegram_message(msg)
        mt5.shutdown()
        sys.exit()
    print("Connexion réussie à MetaTrader 5 !")


def ensure_mt5_connection():
    if not mt5.terminal_info():
        send_telegram_message("⚠️ **ALERTE VPS** : Perte de connexion MT5. Reconnexion en cours...")
        attempts = 0
        while attempts < 10:
            time.sleep(5)
            if mt5.initialize():
                send_telegram_message("✅ **SUCCÈS** : Connexion MT5 rétablie avec succès.")
                return True
            attempts += 1

        send_telegram_message("🚨 **ERREUR CRITIQUE** : Impossible de reconnecter MT5 sur le VPS !")
        return False
    return True


def get_account_balance():
    account_info = mt5.account_info()
    return account_info.balance if account_info else 0.0


def calculate_position_size(symbol, sl_distance):
    account_info = mt5.account_info()
    symbol_info = mt5.symbol_info(symbol)

    if account_info is None or symbol_info is None or sl_distance <= 0:
        return 0.01

    balance = account_info.balance
    risk_amount = balance * (RISK_PERCENT / 100.0)

    tick_value = symbol_info.trade_tick_value
    tick_size = symbol_info.trade_tick_size

    if tick_value == 0 or tick_size == 0:
        return 0.01

    value_per_unit = tick_value / tick_size
    raw_lot = risk_amount / (sl_distance * value_per_unit)

    step_lot = symbol_info.volume_step
    min_lot = symbol_info.volume_min
    max_lot = symbol_info.volume_max

    calculated_lot = round(raw_lot / step_lot) * step_lot
    return round(max(min_lot, min(calculated_lot, max_lot)), 2)


def check_signal(symbol):
    now = datetime.datetime.now()
    day_of_week = now.weekday()
    current_hour = now.hour

    if day_of_week == 4 and current_hour >= 21:
        return None, None, None
    if day_of_week in [5, 6]:
        return None, None, None

    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, 100)
    if rates is None or len(rates) < 50:
        return None, 50.0, 0.0

    df = pd.DataFrame(rates)
    df['SMA20'] = df['close'].rolling(window=20).mean()
    df['SMA50'] = df['close'].rolling(window=50).mean()

    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))

    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    df['MACD'] = macd_line - signal_line

    prev = df.iloc[-3]
    curr = df.iloc[-2]

    golden_cross = (prev['SMA20'] <= prev['SMA50']) and (curr['SMA20'] > curr['SMA50'])
    death_cross = (prev['SMA20'] >= prev['SMA50']) and (curr['SMA20'] < curr['SMA50'])
    rsi_val = curr['RSI']
    macd_val = curr['MACD']

    if golden_cross and (45 < rsi_val < 70):
        return "BUY", rsi_val, macd_val
    elif death_cross and (30 < rsi_val < 55):
        return "SELL", rsi_val, macd_val

    return None, rsi_val, macd_val


def send_order(symbol, order_type, rsi_val, macd_val):
    global bot_pause_until

    if not ensure_mt5_connection():
        return

    if bot_pause_until and datetime.datetime.now() < bot_pause_until:
        return

    if is_economic_news_coming():
        return

    # FILTRE DE TENDANCE STRICT H1 + ADX
    can_trade, trend_res = check_strict_trend_and_adx(symbol)
    if not can_trade:
        send_telegram_message(f"🚫 **Filtre H1/ADX** [{symbol}] : {trend_res}")
        return

    if order_type != trend_res:
        return

    confidence, risk_factor, rr_ratio = pm.evaluate_trade_confidence(symbol, order_type, rsi_val, macd_val)

    if risk_factor == 0.0:
        msg_hesit = f"🧠 **PERRY HÉSITE** ({confidence:.1f}% de confiance) sur **{symbol}**.\n\n_Signal {order_type} ignoré pour protéger le capital._"
        send_telegram_message(msg_hesit)
        return

    if not is_trading_session_active(symbol):
        return

    if is_in_cooldown(symbol):
        return

    active_positions = mt5.positions_get(group=f"*{MAGIC_NUMBER}*")
    if active_positions and len(active_positions) >= MAX_CONCURRENT_TRADES:
        return

    if not mt5.symbol_select(symbol, True):
        return

    tick = mt5.symbol_info_tick(symbol)
    symbol_info = mt5.symbol_info(symbol)

    if tick is None or symbol_info is None:
        return

    point = symbol_info.point
    current_spread_pips = (tick.ask - tick.bid) / point / 10.0
    if current_spread_pips > MAX_SPREAD_PIPS:
        return

    atr_val = pm.get_atr_value(symbol)
    if atr_val is None:
        return

    sl_distance = atr_val * ATR_SL_MULTIPLIER
    tp_distance = sl_distance * rr_ratio

    base_lot = calculate_position_size(symbol, sl_distance)
    final_lot = max(symbol_info.volume_min, round(base_lot * risk_factor, 2))

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

    fill_mode = VALID_FILLING_MODES.get(symbol, mt5.ORDER_FILLING_IOC)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": final_lot,
        "type": type_op,
        "price": price,
        "sl": sl,
        "tp": tp,
        "magic": MAGIC_NUMBER,
        "comment": f"Perry ({symbol} Conf: {int(confidence)}%)",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": fill_mode,
    }

    result = mt5.order_send(request)

    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        session_stats["trades_executed"] += 1
        time.sleep(0.2)
        positions = mt5.positions_get(symbol=symbol)
        if positions:
            for p in positions:
                if p.magic == MAGIC_NUMBER:
                    ACTIVE_TRACKED_POSITIONS[p.ticket] = {
                        "symbol": symbol,
                        "type": order_type,
                        "rsi": rsi_val,
                        "macd": macd_val,
                        "spread": current_spread_pips,
                        "entry_price": price,
                        "sl_distance": sl_distance,
                        "be_applied": False
                    }

        msg = (
            f"🚀 **ORDRE EXÉCUTÉ [{symbol}]**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• **Type** : {order_type} | **Volume** : {final_lot} lot(s)\n"
            f"• **Confiance Mémoire** : {confidence:.1f}%\n"
            f"• **Spread d'entrée** : {current_spread_pips:.1f} pips | **R:R** : 1:{rr_ratio}\n"
            f"• **Prix** : {price:.5f} | **SL** : {sl:.5f} | **TP** : {tp:.5f}"
        )
        send_telegram_message(msg)


def close_position(position):
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        return False

    type_op = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if position.type == mt5.ORDER_TYPE_BUY else tick.ask
    fill_mode = VALID_FILLING_MODES.get(position.symbol, mt5.ORDER_FILLING_IOC)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": position.ticket,
        "symbol": position.symbol,
        "volume": position.volume,
        "type": type_op,
        "price": price,
        "magic": MAGIC_NUMBER,
        "comment": "Perry Close Signal Oppose",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": fill_mode,
    }

    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        SYMBOL_COOLDOWNS[position.symbol] = datetime.datetime.now()
        send_telegram_message(
            f"🔄 **CLÔTURE ANTICIPÉE**\n━━━━━━━━━━━━━━━━━━━━\n• **Symbole** : {position.symbol}\n• **Raison** : Signal opposé ou clôture de sécurité")
        return True
    return False


def check_closed_positions():
    global consecutive_losses, bot_pause_until

    if not ACTIVE_TRACKED_POSITIONS:
        return

    current_positions = mt5.positions_get(group=f"*{MAGIC_NUMBER}*")
    current_tickets = [p.ticket for p in current_positions] if current_positions else []
    closed_tickets = [ticket for ticket in list(ACTIVE_TRACKED_POSITIONS.keys()) if ticket not in current_tickets]

    for ticket in closed_tickets:
        info = ACTIVE_TRACKED_POSITIONS.pop(ticket)
        SYMBOL_COOLDOWNS[info["symbol"]] = datetime.datetime.now()

        from_time = datetime.datetime.now() - datetime.timedelta(days=1)
        history = mt5.history_deals_get(from_time, datetime.datetime.now() + datetime.timedelta(hours=1))

        profit = 0.0
        comment = ""
        if history:
            for deal in history:
                if deal.position_id == ticket and deal.entry == mt5.DEAL_ENTRY_OUT:
                    profit = deal.profit
                    comment = deal.comment
                    break

        if "sl" in comment.lower():
            exit_type = "STOP_LOSS"
        elif "tp" in comment.lower():
            exit_type = "TAKE_PROFIT"
        else:
            exit_type = "TRAILING_OR_MANUAL"

        result_status = "WIN" if profit > 0 else "LOSS"

        pm.record_experience(
            symbol=info['symbol'],
            order_type=info['type'],
            rsi_val=info['rsi'],
            macd_val=info['macd'],
            spread_pips=info['spread'],
            exit_type=exit_type,
            result_status=result_status,
            profit=profit
        )

        if profit > 0:
            emoji = "🎯"
            status_text = f"**GAIN VALIDÉ ({exit_type})**"
            consecutive_losses = 0
        elif profit < 0:
            emoji = "💥"
            status_text = f"**PERTE SUBIE ({exit_type})**"
            consecutive_losses += 1
        else:
            emoji = "⚖️"
            status_text = "**ÉQUILIBRE**"

        msg = f"{emoji} {status_text}\n━━━━━━━━━━━━━━━━━━━━\n• **Symbole** : {info['symbol']}\n• **Profit/Perte** : {profit:+.2f} $"
        send_telegram_message(msg)

        if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            bot_pause_until = datetime.datetime.now() + datetime.timedelta(hours=PAUSE_HOURS_AFTER_LOSSES)
            pause_msg = (
                f"⏸ **PAUSE STRATÉGIQUE DÉCLENCHÉE** ⏸\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• **Raison** : {consecutive_losses} pertes consécutives\n"
                f"• **Reprise prévue à** : {bot_pause_until.strftime('%H:%M:%S')}\n\n"
                f"🔍 *Perry se met en pause pendant {PAUSE_HOURS_AFTER_LOSSES}h.*"
            )
            send_telegram_message(pause_msg)
            consecutive_losses = 0


def manage_open_positions():
    now = datetime.datetime.now()
    is_friday_evening = (now.weekday() == 4 and (now.hour > 21 or (now.hour == 21 and now.minute >= 30)))

    positions = mt5.positions_get(group=f"*{MAGIC_NUMBER}*")
    if not positions:
        return

    for pos in positions:
        if is_friday_evening:
            send_telegram_message(
                f"⚠️ **SÉCURITÉ WEEK-END**\n\nClôture automatique de **{pos.symbol}** avant fermeture du marché.")
            close_position(pos)
            continue

        ticket = pos.ticket
        if ticket not in ACTIVE_TRACKED_POSITIONS:
            continue

        pos_info = ACTIVE_TRACKED_POSITIONS[ticket]
        symbol_info = mt5.symbol_info(pos.symbol)
        tick = mt5.symbol_info_tick(pos.symbol)
        atr_val = pm.get_atr_value(pos.symbol)

        if symbol_info is None or tick is None or atr_val is None:
            continue

        entry_price = pos_info["entry_price"]
        sl_distance = pos_info["sl_distance"]

        if pos.type == mt5.ORDER_TYPE_BUY:
            current_profit_dist = tick.bid - entry_price
            if current_profit_dist >= sl_distance and not pos_info["be_applied"]:
                new_sl = entry_price + (symbol_info.point * 10)
                res = mt5.order_send({
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": ticket,
                    "sl": new_sl,
                    "tp": pos.tp,
                })
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    pos_info["be_applied"] = True
                    send_telegram_message(
                        f"🛡️ **BREAKEVEN APPLIQUÉ** [{pos.symbol}]\n━━━━━━━━━━━━━━━━━━━━\n• **Ticket** : #{ticket}\n• **Nouveau SL** : {new_sl:.5f} (Risque zéro)")

            if pos_info["be_applied"]:
                trailing_sl = tick.bid - (atr_val * ATR_TRAILING_MULTIPLIER)
                if trailing_sl > pos.sl:
                    res_trail = mt5.order_send({
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": ticket,
                        "sl": trailing_sl,
                        "tp": pos.tp,
                    })
                    if res_trail and res_trail.retcode == mt5.TRADE_RETCODE_DONE:
                        send_telegram_message(
                            f"📈 **TRAILING STOP AJUSTÉ** [{pos.symbol}]\n• Nouveau SL : {trailing_sl:.5f}")

        elif pos.type == mt5.ORDER_TYPE_SELL:
            current_profit_dist = entry_price - tick.ask
            if current_profit_dist >= sl_distance and not pos_info["be_applied"]:
                new_sl = entry_price - (symbol_info.point * 10)
                res = mt5.order_send({
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": ticket,
                    "sl": new_sl,
                    "tp": pos.tp,
                })
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    pos_info["be_applied"] = True
                    send_telegram_message(
                        f"🛡️ **BREAKEVEN APPLIQUÉ** [{pos.symbol}]\n━━━━━━━━━━━━━━━━━━━━\n• **Ticket** : #{ticket}\n• **Nouveau SL** : {new_sl:.5f} (Risque zéro)")

            if pos_info["be_applied"]:
                trailing_sl = tick.ask + (atr_val * ATR_TRAILING_MULTIPLIER)
                if pos.sl == 0 or trailing_sl < pos.sl:
                    res_trail = mt5.order_send({
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": ticket,
                        "sl": trailing_sl,
                        "tp": pos.tp,
                    })
                    if res_trail and res_trail.retcode == mt5.TRADE_RETCODE_DONE:
                        send_telegram_message(
                            f"📈 **TRAILING STOP AJUSTÉ** [{pos.symbol}]\n• Nouveau SL : {trailing_sl:.5f}")


# Lancement des OPs Agent P

if __name__ == "__main__":
    initialize_mt5()
    pm.init_db()

    session_stats["start_time"] = datetime.datetime.now()
    session_stats["initial_balance"] = get_account_balance()

    # Thread d'écoute des commandes Telegram
    telegram_thread = threading.Thread(target=listen_telegram_commands, daemon=True)
    telegram_thread.start()

    pairs_str = ", ".join(SYMBOLS)
    start_msg = (
        f"🐻 **PERRY LE TRADER H1+ADX ACTIVÉ**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• **Paires surveillées** : {pairs_str}\n"
        f"• **Timeframe** : M5 (Filtre Tendance H1 + ADX > 22)\n"
        f"• **Solde Initial** : {session_stats['initial_balance']:.2f} $\n\n"
        f"📱 **Commandes Telegram actives :**\n"
        f"• `/kpi` : Métriques de la semaine\n"
        f"• `/state` : État technique\n"
        f"• `/status` : Bilan en direct\n"
        f"• `/stop` : Arrêt d'urgence"
    )
    print(start_msg)
    send_telegram_message(start_msg)

    try:
        while is_running:

            if not ensure_mt5_connection():
                time.sleep(10)
                continue

            if not check_funds_and_loss_safety():
                break

            check_closed_positions()
            manage_open_positions()

            if bot_pause_until is None or datetime.datetime.now() >= bot_pause_until:
                for symbol in SYMBOLS:
                    signal, rsi_val, macd_val = check_signal(symbol)
                    positions = mt5.positions_get(symbol=symbol)

                    if positions and len(positions) > 0:
                        for pos in positions:
                            if pos.magic == MAGIC_NUMBER:
                                if pos.type == mt5.ORDER_TYPE_BUY and signal == "SELL":
                                    if close_position(pos):
                                        send_order(symbol, "SELL", rsi_val, macd_val)
                                elif pos.type == mt5.ORDER_TYPE_SELL and signal == "BUY":
                                    if close_position(pos):
                                        send_order(symbol, "BUY", rsi_val, macd_val)
                    else:
                        if signal in ["BUY", "SELL"]:
                            send_order(symbol, signal, rsi_val, macd_val)

            time.sleep(1)

    except KeyboardInterrupt:
        print("Arrêt manuel effectué :/")
        send_session_summary("📊 **BILAN MANUEL DE SESSION**")
        mt5.shutdown()