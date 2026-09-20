import os
import sqlite3
import datetime
import MetaTrader5 as mt5
import pandas as pd
import perry_memory as pm

SYMBOL = "EURUSD_CUSTOM"
TIMEFRAME = mt5.TIMEFRAME_M5
RISK_PERCENT = 1.0
ATR_SL_MULTIPLIER = 1.5

TEST_DB_FILE = "test_memory.db"


def reset_test_db():
    if os.path.exists(TEST_DB_FILE):
        os.remove(TEST_DB_FILE)

    # Redirection temporaire du fichier DB dans le module perry_memory
    pm.DB_FILE = TEST_DB_FILE
    pm.init_db()


def get_year_data(symbol, year):
    utc_from = datetime.datetime(year, 1, 1)
    utc_to = datetime.datetime(year, 12, 31, 23, 59)
    rates = mt5.copy_rates_range(symbol, TIMEFRAME, utc_from, utc_to)

    if rates is None or len(rates) < 200:
        return None

    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')

    df['SMA20'] = df['close'].rolling(20).mean()
    df['SMA50'] = df['close'].rolling(50).mean()

    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))

    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    df['MACD'] = macd_line - signal_line

    return df


def run_backtest_period(symbol, year, train_mode=True):
    df = get_year_data(symbol, year)
    if df is None:
        print(f"Pas assez de données pour {symbol} en {year}")
        return 1000.0, 0.0

    balance = 1000.0
    trades_history = []

    print(f"\n--- Simulation [{symbol}] Année {year} (Train Mode: {train_mode}) ---")

    i = 50
    n_bars = len(df)

    while i < n_bars - 50:
        prev = df.iloc[i - 1]
        curr = df.iloc[i]

        golden_cross = (prev['SMA20'] <= prev['SMA50']) and (curr['SMA20'] > curr['SMA50'])
        death_cross = (prev['SMA20'] >= prev['SMA50']) and (curr['SMA20'] < curr['SMA50'])
        rsi_val = curr['RSI']
        macd_val = curr['MACD']

        signal = None
        if golden_cross and (45 < rsi_val < 70):
            signal = "BUY"
        elif death_cross and (30 < rsi_val < 55):
            signal = "SELL"

        if signal:
            confidence, risk_factor, rr_ratio = pm.evaluate_trade_confidence(
                symbol, signal, rsi_val, macd_val
            )

            if risk_factor > 0.0:
                entry_price = curr['close']
                atr_sim = max((curr['high'] - curr['low']) * ATR_SL_MULTIPLIER, 0.0010)
                sl_dist = atr_sim
                tp_dist = sl_dist * rr_ratio

                result_status = None
                j = i + 1
                while j < n_bars:
                    bar = df.iloc[j]
                    if signal == "BUY":
                        if bar['high'] >= entry_price + tp_dist:
                            result_status = "WIN"
                            break
                        elif bar['low'] <= entry_price - sl_dist:
                            result_status = "LOSS"
                            break
                    elif signal == "SELL":
                        if bar['low'] <= entry_price - tp_dist:
                            result_status = "WIN"
                            break
                        elif bar['high'] >= entry_price + sl_dist:
                            result_status = "LOSS"
                            break
                    j += 1

                if result_status:
                    profit = (balance * (RISK_PERCENT / 100.0) * rr_ratio) if result_status == "WIN" else (-balance * (RISK_PERCENT / 100.0))
                    balance += profit
                    trades_history.append((curr['time'], signal, result_status, profit, balance))

                    if train_mode:
                        # Appel sans l'argument db_name
                        pm.record_experience(
                            symbol=symbol,
                            order_type=signal,
                            rsi_val=rsi_val,
                            macd_val=macd_val,
                            spread_pips=1.0,
                            exit_type="TP" if result_status == "WIN" else "SL",
                            result_status=result_status,
                            profit=profit,
                            override_time=curr['time']
                        )
                    i = j

        i += 1

    wins = sum(1 for t in trades_history if t[2] == "WIN")
    total = len(trades_history)
    win_rate = (wins / total * 100) if total > 0 else 0.0

    print(f"Bilan : {total} trades | Win Rate : {win_rate:.1f}% | Solde final : {balance:.2f} $")
    return balance, win_rate

def run_walk_forward_optimization(symbol, start_year=2021, end_year=2025):
    reset_test_db()

    for year in range(start_year, end_year):
        print(f"\n==========================================")
        print(f"   WALK-FORWARD CYCLE : {year} -> {year + 1}")
        print(f"==========================================")

        # Phase In-Sample (Entraînement de la mémoire)
        run_backtest_period(symbol, year, train_mode=True)

        # Phase Out-of-Sample (Test sur l'année suivante)
        run_backtest_period(symbol, year + 1, train_mode=False)


if __name__ == "__main__":
    if mt5.initialize():
        run_walk_forward_optimization("EURUSD_CUSTOM", start_year=2021, end_year=2025)
        mt5.shutdown()
    else:
        print("Erreur d'initialisation MT5")