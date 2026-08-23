#the automated trading bot. each user who turns it on gets one worker
#thread of their own, which checks the price every few seconds and buys or
#sells when it crosses one of their thresholds.
#
#a few things here exist to stop the bot doing damage. every user has their
#own row of settings rather than sharing one, so turning the bot on cannot
#change anybody else's. the worker holds a real application context, since
#database access outside one fails. what it does is written to a table the
#browser reads, because calling flash() from a thread raises an error and
#kills the worker without saying anything. and a cooldown and a daily limit
#stop it buying again on every tick while the price sits below the buy
#threshold, which would empty the account in seconds.
#
#the bot prices its decisions on the account's simulation date, the same as
#a trade made by hand. while that date is in the past the price it sees is
#fixed at that day's close, so the live loop fires once and then waits;
#moving time forward is what actually drives it, through run_over_period.

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone

from .models import BotConfig, BotEvent, Portfolio, User, db as default_db
from .trading import TradeEngine, TradeError

log = logging.getLogger(__name__)

POLL_SECONDS = 5.0


class BotWorker:
    """One user's bot, running in a thread of its own."""

    def __init__(self, manager: "BotManager", user_id: int) -> None:
        self.manager = manager
        self.user_id = user_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    #lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"bot-user-{self.user_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    #the loop itself
    def _run(self) -> None:
        db = self.manager.db
        app = self.manager.app

        #a real application context, so that reading settings and the database
        #behaves exactly as it does inside a request.
        ctx = app.app_context() if app else None
        if ctx:
            ctx.push()
        try:
            BotEvent.log(db, self.user_id, "Bot started.", level="info")
            while not self._stop.is_set():
                try:
                    self._tick()
                except Exception as exc:
                    #write the failure to the database, never flash() from in here
                    self.last_error = str(exc)
                    log.exception("bot tick failed for user %s", self.user_id)
                    BotEvent.log(db, self.user_id, f"Error: {exc}", level="error")
                    self._stop.wait(30)  #back off after a failure
                self._stop.wait(POLL_SECONDS)
            BotEvent.log(db, self.user_id, "Bot stopped.", level="info")
        finally:
            if ctx:
                ctx.pop()
            db.close()  #let go of this thread's connection

    def _tick(self) -> None:
        db = self.manager.db
        config = BotConfig.get(db, self.user_id)
        if config is None or not config["enabled"]:
            self._stop.set()
            return

        user = User.get_by_id(db, self.user_id)
        if user is None:
            self._stop.set()
            return

        decision = self.manager.evaluate(user, config)
        if decision:
            self.manager.act(user, config, decision)


class BotManager:
    """Owns every worker and the rules they all follow."""

    _instance: "BotManager | None" = None
    _lock = threading.Lock()

    def __init__(self, db=None, app=None) -> None:
        self.db = db or default_db
        self.app = app
        self.workers: dict[int, BotWorker] = {}
        self._engine: TradeEngine | None = None

    @classmethod
    def instance(cls, db=None, app=None) -> "BotManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(db=db, app=app)
            if app is not None:
                cls._instance.app = app
        return cls._instance

    @property
    def engine(self) -> TradeEngine:
        if self._engine is None:
            self._engine = TradeEngine(db=self.db)
        return self._engine

    #starting and stopping
    def start(self, user_id: int) -> None:
        worker = self.workers.get(user_id)
        if worker is None:
            worker = BotWorker(self, user_id)
            self.workers[user_id] = worker
        worker.start()
        log.info("bot started for user %s", user_id)

    def stop(self, user_id: int) -> None:
        worker = self.workers.get(user_id)
        if worker:
            worker.stop()
        log.info("bot stopped for user %s", user_id)

    def status(self, user_id: int) -> dict:
        config = BotConfig.get(self.db, user_id)
        worker = self.workers.get(user_id)
        return {
            "enabled": bool(config["enabled"]) if config else False,
            "running": bool(worker and worker.running),
            "symbol": config["symbol"] if config else None,
            "threshold_buy": config["threshold_buy"] if config else None,
            "threshold_sell": config["threshold_sell"] if config else None,
            "quantity": config["quantity"] if config else None,
            "last_error": worker.last_error if worker else None,
            "trades_today": BotEvent.trades_today(self.db, user_id),
        }

    def restore(self) -> None:
        """Start a worker again for every user who had the bot switched on.

        the settings are saved in the database rather than held in memory, so they
        survive the program being restarted.
        """
        try:
            for row in BotConfig.all_enabled(self.db):
                self.start(row["user_id"])
        except Exception:
            log.exception("could not restore bot workers")

    #the rules the bot follows
    def evaluate(self, user: User, config, price: float | None = None) -> dict | None:
        """Decide whether to trade. returns what to do, or None to sit still."""
        symbol = config["symbol"]
        if not symbol or config["quantity"] in (None, 0):
            return None

        buy_at = config["threshold_buy"]
        sell_at = config["threshold_sell"]
        if buy_at is None or sell_at is None:
            return None

        if price is None:
            #the bot fills at the same price the user would: the live price once
            #the simulation has caught up, and that date's close before then. it
            #would otherwise fill at today's price in a portfolio whose other
            #trades were all made at prices from years ago.
            price = self.engine.market.price_on_date(symbol, user.sim_date)
        if price is None:
            return None

        if not self._cooldown_elapsed(config):
            return None
        if BotEvent.trades_today(self.db, user.id) >= self._setting(config, "max_trades_per_day", 20):
            return None

        if price <= buy_at:
            cost = price * config["quantity"]
            if cost > user.balance:
                return None
            if self._position_value(user, symbol, price) + cost > self._setting(config, "max_position", 1e12):
                return None
            return {"action": "buy", "price": price, "symbol": symbol,
                    "shares": config["quantity"], "reason": f"price ${price:,.2f} <= buy threshold ${buy_at:,.2f}"}

        if price >= sell_at:
            holding = Portfolio.get(self.db, user.id, symbol)
            if not holding or float(holding["shares"]) <= 0:
                return None
            shares = min(config["quantity"], float(holding["shares"]))
            return {"action": "sell", "price": price, "symbol": symbol,
                    "shares": shares, "reason": f"price ${price:,.2f} >= sell threshold ${sell_at:,.2f}"}

        return None

    def act(self, user: User, config, decision: dict) -> bool:
        """Carry out a decision and write down what happened."""
        try:
            result = self.engine.execute(
                user, decision["symbol"], decision["action"], decision["shares"],
                source="bot", price=decision["price"], record_undo=False,
            )
        except TradeError as exc:
            BotEvent.log(self.db, user.id, f"Skipped {decision['action']}: {exc}",
                         level="info", symbol=decision["symbol"], price=decision["price"])
            return False

        BotConfig.save(self.db, user.id, last_trade_at=datetime.now().isoformat(timespec="seconds"))
        BotEvent.log(
            self.db, user.id,
            f"{result.message} Triggered because {decision['reason']}.",
            level="trade", symbol=decision["symbol"], price=decision["price"],
        )
        return True

    @staticmethod
    def _setting(config, key: str, default):
        """Read a setting, treating only an empty value as unset.

        writing config[key] or default would be wrong here, because a zero the user
        chose on purpose, such as no cooldown, counts as false in python and would
        be quietly replaced by the default.
        """
        value = config[key]
        return default if value is None else value

    def _cooldown_elapsed(self, config) -> bool:
        """Stop the bot buying again and again while the price stays low."""
        last = config["last_trade_at"]
        if not last:
            return True
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
        except ValueError:
            return True
        return elapsed >= self._setting(config, "cooldown_seconds", 60)

    def _position_value(self, user: User, symbol: str, price: float) -> float:
        holding = Portfolio.get(self.db, user.id, symbol)
        return float(holding["shares"]) * price if holding else 0.0

    #fast forward
    def run_over_period(self, user: User, from_date: str, to_date: str) -> dict:
        """Run the bot over every bar in an interval being skipped through.

        without this, moving time forward would throw away any trade the bot would
        have made, and the user would arrive in the present holding a portfolio
        that does not match their own strategy.
        """
        config = BotConfig.get(self.db, user.id)
        summary = {"ran": False, "trades": 0, "bars": 0}
        if not config or not config["enabled"] or not config["symbol"]:
            return summary

        symbol = config["symbol"]
        try:
            candles = self.engine.market.replay_candles(symbol, from_date, to_date)
        except Exception:
            log.exception("replay fetch failed during fast-forward")
            return summary

        summary["ran"] = True
        summary["bars"] = len(candles)
        trades = 0

        for candle in candles:
            fresh = User.get_by_id(self.db, user.id)
            config = BotConfig.get(self.db, user.id)
            if fresh is None or config is None:
                break
            #record each trade against its own bar's date rather than the date the
            #fast forward started on, so the ledger shows when it really happened.
            fresh.sim_date = datetime.fromtimestamp(candle.ts, timezone.utc).date().isoformat()

            #the cooldown counts real seconds, but each bar in a replay is a
            #separate moment in simulated time, so it is skipped here and the daily
            #limit does the holding back instead.
            decision = self._evaluate_bar(fresh, config, candle.close)
            if decision and self.act(fresh, config, decision):
                trades += 1
                if trades >= self._setting(config, "max_trades_per_day", 20):
                    BotEvent.log(self.db, user.id,
                                 "Reached the trade limit for this fast-forward.", level="info")
                    break

        summary["trades"] = trades
        if trades:
            BotEvent.log(
                self.db, user.id,
                f"Fast-forward {from_date} to {to_date}: {trades} trade(s) across {len(candles)} bars.",
                level="info",
            )
        return summary

    def _evaluate_bar(self, user: User, config, price: float) -> dict | None:
        """The same rules for one replayed bar, without the real time cooldown."""
        symbol = config["symbol"]
        buy_at, sell_at = config["threshold_buy"], config["threshold_sell"]
        if not symbol or buy_at is None or sell_at is None or not config["quantity"]:
            return None

        if price <= buy_at:
            cost = price * config["quantity"]
            if cost > user.balance:
                return None
            if self._position_value(user, symbol, price) + cost > self._setting(config, "max_position", 1e12):
                return None
            return {"action": "buy", "price": price, "symbol": symbol,
                    "shares": config["quantity"],
                    "reason": f"price ${price:,.2f} <= buy threshold ${buy_at:,.2f}"}

        if price >= sell_at:
            holding = Portfolio.get(self.db, user.id, symbol)
            if not holding or float(holding["shares"]) <= 0:
                return None
            return {"action": "sell", "price": price, "symbol": symbol,
                    "shares": min(config["quantity"], float(holding["shares"])),
                    "reason": f"price ${price:,.2f} >= sell threshold ${sell_at:,.2f}"}
        return None
