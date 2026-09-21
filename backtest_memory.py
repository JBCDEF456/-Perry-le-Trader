import numpy as np
import pandas as pd
import datetime
import MetaTrader5 as mt5
import perry_memory as pm

SYMBOLS = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"]
ATR_SL_MULT = 1.5
RR_RATIO = 2.0
SPREAD_PIPS = 1.0
HIST_BARS_MAX = 200


def historical_context(df, i, point):
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values
    t = df['time'].iloc[i]

    if i >= 50:
        h1_price = close[i-11:i+1].mean()
        sma20 = close[i-19:i+1].mean()
        sma50 = close[i-49:i+1].mean()
        if h1_price > sma20 > sma50:   trend = "UP"
        elif h1_price < sma20 < sma50: trend = "DOWN"
        else:                          trend = "NEUTRAL"
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


def run_backtest_to_memory(days=365):
    if not mt5.initialize():
        print("❌ Échec init MT5.")
        return

    pm.init_db()
    print(f"🔄 Backtest {days} jours...")

    total_written = 0

    for symbol in SYMBOLS:
        si = mt5.symbol_info(symbol)
        if si is None:
            print(f"⚠️ {symbol} introuvable.")
            continue
        point = si.point
        spread_price = SPREAD_PIPS * point * 10

        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, days * 288)
        if rates is None or len(rates) < 500:
            print(f"❌ Données insuffisantes {symbol}.")
            continue

        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
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

        high = df['high'].values
        low = df['low'].values
        close = df['close'].values

        n_trades = 0
        for i in range(60, len(df) - HIST_BARS_MAX):
            p, c = df.iloc[i-1], df.iloc[i]
            golden = p['SMA20'] <= p['SMA50'] and c['SMA20'] > c['SMA50']
            death = p['SMA20'] >= p['SMA50'] and c['SMA20'] < c['SMA50']
            rsi_v = c['RSI']; macd_v = c['MACD']
            if np.isnan(rsi_v) or np.isnan(macd_v):
                continue

            signal = None
            if golden and 45 < rsi_v < 70:  signal = "BUY"
            elif death and 30 < rsi_v < 55: signal = "SELL"
            if not signal:
                continue

            ctx, atr = historical_context(df, i, point)
            if atr is None or atr <= 0:
                continue

            sl_dist = atr * ATR_SL_MULT
            tp_dist = sl_dist * RR_RATIO

            if signal == "BUY":
                entry = close[i] + spread_price
                sl_price = entry - sl_dist
                tp_price = entry + tp_dist
            else:
                entry = close[i] - spread_price
                sl_price = entry + sl_dist
                tp_price = entry - tp_dist

            result = None
            for j in range(i + 1, min(i + 1 + HIST_BARS_MAX, len(df))):
                h, l = high[j], low[j]
                if signal == "BUY":
                    if l <= sl_price and h >= tp_price:
                        result = "LOSS"; break  # pessimiste
                    if l <= sl_price:  result = "LOSS"; break
                    if h >= tp_price:  result = "WIN";  break
                else:
                    if h >= sl_price and l <= tp_price:
                        result = "LOSS"; break
                    if h >= sl_price:  result = "LOSS"; break
                    if l <= tp_price:  result = "WIN";  break

            if result is None:
                continue

            sl_pips = sl_dist / (point * 10)
            tp_pips = tp_dist / (point * 10)
            profit = round(tp_pips * 0.1, 2) if result == "WIN" else round(-sl_pips * 0.1, 2)
            exit_type = "TAKE_PROFIT" if result == "WIN" else "STOP_LOSS"

            pm.record_experience(
                symbol=symbol, order_type=signal,
                rsi_val=float(rsi_v), macd_val=float(macd_v),
                spread_pips=SPREAD_PIPS,
                exit_type=exit_type, result_status=result,
                profit=profit, override_time=c['time'],
                context=ctx, balance_at_trade=1000.0, signal_quality=70,
            )
            n_trades += 1
            total_written += 1

        print(f"  ✓ {symbol} : {n_trades} trades mémorisés")

    mt5.shutdown()
    print(f"\n✅ Backtest terminé. {total_written} trades écrits dans la mémoire.")


if __name__ == "__main__":
    run_backtest_to_memory(days=365)