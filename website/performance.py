#builds the net worth history shown on the account page.
#
#this is the one place in the project where pandas earns its keep. Working out
#what an account was worth on every day since its first trade means lining up
#three things that are all indexed by date but none of which share the same
#dates: the trade ledger (only has rows on days the user traded), the daily
#closing prices (only has rows on days the market was open) and the calendar
#itself. Pandas does that alignment with reindex and forward fill; doing it by
#hand would mean a nested loop over days and symbols with a manual search for
#the most recent price before each day.

from datetime import date as _date, datetime, timedelta, timezone

import pandas as pd

from .models import Transaction

#a chart of more than this many days is not readable, and the daily candle
#request behind it gets slow, so the window is capped.
MAX_DAYS = 730


class PerformanceHistory:
    """Works out what a user's account was worth on each day."""

    def __init__(self, db, market):
        self.db = db
        self.market = market

    def build(self, user, as_of=None):
        """Return the daily cash, holdings value and net worth for a user."""
        as_of = as_of or user.sim_date or _date.today().isoformat()
        trades = Transaction.for_user(self.db, user.id, limit=5000)
        if not trades:
            return {"dates": [], "cash": [], "invested": [], "net_worth": []}

        ledger = pd.DataFrame(
            [(t["sim_date"], t["symbol"], float(t["shares"]), float(t["total"])) for t in trades],
            columns=["date", "symbol", "shares", "total"],
        )
        ledger["date"] = pd.to_datetime(ledger["date"], errors="coerce")
        ledger = ledger.dropna(subset=["date"])
        if ledger.empty:
            return {"dates": [], "cash": [], "invested": [], "net_worth": []}

        calendar = self._calendar(ledger["date"].min(), as_of)
        if calendar.empty:
            return {"dates": [], "cash": [], "invested": [], "net_worth": []}

        holdings = self._holdings(ledger, calendar)
        prices = self._prices(holdings.columns, calendar)

        #both frames are indexed by day and keyed by symbol, so multiplying
        #them lines every share count up with that day's price automatically.
        invested = (holdings * prices).sum(axis=1)
        cash = self._cash(ledger, calendar, user)
        net_worth = cash + invested

        return {
            "dates": [d.strftime("%Y-%m-%d") for d in calendar],
            "cash": [round(v, 2) for v in cash.tolist()],
            "invested": [round(v, 2) for v in invested.tolist()],
            "net_worth": [round(v, 2) for v in net_worth.tolist()],
        }

    @staticmethod
    def _calendar(first_trade, as_of):
        """Every day from the first trade up to the simulation date."""
        end = pd.Timestamp(as_of)
        start = max(pd.Timestamp(first_trade), end - pd.Timedelta(days=MAX_DAYS))
        if start > end:
            return pd.DatetimeIndex([])
        return pd.date_range(start=start, end=end, freq="D")

    @staticmethod
    def _holdings(ledger, calendar):
        """How many of each symbol the user held at the end of each day.

        Buys are positive and sells negative in the ledger, so a running total
        down each symbol's column is the position on that day.
        """
        daily = ledger.pivot_table(
            index="date", columns="symbol", values="shares", aggfunc="sum", fill_value=0.0
        )
        #reindexing onto the full calendar puts a zero-change row on every day
        #the user did not trade, so the running total carries the position
        #forward instead of the line disappearing between trades.
        return daily.reindex(calendar, fill_value=0.0).cumsum()

    def _prices(self, symbols, calendar):
        """A closing price for every symbol on every day in the calendar."""
        start = int(calendar[0].replace(tzinfo=timezone.utc).timestamp())
        end = int((calendar[-1] + pd.Timedelta(days=1)).replace(tzinfo=timezone.utc).timestamp())

        columns = {}
        for symbol in symbols:
            candles = self.market.get_candles(
                symbol, interval="1d", lookback_range=None, start=start, end=end
            )
            if not candles:
                columns[symbol] = pd.Series(0.0, index=calendar)
                continue
            series = pd.Series(
                [c.close for c in candles],
                index=pd.to_datetime(
                    [datetime.fromtimestamp(c.ts, timezone.utc).date() for c in candles]
                ),
            )
            #a symbol can have two bars land on the same day when the provider
            #reports a session in more than one part, so duplicates are dropped
            #before the reindex, which will not accept them.
            columns[symbol] = series[~series.index.duplicated(keep="last")]

        prices = pd.DataFrame(columns).reindex(calendar)
        #weekends, holidays and halts leave gaps. Carrying the last close
        #forward is what a broker statement does; the leading backfill covers a
        #symbol whose first bar lands after the calendar starts.
        return prices.ffill().bfill().fillna(0.0)

    @staticmethod
    def _cash(ledger, calendar, user):
        """The cash balance at the end of each day.

        The balance is reconstructed backwards from the balance held now,
        rather than forwards from the starting balance, so that an account
        whose balance was adjusted by an administrator still ends the chart on
        the figure the user actually sees.
        """
        flows = ledger.groupby("date")["total"].sum().reindex(calendar, fill_value=0.0)
        #a positive total is money leaving the account, so adding back
        #everything spent after a given day gives the balance on that day.
        spent_after = flows[::-1].cumsum()[::-1] - flows
        return spent_after + float(user.balance)
