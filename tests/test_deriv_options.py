"""Integration tests for Deriv options (app/deriv_options.py): access, settings, risk limits, martingale steps, edge gate, AutoTrading cycle.
Run: python3 tests/test_deriv_options.py   (own process, temp SQLite database, no network; Deriv calls are replaced by fakes).
When starlette is not installed a tiny stand-in is used so the module can still be exercised."""
import asyncio
import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    import starlette  # noqa: F401
except ImportError:
    class _HTTP(Exception):
        def __init__(self, status_code, detail=None):
            self.status_code, self.detail = status_code, detail

    class _Any:
        def __init__(self, *a, **k):
            pass
    for _n, _kw in (("starlette", {}), ("starlette.exceptions", {"HTTPException": _HTTP}),
                    ("starlette.responses", {k: _Any for k in ("JSONResponse", "RedirectResponse", "Response", "HTMLResponse")}),
                    ("starlette.routing", {"Route": _Any, "Mount": _Any})):
        m = types.ModuleType(_n)
        m.__dict__.update(_kw)
        sys.modules[_n] = m
for _n in ("httpx", "websockets", "pywebpush", "psycopg", "psycopg.rows"):
    try:
        __import__(_n)
    except ImportError:
        m = types.ModuleType(_n)
        m.__dict__.update({"AsyncClient": object, "HTTPError": Exception, "WebSocketException": Exception, "connect": None})
        sys.modules[_n] = m
os.environ.update(DRIVERLINE_SECRET="s" * 40, DERIV_OPTIONS="1")
os.environ.pop("DATABASE_URL", None)

from app import auth, dbx  # noqa: E402
dbx.SQLITE_PATH = Path(tempfile.mkdtemp()) / "t.db"
from app import deriv_options as O  # noqa: E402
from app import deriv_trade as DT  # noqa: E402
from app import options_logic as L  # noqa: E402

_seq = [0]


def mk(tier):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)", (f"o{_seq[0]}@t.example", "h", tier, "2026-01-01T00:00:00"))
        con.commit()
    return {"id": uid, "tier": tier, "blocked": 0}


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


CFG = dict(L.DEFAULTS, strategy="rsi", symbols=["v100"], martingale=True, ack_martingale=True, stake=1.0, mg_mult=2.0, mg_steps=3,
           max_stake=10, daily_loss=20, require_edge=False, session_sl=0, session_tp=0)
DOWN = [1000.0 - i * 0.7 for i in range(120)]      # a steady fall: RSI is oversold, so the RSI strategy says Rise
ACCT = {"id": "DOT1", "type": "demo", "currency": "USD", "balance": 1000.0, "status": "active"}


class Opt(unittest.TestCase):
    def setUp(self):
        import json
        self.user = mk("admin")
        self.bought, self.quote_ratio = [], 0.85
        with closing(O.db()) as con:
            O._save_row(con, self.user["id"], cfg=json.dumps(CFG), auto_on=1, account_id="DOT1", account_type="demo",
                        session_started=int(time.time()) - 5, session_ends=int(time.time()) + 3000, reset_at=int(time.time()) - 10)

        async def closes(code, bars):
            return list(DOWN)

        async def proposal(code, contract):
            return {"ask": contract["amount"], "payout": contract["amount"] * (1 + self.quote_ratio), "longcode": "x"}

        async def buy(app_id, token, account_id, code, contract, max_price):
            self.bought.append(contract)
            return {"contract_id": 100 + len(self.bought), "buy_price": contract["amount"]}

        async def settle(uid, app_id, token):
            return None

        async def accounts(uid, app_id, token):
            return [dict(ACCT, type=getattr(self, "acct_type", "demo"))]
        self.saved = (O._closes, O._proposal, O._buy, O._settle_open, DT._accounts, DT._load_token, DT._enabled)
        O._closes, O._proposal, O._buy, O._settle_open, DT._accounts = closes, proposal, buy, settle, accounts
        DT._load_token = lambda uid: "tok"
        DT._enabled = lambda: (True, "app")

    def tearDown(self):
        O._closes, O._proposal, O._buy, O._settle_open, DT._accounts, DT._load_token, DT._enabled = self.saved

    def cycle(self):
        run(O._auto_user(self.user["id"], {}))

    def rows(self):
        with closing(O.db()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM opt_trades WHERE user_id=? ORDER BY id", (self.user["id"],)).fetchall()]

    def finish(self, tid, profit):
        with closing(O.db()) as con:
            con.execute("UPDATE opt_trades SET result_status=?, profit=?, closed_at=? WHERE id=?", ("won" if profit > 0 else "lost", profit, int(time.time()), tid))
            con.commit()

    def test_access_modes(self):
        free, admin = mk("free"), mk("admin")
        self.assertFalse(O.access(free)["page"])
        self.assertTrue(O.access(admin)["page"] and O.access(admin)["auto"])
        with closing(O.db()) as con:
            con.execute("INSERT INTO opt_meta(k,v) VALUES('mode','public') ON CONFLICT(k) DO UPDATE SET v='public'")
            con.commit()
        try:
            self.assertTrue(O.access(free)["page"])
            self.assertFalse(O.access(free)["auto"])        # AutoTrading is never public
        finally:
            with closing(O.db()) as con:
                con.execute("UPDATE opt_meta SET v='testers' WHERE k='mode'")
                con.commit()

    def test_martingale_chain_and_reset(self):
        self.cycle()
        r = self.rows()
        self.assertEqual((len(r), r[0]["stake"], r[0]["step"], r[0]["contract_type"]), (1, 1.0, 0, "CALL"))
        self.cycle()
        self.assertEqual(len(self.rows()), 1, "one open trade at a time")
        self.finish(r[0]["id"], -1.0)
        self.cycle()
        r = self.rows()
        self.assertEqual((r[1]["stake"], r[1]["step"]), (2.0, 1))
        self.finish(r[1]["id"], -2.0)
        self.cycle()
        r = self.rows()
        self.assertEqual((r[2]["stake"], r[2]["step"]), (4.0, 2))
        self.finish(r[2]["id"], -4.0)
        self.cycle()
        r = self.rows()
        self.assertEqual((r[3]["stake"], r[3]["step"]), (1.0, 0), "after the last step the chain starts again from the base stake")
        self.finish(r[3]["id"], 0.85)
        self.cycle()
        self.assertEqual(self.rows()[4]["step"], 0)

    def test_daily_loss_limit_stops_trading(self):
        with closing(O.db()) as con:
            for i in range(3):
                tid = con.insert("INSERT INTO opt_trades(user_id,account_id,account_type,symbol,strategy,contract_type,duration,stake,step,source,status,"
                                 "result_status,profit,created_at,closed_at,contract_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (self.user["id"], "DOT1", "demo", "v100", "rsi", "CALL", 3, 7.0, 0, "auto", "placed", "lost", -7.0,
                                  int(time.time()) - 60, int(time.time()) - 30, str(i)))
            con.commit()
        self.cycle()
        self.assertEqual(len(self.rows()), 3, "21 USD already lost today against a 20 USD limit")
        with closing(O.db()) as con:
            self.assertIn("Daily loss", con.execute("SELECT last_note FROM opt_settings WHERE user_id=?", (self.user["id"],)).fetchone()["last_note"])

    def test_session_stop_and_target(self):
        self.cycle()
        t = self.rows()[0]
        with closing(O.db()) as con:
            con.execute("UPDATE opt_settings SET cfg=? WHERE user_id=?", (__import__("json").dumps(dict(CFG, session_sl=1.0)), self.user["id"]))
            con.commit()
        self.finish(t["id"], -1.0)
        self.cycle()
        with closing(O.db()) as con:
            self.assertEqual(con.execute("SELECT auto_on FROM opt_settings WHERE user_id=?", (self.user["id"],)).fetchone()["auto_on"], 0)
        self.assertEqual(len(self.rows()), 1)

    def test_real_account_needs_proven_edge(self):
        self.acct_type = "real"
        self.cycle()
        self.assertEqual(self.rows(), [], "no stored backtest: no real-money trade")
        with closing(O.db()) as con:
            con.execute("INSERT INTO opt_validation(strategy,symbol,duration,n,wins,hit,ratio,breakeven,lower,ev,edge,bars,updated_at) "
                        "VALUES('rsi','v100',3,500,330,0.66,0.85,0.54,0.62,0.2,1,5000,1)")
            con.commit()
        self.cycle()
        self.assertEqual(len(self.rows()), 1, "a proven edge that still beats break-even at today's payout")
        self.finish(self.rows()[0]["id"], 0.85)
        self.quote_ratio = 0.5                       # the payout fell: break-even is now 66.7%, above the proven 62%
        self.cycle()
        self.assertEqual(len(self.rows()), 1)

    def test_expired_connection_ends_session(self):
        DT._load_token = lambda uid: None
        self.cycle()
        with closing(O.db()) as con:
            r = con.execute("SELECT auto_on, last_note FROM opt_settings WHERE user_id=?", (self.user["id"],)).fetchone()
        self.assertEqual(r["auto_on"], 0)
        self.assertIn("connection", r["last_note"])


if __name__ == "__main__":
    unittest.main()
