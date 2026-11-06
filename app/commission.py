"""Referral commission sharing: RETIRED (November 2026).

TradeLens no longer shares any Deriv partner commission with members. Deriv manages all official partner commissions directly under
its own programme, and TradeLens points rewards are completely separate from it (see docs/PARTNER_PROGRAMME.md).

What this module still does, and why:
  * It keeps the four historical tables (commission_rows, commission_imports, payout_details, payout_requests) so any past record stays
    available for audit. Nothing is deleted or changed.
  * It installs a database trigger on each of them that REFUSES every new row. Even if old code or a script tried, no commission row,
    import, payout request or payout address can be created any more, so the retired system cannot generate a new liability.
  * It offers one read-only admin view of the historical records (GET /api/admin/commission).
Everything else (import, calculation, member balances, payout requests, payout decisions, member messages) has been removed.
The tables are additive and old; migrations/2026_11_commission_retire.sql installs the same triggers by hand if you prefer."""
import asyncio
from contextlib import closing

from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, dbx

SCHEMA = """
    CREATE TABLE IF NOT EXISTS commission_rows(id {ID}, period TEXT NOT NULL, client_id TEXT NOT NULL, referred_id INTEGER NOT NULL,
        referrer_id INTEGER NOT NULL, commission_cents BIGINT NOT NULL, pct INTEGER NOT NULL, share_cents BIGINT NOT NULL,
        import_id INTEGER, created_at BIGINT NOT NULL, UNIQUE(period, client_id));
    CREATE INDEX IF NOT EXISTS ix_comm_referrer ON commission_rows(referrer_id, period);
    CREATE TABLE IF NOT EXISTS commission_imports(id {ID}, period TEXT NOT NULL, admin_id INTEGER, filename TEXT, rows_total INTEGER NOT NULL,
        rows_shared INTEGER NOT NULL, commission_cents BIGINT NOT NULL, share_cents BIGINT NOT NULL, replaced INTEGER NOT NULL DEFAULT 0,
        created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS payout_details(user_id INTEGER PRIMARY KEY, network TEXT NOT NULL, address TEXT NOT NULL, updated_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS payout_requests(id {ID}, user_id INTEGER NOT NULL, amount_cents BIGINT NOT NULL, network TEXT NOT NULL,
        address TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'requested', txid TEXT, note TEXT, created_at BIGINT NOT NULL,
        decided_at BIGINT, admin_id INTEGER);
    CREATE INDEX IF NOT EXISTS ix_payout_user ON payout_requests(user_id, id);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_payout_open ON payout_requests(user_id) WHERE status='requested'"""

RETIRED_TABLES = ("commission_rows", "commission_imports", "payout_details", "payout_requests")
RETIRED_MESSAGE = "Commission sharing has been retired: no new commission or payout records can be created."


def _block_new_rows(con):
    """Refuse every INSERT on the retired tables. Safe to repeat."""
    cur = con.raw.cursor()
    if con.pg:
        cur.execute("CREATE OR REPLACE FUNCTION commission_retired() RETURNS trigger AS $$ BEGIN "
                    f"RAISE EXCEPTION '{RETIRED_MESSAGE}'; END; $$ LANGUAGE plpgsql")
        for t in RETIRED_TABLES:
            cur.execute(f"DROP TRIGGER IF EXISTS trg_{t}_retired ON {t}")
            cur.execute(f"CREATE TRIGGER trg_{t}_retired BEFORE INSERT ON {t} FOR EACH ROW EXECUTE FUNCTION commission_retired()")
    else:
        for t in RETIRED_TABLES:
            cur.execute(f"CREATE TRIGGER IF NOT EXISTS trg_{t}_retired BEFORE INSERT ON {t} "
                        f"BEGIN SELECT RAISE(ABORT, '{RETIRED_MESSAGE}'); END")


def db():
    auth.db().close()
    return dbx.connect("commission", SCHEMA, _block_new_rows)


def history() -> dict:
    """Read-only copy of what the retired system recorded (for audit). Empty when it never recorded anything."""
    with closing(db()) as con:
        imports = [dict(r) for r in con.execute("SELECT * FROM commission_imports ORDER BY id DESC LIMIT 50").fetchall()]
        rows = con.execute("SELECT COUNT(*) AS n, COALESCE(SUM(share_cents),0) AS s FROM commission_rows").fetchone()
        reqs = [dict(r) for r in con.execute("SELECT id,user_id,amount_cents,network,status,txid,note,created_at,decided_at,admin_id "
                                             "FROM payout_requests ORDER BY id DESC LIMIT 100").fetchall()]
    return {"retired": True, "message": RETIRED_MESSAGE, "rows": int(rows["n"]), "shared_cents": int(rows["s"]),
            "imports": imports, "payout_requests": reqs}


async def admin_history(request):
    auth.require_admin(request)
    return JSONResponse(await asyncio.to_thread(history))


routes = [
    Route("/api/admin/commission", admin_history, methods=["GET"]),
]
