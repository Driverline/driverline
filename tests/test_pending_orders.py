"""Tests for pending orders on NO TRADE cards (app/pending_orders.py and the EA plumbing in app/ea.py).
Run with `python tests/test_pending_orders.py` (or `python -m pytest tests/test_pending_orders.py`). No network is needed.

They check, with no AI and no broker involved:
  - every proposed order is geometrically valid MT5 order (limit orders on the right side of price, stop orders on the other,
    stop limits with the limit behind the trigger, stop loss and take profit on the right sides, reward:risk >= 1.5)
  - spike risk, TRADE cards and too little data offer nothing
  - tokens cannot be edited, forged or reused after they go stale
  - the place endpoint refuses what it must (free member, EA off, AllowPending off, risk over the member's own rule, duplicates,
    daily loss stop) and, when everything is fine, queues exactly ONE command the EA poll then hands back with every price in it
  - cancelling needs AllowPending and an order that is really waiting"""
import asyncio
import json
import math
import os
import random
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DRIVERLINE_SECRET"] = "test-secret-for-pending-orders"
try:
    import httpx  # noqa: F401
except ImportError:
    try:
        import httpx2
        sys.modules["httpx"] = httpx2
    except ImportError:
        pass
try:
    import websockets  # noqa: F401
except ImportError:
    import types
    sys.modules.setdefault("websockets", types.ModuleType("websockets"))
try:
    import psycopg  # noqa: F401
except ImportError:
    pass

from app import dbx  # noqa: E402
dbx.SQLITE_PATH = Path(tempfile.mkdtemp()) / "test.db"
from starlette.exceptions import HTTPException  # noqa: E402
from app import auth, ea, pending_orders as PO  # noqa: E402
from app.indicators import analyze_all  # noqa: E402


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def ranging(n=300, mid=1.0800, amp=0.0040, seed=1):
    out, p = [], mid
    for i in range(n):
        m = mid + amp * math.sin(i / 9)
        o, c = p, m + random.Random(seed * 100000 + i).uniform(-0.0006, 0.0006)
        out.append({"epoch": 1_700_000_000 + i * 3600, "open": o, "high": max(o, c) + 0.0003, "low": min(o, c) - 0.0003, "close": c})
        p = c
    return out


def trending(n=300, seed=3):
    r, p, out = random.Random(seed), 1.08, []
    for i in range(n):
        o, c = p, p + 0.0002 + r.uniform(-0.0008, 0.0008)
        out.append({"epoch": 1_700_000_000 + i * 3600, "open": o, "high": max(o, c) + r.uniform(0, 0.0004),
                    "low": min(o, c) - r.uniform(0, 0.0004), "close": c})
        p = c
    return out


def propose(series, reason, key="eurusd", status="NO TRADE"):
    cb = {"H1": series, "M15": series, "H4": series, "D1": series}
    return PO.build_pending(analyze_all(cb), {"status": status, "no_trade_reason": reason}, cb, key)


# ------------------------------------------------------------------------------------------------ the proposals
def test_orders_are_valid_mt5_orders():
    seen = set()
    for series, reason in ((ranging(), "consolidation"), (ranging(seed=2, mid=1.2), "poor_risk_reward"),
                           (trending(), "overextended"), (trending(seed=5), "mixed_timeframes"), (trending(seed=8), "unclear_structure")):
        p = propose(series, reason)
        assert p and p["enabled"]
        P = p["price"]
        for o in p["options"]:
            seen.add(o["type"])
            buy, kind = o["direction"] == "BUY", o["type"]
            assert o["rr"] >= PO.MIN_RR - 1e-9, o
            if kind.endswith("STOP_LIMIT"):
                assert (o["price"] > P) if buy else (o["price"] < P)              # trigger is a stop on the far side
                assert (o["limit"] < o["price"]) if buy else (o["limit"] > o["price"])   # limit sits behind the trigger
                entry = o["limit"]
            elif kind.endswith("LIMIT"):
                assert (o["price"] < P) if buy else (o["price"] > P)
                assert (o["tp"] > P) if buy else (o["tp"] < P)          # the target must still be ahead of price, or the EA would drop the order
                assert o["limit"] is None
                entry = o["price"]
            else:
                assert (o["price"] > P) if buy else (o["price"] < P)
                assert o["limit"] is None
                entry = o["price"]
            assert (o["sl"] < entry < o["tp"]) if buy else (o["tp"] < entry < o["sl"]), o
            assert o["token"] and o["what"] and o["miss"] and o["levels"]
        names = {o["type"] for o in p["options"]} | {u["type"] for u in p["unavailable"]}
        assert names == set(PO.ORDER_TYPES)           # every type is either offered or explained
        assert all(u["reason"] for u in p["unavailable"])
    assert seen == set(PO.ORDER_TYPES), seen          # all six types can be produced


def test_consolidation_offers_all_six():
    p = propose(ranging(), "consolidation")
    assert {o["type"] for o in p["options"]} == set(PO.ORDER_TYPES)


def test_overextended_only_pullback_orders():
    p = propose(trending(), "overextended")
    assert {o["type"] for o in p["options"]} <= {"BUY_LIMIT", "SELL_LIMIT"}
    assert not any("STOP" in o["type"] for o in p["options"])


def test_spike_risk_and_trade_cards_and_thin_data_offer_nothing():
    p = propose(ranging(), "spike_risk")
    assert p["enabled"] is False and not p["options"] and p["why_not"]
    assert propose(ranging(), "consolidation", status="TRADE") is None
    assert propose(ranging(n=40), "consolidation") is None


def test_market_warnings():
    assert any("gap" in w for w in propose(ranging(), "consolidation", key="eurusd")["warnings"])
    assert any("spike" in w.lower() for w in propose(ranging(), "consolidation", key="boom500")["warnings"])


# ------------------------------------------------------------------------------------------------ tokens
def test_token_cannot_be_edited_or_reused_when_stale():
    o = propose(ranging(), "consolidation")["options"][0]
    p = PO.read_token(o["token"])
    assert p["t"] == o["type"] and p["s"] == o["sl"] and p["x"] == o["tp"]
    body, sig = o["token"].split(".")
    forged = PO.make_token({**p, "s": p["s"] * 0.9}).split(".")[0] + "." + sig      # new body, old signature
    for bad in (forged, body + "." + sig[:-2] + "AA", "x.y", "", None, o["token"] + "0"):
        try:
            PO.read_token(bad)
            raise AssertionError("accepted a bad token")
        except ValueError:
            pass
    try:
        PO.read_token(o["token"], now=time.time() + PO.TOKEN_TTL + 5)
        raise AssertionError("accepted a stale token")
    except ValueError as e:
        assert "too old" in str(e)


# ------------------------------------------------------------------------------------------------ placing and cancelling
class Req:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


UID = None


def setup_member(tier="member", positions=0, orders=None, pending_allowed=True, balance=10000.0, daily_loss=0.0, spec=True, seen=None):
    global UID
    auth.db().close()
    with closing(ea.db()) as con:
        email = f"m{random.random()}@x.com"
        con.execute("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)", (email, "x", tier, "2026-01-01"))
        UID = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
        con.execute("INSERT INTO ea_keys(user_id,key_hash,created_at,last_seen) VALUES(?,?,?,?)",
                    (UID, "h" + str(UID), int(time.time()), seen if seen is not None else int(time.time())))
        state = {"account": {"balance": balance, "demo": True, "execute_allowed": True, "pending_allowed": pending_allowed,
                             "remote_close": True},
                 "today": {"closed_pnl": -daily_loss}, "positions": [{"ticket": i + 1, "profit": 0, "sl": 1, "risk": 1} for i in range(positions)],
                 "orders": orders or []}
        specs = [{"symbol": "EURUSDm", "tick_size": 0.00001, "tick_value": 1.0, "vol_min": 0.01, "vol_step": 0.01, "vol_max": 100,
                  "contract_size": 100000}] if spec else []
        con.execute("INSERT INTO ea_state(user_id,state,specs,updated_at) VALUES(?,?,?,?)",
                    (UID, json.dumps(state), json.dumps(specs), int(time.time())))
        con.commit()
    auth.require_user = lambda request, _u={"id": UID, "tier": tier}: _u
    return UID


def good_order(kind="BUY_LIMIT"):
    return next(o for o in propose(ranging(), "consolidation")["options"] if o["type"] == kind)


def place(body):
    try:
        return 200, json.loads(_run(PO.place(Req(body))).body)
    except HTTPException as e:
        return e.status_code, e.detail


def test_place_queues_one_command_and_poll_hands_it_to_the_ea():
    setup_member()
    o = good_order("BUY_STOP_LIMIT")
    code, r = place({"token": o["token"], "lots": 0.1, "hours": 8})
    assert code == 200, r
    assert r["symbol"] == "EURUSDm" and r["comment"].startswith("TLP-") and len(r["comment"]) <= 31
    code, again = place({"token": o["token"], "lots": 0.1, "hours": 8})
    assert code == 409                                  # second tap while the first is queued
    with closing(ea.db()) as con:
        row = con.execute("SELECT kind,status,payload FROM ea_commands WHERE user_id=?", (UID,)).fetchall()
        assert len(row) == 1 and row[0]["kind"] == "pending_order" and row[0]["status"] == "pending"
        pl = json.loads(row[0]["payload"])
        assert pl["ptype"] == "BUY_STOP_LIMIT" and pl["sl"] == o["sl"] and pl["tp"] == o["tp"] and pl["price"] == o["price"]
        assert pl["limit"] == o["limit"] and pl["exp_min"] == 480 and pl["lots"] == 0.1
    # the EA's poll
    class PReq:
        headers = {"x-driverline-key": "unused"}
    ea._key_user = lambda con, request: UID
    ea.credits.mt5_allowed = lambda u: True
    out = json.loads(_run(ea.poll(PReq())).body)
    assert out["cmd"] == "pending_order" and out["ptype"] == "BUY_STOP_LIMIT" and out["sym"] == "EURUSDm"
    assert float(out["price"]) == o["price"] and float(out["limit"]) == o["limit"] and float(out["sl"]) == o["sl"]
    assert float(out["tp"]) == o["tp"] and out["exp_min"] == "480" and out["cmt"] == r["comment"] and float(out["maxdev"]) > 0


def test_place_refusals():
    o = good_order()
    setup_member(tier="free")
    code, msg = place({"token": o["token"]})
    assert code == 403
    setup_member(seen=int(time.time()) - 600)
    assert place({"token": o["token"]})[0] == 409
    setup_member(pending_allowed=False)
    code, msg = place({"token": o["token"]})
    assert code == 409 and "AllowPending" in msg
    setup_member(daily_loss=400.0)                      # 4% down, own limit is 3%
    code, msg = place({"token": o["token"]})
    assert code == 409 and "daily loss" in msg
    setup_member(positions=3)                           # own maximum is 3
    assert place({"token": o["token"]})[0] == 409
    setup_member(spec=False)
    code, msg = place({"token": o["token"]})
    assert code == 409 and "Market Watch" in msg
    setup_member()
    assert place({"token": "tampered"})[0] == 409
    assert place({"token": o["token"], "lots": "abc"})[0] == 422
    assert place({"token": o["token"], "hours": 500})[0] == 422
    setup_member(orders=[{"ticket": 9, "comment": "TLP-" + __import__("hashlib").sha256(o["token"].encode()).hexdigest()[:8]}])
    code, msg = place({"token": o["token"]})
    assert code == 409 and "already waiting" in msg


def test_lot_size_is_optional_and_risk_is_a_warning_not_a_block():
    setup_member(balance=1000.0)                         # 1% of 1000 = 10; a big lot risks far more
    o = good_order()
    code, r = place({"token": o["token"], "lots": 5})    # like "Open trade in MT5": allowed, but flagged
    assert code == 200 and r["over_rule"] is True, (code, r)
    setup_member(balance=1000.0)
    code, r = place({"token": o["token"]})               # no lot size typed: the EA uses the symbol's minimum lot
    assert code == 200, r
    with closing(ea.db()) as con:
        assert json.loads(con.execute("SELECT payload FROM ea_commands WHERE user_id=?", (UID,)).fetchone()["payload"])["lots"] == 0
    assert r["risk_pct"] is not None


def test_cancel_needs_allow_pending_and_a_real_order():
    class CReq(Req):
        pass
    setup_member(orders=[{"ticket": 777, "symbol": "EURUSDm", "type": "BUY_LIMIT"}])
    ok = json.loads(_run(ea.command_create(CReq({"kind": "cancel_order", "ticket": 777}))).body)
    assert ok["id"]
    with closing(ea.db()) as con:
        assert con.execute("SELECT kind,ticket FROM ea_commands WHERE id=?", (ok["id"],)).fetchone()["ticket"] == 777
    setup_member(orders=[{"ticket": 777}])
    try:
        _run(ea.command_create(CReq({"kind": "cancel_order", "ticket": 5})))
        raise AssertionError("cancelled an order that is not there")
    except HTTPException as e:
        assert e.status_code == 404
    setup_member(orders=[{"ticket": 777}], pending_allowed=False)
    try:
        _run(ea.command_create(CReq({"kind": "cancel_order", "ticket": 777})))
        raise AssertionError("cancelled with AllowPending off")
    except HTTPException as e:
        assert e.status_code == 409


def test_old_close_commands_still_work():
    class CReq(Req):
        pass
    setup_member(positions=1)
    assert _run(ea.command_create(CReq({"kind": "close", "ticket": 1}))).status_code == 200
    setup_member(positions=0)
    try:
        _run(ea.command_create(CReq({"kind": "close_all"})))
        raise AssertionError("close_all with nothing open")
    except HTTPException as e:
        assert e.status_code == 409


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:
                failed += 1
                import traceback
                print("FAIL", name, type(e).__name__, e)
                traceback.print_exc()
    sys.exit(1 if failed else 0)
