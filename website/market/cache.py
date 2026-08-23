#caching for market data. this is what makes updating the chart every second
#affordable. every caller shares one cached price with a short life, so the
#rate the browser polls at and the number of upstream requests are separate
#numbers: a hundred open tabs still cause at most one request per second.
#
#there are two tiers. TTLCache is in memory and short lived, for live prices
#and searches. CandleStore is kept in sqlite, for the price bars, which cannot
#change once their period has closed and so survive a restart.

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Iterable

from .provider import Candle

log = logging.getLogger(__name__)


class TTLCache:
    """A small thread safe cache whose entries expire after ttl seconds.

    expired entries are kept rather than deleted, so that they can be served
    as a fallback when upstream is down. max_entries is what stops the store
    growing for as long as the program runs.
    """

    def __init__(self, ttl: float, max_entries: int = 5000) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                #keep the value rather than deleting it. a failed request is
                #better answered with an old price and a warning than nothing.
                return None
            return value

    def get_stale(self, key: str) -> Any | None:
        """Return a value even if it has expired."""
        with self._lock:
            entry = self._data.get(key)
            return entry[1] if entry else None

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)  #drop the oldest entry

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._data)


class CandleStore:
    """Price bars kept in the candle_cache table."""

    def __init__(self, db) -> None:
        self.db = db

    def save(self, symbol: str, interval: str, candles: Iterable[Candle]) -> None:
        rows = [
            (symbol.upper(), interval, c.ts, c.open, c.high, c.low, c.close, c.volume)
            for c in candles
        ]
        if not rows:
            return
        try:
            self.db.executemany(
                """
                INSERT INTO candle_cache (symbol, interval, ts, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, interval, ts) DO UPDATE SET
                    open = excluded.open, high = excluded.high,
                    low  = excluded.low,  close = excluded.close,
                    volume = excluded.volume
                """,
                rows,
            )
        except Exception as exc:  #a cache write must never break a request
            log.warning("candle cache write failed for %s %s: %s", symbol, interval, exc)

    def load(self, symbol: str, interval: str, start: int, end: int) -> list[Candle]:
        try:
            rows = self.db.fetchall(
                """
                SELECT ts, open, high, low, close, volume FROM candle_cache
                WHERE symbol = ? AND interval = ? AND ts BETWEEN ? AND ?
                ORDER BY ts
                """,
                (symbol.upper(), interval, int(start), int(end)),
            )
        except Exception as exc:
            log.warning("candle cache read failed: %s", exc)
            return []
        return [
            Candle(ts=r["ts"], open=r["open"], high=r["high"], low=r["low"],
                   close=r["close"], volume=r["volume"])
            for r in rows
        ]

    def coverage(self, symbol: str, interval: str) -> tuple[int, int] | None:
        """The earliest and latest bars held for a symbol, if any."""
        row = self.db.fetchone(
            "SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM candle_cache WHERE symbol = ? AND interval = ?",
            (symbol.upper(), interval),
        )
        if not row or row["lo"] is None:
            return None
        return int(row["lo"]), int(row["hi"])


class QuoteRefresher:
    """Background thread that keeps recently viewed prices fresh.

    a symbol joins the list when someone looks at it and drops off after
    idle_timeout seconds without interest, so the thread only ever works on
    what somebody is actually watching.
    """

    def __init__(self, fetch: Callable[[str], Any], interval: float = 1.0,
                 idle_timeout: float = 120.0) -> None:
        self._fetch = fetch
        self.interval = interval
        self.idle_timeout = idle_timeout
        self._watched: dict[str, float] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def watch(self, symbol: str) -> None:
        with self._lock:
            self._watched[symbol.upper()] = time.monotonic()

    def _active(self) -> list[str]:
        now = time.monotonic()
        with self._lock:
            for sym, last in list(self._watched.items()):
                if now - last > self.idle_timeout:
                    del self._watched[sym]
            return list(self._watched)

    def _run(self) -> None:
        while not self._stop.is_set():
            for symbol in self._active():
                if self._stop.is_set():
                    break
                try:
                    self._fetch(symbol)
                except Exception as exc:
                    log.debug("background refresh failed for %s: %s", symbol, exc)
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="quote-refresher", daemon=True)
        self._thread.start()
        log.info("quote refresher started (every %.1fs)", self.interval)

    def stop(self) -> None:
        self._stop.set()
