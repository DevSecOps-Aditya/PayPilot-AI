"""SQLite connection helpers for PayPilot AI."""
import os
import sqlite3

from flask import current_app, g


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(app):
    """Create tables if they do not exist yet (idempotent)."""
    with app.app_context():
        conn = get_db()
        with open(os.path.join(app.root_path, "schema.sql"), encoding="utf-8") as fh:
            conn.executescript(fh.read())
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(paypal_connections)")}
            for name in ("client_id_encrypted", "client_secret_encrypted"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE paypal_connections ADD COLUMN {name} TEXT")
