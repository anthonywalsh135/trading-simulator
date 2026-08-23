-- Canonical schema for the trading simulator.
--
-- This file is the single source of truth for table structure. It is applied
-- with executescript() on every startup and is written to be idempotent
-- (CREATE ... IF NOT EXISTS), so it is safe to run against an existing
-- database. Changes to EXISTING tables belong in migrations.py, which handles
-- upgrading databases that already hold user data.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Users
-- ---------------------------------------------------------------------------
-- sim_date is the user's simulation date (YYYY-MM-DD). Trades execute at the
-- market price on this date; when it equals today, that is the true live price.
-- It replaces the old YYYY-MM `date` column, which could only address a whole
-- month and made "trade at the live price" impossible to express.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT    UNIQUE NOT NULL,
    password      TEXT    NOT NULL,
    first_name    TEXT    NOT NULL,
    balance       REAL    NOT NULL DEFAULT 100000.0,
    sim_date      TEXT    NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- ---------------------------------------------------------------------------
-- Portfolio holdings
-- ---------------------------------------------------------------------------
-- shares is REAL so fractional crypto amounts survive (the old INTEGER column
-- silently truncated 0.5 BTC to 0). avg_cost tracks the weighted average
-- purchase price, which is what makes profit/loss reporting possible.
-- UNIQUE(user_id, symbol) makes duplicate holding rows structurally impossible.
CREATE TABLE IF NOT EXISTS portfolios (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    symbol     TEXT    NOT NULL,
    shares     REAL    NOT NULL,
    avg_cost   REAL    NOT NULL DEFAULT 0.0,
    UNIQUE (user_id, symbol),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_portfolios_user ON portfolios(user_id);

-- ---------------------------------------------------------------------------
-- Transaction ledger
-- ---------------------------------------------------------------------------
-- Positive shares = buy, negative = sell. sim_date records the simulated date
-- the trade happened on, which is distinct from executed_at (the real wall
-- clock time), because a user trading on a past sim_date is still acting now.
CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    symbol      TEXT    NOT NULL,
    shares      REAL    NOT NULL,
    price       REAL    NOT NULL,
    total       REAL    NOT NULL,
    sim_date    TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT 'manual',   -- 'manual' | 'bot' | 'undo'
    executed_at TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_executed ON transactions(executed_at DESC);

-- ---------------------------------------------------------------------------
-- Friendships
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS friendships (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id   INTEGER NOT NULL,
    receiver_id INTEGER NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',  -- 'pending' | 'accepted' | 'rejected'
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (sender_id, receiver_id),
    CHECK (sender_id <> receiver_id),
    FOREIGN KEY (sender_id)   REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (receiver_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_friendships_sender ON friendships(sender_id);
CREATE INDEX IF NOT EXISTS idx_friendships_receiver ON friendships(receiver_id);

-- ---------------------------------------------------------------------------
-- Trading bot configuration (one row per user)
-- ---------------------------------------------------------------------------
-- Replaces the single module-level `bot_config` dictionary that every user on
-- the site previously shared and overwrote.
CREATE TABLE IF NOT EXISTS bot_configs (
    user_id          INTEGER PRIMARY KEY,
    enabled          INTEGER NOT NULL DEFAULT 0,
    symbol           TEXT,
    threshold_buy    REAL,
    threshold_sell   REAL,
    quantity         REAL,
    max_position     REAL    NOT NULL DEFAULT 1000000.0,
    cooldown_seconds INTEGER NOT NULL DEFAULT 60,
    max_trades_per_day INTEGER NOT NULL DEFAULT 20,
    last_trade_at    TEXT,
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Bot activity log
-- ---------------------------------------------------------------------------
-- The old bot called flash() from a background thread, which raised
-- "working outside of request context" and killed the worker. Events are
-- recorded here instead and polled by the browser.
CREATE TABLE IF NOT EXISTS bot_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    level      TEXT    NOT NULL DEFAULT 'info',   -- 'info' | 'trade' | 'error'
    message    TEXT    NOT NULL,
    symbol     TEXT,
    price      REAL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_bot_events_user ON bot_events(user_id, id DESC);

-- ---------------------------------------------------------------------------
-- Cached OHLC candles
-- ---------------------------------------------------------------------------
-- Persistent cache so restarts, replays and the leaderboard do not re-hit the
-- upstream API for data that cannot change (historical bars are immutable).
CREATE TABLE IF NOT EXISTS candle_cache (
    symbol     TEXT    NOT NULL,
    interval   TEXT    NOT NULL,
    ts         INTEGER NOT NULL,        -- epoch seconds, bar open time
    open       REAL    NOT NULL,
    high       REAL    NOT NULL,
    low        REAL    NOT NULL,
    close      REAL    NOT NULL,
    volume     REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, interval, ts)
);

-- ---------------------------------------------------------------------------
-- Undo stack
-- ---------------------------------------------------------------------------
-- The undo history previously lived in the Flask session as a mutated list,
-- which Flask never persisted (session.modified was not set), so undo silently
-- never worked. Storing the original execution price here also means an undo
-- reverses the original trade rather than executing a fresh one at today's price.
-- prev_shares / prev_avg_cost snapshot the holding as it stood BEFORE the
-- trade. Without them an undo could only reverse a trade by executing the
-- opposite one, which re-derives the weighted average cost instead of
-- restoring it: buying at $27 and selling after the price reached $309 left a
-- $168 average once the sale was undone, and every profit figure after that
-- was measured against a purchase price that never happened.
CREATE TABLE IF NOT EXISTS undo_stack (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    transaction_id INTEGER NOT NULL,
    symbol         TEXT    NOT NULL,
    shares         REAL    NOT NULL,
    price          REAL    NOT NULL,
    action         TEXT    NOT NULL,
    prev_shares    REAL,
    prev_avg_cost  REAL,
    sim_date       TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_undo_user ON undo_stack(user_id, id DESC);

-- ---------------------------------------------------------------------------
-- Schema version bookkeeping
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    name       TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
