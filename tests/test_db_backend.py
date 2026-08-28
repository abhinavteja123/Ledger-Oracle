# tests/test_db_backend.py
"""LEDGER_BACKEND toggle. Supabase-specific network behaviour (the read-only role
actually rejecting a write) is verified live once against a real Supabase project,
not here -- these tests stay offline, matching every other test in this repo.
"""
import sqlite3

import db


def test_default_backend_is_sqlite(monkeypatch):
    monkeypatch.delenv("LEDGER_BACKEND", raising=False)
    assert db.backend() == "sqlite"


def test_sqlite_backend_returns_unmodified_sqlite_connection(tmp_path, monkeypatch):
    monkeypatch.delenv("LEDGER_BACKEND", raising=False)
    path = tmp_path / "t.db"
    sqlite3.connect(path).execute("CREATE TABLE x (a TEXT)").connection.commit()
    conn = db.get_readonly_connection(str(path))
    assert isinstance(conn, sqlite3.Connection)
    conn.close()


def test_supabase_backend_selected_by_env(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "supabase")
    assert db.backend() == "supabase"


def test_pg_wrapper_adapts_qmark_placeholders():
    calls = []

    class _FakeCursor:
        def execute(self, sql, params):
            calls.append((sql, params))
        def fetchall(self):
            return []

    class _FakeConn:
        def cursor(self, cursor_factory=None):
            return _FakeCursor()
        def commit(self):
            pass
        def close(self):
            pass

    wrapper = db._PgCursorWrapper(_FakeConn())
    wrapper.execute("SELECT * FROM captures WHERE utr=?", ("abc",))
    assert calls == [("SELECT * FROM captures WHERE utr=%s", ("abc",))]
