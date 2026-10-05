"""One small database layer.
SQLite file (data/journal.db) for local use and testing; PostgreSQL (for example Supabase)
when the DATABASE_URL environment variable is set. SQL is written once with ? placeholders."""
import os
import re
import sqlite3
from pathlib import Path

SQLITE_PATH = Path(__file__).resolve().parent.parent / "data" / "journal.db"
_ready: set = set()


def using_postgres() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


def is_duplicate(exc: Exception) -> bool:
    name, text = type(exc).__name__.lower(), str(exc).lower()
    return "integrity" in name or "uniqueviolation" in name or "unique" in text or "duplicate key" in text


class Conn:
    def __init__(self, raw, pg: bool):
        self.raw, self.pg = raw, pg

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.pg else sql

    def execute(self, sql: str, params=()):
        cur = self.raw.cursor()
        cur.execute(self._sql(sql), tuple(params))
        return cur

    def insert(self, sql: str, params=()) -> int:
        """INSERT into a table that has an `id` column; returns the new id."""
        cur = self.raw.cursor()
        if self.pg:
            cur.execute(self._sql(sql) + " RETURNING id", tuple(params))
            return cur.fetchone()["id"]
        cur.execute(sql, tuple(params))
        return cur.lastrowid

    def executescript(self, script: str):
        if self.pg:
            for stmt in script.split(";"):
                if stmt.strip():
                    self.raw.cursor().execute(stmt)
        else:
            self.raw.executescript(script)

    def has_column(self, table: str, col: str) -> bool:
        if self.pg:
            return bool(self.execute("SELECT 1 FROM information_schema.columns WHERE table_name=? AND column_name=?",
                                     (table, col)).fetchone())
        return col in [r[1] for r in self.raw.execute(f"PRAGMA table_info({table})")]

    def add_column(self, table: str, col: str, ddl: str):
        if not self.has_column(table, col):
            self.raw.cursor().execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    def commit(self):
        self.raw.commit()

    def close(self):
        self.raw.close()


def connect(name: str, schema: str, migrate=None) -> Conn:
    """Open a connection; create the tables the first time this process uses them."""
    if using_postgres():
        import psycopg
        from psycopg.rows import dict_row
        raw = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row,
                              prepare_threshold=None, connect_timeout=10)
        con, key = Conn(raw, True), (name, "postgres")
    else:
        SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(SQLITE_PATH)
        raw.row_factory = sqlite3.Row
        con, key = Conn(raw, False), (name, str(SQLITE_PATH))
    if key not in _ready:
        con.executescript(schema.replace("{ID}", "BIGSERIAL PRIMARY KEY" if con.pg else "INTEGER PRIMARY KEY AUTOINCREMENT"))
        if migrate:
            migrate(con)
        if con.pg:
            # Supabase serves the public schema over a REST API. With row-level security on and no
            # policies, that API can read nothing. The app connects as the owner role, which is unaffected.
            for table in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", schema):
                con.raw.cursor().execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        con.commit()
        _ready.add(key)
    return con
