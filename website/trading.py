#every buy, sell and undo in the project goes through this module. keeping it
#in one place is what lets the same code serve a browser request and the
#trading bot thread, and it is why the balance, the ledger row and the holding
#can be written inside a single transaction.

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as _date

from .market import MarketDataError, get_market
from .models import BotEvent, Portfolio, Transaction, UndoStack, User, db as default_db

log = logging.getLogger(__name__)

#money is rounded to whole pence at every boundary, otherwise repeated float
#arithmetic drifts by fractions of a penny and a portfolio will not fully sell.
MONEY_DP = 2
SHARE_DP = 8  #crypto routinely trades in 8 decimal places
#the average cost is a price per share rather than a cash amount, so it keeps
#more decimal places than money does. rounding it to pence makes a buy then
#sell round trip report a gain or loss of a penny that never happened.
COST_DP = 8


class TradeError(Exception):
    """A trade that cannot go ahead, with a message safe to show the user."""


@dataclass
class TradeResult:
    action: str
    symbol: str
    shares: float
    price: float
    total: float
    new_balance: float
    transaction_id: int
    message: str

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "symbol": self.symbol,
            "shares": self.shares,
            "price": self.price,
            "total": self.total,
            "new_balance": self.new_balance,
            "transaction_id": self.transaction_id,
            "message": self.message,
        }


def money(value: float) -> float:
    return round(float(value), MONEY_DP)


def shares_of(value: float) -> float:
    return round(float(value), SHARE_DP)


def parse_shares(raw) -> float:
    """Check a quantity typed in by the user."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise TradeError("Enter a quantity.")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise TradeError("Quantity must be a number.") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise TradeError("Quantity must be a real number.")
    if value <= 0:
        raise TradeError("Quantity must be greater than zero.")
    if value > 1e12:
        raise TradeError("That quantity is unrealistically large.")
    return shares_of(value)


class TradeEngine:
    """Executes trades against a user's account."""

    def __init__(self, db=None, market=None) -> None:
        self.db = db or default_db
        self.market = market or get_market(self.db)

    #pricing
    def price_for(self, symbol: str, sim_date: str) -> float:
        """The price a trade fills at.

        this is the live price once the simulation date has caught up to today,
        and that date's closing price while it is behind. keeping both sides of
        a trade on the same date is what stops a user buying at a price from
        2016 and selling at today's.
        """
        try:
            price = self.market.price_on_date(symbol, sim_date)
        except MarketDataError as exc:
            raise TradeError(f"Could not fetch a price for {symbol}.") from exc
        if price is None or price <= 0:
            raise TradeError(
                f"No price available for {symbol} on {sim_date}. "
                "Check the symbol, or pick a date when the market was open."
            )
        return price

    #main entry point
    def execute(
        self,
        user: User,
        symbol: str,
        action: str,
        raw_shares,
        source: str = "manual",
        price: float | None = None,
        record_undo: bool = True,
    ) -> TradeResult:
        """Buy or sell, as one atomic unit of work.

        the user is passed in rather than read from current_user, so the
        trading bot can call this from its own thread.
        """
        action = (action or "").lower().strip()
        if action not in ("buy", "sell"):
            raise TradeError(f"Unknown action: {action!r}")

        symbol = (symbol or "").upper().strip()
        if not symbol:
            raise TradeError("Choose an asset first.")

        shares = parse_shares(raw_shares)
        sim_date = user.sim_date or _date.today().isoformat()
        exec_price = price if price is not None else self.price_for(symbol, sim_date)
        total = money(shares * exec_price)

        if action == "buy":
            return self._buy(user, symbol, shares, exec_price, total, sim_date, source, record_undo)
        return self._sell(user, symbol, shares, exec_price, total, sim_date, source, record_undo)

    #buy
    def _buy(self, user, symbol, shares, price, total, sim_date, source, record_undo) -> TradeResult:
        #read the balance again inside the transaction, since the bot may have
        #traded since the request that is calling this one began.
        with self.db.transaction() as conn:
            row = conn.execute("SELECT balance FROM users WHERE id = ?", (user.id,)).fetchone()
            if row is None:
                raise TradeError("Account not found.")
            balance = float(row["balance"])

            if total > balance:
                raise TradeError(
                    f"Insufficient funds: {symbol} costs ${total:,.2f} "
                    f"but your balance is ${balance:,.2f}."
                )

            new_balance = money(balance - total)
            conn.execute("UPDATE users SET balance = ? WHERE id = ?", (new_balance, user.id))

            holding = conn.execute(
                "SELECT shares, avg_cost FROM portfolios WHERE user_id = ? AND symbol = ?",
                (user.id, symbol),
            ).fetchone()

            if holding:
                old_shares = float(holding["shares"])
                old_cost = float(holding["avg_cost"])
                new_shares = shares_of(old_shares + shares)
                #weighted average cost, which is what profit and loss is
                #measured against.
                new_avg = round(((old_shares * old_cost) + (shares * price)) / new_shares, COST_DP)
            else:
                old_shares, old_cost = 0.0, None
                new_shares, new_avg = shares, round(price, COST_DP)

            Portfolio.upsert(conn, user.id, symbol, new_shares, new_avg)
            txn_id = Transaction.record(conn, user.id, symbol, shares, price, sim_date, source)
            if record_undo:
                #save the holding as it was before the trade so an undo can
                #put the average cost back exactly.
                UndoStack.push(conn, user.id, txn_id, symbol, shares, price, "buy",
                               prev_shares=old_shares, prev_avg_cost=old_cost,
                               sim_date=sim_date)
                UndoStack.trim(conn, user.id)

        user.balance = new_balance
        return TradeResult(
            action="buy", symbol=symbol, shares=shares, price=price, total=total,
            new_balance=new_balance, transaction_id=txn_id,
            message=f"Bought {shares:g} {symbol} at ${price:,.2f} (${total:,.2f}).",
        )

    #sell
    def _sell(self, user, symbol, shares, price, total, sim_date, source, record_undo) -> TradeResult:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT balance FROM users WHERE id = ?", (user.id,)).fetchone()
            if row is None:
                raise TradeError("Account not found.")
            balance = float(row["balance"])

            holding = conn.execute(
                "SELECT shares, avg_cost FROM portfolios WHERE user_id = ? AND symbol = ?",
                (user.id, symbol),
            ).fetchone()
            if holding is None:
                raise TradeError(f"You do not own any {symbol}.")

            held = float(holding["shares"])
            avg_cost = float(holding["avg_cost"])
            #allow a tiny rounding difference so selling all of a fractional
            #holding is not refused by a float artefact.
            if shares > held + 1e-9:
                raise TradeError(f"You only own {held:g} {symbol}.")
            shares = min(shares, held)
            #work the total out again after clamping, so the money added to the
            #balance and the total written to the ledger are the same number.
            total = money(shares * price)

            #write the new balance to the database, not just to the user object
            new_balance = money(balance + total)
            conn.execute("UPDATE users SET balance = ? WHERE id = ?", (new_balance, user.id))

            remaining = shares_of(held - shares)
            if remaining <= 1e-9:
                Portfolio.remove(conn, user.id, symbol)
            else:
                Portfolio.upsert(conn, user.id, symbol, remaining, avg_cost)

            txn_id = Transaction.record(conn, user.id, symbol, -shares, price, sim_date, source)
            if record_undo:
                UndoStack.push(conn, user.id, txn_id, symbol, shares, price, "sell",
                               prev_shares=held, prev_avg_cost=avg_cost,
                               sim_date=sim_date)
                UndoStack.trim(conn, user.id)

        user.balance = new_balance
        realised = money((price - avg_cost) * shares)
        return TradeResult(
            action="sell", symbol=symbol, shares=shares, price=price, total=total,
            new_balance=new_balance, transaction_id=txn_id,
            message=(
                f"Sold {shares:g} {symbol} at ${price:,.2f} (${total:,.2f}), "
                f"{'profit' if realised >= 0 else 'loss'} ${abs(realised):,.2f}."
            ),
        )

    #undo
    def undo_last(self, user: User) -> TradeResult:
        """Reverse the most recent trade and put the account back as it was.

        an undo is not a trade. the cash moves by the value of the trade being
        undone, the share count moves by its quantity, and the average cost is
        restored from the snapshot taken before it. carrying out the opposite
        trade instead would recalculate the average cost rather than restore
        it, which would leave the holding recorded at a price it was never
        bought at.

        the whole reversal is one transaction, and the entry is taken off the
        stack inside it, so two requests at once cannot undo the same trade.
        """
        #set when the entry cannot be applied. the error is raised after the
        #transaction rather than inside it, because a rollback would undo the
        #removal and leave the same dead entry blocking the stack.
        discarded: tuple | None = None
        txn_id: int | None = None

        with self.db.transaction() as conn:
            entry = UndoStack.peek_in(conn, user.id)
            if entry is None:
                raise TradeError("Nothing to undo.")

            symbol = entry["symbol"]
            action = entry["action"]
            shares = shares_of(float(entry["shares"]))
            price = float(entry["price"])
            value = money(shares * price)

            row = conn.execute("SELECT balance FROM users WHERE id = ?", (user.id,)).fetchone()
            if row is None:
                raise TradeError("Account not found.")
            balance = float(row["balance"])

            holding = conn.execute(
                "SELECT shares, avg_cost FROM portfolios WHERE user_id = ? AND symbol = ?",
                (user.id, symbol),
            ).fetchone()
            held = float(holding["shares"]) if holding else 0.0

            if action == "buy":
                #undoing a buy, so give the money back and take the shares away
                delta_shares, new_balance = -shares, money(balance + value)
            else:
                #undoing a sell, so take the money back and return the shares
                delta_shares, new_balance = shares, money(balance - value)
                if value > balance:
                    raise TradeError(
                        f"Undoing that sale costs ${value:,.2f} but your balance "
                        f"is ${balance:,.2f}. Sell something else first."
                    )

            new_shares = shares_of(held + delta_shares)
            if new_shares < -1e-9:
                #the shares have gone since, sold by hand or by the bot, which
                #records no undo entries of its own. drop the entry rather than
                #leave it blocking every older one behind it.
                UndoStack.pop(conn, entry["id"])
                discarded = (action, shares, symbol, held)
            else:
                txn_id = self._apply_undo(
                    conn, user, entry, symbol, price, delta_shares,
                    new_shares, new_balance, holding,
                )

        if discarded:
            action, shares, symbol, held = discarded
            raise TradeError(
                f"That {action} of {shares:g} {symbol} can no longer be undone, "
                f"as you now hold only {held:g}. It has been removed from your undo history."
            )

        user.balance = new_balance
        return TradeResult(
            action="undo",
            symbol=symbol,
            shares=shares,
            price=price,
            total=value,
            new_balance=new_balance,
            transaction_id=txn_id,
            message=f"Undid {action} of {shares:g} {symbol} at ${price:,.2f}.",
        )

    def _apply_undo(self, conn, user, entry, symbol, price, delta_shares,
                    new_shares, new_balance, holding) -> int:
        """Write the reversal and return the id of its ledger row."""
        conn.execute("UPDATE users SET balance = ? WHERE id = ?", (new_balance, user.id))

        #put the average cost back to what it was before the trade. entries
        #saved before the snapshot columns existed have none, so those leave
        #the average alone.
        previous_avg = entry["prev_avg_cost"]
        if new_shares <= 1e-9:
            Portfolio.remove(conn, user.id, symbol)
        else:
            avg_cost = (
                float(previous_avg) if previous_avg is not None
                else (float(holding["avg_cost"]) if holding else price)
            )
            Portfolio.upsert(conn, user.id, symbol, new_shares, round(avg_cost, COST_DP))

        #file the reversal against the simulated date of the trade it undoes,
        #not wherever the simulation has moved to since.
        ledger_date = entry["sim_date"] or user.sim_date or _date.today().isoformat()
        txn_id = Transaction.record(
            conn, user.id, symbol, delta_shares, price, ledger_date, "undo"
        )
        UndoStack.pop(conn, entry["id"])
        return txn_id

    #valuation
    def portfolio_valuation(self, user: User, as_of: str | None = None) -> dict:
        """Value a user's holdings, with cost and profit or loss.

        every position is priced on the account's simulation date, not at the
        live price. buying and immediately reloading the page has to show no
        profit, because nothing has happened yet. once the simulation date has
        caught up to today, that date is today, so a live account sees the live
        price.

        prices are fetched in one batch through the cache rather than one
        request per holding.
        """
        #read the balance from the database rather than from the user object.
        #current_user is loaded once per request, so it goes stale the moment
        #the bot thread trades.
        row = self.db.fetchone("SELECT balance FROM users WHERE id = ?", (user.id,))
        balance = float(row["balance"]) if row else float(user.balance)
        user.balance = balance

        as_of = as_of or user.sim_date or _date.today().isoformat()
        holdings = Portfolio.for_user(self.db, user.id)
        symbols = [h["symbol"] for h in holdings]
        quotes = self.market.quotes_on_date(symbols, as_of) if symbols else {}

        positions = []
        total_value = 0.0
        total_cost = 0.0

        for h in holdings:
            symbol = h["symbol"]
            shares = float(h["shares"])
            avg_cost = float(h["avg_cost"])
            quote = quotes.get(symbol)
            price = quote.price if quote else None

            value = money(shares * price) if price is not None else None
            cost = money(shares * avg_cost)
            pnl = money(value - cost) if value is not None else None
            pnl_pct = round((pnl / cost * 100), 2) if (pnl is not None and cost) else None

            if value is not None:
                total_value += value
                total_cost += cost

            positions.append({
                "symbol": symbol,
                "shares": shares,
                "avg_cost": avg_cost,
                "current_price": price,
                "value": value,
                "cost": cost,
                "pnl": pnl,
                "pnl_percent": pnl_pct,
                "day_change_percent": quote.change_percent if quote else None,
                "stale": quote.stale if quote else True,
                "priced": price is not None,
            })

        total_pnl = money(total_value - total_cost)
        return {
            "positions": positions,
            "total_value": money(total_value),
            "total_cost": money(total_cost),
            "total_pnl": total_pnl,
            "total_pnl_percent": round(total_pnl / total_cost * 100, 2) if total_cost else 0.0,
            "balance": money(balance),
            "net_worth": money(balance + total_value),
            #the date these figures describe, so the pages can label them
            "as_of": as_of,
            "live": as_of >= _date.today().isoformat(),
        }

    def execution_price(self, symbol: str, user: User) -> float | None:
        """The price a trade would fill at now, or None if there is not one.

        the trade panel needs this on its own, because on a past simulation
        date it is not the same number as the live quote.
        """
        try:
            return self.price_for(symbol, user.sim_date or _date.today().isoformat())
        except TradeError:
            return None

    def max_affordable(self, user: User, symbol: str) -> float:
        """How many shares the balance covers, for the percentage buttons."""
        price = self.execution_price(symbol, user)
        if not price or price <= 0:
            return 0.0
        #read the balance from the database, since the user object may have
        #been loaded before the bot last traded.
        row = self.db.fetchone("SELECT balance FROM users WHERE id = ?", (user.id,))
        balance = float(row["balance"]) if row else float(user.balance)
        return shares_of(balance / price)
