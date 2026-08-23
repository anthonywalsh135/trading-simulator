#the blueprint every source of market data has to follow, so that no other
#part of the project depends on which service is actually being used. swapping
#a source out is then a change to one file rather than to every page.
#
#the classes that fill this in sit next to this file:
#yahoo.py    - the main source. shares, crypto and company name search
#binance.py  - crypto only, real time

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(frozen=True)
class Quote:
    """The price of one symbol at one moment."""

    symbol: str
    price: float
    previous_close: float | None = None
    currency: str = "USD"
    market_state: str = "UNKNOWN"  #REGULAR, CLOSED, PRE or POST
    timestamp: int = 0  #seconds since 1970
    stale: bool = False  #came from the cache after a failure
    source: str = ""

    @property
    def change(self) -> float:
        if self.previous_close is None:
            return 0.0
        return round(self.price - self.previous_close, 4)

    @property
    def change_percent(self) -> float:
        if not self.previous_close:
            return 0.0
        return round((self.price - self.previous_close) / self.previous_close * 100, 4)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["change"] = self.change
        data["change_percent"] = self.change_percent
        return data


@dataclass(frozen=True)
class Candle:
    """One bar on a price chart."""

    ts: int  #seconds since 1970, when the bar opened
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SearchResult:
    """One suggestion from the search box."""

    symbol: str
    name: str
    exchange: str = ""
    asset_type: str = "EQUITY"  #EQUITY, CRYPTOCURRENCY, ETF or INDEX

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MarketDataError(Exception):
    """Raised when a source cannot answer a request."""


class SymbolNotFound(MarketDataError):
    """Raised when a symbol does not exist."""


class MarketDataProvider(ABC):
    """The abstract class every market data source inherits from."""

    name: str = "abstract"
    supports_search: bool = False

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        """Return the latest price for a symbol."""

    @abstractmethod
    def get_candles(
        self,
        symbol: str,
        interval: str = "5m",
        start: int | None = None,
        end: int | None = None,
        lookback_range: str | None = None,
    ) -> list[Candle]:
        """Return price bars, oldest first.

        either a lookback_range such as "1d" or a start and end time has to be
        given.
        """

    def search(self, query: str, limit: int = 8) -> list[SearchResult]:
        """Return suggestions for something typed into the search box."""
        return []

    @abstractmethod
    def supports(self, symbol: str) -> bool:
        """Whether this source can serve a symbol."""

    def price_on_date(self, symbol: str, iso_date: str) -> float | None:
        """The closing price on a date, or None if it did not trade.

        used when a simulation date is in the past, because a trade then has to
        fill at that date's price rather than today's.
        """
        import time
        from datetime import datetime, timedelta, timezone

        day = datetime.fromisoformat(iso_date).replace(tzinfo=timezone.utc)
        #widen the window so that a weekend or a holiday still finds the most
        #recent trading day at or before the date asked for.
        start = int((day - timedelta(days=7)).timestamp())
        end = int(min(day + timedelta(days=1), datetime.now(timezone.utc)).timestamp())
        candles = self.get_candles(symbol, interval="1d", start=start, end=end)
        if not candles:
            return None
        target_end = day.timestamp() + 86400
        eligible = [c for c in candles if c.ts < target_end]
        return eligible[-1].close if eligible else None
