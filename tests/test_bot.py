#tests for the trading bot.
#
#these pin down the things that stop it doing damage: it holds an application
#context so the database works from a thread, every user has settings of their
#own, and the cooldown and the daily limit keep it from buying again on every
#tick while the price sits below the buy threshold.

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from website.models import BotConfig, BotEvent, Portfolio, User


@pytest.fixture
def manager(db, engine):
    from website.bot import BotManager

    BotManager._instance = None
    mgr = BotManager(db=db)
    mgr._engine = engine
    return mgr


@pytest.fixture
def bot_user(db, user):
    BotConfig.save(db, user.id, enabled=1, symbol="AAPL", threshold_buy=90.0,
                   threshold_sell=110.0, quantity=5, cooldown_seconds=0,
                   max_trades_per_day=10)
    return user


def test_each_user_has_independent_configuration(db, user):
    """D34: a single module-level dict meant one user's settings overwrote
    everyone else's."""
    other = User(email="other@example.com", password="x", first_name="Other",
                 sim_date=date.today().isoformat())
    other.save(db)

    BotConfig.save(db, user.id, symbol="AAPL", threshold_buy=1.0)
    BotConfig.save(db, other.id, symbol="TSLA", threshold_buy=2.0)

    assert BotConfig.get(db, user.id)["symbol"] == "AAPL"
    assert BotConfig.get(db, other.id)["symbol"] == "TSLA"


def test_bot_buys_below_the_threshold(manager, bot_user, db, market):
    market.fake.set_price("AAPL", 85.0)  #below the 90 buy threshold
    decision = manager.evaluate(bot_user, BotConfig.get(db, bot_user.id))
    assert decision["action"] == "buy"

    assert manager.act(bot_user, BotConfig.get(db, bot_user.id), decision) is True
    holding = Portfolio.get(db, bot_user.id, "AAPL")
    assert holding["shares"] == 5


def test_bot_sells_above_the_threshold(manager, bot_user, db, market, engine):
    engine.execute(bot_user, "AAPL", "buy", 10)
    market.fake.set_price("AAPL", 120.0)  #above the 110 sell threshold
    decision = manager.evaluate(bot_user, BotConfig.get(db, bot_user.id))
    assert decision["action"] == "sell"

    manager.act(bot_user, BotConfig.get(db, bot_user.id), decision)
    assert Portfolio.get(db, bot_user.id, "AAPL")["shares"] == 5


def test_bot_holds_between_thresholds(manager, bot_user, db, market):
    market.fake.set_price("AAPL", 100.0)
    assert manager.evaluate(bot_user, BotConfig.get(db, bot_user.id)) is None


def test_bot_does_not_sell_what_it_does_not_hold(manager, bot_user, db, market):
    market.fake.set_price("AAPL", 120.0)
    assert manager.evaluate(bot_user, BotConfig.get(db, bot_user.id)) is None


def test_bot_will_not_buy_beyond_the_balance(manager, bot_user, db, market):
    BotConfig.save(db, bot_user.id, quantity=100_000)
    market.fake.set_price("AAPL", 85.0)
    assert manager.evaluate(bot_user, BotConfig.get(db, bot_user.id)) is None


def test_cooldown_blocks_rapid_repeat_trades(manager, bot_user, db, market):
    """D40: without a cooldown the bot re-bought on every 5-second tick while
    the price sat below the threshold, draining the balance in seconds."""
    BotConfig.save(db, bot_user.id, cooldown_seconds=300,
                   last_trade_at=datetime.now().isoformat(timespec="seconds"))
    market.fake.set_price("AAPL", 85.0)
    assert manager.evaluate(bot_user, BotConfig.get(db, bot_user.id)) is None

    #Once the cooldown has elapsed, trading resumes.
    BotConfig.save(db, bot_user.id,
                   last_trade_at=(datetime.now() - timedelta(seconds=600)).isoformat(timespec="seconds"))
    assert manager.evaluate(bot_user, BotConfig.get(db, bot_user.id)) is not None


def test_daily_trade_limit_is_enforced(manager, bot_user, db, market):
    BotConfig.save(db, bot_user.id, max_trades_per_day=2, cooldown_seconds=0)
    market.fake.set_price("AAPL", 85.0)

    for _ in range(2):
        decision = manager.evaluate(bot_user, BotConfig.get(db, bot_user.id))
        assert decision is not None
        manager.act(bot_user, BotConfig.get(db, bot_user.id), decision)

    assert manager.evaluate(bot_user, BotConfig.get(db, bot_user.id)) is None


def test_disabled_bot_makes_no_decisions(manager, bot_user, db, market):
    BotConfig.save(db, bot_user.id, symbol=None)
    market.fake.set_price("AAPL", 85.0)
    assert manager.evaluate(bot_user, BotConfig.get(db, bot_user.id)) is None


def test_trades_are_logged_as_events(manager, bot_user, db, market):
    """D39: the old bot gave no indication whether it had ever done anything."""
    market.fake.set_price("AAPL", 85.0)
    decision = manager.evaluate(bot_user, BotConfig.get(db, bot_user.id))
    manager.act(bot_user, BotConfig.get(db, bot_user.id), decision)

    events = BotEvent.recent(db, bot_user.id)
    assert any(e["level"] == "trade" for e in events)
    assert any("buy threshold" in e["message"] for e in events)


def test_bot_trades_are_tagged_in_the_ledger(manager, bot_user, db, market):
    market.fake.set_price("AAPL", 85.0)
    decision = manager.evaluate(bot_user, BotConfig.get(db, bot_user.id))
    manager.act(bot_user, BotConfig.get(db, bot_user.id), decision)

    row = db.fetchone("SELECT source FROM transactions WHERE user_id = ? ORDER BY id DESC",
                      (bot_user.id,))
    assert row["source"] == "bot"


def test_fast_forward_runs_the_bot_over_the_period(manager, bot_user, db, market):
    """Skipping time must not silently discard the trades the bot would make."""
    summary = manager.run_over_period(bot_user, "2024-01-01", "2024-02-01")
    assert summary["ran"] is True
    assert summary["bars"] > 0


def test_run_over_period_is_a_noop_when_disabled(manager, user, db):
    BotConfig.save(db, user.id, enabled=0)
    summary = manager.run_over_period(user, "2024-01-01", "2024-02-01")
    assert summary["ran"] is False
    assert summary["trades"] == 0
