#everything that talks to the database, and the classes that stand for the
#things stored in it.
#
#Database is a singleton, one instance per database file. each thread gets
#its own connection, because the trading bot runs in a thread of its own
#and sqlite will not share a connection between them.
#
#rows come back as sqlite3.Row, which can be read by column name. reading
#them by position instead means that reordering a column quietly breaks
#every query that touched it.
#
#transaction() gives one atomic unit of work. buying an asset writes to
#three tables, and a failure part way through has to leave none of them
#changed rather than take the money without recording the shares.

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import date as _date
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from flask_login import UserMixin

from .config import Config


class Database:
    """A singleton wrapper around one sqlite database file."""

    _instances: dict[str, "Database"] = {}  #one instance per database file
    _instances_lock = threading.Lock()

    def __new__(cls, db_path: str | Path | None = None) -> "Database":
        key = str(Path(db_path or Config.DATABASE_PATH).resolve())
        with cls._instances_lock:
            if key not in cls._instances:
                obj = super().__new__(cls)
                obj.db_path = key
                #sqlite connections cannot be shared between threads, and the bot
                #runs in one of its own, so every thread gets its own connection.
                obj._local = threading.local()
                cls._instances[key] = obj
        return cls._instances[key]

    #connections
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        #write ahead logging lets the bot read while a request is writing,
        #rather than waiting on a lock over the whole database.
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """The connection belonging to the calling thread."""
        existing = getattr(self._local, "conn", None)
        if existing is None:
            existing = self._connect()
            self._local.conn = existing
        return existing

    def raw_connection(self) -> sqlite3.Connection:
        """A new connection the caller owns and has to close itself.

        used by the migration runner, which needs to change settings and run a
        whole script outside the shared connection.
        """
        return self._connect()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block of statements as one unit, undoing it all on error."""
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            #already inside a transaction, so join the one already open
            yield conn
            return
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    #query helpers
    def execute(self, query: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(query, params)
        self.conn.commit()
        return cur

    def executemany(self, query: str, seq: Iterable[Sequence[Any]]) -> None:
        self.conn.executemany(query, seq)
        self.conn.commit()

    def fetchall(self, query: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(query, params).fetchall()

    def fetchone(self, query: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(query, params).fetchone()

    @staticmethod
    def get_db(db_path: str | Path | None = None) -> "Database":
        return Database(db_path)


#the classes below each stand for one table
class User(UserMixin):
    TABLE_NAME = "users"

    def __init__(
        self,
        id=None,
        email=None,
        password=None,
        first_name=None,
        balance=None,
        sim_date=None,
        is_admin=0,
        created_at=None,
    ):
        self.id = id
        self.email = email
        self.password = password
        self.first_name = first_name
        self.balance = Config.STARTING_BALANCE if balance is None else balance
        self.sim_date = sim_date or _date.today().isoformat()
        self.is_admin = bool(is_admin)
        self.created_at = created_at

    @classmethod
    def from_row(cls, row: sqlite3.Row | None) -> "User | None":
        return cls(**dict(row)) if row else None

    #queries
    @staticmethod
    def get_by_email(db: Database, email: str) -> "User | None":
        return User.from_row(
            db.fetchone("SELECT * FROM users WHERE email = ?", (email,))
        )

    @staticmethod
    def get_by_id(db: Database, user_id) -> "User | None":
        return User.from_row(db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,)))

    @staticmethod
    def search(db: Database, query: str, exclude_id=None) -> list["User"]:
        rows = db.fetchall(
            """
            SELECT * FROM users
            WHERE first_name LIKE ? AND id IS NOT ?
            ORDER BY first_name LIMIT 50
            """,
            (f"%{query}%", exclude_id),
        )
        return [User.from_row(r) for r in rows]

    def save(self, db: Database) -> int:
        """Add this user to the database and return their new id."""
        cur = db.execute(
            """
            INSERT INTO users (email, password, first_name, balance, sim_date, is_admin)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self.email,
                self.password,
                self.first_name,
                self.balance,
                self.sim_date,
                int(self.is_admin),
            ),
        )
        self.id = cur.lastrowid
        db.execute("INSERT OR IGNORE INTO bot_configs (user_id, enabled) VALUES (?, 0)", (self.id,))
        return self.id

    @staticmethod
    def update_balance(db: Database, user_id, new_balance: float) -> None:
        db.execute("UPDATE users SET balance = ? WHERE id = ?", (round(new_balance, 2), user_id))

    @staticmethod
    def update_name(db: Database, user_id, first_name: str) -> None:
        """Save a change of name to the database."""
        db.execute("UPDATE users SET first_name = ? WHERE id = ?", (first_name, user_id))

    @staticmethod
    def update_sim_date(db: Database, user_id, sim_date: str) -> None:
        db.execute("UPDATE users SET sim_date = ? WHERE id = ?", (sim_date, user_id))

    @staticmethod
    def update_password(db: Database, email: str, password_hash: str) -> None:
        db.execute("UPDATE users SET password = ? WHERE email = ?", (password_hash, email))

    @staticmethod
    def delete(db: Database, user_id) -> None:
        """Delete a user. their other rows go too, through the foreign keys."""
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))

    @staticmethod
    def all(db: Database) -> list[sqlite3.Row]:
        return db.fetchall("SELECT * FROM users ORDER BY id")


class Portfolio:
    TABLE_NAME = "portfolios"

    @staticmethod
    def get(db: Database, user_id, symbol: str) -> sqlite3.Row | None:
        return db.fetchone(
            "SELECT * FROM portfolios WHERE user_id = ? AND symbol = ?",
            (user_id, symbol.upper()),
        )

    @staticmethod
    def for_user(db: Database, user_id) -> list[sqlite3.Row]:
        return db.fetchall(
            "SELECT * FROM portfolios WHERE user_id = ? ORDER BY symbol", (user_id,)
        )

    @staticmethod
    def upsert(conn: sqlite3.Connection, user_id, symbol: str, shares: float, avg_cost: float) -> None:
        """Add or change a holding, inside a transaction already open."""
        conn.execute(
            """
            INSERT INTO portfolios (user_id, symbol, shares, avg_cost)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, symbol)
            DO UPDATE SET shares = excluded.shares, avg_cost = excluded.avg_cost
            """,
            (user_id, symbol.upper(), shares, avg_cost),
        )

    @staticmethod
    def remove(conn: sqlite3.Connection, user_id, symbol: str) -> None:
        conn.execute(
            "DELETE FROM portfolios WHERE user_id = ? AND symbol = ?", (user_id, symbol.upper())
        )


class Transaction:
    TABLE_NAME = "transactions"

    @staticmethod
    def record(
        conn: sqlite3.Connection,
        user_id,
        symbol: str,
        shares: float,
        price: float,
        sim_date: str,
        source: str = "manual",
    ) -> int:
        cur = conn.execute(
            """
            INSERT INTO transactions (user_id, symbol, shares, price, total, sim_date, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, symbol.upper(), shares, price, round(shares * price, 2), sim_date, source),
        )
        return cur.lastrowid

    @staticmethod
    def for_user(db: Database, user_id, limit: int = 50) -> list[sqlite3.Row]:
        return db.fetchall(
            "SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )

    @staticmethod
    def recent(db: Database, limit: int = 5) -> list[sqlite3.Row]:
        """The most recent trades made by anybody on the site."""
        return db.fetchall(
            """
            SELECT t.*, u.first_name
            FROM transactions t
            JOIN users u ON u.id = t.user_id
            ORDER BY t.id DESC
            LIMIT ?
            """,
            (limit,),
        )


class Friendship:
    TABLE_NAME = "friendships"

    @staticmethod
    def get(db: Database, request_id) -> sqlite3.Row | None:
        return db.fetchone("SELECT * FROM friendships WHERE id = ?", (request_id,))

    @staticmethod
    def between(db: Database, a, b) -> sqlite3.Row | None:
        """Any friendship between two users, sent in either direction."""
        return db.fetchone(
            """
            SELECT * FROM friendships
            WHERE (sender_id = ? AND receiver_id = ?)
               OR (sender_id = ? AND receiver_id = ?)
            """,
            (a, b, b, a),
        )

    @staticmethod
    def send_request(db: Database, sender_id, receiver_id) -> None:
        db.execute(
            """
            INSERT INTO friendships (sender_id, receiver_id, status)
            VALUES (?, ?, 'pending')
            ON CONFLICT(sender_id, receiver_id)
            DO UPDATE SET status = 'pending', created_at = datetime('now')
            """,
            (sender_id, receiver_id),
        )

    @staticmethod
    def set_status(db: Database, request_id, status: str) -> None:
        db.execute("UPDATE friendships SET status = ? WHERE id = ?", (status, request_id))

    @staticmethod
    def delete(db: Database, request_id) -> None:
        db.execute("DELETE FROM friendships WHERE id = ?", (request_id,))

    @staticmethod
    def remove_between(db: Database, a, b) -> int:
        """Remove a friendship, whichever way round it was sent.

        the brackets around the two OR branches matter. without them the accepted
        check only applies to the second branch, and a request that was never
        accepted could be removed as though it were a friendship.
        """
        cur = db.execute(
            """
            DELETE FROM friendships
            WHERE status = 'accepted'
              AND ((sender_id = ? AND receiver_id = ?)
                OR (sender_id = ? AND receiver_id = ?))
            """,
            (a, b, b, a),
        )
        return cur.rowcount

    @staticmethod
    def incoming(db: Database, user_id) -> list[sqlite3.Row]:
        """Friend requests waiting for an answer, with the sender attached.

        joining the sender in here means one query for the page rather than another
        two for every request on it.
        """
        return db.fetchall(
            """
            SELECT f.*, u.first_name AS other_name, u.email AS other_email
            FROM friendships f
            JOIN users u ON u.id = f.sender_id
            WHERE f.receiver_id = ? AND f.status = 'pending'
            ORDER BY f.created_at DESC
            """,
            (user_id,),
        )

    @staticmethod
    def outgoing(db: Database, user_id) -> list[sqlite3.Row]:
        return db.fetchall(
            """
            SELECT f.*, u.first_name AS other_name, u.email AS other_email
            FROM friendships f
            JOIN users u ON u.id = f.receiver_id
            WHERE f.sender_id = ? AND f.status = 'pending'
            ORDER BY f.created_at DESC
            """,
            (user_id,),
        )

    @staticmethod
    def friends_of(db: Database, user_id) -> list[sqlite3.Row]:
        """Everyone a user is friends with, whoever sent the request."""
        return db.fetchall(
            """
            SELECT u.*
            FROM friendships f
            JOIN users u
              ON u.id = CASE WHEN f.sender_id = ? THEN f.receiver_id ELSE f.sender_id END
            WHERE (f.sender_id = ? OR f.receiver_id = ?) AND f.status = 'accepted'
            ORDER BY u.first_name
            """,
            (user_id, user_id, user_id),
        )


class BotConfig:
    TABLE_NAME = "bot_configs"

    @staticmethod
    def get(db: Database, user_id) -> sqlite3.Row | None:
        row = db.fetchone("SELECT * FROM bot_configs WHERE user_id = ?", (user_id,))
        if row is None:
            db.execute("INSERT OR IGNORE INTO bot_configs (user_id, enabled) VALUES (?, 0)", (user_id,))
            row = db.fetchone("SELECT * FROM bot_configs WHERE user_id = ?", (user_id,))
        return row

    @staticmethod
    def save(db: Database, user_id, **fields) -> None:
        allowed = {
            "enabled", "symbol", "threshold_buy", "threshold_sell", "quantity",
            "max_position", "cooldown_seconds", "max_trades_per_day", "last_trade_at",
        }
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        assignments = ", ".join(f"{k} = ?" for k in sets)
        db.execute(
            f"UPDATE bot_configs SET {assignments}, updated_at = datetime('now') WHERE user_id = ?",
            (*sets.values(), user_id),
        )

    @staticmethod
    def all_enabled(db: Database) -> list[sqlite3.Row]:
        return db.fetchall("SELECT * FROM bot_configs WHERE enabled = 1")


class BotEvent:
    TABLE_NAME = "bot_events"

    @staticmethod
    def log(db: Database, user_id, message: str, level: str = "info",
            symbol: str | None = None, price: float | None = None) -> None:
        db.execute(
            """
            INSERT INTO bot_events (user_id, level, message, symbol, price)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, level, message, symbol, price),
        )

    @staticmethod
    def recent(db: Database, user_id, limit: int = 50, after_id: int = 0) -> list[sqlite3.Row]:
        return db.fetchall(
            """
            SELECT * FROM bot_events
            WHERE user_id = ? AND id > ?
            ORDER BY id DESC LIMIT ?
            """,
            (user_id, after_id, limit),
        )

    @staticmethod
    def trades_today(db: Database, user_id) -> int:
        row = db.fetchone(
            """
            SELECT COUNT(*) AS n FROM transactions
            WHERE user_id = ? AND source = 'bot' AND date(executed_at) = date('now')
            """,
            (user_id,),
        )
        return row["n"] if row else 0


class UndoStack:
    """The stack of recent trades that can still be undone.

    this is held in the database rather than in the flask session. a list in the
    session that is changed in place is never saved, because flask cannot tell
    that it changed, so nothing would ever be there to undo.
    """

    TABLE_NAME = "undo_stack"

    @staticmethod
    def push(
        conn: sqlite3.Connection,
        user_id,
        transaction_id,
        symbol,
        shares,
        price,
        action,
        prev_shares: float = 0.0,
        prev_avg_cost: float | None = None,
        sim_date: str | None = None,
    ) -> None:
        """Save a trade that can be undone later.

        prev_shares and prev_avg_cost are the holding as it stood before the trade,
        which is what lets an undo put the average cost back rather than work it out
        again. sim_date is the simulated date the trade happened on, so the reversal
        is filed against the same date.
        """
        conn.execute(
            """
            INSERT INTO undo_stack
                (user_id, transaction_id, symbol, shares, price, action,
                 prev_shares, prev_avg_cost, sim_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, transaction_id, symbol.upper(), shares, price, action,
             prev_shares, prev_avg_cost, sim_date),
        )

    @staticmethod
    def peek(db: Database, user_id) -> sqlite3.Row | None:
        return db.fetchone(
            "SELECT * FROM undo_stack WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        )

    @staticmethod
    def peek_in(conn: sqlite3.Connection, user_id) -> sqlite3.Row | None:
        """peek from inside an open transaction, so that another request cannot take
        the same entry between reading it and removing it.
        """
        return conn.execute(
            "SELECT * FROM undo_stack WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()

    @staticmethod
    def pop(conn: sqlite3.Connection, entry_id) -> None:
        conn.execute("DELETE FROM undo_stack WHERE id = ?", (entry_id,))

    @staticmethod
    def depth(db: Database, user_id) -> int:
        row = db.fetchone("SELECT COUNT(*) AS n FROM undo_stack WHERE user_id = ?", (user_id,))
        return row["n"] if row else 0

    @staticmethod
    def clear(db: Database, user_id) -> None:
        db.execute("DELETE FROM undo_stack WHERE user_id = ?", (user_id,))

    #how many trades a user can step back through
    MAX_DEPTH = 20

    @staticmethod
    def trim(conn: sqlite3.Connection, user_id) -> None:
        """Drop entries past MAX_DEPTH so the stack cannot grow forever."""
        conn.execute(
            """
            DELETE FROM undo_stack
            WHERE user_id = ? AND id NOT IN (
                SELECT id FROM undo_stack WHERE user_id = ? ORDER BY id DESC LIMIT ?
            )
            """,
            (user_id, user_id, UndoStack.MAX_DEPTH),
        )


#the database handle the rest of the project uses
db = Database.get_db(Config.DATABASE_PATH)
