"""Ledger connection abstraction. See
docs/superpowers/specs/2026-08-28-dashboards-supabase-deploy-design.md.

LEDGER_BACKEND=sqlite (default) | supabase. Tests never set this, so they always
exercise the sqlite path -- byte-for-byte the same behaviour tools.py had before this
module existed. Only the deployed app sets LEDGER_BACKEND=supabase.
"""
import os
import sqlite3


class _PgCursorWrapper:
    """Makes a psycopg2 connection quack like sqlite3.Connection's `.execute()`
    convenience method (`conn.execute(sql, params).fetchall()`), and returns
    dict-style rows (RealDictCursor) so `row["utr"]`-style access works identically
    to sqlite3.Row on the sqlite path. SQL is written once, in SQLite `?` style,
    everywhere else in the codebase -- this is the only place `?` becomes `%s`.
    """
    def __init__(self, pg_conn):
        self._conn = pg_conn

    def execute(self, sql: str, params=()):
        from psycopg2.extras import RealDictCursor
        cur = self._conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql.replace("?", "%s"), params)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def backend() -> str:
    return os.environ.get("LEDGER_BACKEND", "sqlite")


def connection_errors() -> tuple:
    """Exception types get_readonly_connection/get_owner_connection can raise on
    failure, so callers (tools.py) can catch the right ones per backend without a
    hard psycopg2 import when running sqlite-only. Mirrors agent.py's
    LLM_ERRORS -- same class of gap (a backend added later needs its errors
    added to every except clause written for the original backend)."""
    if backend() == "supabase":
        import psycopg2
        return (sqlite3.OperationalError, psycopg2.Error)
    return (sqlite3.OperationalError,)


def get_readonly_connection(db_path: str):
    """Read-only connection for tools.py. sqlite: file:...?mode=ro, unchanged from
    the original implementation. supabase: a Postgres connection using the
    SELECT-only `ledger_reader` role (SUPABASE_READONLY_DB_URL) -- db_path is
    ignored in this mode."""
    if backend() == "supabase":
        import psycopg2
        return _PgCursorWrapper(psycopg2.connect(os.environ["SUPABASE_READONLY_DB_URL"]))
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_owner_connection(db_path: str | None = None):
    """Read-write connection for eval/generate.py's seeding and app.py's
    claims_history / consumed_references writes. sqlite: a normal rw connection to
    db_path. supabase: a Postgres connection using the owner role (SUPABASE_DB_URL)."""
    if backend() == "supabase":
        import psycopg2
        return _PgCursorWrapper(psycopg2.connect(os.environ["SUPABASE_DB_URL"]))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn
