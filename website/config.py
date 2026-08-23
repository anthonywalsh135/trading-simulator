#every setting for the project. anything that changes between one machine
#and another, or that must never be committed, is read here from the
#environment through a .env file in the project root. nothing else is
#allowed to read os.environ or to write out a path of its own, because that
#is what tied the earlier version to one windows account and put a password
#into the repository.
#
#all the paths are worked out from where this file sits, so the project runs
#the same whatever folder it is started from.

from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

#paths
#
#config.py sits in <root>/website/, so the project root is one level up
PACKAGE_DIR = Path(__file__).resolve().parent
BASE_DIR = PACKAGE_DIR.parent

INSTANCE_DIR = BASE_DIR / "instance"
SCHEMA_PATH = PACKAGE_DIR / "schema.sql"
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

#load .env before reading any setting. anything already set in the real
#environment wins, so a host and the tests can override the file.
load_dotenv(BASE_DIR / ".env", override=False)

#can be overridden so the tests can use a temporary database
_env_db = os.getenv("DATABASE_PATH")
DATABASE_PATH = Path(_env_db) if _env_db else INSTANCE_DIR / "database.db"


def _clean(value: str | None) -> str:
    """Take the spaces out of a value read from the environment.

    google shows an app password as four groups separated by spaces, and it gets
    pasted in with the spaces still there, which makes the email login fail with
    a message that does not explain why.
    """
    if value is None:
        return ""
    return "".join(value.split())


class Config:
    """Every setting, worked out once when this file is first imported."""

    #paths, copied from the constants above
    BASE_DIR = BASE_DIR
    PACKAGE_DIR = PACKAGE_DIR
    INSTANCE_DIR = INSTANCE_DIR
    DATABASE_PATH = DATABASE_PATH
    SCHEMA_PATH = SCHEMA_PATH
    TEMPLATES_DIR = TEMPLATES_DIR
    STATIC_DIR = STATIC_DIR

    #security
    #
    #a made up key keeps development working straight away, but it changes
    #on every restart, which logs everybody out. that is deliberate, to push
    #a real key into .env.
    SECRET_KEY: str = os.getenv("SECRET_KEY") or secrets.token_urlsafe(48)
    SECRET_KEY_IS_EPHEMERAL: bool = not os.getenv("SECRET_KEY")

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    #set to 1 when the site is served over https
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 12  #12 hours

    WTF_CSRF_TIME_LIMIT = None  #the csrf token lasts as long as the session

    #email, used to send one time codes
    GMAIL_USER: str = _clean(os.getenv("GMAIL_USER"))
    GMAIL_APP_PASSWORD: str = _clean(os.getenv("GMAIL_APP_PASSWORD"))
    SMTP_HOST = "smtp.gmail.com"
    SMTP_PORT = 587

    #market data. yahoo and binance need no credentials at all, so there is
    #no api key anywhere in the project.

    #yahoo turns away any request that does not look like a browser
    HTTP_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )
    HTTP_TIMEOUT = 10  #seconds
    HTTP_RETRIES = 3

    #how long a live price stays fresh in the cache. the browser asks more
    #often than this, and the cache is what stops that turning into the same
    #number of requests going out.
    QUOTE_CACHE_SECONDS = 1.0
    CANDLE_CACHE_SECONDS = 30.0
    SEARCH_CACHE_SECONDS = 300.0

    #the simulation
    STARTING_BALANCE = 100_000.0
    EARLIEST_SIM_DATE = "2000-01-01"

    #runtime
    DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"

    @classmethod
    def email_configured(cls) -> bool:
        """Whether one time codes can actually be emailed."""
        return bool(cls.GMAIL_USER and cls.GMAIL_APP_PASSWORD)

    @classmethod
    def warnings(cls) -> list[str]:
        """Warnings to print at startup, so a bad setting is noticed."""
        issues = []
        if cls.SECRET_KEY_IS_EPHEMERAL:
            issues.append(
                "SECRET_KEY is not set in .env, so a random key is in use. "
                "Sessions will be dropped on every restart."
            )
        if not cls.email_configured():
            issues.append(
                "GMAIL_USER and GMAIL_APP_PASSWORD are not set in .env, so "
                "OTP emails cannot be sent, so sign-up and password reset will fail."
            )
        elif len(cls.GMAIL_APP_PASSWORD) != 16:
            issues.append(
                f"GMAIL_APP_PASSWORD is {len(cls.GMAIL_APP_PASSWORD)} characters; "
                "a Google app password is 16. SMTP login will probably fail."
            )
        return issues
