#cryptocurrency prices from binance. no api key is needed, the price is real
#time and the bar history goes back years.
#
#symbols are given to this class in the site's BTC-USD form and turned into
#binance's BTCUSDT pairs, because binance prices against the usdt stablecoin
#rather than against dollars directly.

from __future__ import annotations

import logging
import time

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

BASE = "https://api.binance.com/api/v3"

#binance refuses a request for more than 1000 bars
MAX_LIMIT = 1000

#days covered by each chart range, so a range can be turned into the startTime
#that the klines endpoint actually understands
RANGE_DAYS = {
    "1d": 1, "5d": 5, "1mo": 31, "3mo": 92,
    "6mo": 183, "1y": 366, "2y": 731, "5y": 1826,
}

#seconds per bar, used to keep a window inside MAX_LIMIT bars
INTERVAL_SECONDS = {
    "1m": 60, "2m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "60m": 3600, "1h": 3600, "1d": 86400, "1wk": 604800, "1mo": 2592000,
}

#binance names its intervals slightly differently to the rest of the site
INTERVAL_MAP = {
    "1m": "1m", "2m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "60m": "1h", "1h": "1h", "1d": "1d", "1wk": "1w", "1mo": "1M",
}

#common coins offered as suggestions, so the search has something to show
#before the user has typed enough for a lookup to mean anything
KNOWN_COINS = [
    ("BTC-USD", "Bitcoin"), ("ETH-USD", "Ethereum"), ("SOL-USD", "Solana"),
    ("XRP-USD", "XRP"), ("ADA-USD", "Cardano"), ("DOGE-USD", "Dogecoin"),
    ("AVAX-USD", "Avalanche"), ("DOT-USD", "Polkadot"), ("LINK-USD", "Chainlink"),
    ("MATIC-USD", "Polygon"), ("LTC-USD", "Litecoin"), ("BCH-USD", "Bitcoin Cash"),
]


def to_binance_pair(symbol: str) -> str:
    """Turn BTC-USD into BTCUSDT. Symbols already paired are left alone."""
    s = symbol.upper().strip()
    if "-" in s:
        base, _, quote = s.partition("-")
        if quote in ("USD", "USDT"):
            return f"{base}USDT"
        return f"{base}{quote}"
    return s if s.endswith(("USDT", "BUSD", "USDC")) else f"{s}USDT"


class BinanceProvider(MarketDataProvider):
    name = "binance"
    supports_search = True

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": Config.HTTP_USER_AGENT})

    def _get(self, path: str, params: dict):
        last_error: Exception | None = None
        for attempt in range(Config.HTTP_RETRIES):
            try:
                response = self._session.get(
                    f"{BASE}{path}", params=params, timeout=Config.HTTP_TIMEOUT
                )
                if response.status_code == 400:
                    raise SymbolNotFound(f"unknown Binance symbol: {params}")
                response.raise_for_status()
                return response.json()
            except SymbolNotFound:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < Config.HTTP_RETRIES - 1:
                    time.sleep(0.4 * (2**attempt))
        raise MarketDataError(f"binance request failed: {last_error}") from last_error

    def supports(self, symbol: str) -> bool:
        s = symbol.upper().strip()
        return s.endswith(("-USD", "USDT")) or s in {c for c, _ in KNOWN_COINS}

    def get_quote(self, symbol: str) -> Quote:
        pair = to_binance_pair(symbol)
        data = self._get("/ticker/24hr", {"symbol": pair})
        price = float(data["lastPrice"])
        prev = float(data.get("prevClosePrice") or 0) or None
        return Quote(
            symbol=symbol.upper(),
            price=price,
            previous_close=prev,
            currency="USD",
            market_state="REGULAR",  #crypto never closes
            timestamp=int(time.time()),
            source=self.name,
        )

    def get_candles(
        self,
        symbol: str,
        interval: str = "5m",
        start: int | None = None,
        end: int | None = None,
        lookback_range: str | None = None,
    ) -> list[Candle]:
        pair = to_binance_pair(symbol)
        params: dict = {"symbol": pair, "interval": INTERVAL_MAP.get(interval, "5m"), "limit": MAX_LIMIT}

        #klines returns bars forwards from startTime, so a window holding more
        #than MAX_LIMIT bars comes back as its oldest thousand with the recent
        #end missing. so fix every window to its end and only widen it
        #backwards as far as the limit allows.
        window = MAX_LIMIT * INTERVAL_SECONDS.get(interval, 300)
        end_s = int(end) if end is not None else int(time.time())

        if start is not None:
            start_s = max(int(start), end_s - window)
        elif lookback_range in RANGE_DAYS:
            #turn the range into a start time. without this the range is
            #ignored and the last 1000 bars come back whatever was asked for,
            #so the chart buttons change the bar size but not the period.
            start_s = end_s - min(RANGE_DAYS[lookback_range] * 86400, window)
        else:
            start_s = None

        if start_s is not None:
            params["startTime"] = start_s * 1000
        if end is not None:
            params["endTime"] = end_s * 1000

        rows = self._get("/klines", params)
        return [
            Candle(
                ts=int(r[0] // 1000),
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5]),
            )
            for r in rows
        ]

    def search(self, query: str, limit: int = 8) -> list[SearchResult]:
        q = (query or "").strip().upper()
        if not q:
            return []
        matches = [
            SearchResult(symbol=sym, name=name, exchange="Crypto", asset_type="CRYPTOCURRENCY")
            for sym, name in KNOWN_COINS
            if q in sym or q in name.upper()
        ]
        return matches[:limit]
