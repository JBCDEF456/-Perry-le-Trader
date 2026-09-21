import sqlite3
import datetime
import math
import time
import threading
import numpy as np
import MetaTrader5 as mt5

DB_FILE = "perry_memory.db"

_DB_WRITE_LOCK = threading.RLock()


def _get_conn():
    conn = sqlite3.connect(DB_FILE, timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


_cache = {}
CACHE_TTL = 30


def _get_cached(key, fn, ttl=CACHE_TTL):
    now = time.time()
    if key in _cache and now - _cache[key][1] < ttl:
        return _cache[key][0]
    val = fn()
    _cache[key] = (val, now)
    return val


def init_db():
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS trade_experience (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, type TEXT, hour INTEGER, day_of_week INTEGER,
                trend_h1 TEXT, trend_m30 TEXT, rsi_val REAL, rsi_bucket TEXT,
                macd_val REAL, spread_pips REAL, atr_bucket TEXT,
                exit_type TEXT, result TEXT, profit REAL,
                balance_at_trade REAL DEFAULT 0.0,
                signal_quality INTEGER DEFAULT 0,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Migrations
        cur.execute("PRAGMA table_info(trade_experience)")
        cols = [c[1] for c in cur.fetchall()]
        migrations = {
            "day_of_week": "INTEGER DEFAULT 0",
            "rsi_val": "REAL DEFAULT 50.0",
            "macd_val": "REAL DEFAULT 0.0",
            "spread_pips": "REAL DEFAULT 0.0",
            "balance_at_trade": "REAL DEFAULT 0.0",
            "signal_quality": "INTEGER DEFAULT 0",
        }
        for k, v in migrations.items():
            if k not in cols:
                cur.execute(f"ALTER TABLE trade_experience ADD COLUMN {k} {v}")

        # Index pour accélérer les SELECT de evaluate_trade_confidence
        cur.execute('''CREATE INDEX IF NOT EXISTS idx_symbol_type
                       ON trade_experience(symbol, type)''')
        cur.execute('''CREATE INDEX IF NOT EXISTS idx_symbol_type_bucket
                       ON trade_experience(symbol, type, rsi_bucket, atr_bucket)''')
    finally:
        conn.close()


# Soutient MT5

def get_sma_trend(symbol, timeframe, period=200):
    def _fetch():
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, period)
        if rates is None or len(rates) < period:
            return "NEUTRAL"
        closes = [r['close'] for r in rates]
        sma = sum(closes) / len(closes)
        return "UP" if closes[-1] > sma else "DOWN"
    return _get_cached(f"trend_{symbol}_{timeframe}", _fetch)


def get_atr_value(symbol, timeframe=mt5.TIMEFRAME_M5, period=14):
    def _fetch():
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, period + 1)
        if rates is None or len(rates) < period + 1:
            return None
        tr = [max(rates[i]['high'] - rates[i]['low'],
                  abs(rates[i]['high'] - rates[i-1]['close']),
                  abs(rates[i]['low'] - rates[i-1]['close']))
              for i in range(1, len(rates))]
        return sum(tr) / len(tr) if tr else None
    return _get_cached(f"atr_{symbol}_{timeframe}_{period}", _fetch)


def get_market_volatility_atr(symbol):
    def _fetch():
        atr = get_atr_value(symbol)
        si = mt5.symbol_info(symbol)
        if atr is None or si is None or si.point == 0:
            return "NORMAL"
        pips = atr / (si.point * 10)
        if pips < 3.0:
            return "LOW"
        elif pips > 8.0:
            return "HIGH"
        return "NORMAL"
    return _get_cached(f"vol_{symbol}", _fetch)


# Sauvegarde

def record_experience(symbol, order_type, rsi_val, macd_val, spread_pips,
                     exit_type, result_status, profit, override_time=None,
                     context=None, balance_at_trade=0.0, signal_quality=0):

    rsi_bucket = f"{(int(rsi_val) // 10) * 10}-{((int(rsi_val) // 10) * 10) + 10}"

    if context:
        trend_h1 = context.get("trend_h1", "NEUTRAL")
        trend_m30 = context.get("trend_m30", "NEUTRAL")
        atr_bucket = context.get("atr_bucket", "NORMAL")
        trade_time = override_time if override_time else datetime.datetime.now()
        current_hour = context.get("hour", trade_time.hour)
        dow = context.get("day_of_week", trade_time.weekday())
    else:
        trade_time = override_time if override_time else datetime.datetime.now()
        current_hour = trade_time.hour
        dow = trade_time.weekday()
        trend_h1 = get_sma_trend(symbol, mt5.TIMEFRAME_H1)
        trend_m30 = get_sma_trend(symbol, mt5.TIMEFRAME_M30)
        atr_bucket = get_market_volatility_atr(symbol)

    ts = trade_time.strftime("%Y-%m-%d %H:%M:%S") if hasattr(trade_time, 'strftime') else str(trade_time)

    with _DB_WRITE_LOCK:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO trade_experience
                (symbol, type, hour, day_of_week, trend_h1, trend_m30, rsi_val,
                 rsi_bucket, macd_val, spread_pips, atr_bucket, exit_type, result,
                 profit, balance_at_trade, signal_quality, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (symbol, order_type, current_hour, dow, trend_h1, trend_m30,
                  rsi_val, rsi_bucket, macd_val, spread_pips, atr_bucket,
                  exit_type, result_status, profit, balance_at_trade,
                  signal_quality, ts))
        finally:
            conn.close()


# Confiance

def evaluate_trade_confidence(symbol, order_type, rsi_val, macd_val):
    trend_h1 = get_sma_trend(symbol, mt5.TIMEFRAME_H1)
    rsi_bucket = f"{(int(rsi_val) // 10) * 10}-{((int(rsi_val) // 10) * 10) + 10}"
    atr_bucket = get_market_volatility_atr(symbol)
    now = datetime.datetime.now()

    rows = []
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute('''
            SELECT result, profit, timestamp FROM trade_experience
            WHERE symbol = ? AND type = ? AND rsi_bucket = ? AND atr_bucket = ?
            ORDER BY id DESC LIMIT 100
        ''', (symbol, order_type, rsi_bucket, atr_bucket))
        rows = cur.fetchall()

        if len(rows) < 15:
            cur.execute('''
                SELECT result, profit, timestamp FROM trade_experience
                WHERE symbol = ? AND type = ?
                ORDER BY id DESC LIMIT 100
            ''', (symbol, order_type))
            rows = cur.fetchall()
    finally:
        conn.close()

    if len(rows) < 15:
        return 50.0, 0.5, 1.5

    wins_w, total_w = 0.0, 0.0
    profits = []
    for result, profit, ts_str in rows:
        try:
            t = datetime.datetime.strptime(ts_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
            days_old = max(0, (now - t).days)
        except Exception:
            days_old = 0
        w = math.exp(-0.05 * days_old)
        if result == "WIN":
            wins_w += w
        total_w += w
        if profit is not None:
            profits.append(profit)

    base_conf = (wins_w / total_w) * 100.0 if total_w > 0 else 50.0

    h1_bonus = 0.0
    if (order_type == "BUY" and trend_h1 == "UP") or \
       (order_type == "SELL" and trend_h1 == "DOWN"):
        h1_bonus = 5.0
    elif (order_type == "BUY" and trend_h1 == "DOWN") or \
         (order_type == "SELL" and trend_h1 == "UP"):
        h1_bonus = -15.0

    if len(profits) >= 15:
        expectancy = float(np.mean(profits))
        if expectancy < 0:
            return max(0.0, base_conf + h1_bonus), 0.0, 1.0

    final_conf = max(0.0, min(100.0, base_conf + h1_bonus))

    if final_conf >= 68.0:    risk_factor, rr_ratio = 1.0, 2.5
    elif final_conf >= 58.0:  risk_factor, rr_ratio = 0.75, 2.0
    elif final_conf >= 50.0:  risk_factor, rr_ratio = 0.5, 1.5
    else:                     risk_factor, rr_ratio = 0.0, 1.0

    return final_conf, risk_factor, rr_ratio