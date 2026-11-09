-- PayPilot AI database schema (SQLite)
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'INR',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Income and expenses live in one table, split by `type`.
CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type        TEXT NOT NULL CHECK (type IN ('expense', 'income')),
    amount      REAL NOT NULL CHECK (amount > 0),
    category    TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    tx_date     TEXT NOT NULL,                -- YYYY-MM-DD
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tx_user_date ON transactions (user_id, tx_date);
CREATE INDEX IF NOT EXISTS idx_tx_user_cat  ON transactions (user_id, category);

CREATE TABLE IF NOT EXISTS paypal_connections (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    tracking_id TEXT NOT NULL UNIQUE,
    merchant_id TEXT,
    status      TEXT NOT NULL CHECK (status IN ('pending', 'connected', 'failed')),
    client_id_encrypted TEXT,
    client_secret_encrypted TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS paypal_orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    merchant_id         TEXT NOT NULL,
    paypal_order_id     TEXT UNIQUE,
    amount              TEXT NOT NULL,
    currency            TEXT NOT NULL,
    description         TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('creating', 'created', 'completed', 'cancelled', 'failed')),
    capture_id          TEXT,
    approval_url        TEXT,
    request_id          TEXT NOT NULL UNIQUE,
    capture_request_id  TEXT NOT NULL UNIQUE,
    return_token_hash   TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_paypal_orders_user ON paypal_orders (user_id, id);

-- One monthly limit per category per user; spend is computed from transactions.
CREATE TABLE IF NOT EXISTS budgets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category      TEXT NOT NULL,
    monthly_limit REAL NOT NULL CHECK (monthly_limit > 0),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, category)
);

CREATE TABLE IF NOT EXISTS goals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    target_amount REAL NOT NULL CHECK (target_amount > 0),
    saved_amount  REAL NOT NULL DEFAULT 0 CHECK (saved_amount >= 0),
    deadline      TEXT,                       -- YYYY-MM-DD, optional
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_goals_user ON goals (user_id);

CREATE TABLE IF NOT EXISTS subscriptions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name              TEXT NOT NULL,
    amount            REAL NOT NULL CHECK (amount > 0),
    billing_cycle     TEXT NOT NULL CHECK (billing_cycle IN ('weekly', 'monthly', 'yearly')),
    next_billing_date TEXT NOT NULL,          -- anchor date, rolled forward when read
    category          TEXT NOT NULL DEFAULT 'Subscriptions',
    is_active         INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions (user_id);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    mode       TEXT,                          -- 'gemini' | 'offline' for assistant rows
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_messages (user_id, id);

CREATE TABLE IF NOT EXISTS reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month      TEXT NOT NULL,                 -- YYYY-MM
    title      TEXT NOT NULL,
    content    TEXT NOT NULL,                 -- Markdown
    mode       TEXT NOT NULL DEFAULT 'gemini',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reports_user ON reports (user_id, id);
