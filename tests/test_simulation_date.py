#tests for pricing everything on the account's simulation date.
#
#an account whose simulation date is in the past has to see the market as it
#was on that date: in the price a trade fills at, in what its holdings are
#worth, and in the figures sent back to the browser. only the first of those
#was ever true, which made the simulation contradict itself in the most
#visible way possible. buying at a price from 2016 and then valuing the
#holding at today's price reports a profit of several thousand per cent on a
#trade that has only just been made and has not moved at all.

from __future__ import annotations

from datetime import date, timedelta

import pytest

from website.models import Portfolio, User

PAST = "2016-08-23"


@pytest.fixture
def past_user(db, user):
    """The usual test user, trading ten years ago."""
    User.update_sim_date(db, user.id, PAST)
    user.sim_date = PAST
    return user


@pytest.fixture
def two_priced(market):
    """AAPL at 25 on the simulation date and 300 live."""
    market.fake.set_price("AAPL", 300.0)
    market.fake.set_price_on("AAPL", PAST, 25.0)
    return market


#valuation
def test_holdings_are_valued_on_the_simulation_date_not_at_the_live_price(
    engine, past_user, two_priced
):
    """Holdings are valued on the simulation date, not at the live price.

    valuing a holding bought at a price from 2016 at today's price shows the
    ten years of movement the account has not lived through yet as profit.
    """
    engine.execute(past_user, "AAPL", "buy", 10)  #10 @ $25 = $250

    valuation = engine.portfolio_valuation(past_user)
    position = valuation["positions"][0]

    assert position["current_price"] == 25.0, "priced on the simulation date"
    assert position["value"] == 250.0
    assert valuation["as_of"] == PAST
    assert valuation["live"] is False


def test_buying_shows_no_profit_until_time_moves(engine, past_user, two_priced, db):
    """Buying shows no profit until time actually moves.

    nothing has happened between the purchase and the valuation, so the profit
    has to be zero and the net worth unchanged.
    """
    before = engine.portfolio_valuation(past_user)["net_worth"]
    engine.execute(past_user, "AAPL", "buy", 10)
    after = engine.portfolio_valuation(past_user)

    assert after["positions"][0]["pnl"] == 0.0
    assert after["total_pnl"] == 0.0
    assert after["net_worth"] == before


def test_net_worth_follows_the_simulation_date(engine, past_user, two_priced, db):
    """Moving the date has to revalue the portfolio, in both directions.

    this is what the whole change exists for: the net worth reported is worked
    out from the date being traded on, not fixed once and left.
    """
    engine.execute(past_user, "AAPL", "buy", 10)  #$250 spent at the 2016 price
    assert engine.portfolio_valuation(past_user)["total_value"] == 250.0

    today = date.today().isoformat()
    User.update_sim_date(db, past_user.id, today)
    past_user.sim_date = today
    assert engine.portfolio_valuation(past_user)["total_value"] == 3000.0  #10 @ $300

    User.update_sim_date(db, past_user.id, PAST)
    past_user.sim_date = PAST
    assert engine.portfolio_valuation(past_user)["total_value"] == 250.0


def test_a_current_simulation_date_still_uses_the_live_price(engine, user, market):
    """An account trading in the present is unaffected by any of this."""
    market.fake.set_price("AAPL", 100.0)
    engine.execute(user, "AAPL", "buy", 10)
    market.fake.set_price("AAPL", 150.0)

    valuation = engine.portfolio_valuation(user)
    assert valuation["positions"][0]["current_price"] == 150.0
    assert valuation["live"] is True


def test_valuation_can_be_asked_for_an_explicit_date(engine, past_user, two_priced):
    """The leaderboard values other traders on their own dates, not the caller's."""
    engine.execute(past_user, "AAPL", "buy", 4)
    today = date.today().isoformat()
    assert engine.portfolio_valuation(past_user, as_of=today)["total_value"] == 1200.0


#trading at the right price
def test_trades_execute_at_the_simulation_date_price(engine, past_user, two_priced, db):
    engine.execute(past_user, "AAPL", "buy", 10)
    row = db.fetchone("SELECT price FROM transactions WHERE user_id = ?", (past_user.id,))
    assert row["price"] == 25.0


def test_max_affordable_uses_the_execution_price(engine, past_user, two_priced, db):
    """The percentage buttons size a trade from the price it will fill at.

    with 100,000 and a simulated price of 25 the balance covers 4,000 shares.
    sizing from the live 300 offers 333, which is twelve times too few.
    """
    assert engine.max_affordable(past_user, "AAPL") == 4000.0
    assert engine.execution_price("AAPL", past_user) == 25.0


def test_max_affordable_reads_the_balance_from_the_database(engine, past_user, two_priced, db):
    """The balance is read from the database, since the object goes stale."""
    User.update_balance(db, past_user.id, 500.0)
    past_user.balance = 100_000.0  #stale in-memory value
    assert engine.max_affordable(past_user, "AAPL") == 20.0


#undo restores the cost basis
def test_undoing_a_sale_restores_the_average_cost(engine, past_user, two_priced, db):
    """Undoing a sale puts the average cost back.

    carrying out the opposite trade works the average out again rather than
    restoring it, so buying at 25 and selling once the price reached 300 leaves
    the holding recorded at a 162.50 average it was never bought at, and every
    profit figure afterwards is measured against that.
    """
    engine.execute(past_user, "AAPL", "buy", 10)  #10 @ $25
    today = date.today().isoformat()
    User.update_sim_date(db, past_user.id, today)
    past_user.sim_date = today
    engine.execute(past_user, "AAPL", "sell", 5)  #5 @ $300

    engine.undo_last(past_user)

    holding = Portfolio.get(db, past_user.id, "AAPL")
    assert holding["shares"] == 10
    assert holding["avg_cost"] == pytest.approx(25.0), "the cost basis must be restored"


def test_undoing_a_purchase_restores_the_previous_average_cost(engine, past_user, two_priced, db):
    engine.execute(past_user, "AAPL", "buy", 10)  #10 @ $25
    today = date.today().isoformat()
    User.update_sim_date(db, past_user.id, today)
    past_user.sim_date = today
    engine.execute(past_user, "AAPL", "buy", 10)  #10 @ $300

    holding = Portfolio.get(db, past_user.id, "AAPL")
    assert holding["avg_cost"] == pytest.approx(162.5), "blended while both lots are held"

    engine.undo_last(past_user)

    holding = Portfolio.get(db, past_user.id, "AAPL")
    assert holding["shares"] == 10
    assert holding["avg_cost"] == pytest.approx(25.0)


def test_undo_round_trip_returns_the_account_to_its_starting_state(engine, user, db, market):
    market.fake.set_price("AAPL", 100.0)
    start = db.fetchone("SELECT balance FROM users WHERE id = ?", (user.id,))["balance"]

    engine.execute(user, "AAPL", "buy", 10)
    engine.execute(user, "AAPL", "sell", 4)
    engine.undo_last(user)
    engine.undo_last(user)

    assert db.fetchone("SELECT balance FROM users WHERE id = ?", (user.id,))["balance"] == start
    assert Portfolio.get(db, user.id, "AAPL") is None


def test_an_unreversible_undo_is_discarded_rather_than_blocking_the_stack(engine, user, db, market):
    """An entry that cannot be applied is dropped rather than left in the way.

    removing it inside the transaction that then fails means the removal is
    undone with everything else, so the same dead entry sits at the top of the
    stack and blocks every older one behind it.
    """
    from website.models import UndoStack

    market.fake.set_price("AAPL", 100.0)
    market.fake.set_price("BTC-USD", 50_000.0)
    engine.execute(user, "AAPL", "buy", 10)
    engine.execute(user, "BTC-USD", "buy", 1)

    #the shares vanish by a route that records no undo entry of its own,
    #which is exactly what the trading bot does.
    db.execute("DELETE FROM portfolios WHERE user_id = ? AND symbol = 'BTC-USD'", (user.id,))

    from website.trading import TradeError

    with pytest.raises(TradeError, match="no longer be undone"):
        engine.undo_last(user)

    assert UndoStack.depth(db, user.id) == 1, "the dead entry must be gone"
    engine.undo_last(user)  #the older entry is reachable again
    assert Portfolio.get(db, user.id, "AAPL") is None


def test_undo_is_filed_against_the_original_simulation_date(engine, past_user, two_priced, db):
    engine.execute(past_user, "AAPL", "buy", 3)
    today = date.today().isoformat()
    User.update_sim_date(db, past_user.id, today)
    past_user.sim_date = today

    engine.undo_last(past_user)

    row = db.fetchone(
        "SELECT sim_date FROM transactions WHERE user_id = ? AND source = 'undo'",
        (past_user.id,),
    )
    assert row["sim_date"] == PAST


#chart data
def test_chart_bars_end_on_the_simulation_date(market):
    """The chart ends on the simulation date.

    the figures underneath the chart are worked out from these bars, so loading
    today's session makes them describe a session ten years after the one being
    traded.
    """
    from datetime import datetime, timezone

    bars, _ = market.candles_as_of("AAPL", interval="1d", lookback_range="1mo", as_of=PAST)
    assert bars
    last = datetime.fromtimestamp(bars[-1].ts, timezone.utc).date().isoformat()
    assert last <= PAST


def test_an_interval_that_cannot_reach_back_is_promoted(market):
    """An interval that cannot reach back that far is moved up.

    5 minute bars from 2016 do not exist upstream, so asking for them returns
    an empty chart unless daily bars are used instead.
    """
    _, interval = market.candles_as_of("AAPL", interval="5m", lookback_range="1d", as_of=PAST)
    assert interval == "1d"


def test_a_recent_date_keeps_the_requested_interval(market):
    recent = (date.today() - timedelta(days=2)).isoformat()
    _, interval = market.candles_as_of("AAPL", interval="5m", lookback_range="1d", as_of=recent)
    assert interval == "5m"


#the bot
def test_the_bot_prices_decisions_on_the_simulation_date(db, two_priced, past_user):
    """The bot prices its decisions on the simulation date.

    reading the live price puts bot trades filled today in the same portfolio
    as trades made by hand at prices from years ago.
    """
    from website.bot import BotManager
    from website.models import BotConfig
    from website.trading import TradeEngine

    manager = BotManager(db=db)
    manager._engine = TradeEngine(db=db, market=two_priced)

    BotConfig.save(db, past_user.id, enabled=1, symbol="AAPL",
                   threshold_buy=30.0, threshold_sell=1000.0, quantity=1,
                   cooldown_seconds=0)
    config = BotConfig.get(db, past_user.id)

    #$25 on the simulation date is below the $30 buy threshold; the live $300
    #is not. A decision to buy proves the simulated price was the one used.
    decision = manager.evaluate(past_user, config)
    assert decision is not None
    assert decision["action"] == "buy"
    assert decision["price"] == 25.0
