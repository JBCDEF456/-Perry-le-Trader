import sqlite3
import datetime
import math
import MetaTrader5 as mt5

DB_FILE = "perry_memory.db"


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trade_experience (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            type TEXT,
            hour INTEGER,
            day_of_week INTEGER,
            trend_h1 TEXT,
            trend_m30 TEXT,
            rsi_val REAL,
            rsi_bucket TEXT,
            macd_val REAL,
            spread_pips REAL,
            atr_bucket TEXT,
            exit_type TEXT,
            result TEXT,
            profit REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()

    # Migration automatique pour les installations existantes
    cursor.execute("PRAGMA table_info(trade_experience)")
    columns = [col[1] for col in cursor.fetchall()]

    new_columns = {
        "day_of_week": "INTEGER DEFAULT 0",
        "rsi_val": "REAL DEFAULT 50.0",
        "macd_val": "REAL DEFAULT 0.0",
        "spread_pips": "REAL DEFAULT 0.0"
    }

    for col_name, col_type in new_columns.items():
        if col_name not in columns:
            cursor.execute(f"ALTER TABLE trade_experience ADD COLUMN {col_name} {col_type}")

    conn.commit()
    conn.close()


def get_sma_trend(symbol, timeframe, period=200):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, period)
    if rates is None or len(rates) < period:
        return "NEUTRAL"
    close_prices = [r['close'] for r in rates]
    sma = sum(close_prices) / len(close_prices)
    return "UP" if close_prices[-1] > sma else "DOWN"


def get_atr_value(symbol, timeframe=mt5.TIMEFRAME_M5, period=14):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, period + 1)
    if rates is None or len(rates) < period + 1:
        return None

    tr_list = []
    for i in range(1, len(rates)):
        high = rates[i]['high']
        low = rates[i]['low']
        prev_close = rates[i - 1]['close']
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)

    return sum(tr_list) / len(tr_list) if tr_list else None


def get_market_volatility_atr(symbol):
    atr = get_atr_value(symbol)
    symbol_info = mt5.symbol_info(symbol)
    if atr is None or symbol_info is None or symbol_info.point == 0:
        return "NORMAL"

    pips = atr / (symbol_info.point * 10)
    if pips < 3.0:
        return "LOW"
    elif pips > 8.0:
        return "HIGH"
    return "NORMAL"


def record_experience(symbol, order_type, rsi_val, macd_val, spread_pips, exit_type, result_status, profit, override_time=None):
    rsi_bucket = f"{(int(rsi_val) // 10) * 10}-{((int(rsi_val) // 10) * 10) + 10}"
    trade_time = override_time if override_time else datetime.datetime.now()
    current_hour = trade_time.hour
    day_of_week = trade_time.weekday()
    trade_time_str = trade_time.strftime("%Y-%m-%d %H:%M:%S") if hasattr(trade_time, 'strftime') else str(trade_time)

    trend_h1 = get_sma_trend(symbol, mt5.TIMEFRAME_H1)
    trend_m30 = get_sma_trend(symbol, mt5.TIMEFRAME_M30)
    atr_bucket = get_market_volatility_atr(symbol)

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO trade_experience 
            (symbol, type, hour, day_of_week, trend_h1, trend_m30, rsi_val, rsi_bucket, macd_val, spread_pips, atr_bucket, exit_type, result, profit, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (symbol, order_type, current_hour, day_of_week, trend_h1, trend_m30, rsi_val, rsi_bucket, macd_val, spread_pips, atr_bucket, exit_type, result_status, profit, trade_time_str))
        conn.commit()


def evaluate_trade_confidence(symbol, order_type, rsi_val, macd_val):
    trend_h1 = get_sma_trend(symbol, mt5.TIMEFRAME_H1)

    h1_penalty = 0.0
    if (order_type == "BUY" and trend_h1 == "DOWN") or (order_type == "SELL" and trend_h1 == "UP"):
        h1_penalty -= 20.0
    elif (order_type == "BUY" and trend_h1 == "UP") or (order_type == "SELL" and trend_h1 == "DOWN"):
        h1_penalty += 10.0

    rsi_bucket = f"{(int(rsi_val) // 10) * 10}-{((int(rsi_val) // 10) * 10) + 10}"
    atr_bucket = get_market_volatility_atr(symbol)
    now = datetime.datetime.now()
    current_hour = now.hour
    current_day = now.weekday()

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT result, timestamp FROM trade_experience 
            WHERE symbol = ? 
              AND type = ? 
              AND rsi_bucket = ? 
              AND atr_bucket = ? 
              AND day_of_week = ?
              AND ABS(hour - ?) <= 2
            ORDER BY id DESC LIMIT 50
        ''', (symbol, order_type, rsi_bucket, atr_bucket, current_day, current_hour))
        rows = cursor.fetchall()

    if not rows or len(rows) < 3:
        base_confidence = 50.0
    else:
        weighted_wins = 0.0
        total_weight = 0.0

        for result, ts_str in rows:
            try:
                trade_date = datetime.datetime.strptime(ts_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
            except Exception:
                trade_date = now

            days_old = (now - trade_date).days
            weight = math.exp(-0.15 * days_old)

            if result == "WIN":
                weighted_wins += weight
            total_weight += weight

        base_confidence = (weighted_wins / total_weight) * 100.0 if total_weight > 0 else 50.0

    final_confidence = max(0.0, min(100.0, base_confidence + h1_penalty))

    if final_confidence >= 70.0:
        risk_factor = 1.0
        rr_ratio = 2.5
    elif final_confidence >= 50.0:
        risk_factor = 0.5
        rr_ratio = 1.5
    else:
        risk_factor = 0.0
        rr_ratio = 1.0

    return final_confidence, risk_factor, rr_ratio