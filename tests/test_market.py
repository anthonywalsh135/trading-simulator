#tests for the market data layer.
#
#these check the shape of the request that goes out rather than what comes
#back, so the http call is replaced and nothing touches the network.

from __future__ import annotations

import time

import pytest

from website.market.binance import MAX_LIMIT, BinanceProvider
from website.market.cache import TTLCache


@pytest.fixture
def binance(monkeypatch):
    """A binance provider that writes down what it was asked for.

    it returns a single bar, so the window it asked for can be checked.
    """
    provider = BinanceProvider()
    provider.sent = []

    def fake_get(path, params):
        provider.sent.append(params)
        return [[int(time.time() * 1000), "1", "2", "0.5", "1.5", "10"]]

    monkeypatch.setattr(provider, "_get", fake_get)
    return provider


#binance windowing
def test_binance_honours_the_requested_range(binance):
    """The range button has to change the period on screen.

    with the range ignored, the last 1000 bars always come back, so the buttons
    change the size of each bar but never the period being shown: one day of 5
    minute candles draws three and a half days.
    """
    binance.get_candles("BTC-USD", interval="1d", lookback_range="1y")
    params = binance.sent[-1]
    span_days = (time.time() - params["startTime"] / 1000) / 86400
    assert 360 <= span_days <= 372


def test_binance_window_is_anchored_to_its_end(binance):
    """A window is anchored to its end, not its start.

    klines returns bars forwards from startTime, so a window holding more than
    MAX_LIMIT bars comes back as its oldest thousand with the recent end quietly
    missing.
    """
    binance.get_candles("BTC-USD", interval="1m", lookback_range="1y")
    params = binance.sent[-1]
    bars_requested = (time.time() - params["startTime"] / 1000) / 60
    assert bars_requested <= MAX_LIMIT + 1


def test_binance_clamps_an_explicit_window_to_the_bar_limit(binance):
    end = int(time.time())
    start = end - 365 * 86400
    binance.get_candles("BTC-USD", interval="5m", start=start, end=end)
    params = binance.sent[-1]
    assert params["endTime"] == end * 1000
    assert params["startTime"] >= (end - MAX_LIMIT * 300) * 1000


def test_binance_leaves_an_unbounded_request_alone(binance):
    binance.get_candles("BTC-USD", interval="5m")
    assert "startTime" not in binance.sent[-1]


#cache
def test_expired_entries_are_still_available_as_a_stale_fallback():
    """An expired price is still available as a fallback."""
    cache = TTLCache(0)
    cache.set("k", 42)
    assert cache.get("k") is None
    assert cache.get_stale("k") == 42


def test_the_cache_is_bounded():
    """The cache has to be bounded.

    entries are kept after they expire on purpose, so without a limit the store
    of closing prices, which holds one key per symbol per date, grows for as
    long as the program runs.
    """
    cache = TTLCache(60, max_entries=10)
    for i in range(50):
        cache.set(f"k{i}", i)
    assert len(cache.keys()) == 10
    assert cache.get_stale("k49") == 49, "the newest entry survives"
    assert cache.get_stale("k0") is None, "the oldest is evicted"


#interval promotion
@pytest.mark.parametrize(
    "interval,age_days,expected",
    [
        ("5m", 3, "5m"),  #inside the intraday window
        ("5m", 400, "60m"),  #too old for five-minute bars
        ("5m", 4000, "1d"),  #too old for anything intraday
        ("1m", 30, "5m"),
        ("1d", 4000, "1d"),  #daily history has no practical limit
    ],
)
def test_interval_is_promoted_to_one_that_reaches_back_far_enough(interval, age_days, expected):
    """An interval that cannot reach far enough back is moved up.

    yahoo throws intraday bars away after a few weeks, so a request for 5
    minute candles from 2016 comes back empty rather than as an error.
    """
    from website.market.service import MarketService

    assert MarketService._interval_reaching(interval, age_days) == expected
