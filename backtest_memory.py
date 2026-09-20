import MetaTrader5 as mt5
import pandas as pd
import datetime
import perry_memory as pm

SYMBOLS = ["EURUSD", "GBPUSD", "USDCHF", "USDJPY", "AUDUSD", "NZDUSD", "AUDJPY"]
SL_PIPS = 15.0
TP_PIPS = 25.0


def run_backtest_to_memory(days=365):
    if not mt5.initialize():
        print("❌ Échec d'initialisation MT5 pour le backtest.")
        return

    pm.init_db()
    print(f"🔄 Lancement du backtest sur les {days} derniers jours pour construire la mémoire...")

    for symbol in SYMBOLS:
        print(f"📊 Analyse de l'historique pour {symbol}...")

        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, days * 288)
        if rates is None or len(rates) == 0:
            print(f"❌ Aucune donnée historique M5 trouvée pour {symbol}. Vérifie le téléchargement MT5.")
            continue
        else:
            print(f"✅ {len(rates)} bougies historiques M5 récupérées pour {symbol}.")

        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')

        # Calcul des Moyennes Mobiles
        df['SMA20'] = df['close'].rolling(20).mean()
        df['SMA50'] = df['close'].rolling(50).mean()

        # Calcul du RSI (14)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))

        # Calcul du MACD (12, 26, 9)
        ema12 = df['close'].ewm(span=12, adjust=False).mean()
        ema26 = df['close'].ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        df['MACD'] = macd_line - signal_line

        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            continue
        point = symbol_info.point

        for i in range(200, len(df) - 50):
            prev = df.iloc[i - 1]
            curr = df.iloc[i]

            golden_cross = (prev['SMA20'] <= prev['SMA50']) and (curr['SMA20'] > curr['SMA50'])
            death_cross = (prev['SMA20'] >= prev['SMA50']) and (curr['SMA20'] < curr['SMA50'])
            rsi_val = curr['RSI']
            macd_val = curr['MACD']  # Extraction de la valeur MACD exacte

            signal = None
            if golden_cross and (45 < rsi_val < 70):
                signal = "BUY"
            elif death_cross and (30 < rsi_val < 55):
                signal = "SELL"

            if signal:
                entry_price = curr['close']
                trade_time = curr['time']

                future_bars = df.iloc[i + 1: i + 50]
                result = "LOSS"
                exit_type = "STOP_LOSS"
                profit = -1.50

                if signal == "BUY":
                    tp_price = entry_price + (TP_PIPS * point)
                    sl_price = entry_price - (SL_PIPS * point)

                    for idx, bar in future_bars.iterrows():
                        if bar['SMA20'] < bar['SMA50'] and prev['SMA20'] >= prev['SMA50']:
                            exit_type = "SIGNAL_OPPOSE"
                            pnl_pips = (bar['close'] - entry_price) / (point * 10)
                            profit = round(pnl_pips * 0.1, 2)
                            result = "WIN" if profit > 0 else "LOSS"
                            break

                        if bar['high'] >= tp_price:
                            result = "WIN"
                            exit_type = "TAKE_PROFIT"
                            profit = 2.50
                            break
                        elif bar['low'] <= sl_price:
                            result = "LOSS"
                            exit_type = "STOP_LOSS"
                            profit = -1.50
                            break

                elif signal == "SELL":
                    tp_price = entry_price - (TP_PIPS * point)
                    sl_price = entry_price + (SL_PIPS * point)

                    for idx, bar in future_bars.iterrows():
                        if bar['SMA20'] > bar['SMA50'] and prev['SMA20'] <= prev['SMA50']:
                            exit_type = "SIGNAL_OPPOSE"
                            pnl_pips = (entry_price - bar['close']) / (point * 10)
                            profit = round(pnl_pips * 0.1, 2)
                            result = "WIN" if profit > 0 else "LOSS"
                            break

                        if bar['low'] <= tp_price:
                            result = "WIN"
                            exit_type = "TAKE_PROFIT"
                            profit = 2.50
                            break
                        elif bar['high'] >= sl_price:
                            result = "LOSS"
                            exit_type = "STOP_LOSS"
                            profit = -1.50
                            break

                pm.record_experience(
                    symbol=symbol,
                    order_type=signal,
                    rsi_val=rsi_val,
                    macd_val=macd_val,
                    spread_pips=1.0,  # Spread moyen estimé en backtest
                    exit_type=exit_type,
                    result_status=result,
                    profit=profit,
                    override_time=trade_time
                )

    mt5.shutdown()
    print("✅ Backtest terminé ! La mémoire SQLite de Perry est parfaitement initialisée.")


if __name__ == "__main__":
    run_backtest_to_memory(days=365)