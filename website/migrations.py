#brings an existing database up to date with the current schema.
#
#schema.sql creates anything that is missing, and each migration below
#upgrades a table that already holds somebody's data. every migration writes
#its name into schema_migrations once it has run, so running this against a
#database that is already current does nothing.
#
#sqlite cannot change the type of a column or add a constraint to a table
#that already exists, so those changes follow the usual pattern: build a new
#table, copy the rows across with whatever conversion is needed, drop the
#old one and rename the new one into its place.

from __future__ import annotations

import logging
import sqlite3
from typing import Callable

from .config import Config


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


#the migrations themselves
def _m001_users_sim_date(conn: sqlite3.Connection, log: logging.Logger) -> None:
    """Turn the old month column into a full date, and add is_admin.

    a column holding only a month cannot say "today", so trading at the live
    price could not be expressed at all. existing values gain a day of 01 so
    nobody loses their place in the simulation.
    """
    if not _table_exists(conn, "users"):
        return  #a new database, so schema.sql makes this table itself
    if "sim_date" in _columns(conn, "users"):
        return

    log.info("migration 001: rebuilding users table (date -> sim_date, +is_admin)")
    conn.execute("ALTER TABLE users RENAME TO users_old")
    conn.execute(
        """
        CREATE TABLE users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email      TEXT    UNIQUE NOT NULL,
            password   TEXT    NOT NULL,
            first_name TEXT    NOT NULL,
            balance    REAL    NOT NULL DEFAULT 100000.0,
            sim_date   TEXT    NOT NULL,
            is_admin   INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    #turn YYYY-MM into YYYY-MM-01. anything already a full date is kept as
    #it is, and anything unreadable falls back to today.
    conn.execute(
        """
        INSERT INTO users (id, email, password, first_name, balance, sim_date, is_admin)
        SELECT id,
               TRIM(email),
               password,
               TRIM(first_name),
               balance,
               CASE
                   WHEN date IS NULL OR date = ''  THEN date('now')
                   WHEN length(date) = 7           THEN date || '-01'
                   WHEN length(date) = 10          THEN date
                   ELSE date('now')
               END,
               CASE WHEN id = 1 THEN 1 ELSE 0 END
        FROM users_old
        """
    )
    conn.execute("DROP TABLE users_old")


def _m002_transactions_rebuild(conn: sqlite3.Connection, log: logging.Logger) -> None:
    """Add the total, the simulated date and the source to the trade ledger."""
    if not _table_exists(conn, "transactions"):
        return  #a new database, so schema.sql makes this table itself
    if "executed_at" in _columns(conn, "transactions"):
        return

    log.info("migration 002: rebuilding transactions table")
    conn.execute("ALTER TABLE transactions RENAME TO transactions_old")
    conn.execute(
        """
        CREATE TABLE transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            symbol      TEXT    NOT NULL,
            shares      REAL    NOT NULL,
            price       REAL    NOT NULL,
            total       REAL    NOT NULL,
            sim_date    TEXT    NOT NULL,
            source      TEXT    NOT NULL DEFAULT 'manual',
            executed_at TEXT    NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        INSERT INTO transactions (id, user_id, symbol, shares, price, total, sim_date, source, executed_at)
        SELECT id, user_id, UPPER(TRIM(symbol)), shares, price,
               shares * price,
               date(Transaction_date),
               'manual',
               Transaction_date
        FROM transactions_old
        WHERE user_id IN (SELECT id FROM users)
        """
    )
    conn.execute("DROP TABLE transactions_old")


def _m003_portfolios_rebuild(conn: sqlite3.Connection, log: logging.Logger) -> None:
    """Let holdings hold fractions, add the average cost, and stop duplicates.

    two rows for the same holding are possible without the constraint, so any
    that exist are added together. the average cost is worked out from the trade ledger where there
    is one.
    """
    if not _table_exists(conn, "portfolios"):
        return  #a new database, so schema.sql makes this table itself
    if "avg_cost" in _columns(conn, "portfolios"):
        return

    log.info("migration 003: rebuilding portfolios table (REAL shares, avg_cost, UNIQUE)")
    conn.execute("ALTER TABLE portfolios RENAME TO portfolios_old")
    conn.execute(
        """
        CREATE TABLE portfolios (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER NOT NULL,
            symbol   TEXT    NOT NULL,
            shares   REAL    NOT NULL,
            avg_cost REAL    NOT NULL DEFAULT 0.0,
            UNIQUE (user_id, symbol),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        INSERT INTO portfolios (user_id, symbol, shares, avg_cost)
        SELECT p.user_id,
               UPPER(TRIM(p.symbol)),
               SUM(p.shares),
               COALESCE((
                   SELECT SUM(t.shares * t.price) / NULLIF(SUM(t.shares), 0)
                   FROM transactions t
                   WHERE t.user_id = p.user_id
                     AND t.symbol  = UPPER(TRIM(p.symbol))
                     AND t.shares  > 0
               ), 0.0)
        FROM portfolios_old p
        WHERE p.user_id IN (SELECT id FROM users)
        GROUP BY p.user_id, UPPER(TRIM(p.symbol))
        """
    )
    conn.execute("DROP TABLE portfolios_old")


def _m004_friendships_rebuild(conn: sqlite3.Connection, log: logging.Logger) -> None:
    """Clear out broken friendships and stop new ones being possible.

    the live database held a row whose sender no longer existed, which would
    fail the moment the foreign keys were switched on.
    """
    cols = _columns(conn, "friendships")
    if not cols:
        return
    #see whether the unique constraint is already there
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='friendships'"
    ).fetchone()
    if ddl and "UNIQUE" in (ddl[0] or ""):
        return

    orphans = conn.execute(
        """
        SELECT COUNT(*) FROM friendships
        WHERE sender_id   NOT IN (SELECT id FROM users)
           OR receiver_id NOT IN (SELECT id FROM users)
           OR sender_id = receiver_id
        """
    ).fetchone()[0]
    log.info("migration 004: rebuilding friendships (%d orphaned/invalid rows dropped)", orphans)

    conn.execute("ALTER TABLE friendships RENAME TO friendships_old")
    conn.execute(
        """
        CREATE TABLE friendships (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id   INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'pending',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (sender_id, receiver_id),
            CHECK (sender_id <> receiver_id),
            FOREIGN KEY (sender_id)   REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (receiver_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO friendships (id, sender_id, receiver_id, status, created_at)
        SELECT id, sender_id, receiver_id, status, created_at
        FROM friendships_old
        WHERE sender_id   IN (SELECT id FROM users)
          AND receiver_id IN (SELECT id FROM users)
          AND sender_id <> receiver_id
        """
    )
    conn.execute("DROP TABLE friendships_old")


def _m005_seed_bot_configs(conn: sqlite3.Connection, log: logging.Logger) -> None:
    """Give every user who does not have one a row of bot settings, switched off."""
    conn.execute(
        """
        INSERT OR IGNORE INTO bot_configs (user_id, enabled)
        SELECT id, 0 FROM users
        """
    )


def _m006_undo_stack_snapshot(conn: sqlite3.Connection, log: logging.Logger) -> None:
    """Save the holding as it stood before each trade that can be undone.

    an undo that carries out the opposite trade works the average cost out
    again rather than putting it back, which leaves a holding recorded at a
    price it was never bought at. saving the shares and the average cost from
    before the trade is what lets an undo restore it exactly.

    entries written before this migration have no snapshot, so prev_avg_cost
    stays empty for them and the undo leaves the average alone.
    """
    cols = _columns(conn, "undo_stack")
    if not cols or "prev_avg_cost" in cols:
        return

    log.info("migration 006: adding holding snapshot to undo_stack")
    for column, decl in (
        ("prev_shares", "REAL"),
        ("prev_avg_cost", "REAL"),
        ("sim_date", "TEXT"),
    ):
        if column not in cols:
            conn.execute(f"ALTER TABLE undo_stack ADD COLUMN {column} {decl}")


#these rebuild tables that already exist, and have to run before
#schema.sql, because schema.sql declares indexes on columns that only
#exist afterwards, and CREATE TABLE IF NOT EXISTS will not change a
#table that is already there.
PRE_SCHEMA_MIGRATIONS: list[tuple[str, Callable[[sqlite3.Connection, logging.Logger], None]]] = [
    ("001_users_sim_date", _m001_users_sim_date),
    ("002_transactions_rebuild", _m002_transactions_rebuild),
    ("003_portfolios_rebuild", _m003_portfolios_rebuild),
    ("004_friendships_rebuild", _m004_friendships_rebuild),
]

#these fill in tables that schema.sql has just created
POST_SCHEMA_MIGRATIONS: list[tuple[str, Callable[[sqlite3.Connection, logging.Logger], None]]] = [
    ("005_seed_bot_configs", _m005_seed_bot_configs),
    ("006_undo_stack_snapshot", _m006_undo_stack_snapshot),
]


def _run_set(conn, log, migrations, applied) -> list[str]:
    done = []
    for name, fn in migrations:
        if name in applied:
            continue
        fn(conn, log)
        conn.execute("INSERT OR REPLACE INTO schema_migrations (name) VALUES (?)", (name,))
        done.append(name)
    return done


def run_migrations(db, log: logging.Logger | None = None) -> None:
    """Bring the database up to date.

    the order matters: rebuild the old tables, then run schema.sql to create
    anything still missing along with the indexes, then fill in any data.

    the foreign keys are switched off while this runs, because rebuilding a
    table means dropping one that others point at. they are switched back on
    and checked before this returns.
    """
    log = log or logging.getLogger(__name__)
    schema_sql = Config.SCHEMA_PATH.read_text(encoding="utf-8")

    conn = db.raw_connection()
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name       TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        applied = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}

        done = _run_set(conn, log, PRE_SCHEMA_MIGRATIONS, applied)
        conn.commit()

        conn.executescript(schema_sql)

        done += _run_set(conn, log, POST_SCHEMA_MIGRATIONS, applied)
        conn.commit()

        if done:
            log.info("applied %d migration(s): %s", len(done), ", ".join(done))

        conn.execute("PRAGMA foreign_keys = ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            log.error("foreign key violations after migration: %s", violations)
    finally:
        conn.close()
