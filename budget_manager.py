import datetime
import json
import os
import threading
import time

STATE_FILE = "budget_state.json"


class BudgetManager:

    BALANCE_TIERS = [
        (0,     20,   0.00),
        (20,    50,   0.30),
        (50,    100,  0.50),
        (100,   300,  0.75),
        (300,   500,  1.00),
        (500,   2000, 0.75),
        (2000,  10**9,0.50),
    ]

    MAX_DAILY_LOSS_PCT     = 4.0
    MAX_WEEKLY_LOSS_PCT    = 8.0
    MAX_DAILY_TRADES       = 10
    MAX_TRADES_PER_SYMBOL  = 3
    CONSEC_LOSSES_PAUSE    = 3
    PAUSE_HOURS            = 2

    DD_HALVE_RISK_PCT      = 8.0
    DD_STOP_TRADING_PCT    = 18.0

    SAVE_INTERVAL_SEC      = 5.0

    def __init__(self):
        self.lock = threading.RLock()
        self.state = self._default_state()
        self._last_save = 0.0
        self._save_pending = False
        self._load()

    def _default_state(self):
        return {
            "peak_balance": 0.0,
            "day_start_balance": 0.0,
            "day_start_date": "",
            "week_start_balance": 0.0,
            "week_start_date": "",
            "day_trades": 0,
            "symbol_trades": {},
            "consecutive_losses": 0,
            "consecutive_wins": 0,
            "recovery_mode_remaining": 0,
            "last_updated": "",
            "pause_until": "",
        }

    def _load(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            self.state.update(data)
        except Exception as e:
            print(f"⚠️ budget load : {e}")

    def _save(self, force=False):
        now = time.time()
        if not force and now - self._last_save < self.SAVE_INTERVAL_SEC:
            self._save_pending = True
            return
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=2, default=str)
            self._last_save = now
            self._save_pending = False
        except Exception as e:
            print(f"⚠️ budget save : {e}")

    def flush_pending(self):
        if self._save_pending:
            self._save(force=True)

    def sync(self, current_balance, equity=None):
        with self.lock:
            now = datetime.datetime.now()
            today = now.strftime("%Y-%m-%d")
            week = now.strftime("%Y-W%W")

            if current_balance > self.state["peak_balance"]:
                self.state["peak_balance"] = current_balance

            if self.state["day_start_date"] != today:
                self.state["day_start_balance"] = current_balance
                self.state["day_start_date"] = today
                self.state["day_trades"] = 0
                self.state["symbol_trades"] = {}

            if self.state["week_start_date"] != week:
                self.state["week_start_balance"] = current_balance
                self.state["week_start_date"] = week

            self.state["last_updated"] = now.isoformat()
        self._save()

    # Regle
    def _base_risk_pct(self, balance):
        for lo, hi, pct in self.BALANCE_TIERS:
            if lo <= balance < hi:
                return pct
        return 0.0

    def _drawdown_pct(self, balance):
        peak = self.state.get("peak_balance", 0) or balance
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - balance) / peak * 100.0)

    def is_paused(self):
        p = self.state.get("pause_until", "")
        if not p:
            return False, ""
        try:
            until = datetime.datetime.fromisoformat(p)
            if datetime.datetime.now() < until:
                return True, until.strftime("%d/%m %H:%M")
        except Exception:
            pass
        return False, ""

    def can_open_trade(self, symbol, current_balance):
        with self.lock:
            paused, until = self.is_paused()
            if paused:
                return False, f"Pause jusqu'à {until}"

            if current_balance < 20.0:
                return False, f"Solde {current_balance:.2f}$ < 20$ minimum"

            if self.state["day_trades"] >= self.MAX_DAILY_TRADES:
                return False, f"Limite quotidienne {self.MAX_DAILY_TRADES} atteinte"

            if symbol and self.state["symbol_trades"].get(symbol, 0) >= self.MAX_TRADES_PER_SYMBOL:
                return False, f"{symbol} : {self.MAX_TRADES_PER_SYMBOL} trades/jour max"

            day_start = self.state.get("day_start_balance", 0) or current_balance
            if day_start > 0:
                loss_pct = (day_start - current_balance) / day_start * 100.0
                if loss_pct >= self.MAX_DAILY_LOSS_PCT:
                    return False, f"Perte jour {loss_pct:.2f}% ≥ {self.MAX_DAILY_LOSS_PCT}%"

            week_start = self.state.get("week_start_balance", 0) or current_balance
            if week_start > 0:
                wloss = (week_start - current_balance) / week_start * 100.0
                if wloss >= self.MAX_WEEKLY_LOSS_PCT:
                    return False, f"Perte semaine {wloss:.2f}% ≥ {self.MAX_WEEKLY_LOSS_PCT}%"

            dd = self._drawdown_pct(current_balance)
            if dd >= self.DD_STOP_TRADING_PCT:
                self._set_pause(hours=24)
                return False, f"Drawdown {dd:.1f}% ≥ {self.DD_STOP_TRADING_PCT}% → pause 24h"

            return True, "OK"

    def _set_pause(self, hours):
        until = datetime.datetime.now() + datetime.timedelta(hours=hours)
        self.state["pause_until"] = until.isoformat()
        self._save(force=True)

    def get_risk_percent(self, symbol, current_balance):
        with self.lock:
            base = self._base_risk_pct(current_balance)
            if base <= 0:
                return 0.0

            dd = self._drawdown_pct(current_balance)
            if dd >= self.DD_HALVE_RISK_PCT:
                base *= 0.5

            if self.state.get("recovery_mode_remaining", 0) > 0:
                base *= 0.5

            if self.state.get("consecutive_wins", 0) >= 3:
                base *= 1.2

            return min(base, 1.0)

    def register_trade_open(self, symbol):
        with self.lock:
            self.state["day_trades"] += 1
            st = self.state["symbol_trades"]
            st[symbol] = st.get(symbol, 0) + 1
            self.state["symbol_trades"] = st
        self._save()

    def register_trade_close(self, symbol, profit, current_balance):
        with self.lock:
            if profit > 0:
                self.state["consecutive_wins"] += 1
                self.state["consecutive_losses"] = 0
                if self.state.get("recovery_mode_remaining", 0) > 0:
                    self.state["recovery_mode_remaining"] -= 1
            elif profit < 0:
                self.state["consecutive_losses"] += 1
                self.state["consecutive_wins"] = 0
                if self.state["consecutive_losses"] >= self.CONSEC_LOSSES_PAUSE:
                    self._set_pause(hours=self.PAUSE_HOURS)
                    self.state["consecutive_losses"] = 0
                self.state["recovery_mode_remaining"] = 3

            if current_balance > self.state["peak_balance"]:
                self.state["peak_balance"] = current_balance
        self._save(force=True)

    # Rapport
    def get_status(self, current_balance):
        with self.lock:
            dd = self._drawdown_pct(current_balance)
            base = self._base_risk_pct(current_balance)
            risk = self.get_risk_percent("", current_balance)
            paused, until = self.is_paused()
            rec = self.state.get("recovery_mode_remaining", 0)

            return {
                "balance": current_balance,
                "peak": self.state.get("peak_balance", 0),
                "drawdown_pct": dd,
                "base_risk_pct": base,
                "effective_risk_pct": risk,
                "day_trades": self.state["day_trades"],
                "day_max": self.MAX_DAILY_TRADES,
                "consec_wins": self.state.get("consecutive_wins", 0),
                "consec_losses": self.state.get("consecutive_losses", 0),
                "recovery_mode": rec > 0,
                "recovery_left": rec,
                "paused": paused,
                "pause_until": until,
                "symbol_trades": dict(self.state.get("symbol_trades", {})),
            }

    def reset_day(self, current_balance):
        with self.lock:
            now = datetime.datetime.now()
            self.state["day_start_balance"] = current_balance
            self.state["day_start_date"] = now.strftime("%Y-%m-%d")
            self.state["day_trades"] = 0
            self.state["symbol_trades"] = {}
            self.state["consecutive_losses"] = 0
            self.state["consecutive_wins"] = 0
            self.state["recovery_mode_remaining"] = 0
        self._save(force=True)