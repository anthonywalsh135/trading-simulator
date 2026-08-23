#the object oriented part of the project. an abstract class sets out what every
#tradeable asset has to be able to do, and the classes below it fill in what
#differs between one kind of asset and another.
#
#Crypto really does differ from Stock rather than only changing a title: it is
#always open because crypto markets never close, it allows fractions of a coin
#to be bought, and it shows more decimal places for coins worth less than a
#pound.

from __future__ import annotations

from abc import ABC, abstractmethod

from .market import Candle, Quote, get_market


class Asset(ABC):
    """The blueprint for a kind of tradeable asset."""

    #the name shown in page headings and messages
    label: str = "Asset"
    #whether part of a unit can be bought
    allows_fractional: bool = False
    #decimal places used when showing a price
    price_dp: int = 2

    def __init__(self, market=None) -> None:
        self.market = market or get_market()

    #what every asset has to provide
    @abstractmethod
    def quote(self, symbol: str) -> Quote:
        """The current price of a symbol."""

    @abstractmethod
    def candles(self, symbol: str, interval: str, lookback_range: str) -> list[Candle]:
        """The price bars used to draw the chart."""

    @abstractmethod
    def is_open(self, quote: Quote) -> bool:
        """Whether the market for this asset is open right now."""

    @abstractmethod
    def search(self, query: str, limit: int = 8) -> list:
        """Search suggestions limited to this kind of asset."""

    #behaviour shared by every asset
    def statistics(self, candles: list[Candle]) -> dict:
        """Work out the summary figures shown underneath the chart."""
        if not candles:
            return {}
        closes = [c.close for c in candles]
        n = len(closes)
        mean = sum(closes) / n
        variance = sum((c - mean) ** 2 for c in closes) / n if n > 1 else 0.0
        return {
            "mean": round(mean, self.price_dp),
            "high": round(max(c.high for c in candles), self.price_dp),
            "low": round(min(c.low for c in candles), self.price_dp),
            "volatility": round(variance**0.5, 4),
            "volume": round(sum(c.volume for c in candles), 2),
            "open": round(candles[0].open, self.price_dp),
            "close": round(candles[-1].close, self.price_dp),
            "bars": n,
        }

    def format_price(self, value: float) -> str:
        return f"${value:,.{self.price_dp}f}"


class Stock(Asset):
    """Shares and funds, traded in whole units while the market is open."""

    label = "Stock"
    allows_fractional = False
    price_dp = 2

    def quote(self, symbol: str) -> Quote:
        return self.market.get_quote(symbol)

    def candles(self, symbol: str, interval: str = "5m", lookback_range: str = "1d") -> list[Candle]:
        return self.market.get_candles(symbol, interval=interval, lookback_range=lookback_range)

    def is_open(self, quote: Quote) -> bool:
        return quote.market_state == "REGULAR"

    def search(self, query: str, limit: int = 8) -> list:
        return [
            r for r in self.market.search(query, limit=limit + 4)
            if r.asset_type in ("EQUITY", "ETF", "INDEX", "MUTUALFUND")
        ][:limit]


class Crypto(Stock):
    """Cryptocurrency, which never closes and can be bought in fractions.

    inherits from Stock because the charting and the summary figures work the
    same way. what actually differs is replaced below.
    """

    label = "Cryptocurrency"
    allows_fractional = True
    price_dp = 2

    def is_open(self, quote: Quote) -> bool:
        """Crypto markets are always open."""
        return True

    def search(self, query: str, limit: int = 8) -> list:
        results = [
            r for r in self.market.search(query, limit=limit + 6)
            if r.asset_type == "CRYPTOCURRENCY"
        ]
        return results[:limit]

    def format_price(self, value: float) -> str:
        #coins worth less than a pound need more than two decimal places
        dp = self.price_dp if value >= 1 else 6
        return f"${value:,.{dp}f}"


def asset_for(kind: str) -> Asset:
    """Return the right asset class for a page, either stocks or crypto."""
    return Crypto() if kind == "crypto" else Stock()
