#the routes that return a page. this file handles the request and renders
#the template, and nothing else. the classes for assets, the prediction, the
#trading and the bot all live in modules of their own, and the routes that
#return json are in api.py.
#
#route names are snake_case throughout. the old mixed spellings are kept as
#redirects, so any bookmark still works.

from __future__ import annotations

import logging
from datetime import date, datetime
from functools import wraps

from flask import (
    Blueprint, flash, jsonify, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required, logout_user

from .assets import asset_for
from .auth import OTPManager
from .market import MarketDataError, get_market
from .models import (
    BotConfig, BotEvent, Friendship, Portfolio, Transaction, UndoStack, User, db,
)
from .trading import TradeEngine, TradeError

log = logging.getLogger(__name__)

views = Blueprint("views", __name__)


#helpers
def admin_required(view):
    """Only let an administrator through to a route.

    whether somebody is an administrator is a column on their row, rather than
    a check for a particular id number.
    """

    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not getattr(current_user, "is_admin", False):
            flash("You are not authorised to view that page.", "error")
            return redirect(url_for("views.home"))
        return view(*args, **kwargs)

    return wrapped


def engine() -> TradeEngine:
    return TradeEngine(db=db, market=get_market(db))


def valuation():
    """What the logged in user is worth right now.

    one helper instead of the same block of code copied into every route that
    needs it.
    """
    return engine().portfolio_valuation(current_user)


@views.app_context_processor
def inject_globals():
    """The values every template needs, so no route has to remember them.

    a template that reads a value the route forgot to pass renders a blank
    space rather than complaining, so passing them from one place is safer.
    """
    if not current_user.is_authenticated:
        return {}
    today = date.today().isoformat()
    try:
        data = valuation()
    except Exception:
        log.exception("valuation failed for the page header")
        balance = float(getattr(current_user, "balance", 0.0) or 0.0)
        data = {"total_value": 0.0, "balance": balance, "net_worth": balance,
                "total_pnl": 0.0, "as_of": current_user.sim_date}
    return {
        "total_investment_value": data["total_value"],
        "net_worth": data["net_worth"],
        "header_balance": data["balance"],
        "total_pnl": data.get("total_pnl", 0.0),
        "sim_date": current_user.sim_date,
        "today": today,
        "is_today": current_user.sim_date >= today,
        #the date the figures in the header describe. on a past simulation date
        #they are that date's prices rather than today's, and the header says so.
        "valued_as_of": data.get("as_of", current_user.sim_date),
        "undo_depth": UndoStack.depth(db, current_user.id),
    }


def parse_iso_date(value: str) -> date:
    """Read a date and check it is one the simulation can use."""
    try:
        parsed = datetime.fromisoformat(value).date()
    except (TypeError, ValueError):
        raise ValueError("Enter a date as YYYY-MM-DD.") from None
    if parsed > date.today():
        raise ValueError("You cannot set a date in the future.")
    if parsed.year < 2000:
        raise ValueError("The simulation starts from the year 2000.")
    return parsed


#pages
@views.route("/")
@login_required
def home():
    data = valuation()
    return render_template(
        "home.html",
        user=current_user,
        positions=data["positions"][:5],
        valuation=data,
        recent=Transaction.for_user(db, current_user.id, limit=5),
    )


@views.route("/stocks")
@login_required
def stocks():
    return render_template(
        "trade.html", user=current_user, kind="stocks",
        asset=asset_for("stocks"), valuation=valuation(),
    )


@views.route("/crypto")
@login_required
def crypto():
    return render_template(
        "trade.html", user=current_user, kind="crypto",
        asset=asset_for("crypto"), valuation=valuation(),
    )


@views.route("/gdpr")
@login_required
def gdpr():
    """The privacy notice.

    @login_required has to sit below @views.route. above it, the decorator is
    applied to a function the router never sees and the page is reachable with
    no session at all.
    """
    return render_template("gdpr.html", user=current_user)


@views.route("/information")
@login_required
def information():
    """The leaderboard, the recent trades and a few figures about the site."""
    market = get_market(db)

    users = User.all(db)
    holdings = db.fetchall("SELECT user_id, symbol, shares FROM portfolios")

    #value every trader on their own simulation date, which is the same
    #date they see everywhere else on the site. pricing everybody at the
    #live price puts an account that bought at prices from years ago at the
    #top of the table with a net worth it does not really have.
    sim_dates = {u["id"]: u["sim_date"] for u in users}

    #price each symbol and date once. asking for the price inside the loop
    #means two requests per holding per user, which is a hundred requests
    #for a site with ten users holding five things each.
    prices: dict[tuple[str, str], float | None] = {}
    by_user: dict[int, float] = {}
    for h in holdings:
        as_of = sim_dates.get(h["user_id"]) or date.today().isoformat()
        key = (h["symbol"], as_of)
        if key not in prices:
            try:
                prices[key] = market.price_on_date(h["symbol"], as_of)
            except MarketDataError:
                prices[key] = None
        price = prices[key]
        if price is not None:
            by_user[h["user_id"]] = by_user.get(h["user_id"], 0.0) + price * float(h["shares"])

    leaderboard = sorted(
        (
            {
                "username": u["first_name"],
                "net_worth": round(float(u["balance"]) + by_user.get(u["id"], 0.0), 2),
                "sim_date": u["sim_date"],
                "is_me": u["id"] == current_user.id,
            }
            for u in users
        ),
        key=lambda row: row["net_worth"],
        reverse=True,
    )
    for index, row in enumerate(leaderboard, start=1):
        row["rank"] = index

    insights = {}
    top = db.fetchone(
        """
        SELECT symbol, SUM(ABS(shares)) AS volume
        FROM transactions GROUP BY symbol ORDER BY volume DESC LIMIT 1
        """
    )
    if top:
        insights["top_traded"] = {"symbol": top["symbol"], "volume": round(top["volume"], 2)}

    stats = db.fetchone(
        "SELECT AVG(price) AS avg_price, SUM(ABS(shares)) AS volume, COUNT(*) AS trades FROM transactions"
    )
    if stats and stats["trades"]:
        insights["avg_price"] = round(stats["avg_price"] or 0, 2)
        insights["total_volume"] = round(stats["volume"] or 0, 2)
        insights["total_trades"] = stats["trades"]

    return render_template(
        "information.html",
        user=current_user,
        leaderboard=leaderboard,
        transactions=Transaction.recent(db, limit=10),
        insights=insights,
    )


#the account page
@views.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        action = request.form.get("form_action")

        if action == "rename":
            new_name = (request.form.get("first_name") or "").strip()
            if not new_name:
                flash("Enter a name.", "error")
            elif new_name == current_user.first_name:
                flash("That is already your name.", "info")
            else:
                #write the new name to the database, not just to the user object
                User.update_name(db, current_user.id, new_name)
                current_user.first_name = new_name
                flash("Name updated.", "success")
            return redirect(url_for("views.account"))

        if action == "sim_date":
            try:
                parsed = parse_iso_date(request.form.get("sim_date", ""))
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("views.account"))
            User.update_sim_date(db, current_user.id, parsed.isoformat())
            current_user.sim_date = parsed.isoformat()
            flash(f"Simulation date set to {parsed.isoformat()}.", "success")
            return redirect(url_for("views.account"))

    data = valuation()

    friends = []
    for friend in Friendship.friends_of(db, current_user.id):
        friend_user = User.from_row(friend)
        friend_value = engine().portfolio_valuation(friend_user)
        friends.append({
            "user": friend_user,
            "balance": friend_value["balance"],
            "investments": friend_value["total_value"],
            "net_worth": friend_value["net_worth"],
            "sim_date": friend_user.sim_date,
        })

    return render_template(
        "account.html", user=current_user, valuation=data, friends=friends,
        transactions=Transaction.for_user(db, current_user.id, limit=25),
    )


#friends
@views.route("/friends")
@login_required
def friends():
    return render_template(
        "friends.html",
        user=current_user,
        incoming=Friendship.incoming(db, current_user.id),
        outgoing=Friendship.outgoing(db, current_user.id),
        results=[],
        query="",
    )


@views.route("/friends/search", methods=["POST"])
@login_required
def search_users():
    query = (request.form.get("query") or "").strip()
    results = User.search(db, query, exclude_id=current_user.id) if query else []
    return render_template(
        "friends.html",
        user=current_user,
        incoming=Friendship.incoming(db, current_user.id),
        outgoing=Friendship.outgoing(db, current_user.id),
        results=results,
        query=query,
    )


@views.route("/friends/request/<int:user_id>", methods=["POST"])
@login_required
def send_friend_request(user_id: int):
    """Send a friend request.

    this is a POST rather than a GET. as a GET it changes something from a
    plain link, so any other page could fire it with an image tag.
    """
    if user_id == current_user.id:
        flash("You cannot add yourself.", "error")
        return redirect(url_for("views.friends"))

    target = User.get_by_id(db, user_id)
    if target is None:
        flash("That user no longer exists.", "error")
        return redirect(url_for("views.friends"))

    #check both directions. looking only for a request sent by the current
    #user lets A to B and B to A both exist at once, and means one refusal
    #blocks any future request forever.
    existing = Friendship.between(db, current_user.id, user_id)
    if existing and existing["status"] == "accepted":
        flash(f"You are already friends with {target.first_name}.", "info")
    elif existing and existing["status"] == "pending":
        flash("There is already a pending request between you.", "info")
    else:
        Friendship.send_request(db, current_user.id, user_id)
        flash(f"Friend request sent to {target.first_name}.", "success")
    return redirect(url_for("views.friends"))


@views.route("/friends/respond/<int:request_id>/<response>", methods=["POST"])
@login_required
def respond_friend_request(request_id: int, response: str):
    row = Friendship.get(db, request_id)
    if row is None:
        flash("That request no longer exists.", "error")
        return redirect(url_for("views.friends"))

    if response in ("accept", "reject") and row["receiver_id"] == current_user.id:
        Friendship.set_status(db, request_id, "accepted" if response == "accept" else "rejected")
        flash(f"Request {response}ed.", "success" if response == "accept" else "info")
    elif response == "cancel" and row["sender_id"] == current_user.id:
        Friendship.delete(db, request_id)
        flash("Request cancelled.", "info")
    else:
        flash("You are not able to respond to that request.", "error")
    return redirect(url_for("views.friends"))


@views.route("/friends/remove/<int:friend_id>", methods=["POST"])
@login_required
def remove_friend(friend_id: int):
    removed = Friendship.remove_between(db, current_user.id, friend_id)
    flash("Friend removed." if removed else "You are not friends with that user.",
          "success" if removed else "error")
    return redirect(url_for("views.account"))


#the trading bot page
@views.route("/bot", methods=["GET", "POST"])
@login_required
def trading_bot():
    from .bot import BotManager

    manager = BotManager.instance()

    if request.method == "POST":
        symbol = (request.form.get("symbol") or "").upper().strip()
        enabled = request.form.get("enable_bot") == "on"

        try:
            buy_at = float(request.form.get("threshold_buy") or 0)
            sell_at = float(request.form.get("threshold_sell") or 0)
            quantity = float(request.form.get("quantity") or 0)
        except ValueError:
            flash("Thresholds and quantity must be numbers.", "error")
            return redirect(url_for("views.trading_bot"))

        #each check returns as well as flashing. flashing an error and carrying
        #on saves the settings that were just refused and starts the bot anyway.
        if enabled:
            if not symbol:
                flash("Choose an asset for the bot to watch.", "error")
                return redirect(url_for("views.trading_bot"))
            if quantity <= 0:
                flash("Quantity must be greater than zero.", "error")
                return redirect(url_for("views.trading_bot"))
            if buy_at <= 0 or sell_at <= 0:
                flash("Both thresholds must be greater than zero.", "error")
                return redirect(url_for("views.trading_bot"))
            if buy_at >= sell_at:
                flash("The buy threshold must be below the sell threshold.", "error")
                return redirect(url_for("views.trading_bot"))
            if get_market(db).get_price(symbol) is None:
                flash(f"Could not find a price for {symbol}.", "error")
                return redirect(url_for("views.trading_bot"))

        BotConfig.save(
            db, current_user.id, enabled=int(enabled), symbol=symbol or None,
            threshold_buy=buy_at or None, threshold_sell=sell_at or None,
            quantity=quantity or None,
            cooldown_seconds=int(request.form.get("cooldown") or 60),
            max_trades_per_day=int(request.form.get("max_trades") or 20),
        )

        if enabled:
            manager.start(current_user.id)
            flash(f"Bot enabled, watching {symbol}.", "success")
        else:
            manager.stop(current_user.id)
            flash("Bot disabled.", "info")
        return redirect(url_for("views.trading_bot"))

    return render_template(
        "bot.html", user=current_user,
        config=BotConfig.get(db, current_user.id),
        status=manager.status(current_user.id),
        events=BotEvent.recent(db, current_user.id, limit=30),
        valuation=valuation(),
    )


#deleting an account
@views.route("/account/delete", methods=["GET", "POST"])
@login_required
def account_delete():
    from flask import session

    if request.method == "POST":
        if request.form.get("send_otp"):
            #only send a code when one is asked for. sending on every GET means
            #refreshing the page fills the inbox, and makes each refresh wait on the
            #mail server.
            manager = OTPManager(current_user.email)
            try:
                manager.send("delete")
                flash("A confirmation code has been sent to your email.", "success")
            except Exception:
                log.exception("could not send deletion OTP")
                flash("Could not send the email. Please try again shortly.", "error")
            return redirect(url_for("views.account_delete"))

        code = (request.form.get("otp_code") or "").strip()
        if not OTPManager.verify(code, "delete"):
            flash("That code is not correct or has expired.", "error")
            return redirect(url_for("views.account_delete"))

        user_id = current_user.id
        logout_user()
        #the foreign keys take the holdings, trades, friendships, bot settings
        #and events with the user, so none of them have to be listed here.
        User.delete(db, user_id)
        session.clear()
        flash("Your account and all associated data have been deleted.", "success")
        return redirect(url_for("auth.login"))

    return render_template("account_delete.html", user=current_user,
                           otp_sent=OTPManager.is_pending("delete"))


#admin
@views.route("/admin", methods=["GET", "POST"])
@admin_required
def admin():
    if request.method == "POST":
        action = request.form.get("form_action")

        if action == "delete_user":
            target = int(request.form.get("user_id"))
            if target == current_user.id:
                flash("You cannot delete your own admin account here.", "error")
            else:
                User.delete(db, target)
                flash(f"User {target} deleted.", "success")

        elif action == "edit_user":
            target = request.form.get("user_id")
            name = (request.form.get("first_name") or "").strip()
            raw_balance = request.form.get("balance")
            try:
                balance = round(float(raw_balance), 2)
            except (TypeError, ValueError):
                #check the balance really is a number. int(float(x)) % 1 is always 0,
                #so it lets anything through and stores the balance as text.
                flash("Balance must be a number.", "error")
                return redirect(url_for("views.admin"))
            if not name:
                flash("Name cannot be empty.", "error")
                return redirect(url_for("views.admin"))
            db.execute("UPDATE users SET first_name = ?, balance = ? WHERE id = ?",
                       (name, balance, target))
            flash(f"User {target} updated.", "success")

        elif action == "delete_holding":
            db.execute("DELETE FROM portfolios WHERE id = ?", (request.form.get("holding_id"),))
            flash("Holding removed.", "success")

        return redirect(url_for("views.admin"))

    users = User.all(db)
    holdings: dict[int, list] = {}
    for row in db.fetchall("SELECT * FROM portfolios ORDER BY symbol"):
        holdings.setdefault(row["user_id"], []).append(row)

    return render_template("admin.html", user=current_user, users=users, holdings=holdings)


#old urls, kept so existing bookmarks still work
@views.route("/GDPR")
def legacy_gdpr():
    return redirect(url_for("views.gdpr"), code=301)


@views.route("/Information")
def legacy_information():
    return redirect(url_for("views.information"), code=301)


@views.route("/trading_bot")
def legacy_bot():
    return redirect(url_for("views.trading_bot"), code=301)


@views.route("/search_users")
def legacy_search_users():
    return redirect(url_for("views.friends"), code=301)


@views.route("/friend_requests")
def legacy_friend_requests():
    return redirect(url_for("views.friends"), code=301)


@views.route("/account_delete")
def legacy_account_delete():
    return redirect(url_for("views.account_delete"), code=301)
