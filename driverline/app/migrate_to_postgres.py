"""Copy the local SQLite database into PostgreSQL (for example Supabase). Run once, after setting DATABASE_URL:
    DATABASE_URL="postgresql://..." python -m app.migrate_to_postgres [path/to/journal.db]
Rows that already exist in Postgres are skipped, so it is safe to run twice."""
import sqlite3
import sys

from . import auth, dbx, ea, journal, radar

TABLES = ["users", "invites", "usage", "resets", "trades", "ea_keys", "ea_state", "ea_rules",
          "ea_commands", "analyses", "radar_hist"]


def main(src_path=None):
    src = sqlite3.connect(src_path or dbx.SQLITE_PATH)
    src.row_factory = sqlite3.Row
    for make in (auth.db, journal.db, ea.db, radar.db):  # creates every table in the target database
        make().close()
    dst = auth.db()
    for t in TABLES:
        try:
            rows = src.execute(f"SELECT * FROM {t}").fetchall()
        except sqlite3.OperationalError:
            continue
        if not rows:
            continue
        cols = list(rows[0].keys())
        sql = f"INSERT INTO {t}({','.join(cols)}) VALUES({','.join('?' * len(cols))}) ON CONFLICT DO NOTHING"
        for r in rows:
            dst.execute(sql, tuple(r))
        dst.commit()
        if dst.pg and "id" in cols:  # keep the id counter ahead of the copied rows
            dst.execute(f"SELECT setval(pg_get_serial_sequence('{t}','id'), (SELECT COALESCE(MAX(id),1) FROM {t}))")
            dst.commit()
        print(f"{t}: {len(rows)} row(s) copied")
    dst.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
