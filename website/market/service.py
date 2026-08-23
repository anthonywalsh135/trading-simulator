#the one place the rest of the project asks for a price. it sends each symbol
#to a provider that can serve it, falls back to the next one when that fails,
#answers from the cache so page loads and polling do not become upstream
#requests, and returns the last known price marked stale when every provider is
#down, so a network problem does not blank out the whole site.
#
#no view function should call a provider directly.

from __future__ import annotations

import logging
import time
from datetime import date as _date, datetime, timedelta, timezone

from ..config import Config
from .binance import BinanceProvider
from .cache import CandleStore, QuoteRefresher, TTLCache
from .provider import Candle, MarketDataError, Quote, SearchResult, SymbolNotFound
from .yahoo import YahooProvider

log = logging.getLogger(__name__)

CRYPTO_HINTS = ("-USD", "USDT", "-GBP", "-EUR")

#how many days each chart range covers, needed when a window has to be given
#as start and end timestamps because it ends on a past simulation date.
RANGE_DAYS = {
    "1d": 1, "5d": 5, "1mo": 31, "3mo": 92,
    "6mo": 183, "1y": 366, "2y": 731, "5y": 1826, "max": 7300,
}

#how far back each interval reaches. yahoo throws intraday bars away after
#these windows, so asking for 5 minute candles from 2016 returns nothing at all
#rather than an error. anything older is moved up to the finest interval that
#still has data.
INTERVAL_MAX_AGE_DAYS = {
    "1m": 7, "2m": 59, "5m": 59, "15m": 59, "30m": 59, "60m": 729, "1h": 729,
}

#the order intervals are tried in when one cannot reach far enough back
INTERVAL_LADDER = ["1m", "5m", "15m", "60m", "1d"]


def today_iso() -> str:
    """Today's date.

    defined once so the market layer, the views and the api all agree on where
    today is. using the utc date here and the local date elsewhere means that
    either side of midnight a simulation date can count as both today and the
    past at the same time.
    """
    return _date.today().isoformat()


class MarketService:
    def __init__(self, db) -> None:
        self.db = db
        self.yahoo = YahooProvider()
        self.binance = BinanceProvider()
        self.providers = [self.yahoo, self.binance]

        self._quotes = TTLCache(Config.QUOTE_CACHE_SECONDS)
        self._candles = TTLCache(Config.CANDLE_CACHE_SECONDS)
        self._searches = TTLCache(Config.SEARCH_CACHE_SECONDS)
        #a closing price for a past date cannot change, so it is kept apart
        #from the short lived caches and read back after it has expired.
        self._historical = TTLCache(Config.CANDLE_CACHE_SECONDS)
        self._store = CandleStore(db)

        self.refresher = QuoteRefresher(
            fetch=lambda s: self.get_quote(s, use_cache=False),
            interval=Config.QUOTE_CACHE_SECONDS,
        )

    #routing
    @staticmethod
    def is_crypto(symbol: str) -> bool:
        return symbol.upper().endswith(CRYPTO_HINTS)

    def _ordered_providers(self, symbol: str):
        """The providers to try, best match first."""
        if self.is_crypto(symbol):
            #binance is real time and never closes for crypto, yahoo backs it up
            return [self.binance, self.yahoo]
        return [self.yahoo]

    #quotes
    def get_quote(self, symbol: str, use_cache: bool = True) -> Quote:
        symbol = symbol.upper().strip()
        key = f"q:{symbol}"

        if use_cache:
            cached = self._quotes.get(key)
            if cached is not None:
                return cached

        errors = []
        for provider in self._ordered_providers(symbol):
            try:
                quote = provider.get_quote(symbol)
                self._quotes.set(key, quote)
                self.refresher.watch(symbol)
                return quote
            except SymbolNotFound as exc:
                errors.append(f"{provider.name}: {exc}")
            except MarketDataError as exc:
                errors.append(f"{provider.name}: {exc}")
                log.warning("quote failed for %s via %s: %s", symbol, provider.name, exc)

        #everything failed, so fall back to the last known price, marked stale
        stale = self._quotes.get_stale(key)
        if stale is not None:
            log.info("serving stale quote for %s", symbol)
            return Quote(
                symbol=stale.symbol, price=stale.price,
                previous_close=stale.previous_close, currency=stale.currency,
                market_state=stale.market_state, timestamp=stale.timestamp,
                stale=True, source=stale.source,
            )
        raise SymbolNotFound(f"no price for {symbol} ({'; '.join(errors)})")

    def get_quotes(self, symbols) -> dict[str, Quote]:
        """Look up several symbols at once, skipping any that fail."""
        out: dict[str, Quote] = {}
        for symbol in {s.upper().strip() for s in symbols if s}:
            try:
                out[symbol] = self.get_quote(symbol)
            except MarketDataError:
                continue
        return out

    def get_price(self, symbol: str) -> float | None:
        """Just the price for a symbol, or None."""
        try:
            return self.get_quote(symbol).price
        except MarketDataError:
            return None

    #candles
    def get_candles(
        self,
        symbol: str,
        interval: str = "5m",
        lookback_range: str | None = "1d",
        start: int | None = None,
        end: int | None = None,
    ) -> list[Candle]:
        symbol = symbol.upper().strip()
        key = f"c:{symbol}:{interval}:{lookback_range}:{start}:{end}"
        cached = self._candles.get(key)
        if cached is not None:
            return cached

        errors = []
        for provider in self._ordered_providers(symbol):
            try:
                candles = provider.get_candles(
                    symbol, interval=interval, start=start, end=end,
                    lookback_range=lookback_range,
                )
                if candles:
                    self._candles.set(key, candles)
                    self._store.save(symbol, interval, candles)
                    return candles
            except MarketDataError as exc:
                errors.append(f"{provider.name}: {exc}")

        #upstream is unavailable, so use whatever the saved candles hold
        if start is not None and end is not None:
            persisted = self._store.load(symbol, interval, start, end)
            if persisted:
                log.info("serving %d cached candles for %s", len(persisted), symbol)
                return persisted
        log.warning("no candles for %s (%s)", symbol, "; ".join(errors))
        return []

    def candles_as_of(
        self,
        symbol: str,
        interval: str = "5m",
        lookback_range: str = "1d",
        as_of: str | None = None,
    ) -> tuple[list[Candle], str]:
        """Bars for a chart ending on the simulation date.

        returns the bars and the interval they are actually drawn at, which is
        not always the one asked for. intraday history runs out upstream, so a
        request for 5 minute bars from 2016 comes back as daily bars instead of
        an empty chart, and the page says which is on screen.
        """
        as_of = as_of or today_iso()
        if as_of >= today_iso():
            #the simulation has caught up, so the normal live window applies
            return self.get_candles(symbol, interval=interval,
                                    lookback_range=lookback_range), interval

        #close the window on the last second of the simulated day rather than
        #midnight the next morning, so a bar from a date the account has not
        #reached yet cannot appear on the chart.
        end_dt = (datetime.fromisoformat(as_of).replace(tzinfo=timezone.utc)
                  + timedelta(days=1) - timedelta(seconds=1))
        age_days = (datetime.now(timezone.utc) - end_dt).days
        interval = self._interval_reaching(interval, age_days)

        span = RANGE_DAYS.get(lookback_range, 1)
        #one day of daily bars is a single candle, which is not a chart, so
        #widen the span when the interval has been moved up.
        if interval in ("1d", "1wk", "1mo"):
            span = max(span, 90)
        start_dt = end_dt - timedelta(days=span)

        bars = self.get_candles(
            symbol, interval=interval, lookback_range=None,
            start=int(start_dt.timestamp()), end=int(end_dt.timestamp()),
        )
        return bars, interval

    @staticmethod
    def _interval_reaching(interval: str, age_days: int) -> str:
        """The finest interval whose history still reaches age_days back."""
        if age_days <= INTERVAL_MAX_AGE_DAYS.get(interval, 10**6):
            return interval
        for candidate in INTERVAL_LADDER:
            if INTERVAL_MAX_AGE_DAYS.get(candidate, 10**6) >= age_days:
                return candidate
        return "1d"

    #pricing on a simulation date
    def quote_on_date(self, symbol: str, iso_date: str | None = None) -> Quote | None:
        """What a symbol was worth on a given date.

        once the date has caught up to today this is the live quote. otherwise
        it is that date's closing price, paired with the previous trading day's
        close so the pages can still show a daily change.

        a closing price for a past date never changes, so it is cached under
        its own key and never fetched twice.
        """
        symbol = symbol.upper().strip()
        iso_date = iso_date or today_iso()
        if iso_date >= today_iso():
            try:
                return self.get_quote(symbol)
            except MarketDataError:
                return None

        key = f"dq:{symbol}:{iso_date}"
        cached = self._historical.get_stale(key)
        if cached is not None:
            return cached

        for provider in self._ordered_providers(symbol):
            try:
                closes = self._daily_closes(provider, symbol, iso_date)
            except MarketDataError:
                continue
            if not closes:
                continue
            ts, close = closes[-1]
            quote = Quote(
                symbol=symbol,
                price=close,
                previous_close=closes[-2][1] if len(closes) > 1 else None,
                market_state="CLOSED",  #a past date is never live
                timestamp=ts,
                source=f"{provider.name}@{iso_date}",
            )
            self._historical.set(key, quote)
            return quote
        return None

    @staticmethod
    def _daily_closes(provider, symbol: str, iso_date: str) -> list[tuple[int, float]]:
        """Daily closing prices up to and including a date, oldest first.

        the window reaches ten days back so a date falling on a weekend, a
        holiday or a trading halt still finds the most recent session before
        it, and so there is a previous close to compare against.
        """
        day = datetime.fromisoformat(iso_date).replace(tzinfo=timezone.utc)
        start = int((day - timedelta(days=10)).timestamp())
        end = int(min(day + timedelta(days=1), datetime.now(timezone.utc)).timestamp())
        candles = provider.get_candles(symbol, interval="1d", start=start, end=end)
        cutoff = day.timestamp() + 86400
        return [(c.ts, c.close) for c in candles if c.ts < cutoff]

    def quotes_on_date(self, symbols, iso_date: str | None = None) -> dict[str, Quote]:
        """quote_on_date for several symbols, skipping any that fail."""
        iso_date = iso_date or today_iso()
        out: dict[str, Quote] = {}
        for symbol in {s.upper().strip() for s in symbols if s}:
            quote = self.quote_on_date(symbol, iso_date)
            if quote is not None:
                out[symbol] = quote
        return out

    def price_on_date(self, symbol: str, iso_date: str) -> float | None:
        """The closing price on a date, or None if it did not trade."""
        quote = self.quote_on_date(symbol, iso_date)
        return quote.price if quote else None

    def replay_candles(self, symbol: str, from_date: str, to_date: str | None = None) -> list[Candle]:
        """The bars a fast forward animates through.

        the interval is picked from the length of the span, so the animation
        always has a useful number of frames without asking for more history
        than the interval holds.
        """
        start_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
        end_dt = (
            datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc)
            if to_date else datetime.now(timezone.utc)
        )
        days = max((end_dt - start_dt).days, 0)

        if days <= 5:
            interval = "5m"
        elif days <= 55:
            interval = "60m"
        else:
            interval = "1d"

        return self.get_candles(
            symbol, interval=interval, lookback_range=None,
            start=int(start_dt.timestamp()), end=int(end_dt.timestamp()),
        )

    #search
    def search(self, query: str, limit: int = 8) -> list[SearchResult]:
        """Suggest symbols for a company name or ticker.

        crypto matches go in front of the yahoo results so that typing "bit"
        offers bitcoin, not only companies with "bit" in their name.
        """
        query = (query or "").strip()
        if len(query) < 1:
            return []

        key = f"s:{query.lower()}:{limit}"
        cached = self._searches.get(key)
        if cached is not None:
            return cached

        results: list[SearchResult] = list(self.binance.search(query, limit=3))
        seen = {r.symbol for r in results}
        try:
            for result in self.yahoo.search(query, limit=limit):
                if result.symbol not in seen:
                    results.append(result)
                    seen.add(result.symbol)
        except MarketDataError as exc:
            log.warning("symbol search failed for %r: %s", query, exc)

        results = results[:limit]
        self._searches.set(key, results)
        return results

    def resolve(self, symbol: str) -> SearchResult | None:
        """Check a symbol exists and return its name and type."""
        symbol = symbol.upper().strip()
        for result in self.search(symbol, limit=10):
            if result.symbol.upper() == symbol:
                return result
        try:
            self.get_quote(symbol)
            return SearchResult(symbol=symbol, name=symbol)
        except MarketDataError:
            return None


_service: MarketService | None = None


def get_market(db=None) -> MarketService:
    """The shared market service, created on first use."""
    global _service
    if _service is None:
        if db is None:
            from ..models import db as default_db
            db = default_db
        _service = MarketService(db)
    return _service
