#logging in, signing up, resetting a password and sending one time codes.
#
#the one time codes are hashed before they go into the session, expire after
#ten minutes, allow five attempts and are tied to what they were issued for,
#so a code sent to sign somebody up cannot be used to delete their account
#instead. the session is signed but the browser can still read it, so a code
#stored in plain text there is a code the user can read out of their own
#cookie.
#
#the email is sent on a thread of its own, because talking to the mail server
#takes several seconds and nothing should wait on it.
#
#hash_algorithm below is my own. see its own notes for what it does and,
#more importantly, what it does not do.

from __future__ import annotations

import hmac
import logging
import secrets
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from hashlib import sha256

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from .config import Config
from .models import User, db

log = logging.getLogger(__name__)

auth = Blueprint("auth", __name__)

OTP_TTL_SECONDS = 600  #ten minutes
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_SECONDS = 60  #the least time allowed between two codes

MIN_PASSWORD_LENGTH = 8  #raised from six


#hashing passwords
def hash_algorithm(password: str) -> str:
    """A hash of my own, applied before the real one.

    the character codes of the password are added up, that total shifts every
    character along, and each shifted character is then moved back by the total
    modulo 7 and joined together as a string of digits. the result depends on
    every character, so changing one changes the whole output.

    what this is and is not: it is not a cryptographic hash. it has no
    avalanche property, its length gives away the length of the password, and
    it can be worked backwards with effort. it is applied before werkzeug's
    scrypt, which is what actually protects the password. taking it out would
    not weaken anything and leaving it in does not strengthen anything. it is
    here because it is part of the design, and putting it underneath scrypt
    means it cannot do any harm.
    """
    if not password:
        return ""
    total = sum(ord(c) for c in password)
    shifted = "".join(chr(ord(c) + total % 19) for c in password)
    return "".join(str(ord(c) - total % 7) for c in shifted)


def hash_password(password: str) -> str:
    """Work out the value that gets stored in the database."""
    return generate_password_hash(hash_algorithm(password), method="scrypt")


def verify_password(stored_hash: str, password: str) -> bool:
    return check_password_hash(stored_hash, hash_algorithm(password))


#one time codes
class OTPManager:
    """Sends out one time codes and checks them."""

    def __init__(self, email: str) -> None:
        self.email = email

    @staticmethod
    def _key(purpose: str) -> str:
        return f"otp:{purpose}"

    @staticmethod
    def _digest(code: str, purpose: str) -> str:
        """Hash the code before it goes into the session.

        the session is signed but the browser can still read it, so a code kept
        there in plain text is one the user can read out of their own cookie.
        """
        return sha256(f"{purpose}:{code}:{Config.SECRET_KEY}".encode()).hexdigest()

    def send(self, purpose: str = "signup") -> None:
        """Make a code, save its hash, and email it on a thread of its own."""
        existing = session.get(self._key(purpose))
        if existing and time.time() - existing.get("sent_at", 0) < OTP_RESEND_SECONDS:
            log.info("OTP resend suppressed for %s (%s)", self.email, purpose)
            return

        code = f"{secrets.randbelow(1_000_000):06d}"
        session[self._key(purpose)] = {
            "digest": self._digest(code, purpose),
            "email": self.email,
            "expires_at": time.time() + OTP_TTL_SECONDS,
            "attempts": 0,
            "sent_at": time.time(),
        }
        session.modified = True

        if not Config.email_configured():
            raise RuntimeError(
                "Email is not configured. Set GMAIL_USER and GMAIL_APP_PASSWORD in .env"
            )

        #sending takes seconds, so do not make the user wait for it
        threading.Thread(
            target=self._send_email, args=(self.email, code, purpose), daemon=True
        ).start()

    @staticmethod
    def _send_email(recipient: str, code: str, purpose: str) -> None:
        subjects = {
            "signup": "Your sign-up code",
            "reset": "Your password reset code",
            "delete": "Confirm account deletion",
        }
        message = EmailMessage()
        message["From"] = Config.GMAIL_USER
        message["To"] = recipient
        message["Subject"] = subjects.get(purpose, "Your verification code")
        message.set_content(
            f"Your code is {code}\n\n"
            f"It expires in {OTP_TTL_SECONDS // 60} minutes.\n"
            "If you did not request this, you can ignore this email."
        )
        try:
            context = ssl.create_default_context()
            with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT, timeout=20) as smtp:
                smtp.starttls(context=context)
                smtp.login(Config.GMAIL_USER, Config.GMAIL_APP_PASSWORD)
                smtp.send_message(message)
            log.info("sent %s code to %s", purpose, recipient)
        except Exception:
            log.exception("failed to send %s code to %s", purpose, recipient)

    @classmethod
    def verify(cls, code: str, purpose: str, email: str | None = None) -> bool:
        """Check a code, and use it up if it was right."""
        record = session.get(cls._key(purpose))
        if not record:
            return False
        if time.time() > record.get("expires_at", 0):
            session.pop(cls._key(purpose), None)
            return False
        if record.get("attempts", 0) >= OTP_MAX_ATTEMPTS:
            session.pop(cls._key(purpose), None)
            return False
        if email is not None and record.get("email") != email:
            return False

        record["attempts"] = record.get("attempts", 0) + 1
        session[cls._key(purpose)] = record
        session.modified = True

        #compare in constant time, so how long the answer takes cannot give the
        #hash away one character at a time.
        if hmac.compare_digest(record["digest"], cls._digest(code or "", purpose)):
            session.pop(cls._key(purpose), None)
            session.modified = True
            return True
        return False

    @classmethod
    def is_pending(cls, purpose: str) -> bool:
        record = session.get(cls._key(purpose))
        return bool(record and time.time() < record.get("expires_at", 0))

    @classmethod
    def pending_email(cls, purpose: str) -> str | None:
        record = session.get(cls._key(purpose))
        return record.get("email") if record else None


#limiting login attempts
_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()
MAX_LOGIN_ATTEMPTS = 8
LOGIN_WINDOW_SECONDS = 300


def _throttled(key: str) -> bool:
    """A simple limit on how many attempts are allowed in a window."""
    now = time.time()
    with _attempts_lock:
        history = [t for t in _attempts.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
        _attempts[key] = history
        return len(history) >= MAX_LOGIN_ATTEMPTS


def _record_attempt(key: str) -> None:
    with _attempts_lock:
        _attempts.setdefault(key, []).append(time.time())


#routes
@auth.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("views.home"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        throttle_key = f"{request.remote_addr}:{email}"

        if _throttled(throttle_key):
            flash("Too many attempts. Please wait a few minutes and try again.", "error")
            return render_template("login.html", user=current_user)

        user = User.get_by_email(db, email)
        if user and verify_password(user.password, password):
            login_user(user, remember=request.form.get("remember") == "on")
            flash(f"Welcome back, {user.first_name}.", "success")
            next_page = request.args.get("next")
            #only follow a redirect back into this site, so ?next= cannot be used to
            #send somebody somewhere else.
            if next_page and next_page.startswith("/") and not next_page.startswith("//"):
                return redirect(next_page)
            return redirect(url_for("views.home"))

        _record_attempt(throttle_key)
        #the same message either way, so the form cannot be used to find out
        #which email addresses have an account.
        flash("Incorrect email or password.", "error")

    return render_template("login.html", user=current_user)


@auth.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("views.home"))

    step = "email"
    email = ""

    if request.method == "POST":
        action = request.form.get("form_action")
        email = (request.form.get("email") or "").strip().lower()

        if action == "send_code":
            if "@" not in email or "." not in email.split("@")[-1]:
                flash("Enter a valid email address.", "error")
            elif User.get_by_email(db, email):
                flash("An account already exists with that email. Please log in.", "error")
            else:
                try:
                    OTPManager(email).send("signup")
                    flash("We have emailed you a 6-digit code.", "success")
                    step = "verify"
                except Exception:
                    log.exception("signup OTP failed")
                    flash("Could not send the email. Please try again shortly.", "error")
            return render_template("signup.html", user=current_user, step=step, email=email)

        if action == "complete":
            code = (request.form.get("otp_code") or "").strip()
            first_name = (request.form.get("first_name") or "").strip()
            password1 = request.form.get("password1") or ""
            password2 = request.form.get("password2") or ""
            step = "verify"

            if not all([code, first_name, password1, password2]):
                flash("Please complete every field.", "error")
            elif password1 != password2:
                flash("The passwords do not match.", "error")
            elif len(password1) < MIN_PASSWORD_LENGTH:
                flash(f"Use at least {MIN_PASSWORD_LENGTH} characters.", "error")
            elif not OTPManager.verify(code, "signup", email=email):
                flash("That code is not correct or has expired.", "error")
            elif User.get_by_email(db, email):
                #check again right before inserting, in case the address was registered
                #in between, which would otherwise be a 500 rather than a message.
                flash("An account already exists with that email.", "error")
            else:
                from datetime import date

                user = User(
                    email=email, password=hash_password(password1),
                    first_name=first_name, balance=Config.STARTING_BALANCE,
                    sim_date=date.today().isoformat(),
                )
                user.save(db)
                login_user(user, remember=True)
                flash(f"Welcome, {first_name}. Your account is ready.", "success")
                return redirect(url_for("views.home"))

            return render_template("signup.html", user=current_user, step=step, email=email)

    return render_template("signup.html", user=current_user, step=step, email=email)


@auth.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    step = "email"
    email = ""

    if request.method == "POST":
        action = request.form.get("form_action")
        email = (request.form.get("email") or "").strip().lower()

        if action == "send_code":
            user = User.get_by_email(db, email)
            if user:
                try:
                    OTPManager(email).send("reset")
                except Exception:
                    log.exception("reset OTP failed")
            #always say the same thing, so the form cannot be used to find out which
            #email addresses have an account.
            flash("If that email has an account, we have sent it a code.", "success")
            step = "verify"
            return render_template("forgot_password.html", user=current_user, step=step, email=email)

        if action == "reset":
            code = (request.form.get("otp_code") or "").strip()
            password1 = request.form.get("password1") or ""
            password2 = request.form.get("password2") or ""
            step = "verify"

            user = User.get_by_email(db, email)
            if not all([code, password1, password2]):
                flash("Please complete every field.", "error")
            elif password1 != password2:
                flash("The passwords do not match.", "error")
            elif len(password1) < MIN_PASSWORD_LENGTH:
                flash(f"Use at least {MIN_PASSWORD_LENGTH} characters.", "error")
            elif not OTPManager.verify(code, "reset", email=email):
                flash("That code is not correct or has expired.", "error")
            elif user is None:
                #an unknown email gets the same message rather than reaching the line
                #below and failing on None.
                flash("That code is not correct or has expired.", "error")
            else:
                User.update_password(db, email, hash_password(password1))
                flash("Your password has been reset. You can now log in.", "success")
                return redirect(url_for("auth.login"))

            return render_template("forgot_password.html", user=current_user, step=step, email=email)

    return render_template("forgot_password.html", user=current_user, step=step, email=email)


@auth.route("/logout")
@login_required
def logout():
    logout_user()
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))


#old urls, kept so existing bookmarks still work
@auth.route("/Login")
def legacy_login():
    return redirect(url_for("auth.login"), code=301)


@auth.route("/Sign-Up")
def legacy_signup():
    return redirect(url_for("auth.signup"), code=301)


@auth.route("/Forgot_password")
def legacy_forgot():
    return redirect(url_for("auth.forgot_password"), code=301)


@auth.route("/Logout")
def legacy_logout():
    return redirect(url_for("auth.logout"), code=301)
