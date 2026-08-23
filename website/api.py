#the routes that return json rather than a page. these are what let the
#browser update part of the screen without reloading all of it: the chart
#redraws itself, the search box suggests companies as you type, and the fast
#forward streams the bars it animates through.
#
#every route needs a login. anything that changes something also needs a
#csrf token, which the browser sends in the X-CSRFToken header.

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import HTTPException

from .assets import asset_for
from .config import Config
from .market import MarketDataError, SymbolNotFound, get_market
from .models import BotEvent, UndoStack, User, db
from .performance import PerformanceHistory
from .prediction import predict
from .trading import TradeEngine, TradeError

log = logging.getLogger(__name__)

api = Blueprint("api", __name__, url_prefix="/api")


def _engine() -> TradeEngine:
    return TradeEngine(db=db, market=get_market(db))


def _error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


@api.errorhandler(CSRFError)
def _csrf_failed(exc):
    """A missing or expired csrf token is the browser at fault, not the server."""
    return jsonify({
        "ok": False,
        "error": "Your session has expired. Please refresh the page and try again.",
    }), 400


@api.errorhandler(HTTPException)
def _http_error(exc):
    """Keep the real status code rather than turning everything into a 500."""
    return jsonify({"ok": False, "error": exc.description}), exc.code


@api.errorhandler(Exception)
def _unhandled(exc):
    """Answer with json rather than flask's html error page."""
    log.exception("unhandled API error")
    return jsonify({"ok": False, "error": "Something went wrong. Please try again."}), 500


#search
@api.get("/search")
@login_required
def search():
    """Suggest companies and tickers as the user types.

    typing "apple" returns Apple Inc. as AAPL, so a mistyped symbol is caught
    before the form is sent rather than after.
    """
    query = (request.args.get("q") or "").strip()
    kind = request.args.get("kind", "all")
    limit = min(int(request.args.get("limit", 8) or 8), 20)

    if not query:
        return jsonify({"ok": True, "results": []})

    try:
        if kind in ("stocks", "crypto"):
            results = asset_for(kind).search(query, limit=limit)
        else:
            results = get_market(db).search(query, limit=limit)
    except MarketDataError as exc:
        log.warning("search failed for %r: %s", query, exc)
        return jsonify({"ok": True, "results": [], "degraded": True})

    return jsonify({"ok": True, "results": [r.to_dict() for r in results]})


#prices
@api.get("/quote/<symbol>")
@login_required
def quote(symbol: str):
    """The price of one symbol, answered from the shared cache.

    two prices come back, because on a past simulation date they are not the
    same number. quote is what the asset is worth on the real market right now.
    execution is what this account would actually pay or receive, which is the
    closing price on its simulation date.

    the trade panel needs the second one. sizing a trade from the live price
    while the engine charges the historical one means the percentage buttons
    ask for the wrong number of shares and the estimated cost is wrong too.
    """
    market = get_market(db)
    try:
        q = market.get_quote(symbol)
    except SymbolNotFound:
        return _error(f"Unknown symbol: {symbol}", 404)
    except MarketDataError as exc:
        return _error(str(exc), 503)

    as_of = current_user.sim_date or date.today().isoformat()
    live = as_of >= date.today().isoformat()
    execution = q.price if live else market.price_on_date(symbol, as_of)

    return jsonify({
        "ok": True,
        "quote": q.to_dict(),
        "execution": {"price": execution, "as_of": as_of, "live": live},
    })


@api.get("/candles/<symbol>")
@login_required
def candles(symbol: str):
    """The bars for the chart, ending on the account's simulation date.

    loading today's session whatever date is being traded puts a chart from
    this year above a price from years ago, and the figures underneath describe
    the wrong session entirely.

    intraday bars do not reach back forever, so a request for 5 minute bars
    from 2016 comes back as daily bars with interval_adjusted set, rather than
    as an empty chart.
    """
    requested = request.args.get("interval", "5m")
    lookback = request.args.get("range", "1d")
    as_of = current_user.sim_date or date.today().isoformat()

    try:
        bars, interval = get_market(db).candles_as_of(
            symbol, interval=requested, lookback_range=lookback, as_of=as_of
        )
    except MarketDataError as exc:
        return _error(str(exc), 503)

    if not bars:
        return _error(
            f"No chart data available for {symbol} on {as_of}. "
            "Try a wider range, or a date when the market was open.",
            404,
        )

    asset = asset_for(request.args.get("kind", "stocks"))
    payload = {
        "ok": True,
        "symbol": symbol.upper(),
        "interval": interval,
        "interval_adjusted": interval != requested,
        "requested_interval": requested,
        "as_of": as_of,
        "live": as_of >= date.today().isoformat(),
        "candles": [c.to_dict() for c in bars],
        "stats": asset.statistics(bars),
    }

    if request.args.get("predict") == "1":
        forecast = predict(bars)
        payload["prediction"] = forecast.to_dict() if forecast else None

    return jsonify(payload)


#the portfolio
@api.get("/portfolio")
@login_required
def portfolio():
    valuation = _engine().portfolio_valuation(current_user)
    valuation["ok"] = True
    valuation["sim_date"] = current_user.sim_date
    valuation["undo_depth"] = UndoStack.depth(db, current_user.id)
    return jsonify(valuation)


@api.get("/performance")
@login_required
def performance():
    """The cash, the holdings and the net worth for every day since the first trade."""
    history = PerformanceHistory(db, get_market(db)).build(current_user)
    history["ok"] = True
    return jsonify(history)


@api.get("/pricing/<symbol>")
@login_required
def pricing(symbol: str):
    """What this account would pay for a symbol, and how much it can afford."""
    engine = _engine()
    as_of = current_user.sim_date or date.today().isoformat()
    return jsonify({
        "ok": True,
        "symbol": symbol.upper(),
        "price": engine.execution_price(symbol, current_user),
        "as_of": as_of,
        "live": as_of >= date.today().isoformat(),
        "max_shares": engine.max_affordable(current_user, symbol),
    })


#trading
@api.post("/trade")
@login_required
def trade():
    """Carry out a buy or a sell and return the new state of the account."""
    data = request.get_json(silent=True) or {}
    try:
        result = _engine().execute(
            current_user,
            data.get("symbol"),
            data.get("action"),
            data.get("shares"),
        )
    except TradeError as exc:
        return _error(str(exc))

    return jsonify({"ok": True, "trade": result.to_dict(), **_account_state()})


@api.post("/undo")
@login_required
def undo():
    try:
        result = _engine().undo_last(current_user)
    except TradeError as exc:
        return _error(str(exc))

    return jsonify({"ok": True, "trade": result.to_dict(), **_account_state()})


def _account_state() -> dict:
    """The account summary that every route which changes something returns.

    one helper, because the fields have to be the same everywhere. a route that
    leaves one out makes the browser read undefined and change the wrong part
    of the page.
    """
    valuation = _engine().portfolio_valuation(current_user)
    return {
        "balance": valuation["balance"],
        "net_worth": valuation["net_worth"],
        "total_value": valuation["total_value"],
        "positions": valuation["positions"],
        "as_of": valuation["as_of"],
        "live": valuation["live"],
        "undo_depth": UndoStack.depth(db, current_user.id),
    }


@api.get("/max-affordable/<symbol>")
@login_required
def max_affordable(symbol: str):
    return jsonify({"ok": True, "shares": _engine().max_affordable(current_user, symbol)})


#moving through time
@api.get("/replay/<symbol>")
@login_required
def replay(symbol: str):
    """The bars between the simulation date and now, for the fast forward."""
    from_date = request.args.get("from") or current_user.sim_date
    to_date = request.args.get("to") or date.today().isoformat()

    try:
        _validate_date(from_date)
        _validate_date(to_date)
    except ValueError as exc:
        return _error(str(exc))

    if from_date >= to_date:
        return jsonify({"ok": True, "candles": [], "already_current": True})

    try:
        bars = get_market(db).replay_candles(symbol, from_date, to_date)
    except MarketDataError as exc:
        return _error(str(exc), 503)

    return jsonify({
        "ok": True,
        "symbol": symbol.upper(),
        "from": from_date,
        "to": to_date,
        "candles": [c.to_dict() for c in bars],
    })


@api.post("/fast-forward")
@login_required
def fast_forward():
    """Move the simulation date forward once a replay has finished.

    the bot is run across the interval being skipped first, so moving forward
    produces the trades it would have made rather than losing them.
    """
    data = request.get_json(silent=True) or {}
    target = data.get("to") or date.today().isoformat()

    try:
        _validate_date(target)
    except ValueError as exc:
        return _error(str(exc))

    previous = current_user.sim_date
    if target < previous:
        return _error("Time only moves forward in the simulation.")

    from .bot import BotManager

    bot_summary = BotManager.instance().run_over_period(current_user, previous, target)

    User.update_sim_date(db, current_user.id, target)
    current_user.sim_date = target

    return jsonify({
        "ok": True,
        "from": previous,
        "to": target,
        "days_advanced": (datetime.fromisoformat(target) - datetime.fromisoformat(previous)).days,
        "bot": bot_summary,
        **_account_state(),
    })


def _validate_date(value: str) -> None:
    """Check a date is one the simulation can be moved to."""
    try:
        parsed = datetime.fromisoformat(value).date()
    except (TypeError, ValueError):
        raise ValueError("Enter a date as YYYY-MM-DD.") from None
    if parsed > date.today():
        raise ValueError("You cannot move to a date in the future.")
    if parsed < date.fromisoformat(Config.EARLIEST_SIM_DATE):
        raise ValueError(f"The simulation starts from {Config.EARLIEST_SIM_DATE}.")


#the trading bot
@api.get("/bot/events")
@login_required
def bot_events():
    after = int(request.args.get("after", 0) or 0)
    rows = BotEvent.recent(db, current_user.id, limit=50, after_id=after)
    return jsonify({
        "ok": True,
        "events": [
            {
                "id": r["id"], "level": r["level"], "message": r["message"],
                "symbol": r["symbol"], "price": r["price"], "created_at": r["created_at"],
            }
            for r in rows
        ],
    })


@api.get("/bot/status")
@login_required
def bot_status():
    from .bot import BotManager

    return jsonify({"ok": True, **BotManager.instance().status(current_user.id)})
