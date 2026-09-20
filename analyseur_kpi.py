import sqlite3
import numpy as np
import pandas as pd


def analyze_perry_weekly_kpis(db_file="perry_memory.db", start_date_live="2026-09-21", risk_free_rate=0.02):
    conn = sqlite3.connect(db_file)

    # Récupération uniquement des trades pris depuis le lancement réel
    query = f"""
        SELECT profit, result, timestamp 
        FROM trade_experience 
        WHERE profit IS NOT NULL AND profit != 0 
          AND timestamp >= '{start_date_live} 00:00:00'
        ORDER BY timestamp ASC
    """
    df = pd.read_sql_query(query, conn)
    conn.close()

    if df.empty:
        print(f"⚠️ Aucun trade en direct enregistré depuis le {start_date_live}.")
        return

    # Conversion et regroupement par semaine (Year-Week ISO)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df['Year_Week'] = df['timestamp'].dt.strftime('%Y-W%U')

    weeks = df['Year_Week'].unique()

    print(f"\n==================================================================")
    print(f"📊 REPORTING HEBDOMADAIRE PERRY (Depuis {start_date_live})")
    print(f"==================================================================")

    total_live_profits = []

    for week_num, week_code in enumerate(weeks, 1):
        df_week = df[df['Year_Week'] == week_code]
        profits = df_week['profit'].values
        total_live_profits.extend(profits)

        gains = profits[profits > 0]
        pertes = profits[profits < 0]

        gross_profits = np.sum(gains) if len(gains) > 0 else 0.0
        gross_losses = abs(np.sum(pertes)) if len(pertes) > 0 else 1e-6
        profit_factor = gross_profits / gross_losses

        # Drawdown de la semaine
        equity_week = np.cumsum(profits) + 1000.0
        peak = np.maximum.accumulate(equity_week)
        drawdown = (equity_week - peak) / peak
        max_dd_pct = abs(np.min(drawdown)) * 100.0 if len(drawdown) > 0 else 0.0

        win_rate = (len(gains) / len(profits)) * 100.0 if len(profits) > 0 else 0.0
        net_profit = np.sum(profits)

        # Dates de début et fin de semaine
        start_w = df_week['timestamp'].min().strftime('%d/%m')
        end_w = df_week['timestamp'].max().strftime('%d/%m')

        print(f"\n🗓️  SEMAINE {week_num} [{start_w} au {end_w}] ({week_code})")
        print(f"   • Trades exécutés : {len(profits)}")
        print(f"   • Win Rate        : {win_rate:.1f} %")
        print(f"   • Profit Factor   : {profit_factor:.2f}  ({'✅' if profit_factor >= 1.5 else '⚠️'})")
        print(f"   • Max Drawdown    : {max_dd_pct:.2f} %")
        print(f"   • Gain/Perte Net  : {net_profit:+.2f} $")

    # Bilan Global Cumulé
    all_profits = np.array(total_live_profits)
    tot_gains = all_profits[all_profits > 0]
    tot_pertes = all_profits[all_profits < 0]
    global_pf = np.sum(tot_gains) / abs(np.sum(tot_pertes)) if len(tot_pertes) > 0 else np.sum(tot_gains)

    print(f"\n------------------------------------------------------------------")
    print(
        f"🏆 CUMUL GLOBAL : {len(all_profits)} Trades | PF Global : {global_pf:.2f} | PnL Total : {np.sum(all_profits):+.2f} $")
    print(f"------------------------------------------------------------------\n")


if __name__ == "__main__":
    analyze_perry_weekly_kpis(db_file="perry_memory.db", start_date_live="2026-09-21")