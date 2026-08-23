#tests for the trading engine.
#
#the first one matters most: it checks that money is not lost on a sale,
#which is the defect that made the earlier version silently destroy the
#proceeds of every sale it made.

from __future__ import annotations

import pytest

from website.models import Portfolio, User
from website.trading import TradeError, parse_shares


#losing money on a sale
def test_buy_then_sell_restores_balance_in_the_database(engine, user, db):
    """Buy 3 shares, sell them back, and the balance must return to its start.

    updating the balance on the buy path but not the sell path means the sale
    proceeds are only ever written to an attribute on an object that is thrown
    away at the end of the request, so the money disappears.

    reading the balance back out of the database, rather than off the object,
    is what lets this test catch that at all.
    """
    starting = db.fetchone("SELECT balance FROM users WHERE id = ?", (user.id,))["balance"]
    assert starting == 100_000.0

    engine.execute(user, "AAPL", "buy", 3)
    after_buy = db.fetchone("SELECT balance FROM users WHERE id = ?", (user.id,))["balance"]
    assert after_buy == 99_700.0, "buy must deduct 3 x $100 from the stored balance"

    engine.execute(user, "AAPL", "sell", 3)
    after_sell = db.fetchone("SELECT balance FROM users WHERE id = ?", (user.id,))["balance"]
    assert after_sell == starting, "sale proceeds must be written to the database"


def test_sell_removes_holding_when_fully_liquidated(engine, user, db):
    engine.execute(user, "AAPL", "buy", 5)
    engine.execute(user, "AAPL", "sell", 5)
    assert Portfolio.get(db, user.id, "AAPL") is None


def test_partial_sell_keeps_remaining_shares(engine, user, db):
    engine.execute(user, "AAPL", "buy", 10)
    engine.execute(user, "AAPL", "sell", 4)
    holding = Portfolio.get(db, user.id, "AAPL")
    assert holding["shares"] == 6


#checking what the user typed
def test_cannot_buy_beyond_balance(engine, user):
    with pytest.raises(TradeError, match="Insufficient funds"):
        engine.execute(user, "AAPL", "buy", 100_000)


def test_cannot_sell_what_you_do_not_own(engine, user):
    with pytest.raises(TradeError, match="do not own"):
        engine.execute(user, "AAPL", "sell", 1)


def test_cannot_oversell(engine, user):
    engine.execute(user, "AAPL", "buy", 2)
    with pytest.raises(TradeError, match="only own"):
        engine.execute(user, "AAPL", "sell", 5)


@pytest.mark.parametrize("bad", ["", None, "abc", "0", "-5", "  "])
def test_invalid_quantities_raise_a_friendly_error(bad):
    """These all produce a 500 when the quantity is passed straight to int()."""
    with pytest.raises(TradeError):
        parse_shares(bad)


def test_fractional_shares_survive(engine, user, db):
    """Fractions of a coin have to survive.

    an INTEGER column and a call to int() both turn 0.5 BTC into 0, which makes
    crypto impossible to trade at all.
    """
    engine.execute(user, "BTC-USD", "buy", 0.5)
    holding = Portfolio.get(db, user.id, "BTC-USD")
    assert holding["shares"] == 0.5
    assert db.fetchone("SELECT balance FROM users WHERE id = ?", (user.id,))["balance"] == 75_000.0


def test_unknown_action_rejected(engine, user):
    with pytest.raises(TradeError):
        engine.execute(user, "AAPL", "teleport", 1)


def test_unknown_symbol_rejected(engine, user):
    with pytest.raises(TradeError):
        engine.execute(user, "NOTREAL", "buy", 1)


#cost and profit
def test_average_cost_is_weighted(engine, user, db, market):
    engine.execute(user, "AAPL", "buy", 10)  #10 @ $100
    market.fake.set_price("AAPL", 200.0)
    engine.execute(user, "AAPL", "buy", 10)  #10 @ $200
    holding = Portfolio.get(db, user.id, "AAPL")
    assert holding["shares"] == 20
    assert holding["avg_cost"] == 150.0  #(1000 + 2000) / 20


def test_valuation_reports_profit(engine, user, market):
    engine.execute(user, "AAPL", "buy", 10)  #cost $1,000
    market.fake.set_price("AAPL", 150.0)  #now worth $1,500
    valuation = engine.portfolio_valuation(user)
    position = valuation["positions"][0]
    assert position["cost"] == 1000.0
    assert position["value"] == 1500.0
    assert position["pnl"] == 500.0
    assert position["pnl_percent"] == 50.0
    assert valuation["net_worth"] == valuation["balance"] + 1500.0


#undo
def test_undo_reverses_a_buy(engine, user, db):
    """Undoing a buy gives the money back and removes the shares.

    an undo history kept as a list in the flask session and changed in place is
    never saved, so undo does nothing at all and says nothing about it.
    """
    start = db.fetchone("SELECT balance FROM users WHERE id = ?", (user.id,))["balance"]
    engine.execute(user, "AAPL", "buy", 5)
    engine.undo_last(user)
    assert db.fetchone("SELECT balance FROM users WHERE id = ?", (user.id,))["balance"] == start
    assert Portfolio.get(db, user.id, "AAPL") is None


def test_undo_uses_the_original_price_not_the_current_one(engine, user, db, market):
    """An undo reverses at the price paid, not at the price now.

    undoing at the current price makes the undo a second real trade, which can
    make or lose money in its own right.
    """
    start = db.fetchone("SELECT balance FROM users WHERE id = ?", (user.id,))["balance"]
    engine.execute(user, "AAPL", "buy", 10)  #paid $100/share
    market.fake.set_price("AAPL", 500.0)  #price then spikes
    engine.undo_last(user)
    final = db.fetchone("SELECT balance FROM users WHERE id = ?", (user.id,))["balance"]
    assert final == start, "undo must be value-neutral regardless of price moves"


def test_undo_with_empty_stack_raises(engine, user):
    with pytest.raises(TradeError, match="Nothing to undo"):
        engine.undo_last(user)


def test_undo_is_not_itself_undoable(engine, user, db):
    from website.models import UndoStack

    engine.execute(user, "AAPL", "buy", 5)
    assert UndoStack.depth(db, user.id) == 1
    engine.undo_last(user)
    assert UndoStack.depth(db, user.id) == 0


#all or nothing
def test_failed_trade_leaves_no_partial_state(engine, user, db, monkeypatch):
    """A trade that fails part way through must leave nothing behind.

    writing the balance, the ledger and the holding as three separate
    statements leaves money taken with no shares recorded when one of them
    fails.
    """
    from website import models

    def explode(*args, **kwargs):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(models.Transaction, "record", explode)

    with pytest.raises(RuntimeError):
        engine.execute(user, "AAPL", "buy", 10)

    assert db.fetchone("SELECT balance FROM users WHERE id = ?", (user.id,))["balance"] == 100_000.0
    assert Portfolio.get(db, user.id, "AAPL") is None


#the simulation date
def test_trade_is_recorded_against_the_simulation_date(engine, user, db):
    User.update_sim_date(db, user.id, "2024-03-15")
    user.sim_date = "2024-03-15"
    engine.execute(user, "AAPL", "buy", 1)
    row = db.fetchone("SELECT sim_date FROM transactions WHERE user_id = ?", (user.id,))
    assert row["sim_date"] == "2024-03-15"


def test_bot_source_is_recorded(engine, user, db):
    engine.execute(user, "AAPL", "buy", 1, source="bot")
    row = db.fetchone("SELECT source FROM transactions WHERE user_id = ?", (user.id,))
    assert row["source"] == "bot"
