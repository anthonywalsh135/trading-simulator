#share prices, price bars and company name search from yahoo finance. this is
#the main source of data for the whole project.
#
#it was chosen over alpha vantage for three reasons. it needs no api key, so
#there is no credential in the project at all. it has no practical daily
#request limit. and its search endpoint turns a company name into a ticker
#("apple" into AAPL), which is what the search box on every page runs on.
#
#these endpoints are public but not documented, so this class defends itself: a
#browser user agent, because yahoo refuses requests without one, a timeout on
#every request, a retry that waits longer each time, and errors the caller can
#recognise and work around.

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from ..config import Config
from .provider import (
    Candle,
    MarketDataError,
    MarketDataProvider,
    Quote,
    SearchResult,
    SymbolNotFound,
)

log = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"

#yahoo limits how far back each interval goes, and asking for more returns an
#error rather than a shorter series, so every request is clamped to these.
INTERVAL_MAX_DAYS = {
    "1m": 7,
    "2m": 59,
    "5m": 59,
    "15m": 59,
    "30m": 59,
    "60m": 729,
    "1h": 729,
    "1d": 36500,
    "1wk": 36500,
    "1mo": 36500,
}

VALID_INTERVALS = set(INTERVAL_MAX_DAYS)


class YahooProvider(MarketDataProvider):
    name = "yahoo"
    supports_search = True

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": Config.HTTP_USER_AGENT,
                "Accept": "application/json",
            }
        )

    #http
    def _get(self, url: str, params: dict) -> dict:
        """Make a request, retrying and waiting longer after each failure."""
        last_error: Exception | None = None
        for attempt in range(Config.HTTP_RETRIES):
            try:
                response = self._session.get(
                    url, params=params, timeout=Config.HTTP_TIMEOUT
                )
                if response.status_code == 404:
                    raise SymbolNotFound(f"not found upstream: {params}")
                response.raise_for_status()
                return response.json()
            except SymbolNotFound:
                raise
            except Exception as exc:  #network error, bad json or a server error
                last_error = exc
                if attempt < Config.HTTP_RETRIES - 1:
                    time.sleep(0.4 * (2**attempt))
        raise MarketDataError(f"request failed after retries: {last_error}") from last_error

    #interface
    def supports(self, symbol: str) -> bool:
        """Yahoo covers shares, funds, indices and crypto as BTC-USD."""
        return bool(symbol and symbol.strip())

    def get_quote(self, symbol: str) -> Quote:
        symbol = symbol.upper().strip()
        data = self._get(
            CHART_URL.format(symbol=symbol), {"range": "1d", "interval": "1m"}
        )
        result = self._first_result(data, symbol)
        meta = result.get("meta", {})

        price = meta.get("regularMarketPrice")
        if price is None:
            #fall back to the last close in the series that is not empty
            closes = self._closes(result)
            if not closes:
                raise SymbolNotFound(f"no price available for {symbol}")
            price = closes[-1]

        return Quote(
            symbol=symbol,
            price=float(price),
            previous_close=self._float_or_none(
                meta.get("chartPreviousClose") or meta.get("previousClose")
            ),
            currency=meta.get("currency") or "USD",
            market_state=self._market_state(meta),
            timestamp=int(meta.get("regularMarketTime") or time.time()),
            source=self.name,
        )

    @staticmethod
    def _market_state(meta: dict) -> str:
        """Work out whether the market is open.

        the chart endpoint does not return yahoo's marketState field, so this
        is taken from currentTradingPeriod, which gives the start and end of
        the current session.
        """
        period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
        start, end = period.get("start"), period.get("end")
        if not start or not end:
            return "UNKNOWN"
        now = time.time()
        if start <= now <= end:
            return "REGULAR"
        return "PRE" if now < start else "CLOSED"

    def get_candles(
        self,
        symbol: str,
        interval: str = "5m",
        start: int | None = None,
        end: int | None = None,
        lookback_range: str | None = None,
    ) -> list[Candle]:
        symbol = symbol.upper().strip()
        interval = interval if interval in VALID_INTERVALS else "5m"

        params: dict = {"interval": interval, "includePrePost": "false"}
        if lookback_range and start is None:
            params["range"] = lookback_range
        else:
            now = int(time.time())
            end = end or now
            if start is None:
                start = end - 86400
            #clamp the window to what this interval actually holds
            max_seconds = INTERVAL_MAX_DAYS[interval] * 86400
            if end - start > max_seconds:
                start = end - max_seconds
                log.debug("clamped %s %s window to %d days", symbol, interval,
                          INTERVAL_MAX_DAYS[interval])
            params["period1"] = int(start)
            params["period2"] = int(end)

        data = self._get(CHART_URL.format(symbol=symbol), params)
        result = self._first_result(data, symbol)
        return self._to_candles(result)

    def search(self, query: str, limit: int = 8) -> list[SearchResult]:
        query = (query or "").strip()
        if not query:
            return []
        data = self._get(
            SEARCH_URL,
            {
                "q": query,
                "quotesCount": limit,
                "newsCount": 0,
                "listsCount": 0,
                "enableFuzzyQuery": "false",
            },
        )
        scored: list[tuple[float, SearchResult]] = []
        for quote in data.get("quotes", []):
            symbol = quote.get("symbol")
            if not symbol or not quote.get("isYahooFinance", True):
                continue
            name = (
                quote.get("shortname")
                or quote.get("longname")
                or quote.get("name")
                or symbol
            )
            scored.append(
                (
                    self._rank(query, symbol, name, quote),
                    SearchResult(
                        symbol=symbol,
                        name=self._clean_name(name),
                        exchange=quote.get("exchDisp") or quote.get("exchange") or "",
                        asset_type=(quote.get("quoteType") or "EQUITY").upper(),
                    ),
                )
            )

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [result for _, result in scored[:limit]]

    #the main US exchanges. a search for "apple" should give AAPL on nasdaq,
    #not the same company listed again in frankfurt or sao paulo.
    PRIMARY_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "PCX", "ASE", "BTS", "CCC", "CCY"}

    @classmethod
    def _rank(cls, query: str, symbol: str, name: str, quote: dict) -> tuple:
        """Score a search result so the listing the user means comes first.

        yahoo returns a score of its own, but the size of it is wildly
        different between types of instrument: a gold futures contract scores
        3,000,600 while micron technology scores 44,090. adding or taking off a
        fixed amount cannot work against numbers like that.

        so each result is put into a band instead, and yahoo's score only
        decides the order inside a band. sorting the pair then puts the main
        listing on top whatever the upstream score was.
        """
        q = query.upper()
        sym = symbol.upper()
        upper_name = name.upper()
        quote_type = (quote.get("quoteType") or "EQUITY").upper()
        exchange = (quote.get("exchange") or "").upper()

        is_derivative = quote_type in ("FUTURE", "OPTION") or sym.endswith("=F")
        is_foreign = "." in sym
        is_receipt = any(tag in upper_name for tag in ("_DR", " DR ", "DRN", "DRC", "CEDEAR", "CDR"))
        is_primary = exchange in cls.PRIMARY_EXCHANGES

        if is_derivative:
            tier = 0  #cannot be traded here, so always last
        elif is_receipt:
            tier = 1
        elif is_foreign:
            tier = 2
        elif sym == q:
            tier = 9  #an exact ticker match wins outright
        elif is_primary and upper_name.startswith(q):
            tier = 8  #"apple" gives apple inc on nasdaq
        elif is_primary and sym.startswith(q):
            tier = 7
        elif is_primary:
            tier = 6
        elif upper_name.startswith(q):
            tier = 5
        else:
            tier = 3

        return (tier, float(quote.get("score") or 0))

    @staticmethod
    def _clean_name(name: str) -> str:
        """Tidy up the spacing yahoo leaves in some listing names."""
        return " ".join(name.split()).rstrip(" R").strip()

    #parsing helpers
    @staticmethod
    def _first_result(data: dict, symbol: str) -> dict:
        chart = data.get("chart") or {}
        if chart.get("error"):
            raise SymbolNotFound(f"{symbol}: {chart['error'].get('description', 'unknown')}")
        results = chart.get("result") or []
        if not results:
            raise SymbolNotFound(f"no data returned for {symbol}")
        return results[0]

    @staticmethod
    def _float_or_none(value) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _closes(result: dict) -> list[float]:
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        return [c for c in (quote.get("close") or []) if c is not None]

    @staticmethod
    def _to_candles(result: dict) -> list[Candle]:
        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        candles: list[Candle] = []
        for i, ts in enumerate(timestamps):
            #yahoo fills gaps such as holidays and halts with empty values,
            #so skip those bars rather than let them reach the chart.
            close = closes[i] if i < len(closes) else None
            if close is None:
                continue
            candles.append(
                Candle(
                    ts=int(ts),
                    open=float(opens[i] if i < len(opens) and opens[i] is not None else close),
                    high=float(highs[i] if i < len(highs) and highs[i] is not None else close),
                    low=float(lows[i] if i < len(lows) and lows[i] is not None else close),
                    close=float(close),
                    volume=float(volumes[i] if i < len(volumes) and volumes[i] is not None else 0),
                )
            )
        return candles
