#fixtures shared by every test file.
#
#the tests run against a temporary database and a made up market, so they
#give the same answer every time and never touch the real data or the
#network.

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#point the application at a throwaway database before anything under
#website is imported. models.db is set when that module is first read, so
#setting this afterwards would leave it pointing at the real database, and
#reloading the package to fix that gives two copies of every class, which
#makes pytest.raises() fail to match an exception of the same name.
os.environ.setdefault("DATABASE_PATH", str(Path(tempfile.mkdtemp()) / "suite.db"))
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-used-in-production")

from website.config import Config  #noqa: E402
from website.market.provider import Candle, MarketDataProvider, Quote, SearchResult  #noqa: E402
from website.models import Database, User  #noqa: E402


class FakeProvider(MarketDataProvider):
    """A market whose prices the test decides.

    there are two layers, the same as a real source has: a live price for each
    symbol, and a price for a particular date in the past. the second is what
    lets a test tell the two apart, which matters because valuing a holding at
    the live price while the account is trading on a past date is exactly the
    mistake being guarded against.
    """

    name = "fake"
    supports_search = True

    def __init__(self, prices=None):
        self.prices = prices or {"AAPL": 100.0, "BTC-USD": 50_000.0}
        self.historical: dict[tuple[str, str], float] = {}
        self.calls = 0

    def set_price(self, symbol, price):
        self.prices[symbol.upper()] = price

    def set_price_on(self, symbol, iso_date, price):
        """Set what a symbol closed at on a given date."""
        self.historical[(symbol.upper(), iso_date)] = price

    def supports(self, symbol):
        return True

    def get_quote(self, symbol):
        self.calls += 1
        symbol = symbol.upper()
        if symbol not in self.prices:
            from website.market.provider import SymbolNotFound
            raise SymbolNotFound(symbol)
        return Quote(symbol=symbol, price=self.prices[symbol],
                     previous_close=self.prices[symbol] * 0.99, source="fake")

    def get_candles(self, symbol, interval="5m", start=None, end=None, lookback_range=None):
        symbol = symbol.upper()
        if start is None or end is None:
            price = self.prices.get(symbol, 100.0)
            return [Candle(ts=1_700_000_000 + i * 300, open=price, high=price,
                           low=price, close=price, volume=1) for i in range(10)]

        #a window was asked for, so give one bar per day, and the service has
        #something real to walk back through looking for a closing price.
        from datetime import datetime, timedelta, timezone

        bars = []
        day = datetime.fromtimestamp(int(start), timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        last = datetime.fromtimestamp(int(end), timezone.utc)
        while day <= last:
            iso = day.date().isoformat()
            price = self.historical.get((symbol, iso), self.prices.get(symbol, 100.0))
            bars.append(Candle(ts=int(day.timestamp()), open=price, high=price,
                               low=price, close=price, volume=1))
            day += timedelta(days=1)
        return bars

    def search(self, query, limit=8):
        return [SearchResult(symbol=s, name=s) for s in self.prices if query.upper() in s][:limit]


@pytest.fixture
def db(tmp_path):
    """An empty database, brought up to date, in a temporary folder."""
    import logging
    from website.migrations import run_migrations

    path = tmp_path / "test.db"
    database = Database(path)
    run_migrations(database, logging.getLogger("test"))
    yield database
    database.close()
    Database._instances.pop(str(path.resolve()), None)


@pytest.fixture
def market(db):
    from website.market.service import MarketService

    from website.market.cache import TTLCache

    service = MarketService(db)
    fake = FakeProvider()
    service.yahoo = fake
    service.binance = fake
    service.providers = [fake]
    service._ordered_providers = lambda symbol: [fake]
    service.fake = fake
    #switch the caching off. a one second life on a price is the whole point
    #of the cache in the real application, but here it would hide a price the
    #test has just set from the code being tested.
    service._quotes = TTLCache(0)
    service._candles = TTLCache(0)
    service._searches = TTLCache(0)
    #a closing price for a past date is read back after it has expired,
    #because it cannot change, so a cache that holds nothing at all is the
    #only way to switch that layer off for a test.
    service._historical = TTLCache(0, max_entries=0)
    return service


@pytest.fixture
def engine(db, market):
    from website.trading import TradeEngine

    return TradeEngine(db=db, market=market)


@pytest.fixture
def user(db):
    """A user with a balance of 100,000 whose simulation date is today."""
    from datetime import date

    u = User(email="test@example.com", password="x", first_name="Tester",
             balance=100_000.0, sim_date=date.today().isoformat())
    u.save(db)
    return u
