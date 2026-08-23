#tests that walk the site as a logged in user.
#
#these build a real application against a temporary database, so a broken
#template, a url_for that points at nothing and a missing login check are
#all caught here rather than found by hand in a browser.

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(scope="module")
def app():
    """A fully built application.

    conftest.py points DATABASE_PATH at a temporary file before website is
    imported, so this touches nothing real.
    """
    from website import make_app

    application = make_app()
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return application


@pytest.fixture(scope="module")
def registered(app):
    """Make a user directly, without going through the emailed code."""
    from website.auth import hash_password
    from website.models import User, db

    user = User(
        email="router@example.com",
        password=hash_password("correct-horse-battery"),
        first_name="Router",
        balance=100_000.0,
        sim_date=date.today().isoformat(),
    )
    user.save(db)
    return user


@pytest.fixture
def client(app, registered):
    with app.test_client() as c:
        c.post("/login", data={"email": "router@example.com",
                               "password": "correct-horse-battery"})
        yield c


@pytest.fixture
def anon(app):
    with app.test_client() as c:
        yield c


#authorisation
@pytest.mark.parametrize("path", [
    "/", "/stocks", "/crypto", "/gdpr", "/information",
    "/account", "/friends", "/bot", "/admin", "/account/delete",
    "/api/portfolio", "/api/search?q=a", "/api/quote/AAPL",
])
def test_pages_require_login(anon, path):
    """Every page sends a logged out visitor to the login screen.

    /gdpr is the one that matters. with @login_required above @views.route the
    decorator is applied to a function the router never sees, so the page comes
    back with a 200 and no session at all.
    """
    response = anon.get(path)
    assert response.status_code in (302, 401), f"{path} was reachable without logging in"


def test_admin_page_blocked_for_normal_users(client):
    """Admin is gated on the is_admin column, not on having id number 1."""
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 302


#pages render
@pytest.mark.parametrize("path", [
    "/", "/stocks", "/crypto", "/gdpr", "/information", "/account", "/friends", "/bot",
])
def test_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200, f"{path} returned {response.status_code}"
    assert b"Trading Sim" in response.data


def test_public_pages_render(anon):
    for path in ("/login", "/signup", "/forgot-password"):
        assert anon.get(path).status_code == 200


def test_legacy_urls_redirect(anon):
    """Old bookmarks still work after the routes were renamed."""
    for old, new in [("/Login", "/login"), ("/Sign-Up", "/signup"),
                     ("/GDPR", "/gdpr"), ("/Information", "/information"),
                     ("/trading_bot", "/bot")]:
        response = anon.get(old)
        assert response.status_code == 301
        assert response.headers["Location"].endswith(new)


#login
def test_login_rejects_bad_password(anon):
    response = anon.post("/login", data={"email": "router@example.com",
                                         "password": "wrong"}, follow_redirects=True)
    assert b"Incorrect email or password" in response.data


def test_login_does_not_reveal_whether_an_email_exists(anon):
    """Both kinds of failure have to give the same message."""
    missing = anon.post("/login", data={"email": "nobody@example.com",
                                        "password": "x"}, follow_redirects=True)
    wrong = anon.post("/login", data={"email": "router@example.com",
                                      "password": "x"}, follow_redirects=True)
    assert b"Incorrect email or password" in missing.data
    assert b"Incorrect email or password" in wrong.data


def test_login_redirect_cannot_be_used_to_send_users_offsite(anon):
    response = anon.post("/login?next=https://evil.example.com",
                         data={"email": "router@example.com",
                               "password": "correct-horse-battery"})
    assert "evil.example.com" not in response.headers.get("Location", "")


#account actions
def test_rename_persists(client):
    """A change of name has to reach the database, not just the object."""
    from website.models import User, db

    client.post("/account", data={"form_action": "rename", "first_name": "Renamed"})
    user = User.get_by_email(db, "router@example.com")
    assert user.first_name == "Renamed"
    client.post("/account", data={"form_action": "rename", "first_name": "Router"})


def test_sim_date_accepts_a_full_date(client):
    from website.models import User, db

    response = client.post("/account", data={"form_action": "sim_date",
                                             "sim_date": "2024-06-03"},
                           follow_redirects=True)
    assert response.status_code == 200
    assert User.get_by_email(db, "router@example.com").sim_date == "2024-06-03"
    client.post("/account", data={"form_action": "sim_date",
                                  "sim_date": date.today().isoformat()})


@pytest.mark.parametrize("bad", ["2024", "not-a-date", "2099-01-01", "1985-01-01", ""])
def test_bad_sim_dates_are_rejected_without_crashing(client, bad):
    """A bad date gets a message rather than an error page.

    splitting the string on "-" and indexing the result raises IndexError on
    something like "2024".
    """
    response = client.post("/account", data={"form_action": "sim_date", "sim_date": bad},
                           follow_redirects=True)
    assert response.status_code == 200


#aPI
def test_portfolio_api(client):
    data = client.get("/api/portfolio").get_json()
    assert data["ok"] is True
    assert "positions" in data and "net_worth" in data


def test_trade_api_validates_input(client):
    data = client.post("/api/trade", json={"symbol": "AAPL", "action": "buy",
                                           "shares": "abc"}).get_json()
    assert data["ok"] is False
    assert "number" in data["error"].lower()


def test_undo_with_nothing_to_undo(client):
    data = client.post("/api/undo", json={}).get_json()
    assert data["ok"] is False
    assert "nothing to undo" in data["error"].lower()


def test_fast_forward_rejects_moving_backwards(client):
    data = client.post("/api/fast-forward", json={"to": "2001-01-01"}).get_json()
    assert data["ok"] is False


def test_portfolio_api_reports_the_date_it_valued_on(client):
    """The pages have to be able to say which date the figures describe."""
    data = client.get("/api/portfolio").get_json()
    assert data["as_of"] == data["sim_date"]
    assert isinstance(data["live"], bool)


def test_every_mutating_endpoint_returns_the_same_account_fields(client):
    """Every route that changes something returns the same fields.

    a route that leaves one out makes the browser read undefined, which here
    disabled the undo button and lost its counter.
    """
    required = {"balance", "net_worth", "total_value", "positions",
                "as_of", "live", "undo_depth"}

    client.post("/account", data={"form_action": "sim_date", "sim_date": "2024-06-03"})
    data = client.post("/api/fast-forward", json={"to": date.today().isoformat()}).get_json()

    assert data["ok"] is True
    assert required <= set(data), f"missing: {required - set(data)}"


def test_search_api_shape(client):
    data = client.get("/api/search?q=zzzzzznotreal").get_json()
    assert data["ok"] is True
    assert isinstance(data["results"], list)


#friends
def test_friend_request_routes_reject_get(client):
    """D10: these changed state over GET, so any external page could fire them."""
    assert client.get("/friends/request/2").status_code == 405
    assert client.get("/friends/respond/1/accept").status_code == 405


def test_cannot_friend_yourself(client, registered):
    response = client.post(f"/friends/request/{registered.id}", follow_redirects=True)
    assert b"cannot add yourself" in response.data.lower()
