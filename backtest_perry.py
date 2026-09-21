import os
import datetime
import numpy as np
import pandas as pd
import MetaTrader5 as mt5
import perry_memory as pm

TEST_DB_FILE = "test_memory.db"
RISK_PERCENT = 1.0
ATR_SL_MULT = 1.5
HIST_BARS_MAX = 200


def reset_test_db():
    if os.path.exists(TEST_DB_FILE):
        os.remove(TEST_DB_FILE)
    pm.DB_FILE = TEST_DB_FILE
    pm.init_db()


def historical_context(df, i, point):
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values
    t = df['time'].iloc[i]

    if i >= 50:
        h1_price = close[i-11:i+1].mean()
        sma20 = close[i-19:i+1].mean()
        sma50 = close[i-49:i+1].mean()
        if h1_price > sma20 > sma50:
            trend = "UP"
        elif h1_price < sma20 < sma50:
            trend = "DOWN"
        else:
            trend = "NEUTRAL"
    else:
        trend = "NEUTRAL"

    if i >= 14:
        tr = [max(high[k]-low[k], abs(high[k]-close[k-1]), abs(low[k]-close[k-1]))
              for k in range(i-13, i+1)]
        atr = float(np.mean(tr))
        pips = atr / (point * 10)
        bucket = "LOW" if pips < 3 else "HIGH" if pips > 8 else "NORMAL"
    else:
        atr, bucket = None, "NORMAL"

    return {
        "trend_h1": trend, "trend_m30": trend, "atr_bucket": bucket,
        "hour": t.hour, "day_of_week": t.weekday(),
    }, atr


def load_data(symbol, max_bars=200_000):
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, max_bars)
    if rates is None or len(rates) < 1000:
        return None

    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df = _add_indicators(df)

    first = df['time'].iloc[0]
    last = df['time'].iloc[-1]
    print(f"  📊 {len(df)} barres M5 | {first} → {last}")
    return df


def _add_indicators(df):
    df = df.copy()
    df['SMA20'] = df['close'].rolling(20).mean()
    df['SMA50'] = df['close'].rolling(50).mean()

    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    df['RSI'] = 100 - (100 / (1 + gain / loss))

    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    df['MACD'] = macd - macd.ewm(span=9, adjust=False).mean()
    return df


def run_backtest_period(df, symbol, point, write_mode=False, label=""):
    balance = 1000.0
    trades = []
    high = df['high'].values
    low = df['low'].values
    close = df['close'].values
    spread_price = 1.0 * point * 10  # 1 pip

    i = 60
    n = len(df)
    print(f"\n--- {label} (write={write_mode}) ---")

    while i < n - HIST_BARS_MAX:
        p, c = df.iloc[i-1], df.iloc[i]
        golden = p['SMA20'] <= p['SMA50'] and c['SMA20'] > c['SMA50']
        death = p['SMA20'] >= p['SMA50'] and c['SMA20'] < c['SMA50']
        rsi_v = c['RSI']
        macd_v = c['MACD']

        signal = None
        if not (np.isnan(rsi_v) or np.isnan(macd_v)):
            if golden and 45 < rsi_v < 70:
                signal = "BUY"
            elif death and 30 < rsi_v < 55:
                signal = "SELL"

        if not signal:
            i += 1
            continue

        ctx, atr = historical_context(df, i, point)
        if atr is None or atr <= 0:
            i += 1
            continue

        confidence, risk_factor, rr_ratio = pm.evaluate_trade_confidence(
            symbol, signal, float(rsi_v), float(macd_v))

        if risk_factor <= 0:
            i += 1
            continue

        sl_dist = atr * ATR_SL_MULT
        tp_dist = sl_dist * rr_ratio

        if signal == "BUY":
            entry = close[i] + spread_price
            sl_price = entry - sl_dist
            tp_price = entry + tp_dist
        else:
            entry = close[i] - spread_price
            sl_price = entry + sl_dist
            tp_price = entry - tp_dist

        result = None
        j = i + 1
        while j < min(i + 1 + HIST_BARS_MAX, n):
            h, l = high[j], low[j]
            if signal == "BUY":
                if l <= sl_price and h >= tp_price:
                    result = "LOSS"; break  # pessimiste
                if l <= sl_price:
                    result = "LOSS"; break
                if h >= tp_price:
                    result = "WIN"; break
            else:
                if h >= sl_price and l <= tp_price:
                    result = "LOSS"; break
                if h >= sl_price:
                    result = "LOSS"; break
                if l <= tp_price:
                    result = "WIN"; break
            j += 1

        if result:
            sl_pips = sl_dist / (point * 10)
            tp_pips = tp_dist / (point * 10)
            risk_amount = balance * (RISK_PERCENT / 100.0) * risk_factor
            profit = risk_amount * rr_ratio if result == "WIN" else -risk_amount
            balance += profit
            trades.append((c['time'], signal, result, profit, balance))

            if write_mode:
                pm.record_experience(
                    symbol=symbol, order_type=signal,
                    rsi_val=float(rsi_v), macd_val=float(macd_v),
                    spread_pips=1.0,
                    exit_type="TP" if result == "WIN" else "SL",
                    result_status=result, profit=profit,
                    override_time=c['time'], context=ctx,
                    balance_at_trade=balance, signal_quality=70,
                )
            i = j
        else:
            i += 1

    wins = sum(1 for t in trades if t[2] == "WIN")
    total = len(trades)
    wr = (wins / total * 100) if total > 0 else 0.0

    print(f"Bilan : {total} trades | WR {wr:.1f}% | Solde : {balance:.2f} $")
    return balance, wr, total


def run_split_validation(symbol="EURUSD", train_pct=0.5):
    reset_test_db()

    si = mt5.symbol_info(symbol)
    if si is None:
        print(f"❌ {symbol} introuvable dans MT5")
        return
    point = si.point

    print(f"\n{'='*50}\n  SPLIT TEMPOREL {symbol}\n{'='*50}")
    df = load_data(symbol)
    if df is None:
        print(f"❌ Pas assez de données pour {symbol}")
        return

    n = len(df)
    split_idx = int(n * train_pct)

    print(f"\n  Train : {df['time'].iloc[60]} → {df['time'].iloc[split_idx]}")
    print(f"  Test  : {df['time'].iloc[split_idx]} → {df['time'].iloc[-1]}")

    # TRAIN (écrit dans la mémoire de test)
    train_df = df.iloc[:split_idx].reset_index(drop=True)
    run_backtest_period(train_df, symbol, point, write_mode=True, label="TRAIN")

    # TEST (OOS) — recalcul des indicateurs sur la fenêtre
    test_df = df.iloc[split_idx:].reset_index(drop=True)
    test_df = _add_indicators(test_df)
    test_bal, test_wr, test_n = run_backtest_period(
        test_df, symbol, point, write_mode=False, label="TEST (OOS)")

    print(f"\n{'='*50}\n  RÉSULTAT OUT-OF-SAMPLE — {symbol}\n{'='*50}")
    if test_bal > 1050:
        verdict = "✅ Stratégie viable"
    elif test_bal > 1000:
        verdict = "⚠️ Edge marginal"
    else:
        verdict = "🛑 Stratégie à revoir"
    print(f"  {verdict}")
    print(f"  Test : {test_n} trades | WR {test_wr:.1f}% | {test_bal:.2f} $")
    return test_bal, test_wr, test_n


if __name__ == "__main__":
    if not mt5.initialize():
        print("❌ Erreur init MT5")
        exit()

    results = {}
    for sym in ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"]:
        r = run_split_validation(sym, train_pct=0.5)
        if r:
            results[sym] = r

    print(f"\n{'='*50}\n  RÉSUMÉ GÉNÉRAL\n{'='*50}")
    for sym, (bal, wr, n) in results.items():
        flag = "✅" if bal > 1050 else "⚠️" if bal > 1000 else "🛑"
        print(f"  {flag} {sym} : {n} trades | WR {wr:.1f}% | {bal:.2f} $")

    if results:
        avg = np.mean([b for b, _, _ in results.values()])
        print(f"\n  Moyenne OOS : {avg:.2f} $ (départ 1000 $)")

    mt5.shutdown()