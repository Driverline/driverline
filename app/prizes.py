"""Prizes (Phase 3): members spend points on real items; the owner packs and posts them by hand.

  * Catalogue: Admin sets points, stock and on/off for each prize. The six starting prizes are created once with STOCK 0, which shows as
    "coming soon" and cannot be redeemed until the admin sets a stock, so nothing can be redeemed before it can be delivered.
    Suggested points are 100 per USD of cost: Pen $5 = 500, Journal + pen $10 = 1,000, Powerbank $50 = 5,000, Tablet $110 = 11,000,
    Phone $250 = 25,000, Laptop $500 = 50,000 (cost_usd is private: members never see it).
  * Redeeming spends SPENDABLE points only (the level, which follows lifetime points, never drops). Stock, points and the request change in ONE
    transaction, so two taps cannot oversell or overspend. A member may have one request open at a time.
  * Status: requested, then the admin marks it shipped (with a note such as the courier and tracking) or rejects it with a reason; a rejection,
    or the member cancelling while it is still requested, gives the points back and puts the item back in stock.
  * Delivery details (name, phone, address) are stored only on the request, shown to admins only, and sent nowhere else.
Members see prizes only when REWARDS_ON=1 and REWARDS_PRIZES_ON=1. Tables live in rewards.SCHEMA (so redeeming is one transaction)."""
import asyncio
import os
import re
import time
from contextlib import closing

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, rewards

STARTERS = [("pen", "TradeLens pen", "A TradeLens branded pen.", 5, 500), ("journal_pen", "Trading journal and pen", "A printed trading journal with a pen.", 10, 1000),
            ("powerbank", "Powerbank", "A portable phone charger.", 50, 5000), ("tablet", "Tablet", "A tablet for charting and trading.", 110, 11000),
            ("phone", "Phone", "A smartphone.", 250, 25000), ("laptop", "Laptop", "A laptop for trading and analysis.", 500, 50000)]
PHONE_RE = re.compile(r"^\+?[0-9 ()\-]{6,24}$")


def prizes_on() -> bool:
    return rewards.enabled() and os.environ.get("REWARDS_PRIZES_ON", "").strip() == "1"


def _now() -> int:
    return int(time.time())


def seed(con):
    """The starting catalogue, once. Never touches prizes that already exist."""
    if con.execute("SELECT COUNT(*) AS n FROM prizes").fetchone()["n"]:
        return
    for i, (key, name, desc, cost, pts) in enumerate(STARTERS):
        con.execute("INSERT INTO prizes(key,name,description,points,cost_usd,stock,active,sort,created_at) VALUES(?,?,?,?,?,0,1,?,?) "
                    "ON CONFLICT(key) DO NOTHING", (key, name, desc, pts, cost, i, _now()))
    con.commit()


def view(uid: int) -> dict:
    if not prizes_on():
        return {"on": False}
    with closing(rewards.db()) as con:
        seed(con)
        items = [{"id": r["id"], "name": r["name"], "description": r["description"], "points": r["points"],
                  "available": r["stock"] is None or r["stock"] > 0} for r in con.execute(
                      "SELECT id,name,description,points,stock FROM prizes WHERE active=1 ORDER BY sort, id").fetchall()]
        mine = [{"id": r["id"], "prize": r["prize_name"], "points": r["points"], "status": r["status"], "note": r["note"], "at": r["created_at"]}
                for r in con.execute("SELECT * FROM redemptions WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,)).fetchall()]
    return {"on": True, "items": items, "mine": mine, "spendable": rewards.balances(uid)["spendable"],
            "open": any(m["status"] == "requested" for m in mine)}


def _clean(d, key, lo, hi, label):
    v = " ".join(str(d.get(key, "")).split())
    if not lo <= len(v) <= hi:
        raise HTTPException(422, f"{label} must be {lo} to {hi} characters")
    return v


def redeem(uid: int, prize_id: int, d: dict) -> dict:
    name = _clean(d, "full_name", 2, 80, "Name")
    phone = _clean(d, "phone", 6, 24, "Phone number")
    if not PHONE_RE.match(phone):
        raise HTTPException(422, "Enter a valid phone number")
    address = _clean(d, "address", 10, 300, "Delivery address")
    with closing(rewards.db()) as con:
        try:
            p = con.execute("SELECT id,name,points FROM prizes WHERE id=? AND active=1", (prize_id,)).fetchone()
            if not p:
                raise HTTPException(404, "That prize is not available")
            if con.execute("SELECT 1 FROM redemptions WHERE user_id=? AND status='requested'", (uid,)).fetchone():
                raise HTTPException(409, "You already have a prize request waiting. Wait for it to be sent, or cancel it first.")
            if con.execute("UPDATE prizes SET stock=CASE WHEN stock IS NULL THEN NULL ELSE stock-1 END WHERE id=? AND active=1 AND (stock IS NULL OR stock>0)",
                           (p["id"],)).rowcount != 1:
                con.raw.rollback()
                raise HTTPException(409, "Sorry, that prize is out of stock")
            if con.execute("UPDATE points_balances SET spendable=spendable-? WHERE user_id=? AND spendable>=?", (p["points"], uid, p["points"])).rowcount != 1:
                con.raw.rollback()
                raise HTTPException(409, "You do not have enough points for this prize yet")
            rid = con.insert("INSERT INTO redemptions(user_id,prize_id,prize_name,points,status,full_name,phone,address,created_at) VALUES(?,?,?,?,'requested',?,?,?,?)",
                             (uid, p["id"], p["name"], p["points"], name, phone, address, _now()))
            con.execute("INSERT INTO points_ledger(user_id,delta,lifetime,kind,ref,day,note,created_at) VALUES(?,?,0,'redeem',?,NULL,?,?)",
                        (uid, -p["points"], f"r{rid}", f"Redeemed: {p['name']}", _now()))
            con.commit()
        except HTTPException:
            raise
        except Exception as e:
            con.raw.rollback()
            if rewards.dbx.is_duplicate(e):
                raise HTTPException(409, "You already have a prize request waiting")
            raise
    return {"ok": True, "id": rid}


def _give_back(con, red, status: str, admin_id, note: str) -> bool:
    """Close a request that is still 'requested': points and stock come back, once."""
    if con.execute("UPDATE redemptions SET status=?, note=?, decided_at=?, admin_id=? WHERE id=? AND status='requested'",
                   (status, note or None, _now(), admin_id, red["id"])).rowcount != 1:
        return False
    con.execute("UPDATE prizes SET stock=CASE WHEN stock IS NULL THEN NULL ELSE stock+1 END WHERE id=?", (red["prize_id"],))
    con.execute("UPDATE points_balances SET spendable=spendable+? WHERE user_id=?", (red["points"], red["user_id"]))
    con.execute("INSERT INTO points_ledger(user_id,delta,lifetime,kind,ref,day,note,created_at) VALUES(?,?,0,'redeem_refund',?,NULL,?,?) "
                "ON CONFLICT(user_id,kind,ref) DO NOTHING", (red["user_id"], red["points"], f"r{red['id']}", f"Points returned: {red['prize_name']}", _now()))
    con.commit()
    return True


# ------------------------------------------------------------------ member API
async def api_redeem(request):
    u = auth.require_user(request)
    if not prizes_on():
        raise HTTPException(404, "Not available")
    d = await auth._json(request)
    try:
        pid = int(d.get("prize_id"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Choose a prize")
    return JSONResponse(await asyncio.to_thread(redeem, u["id"], pid, d))


async def api_cancel(request):
    u = auth.require_user(request)
    rid = request.path_params["rid"]
    with closing(rewards.db()) as con:
        red = con.execute("SELECT * FROM redemptions WHERE id=? AND user_id=?", (rid, u["id"])).fetchone()
        if not red:
            raise HTTPException(404, "Request not found")
        if not _give_back(con, red, "cancelled", None, "Cancelled by you"):
            raise HTTPException(409, "This request can no longer be cancelled")
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------ admin API
def overview() -> dict:
    with closing(rewards.db()) as con:
        seed(con)
        prizes = [dict(r) for r in con.execute("SELECT * FROM prizes ORDER BY sort, id").fetchall()]
        reds = [dict(r) for r in con.execute("SELECT * FROM redemptions ORDER BY CASE status WHEN 'requested' THEN 0 ELSE 1 END, id DESC LIMIT 60").fetchall()]
    with closing(auth.db()) as a:
        ids = list({r["user_id"] for r in reds})
        em = {r["id"]: r["email"] for r in a.execute(f"SELECT id,email FROM users WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()} if ids else {}
    return {"on": prizes_on(), "prizes": [{"id": p["id"], "name": p["name"], "description": p["description"], "points": p["points"], "cost_usd": p["cost_usd"],
                                          "stock": p["stock"], "active": bool(p["active"])} for p in prizes],
            "requests": [{"id": r["id"], "email": em.get(r["user_id"], "?"), "prize": r["prize_name"], "points": r["points"], "status": r["status"],
                          "full_name": r["full_name"], "phone": r["phone"], "address": r["address"], "note": r["note"], "at": r["created_at"]} for r in reds]}


async def admin_view(request):
    auth.require_admin(request)
    return JSONResponse(await asyncio.to_thread(overview))


async def admin_prize(request):
    """Create (no id) or update a prize: name, description, points, cost, stock (empty = unlimited), active."""
    auth.require_admin(request)
    d = await auth._json(request)
    vals = {}
    if "name" in d:
        vals["name"] = _clean(d, "name", 2, 80, "Name")
    if "description" in d:
        vals["description"] = " ".join(str(d.get("description", "")).split())[:300]
    try:
        if "points" in d:
            vals["points"] = int(d["points"])
            if not 1 <= vals["points"] <= 10_000_000:
                raise ValueError
        if "cost_usd" in d:
            vals["cost_usd"] = float(d["cost_usd"])
            if not 0 <= vals["cost_usd"] <= 1_000_000:
                raise ValueError
        if "stock" in d:
            vals["stock"] = None if d["stock"] in (None, "") else int(d["stock"])
            if vals["stock"] is not None and not 0 <= vals["stock"] <= 100000:
                raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(422, "Points must be a whole number above 0, stock a whole number (or empty for unlimited), cost a number")
    if "active" in d:
        vals["active"] = 1 if d["active"] is True else 0
    with closing(rewards.db()) as con:
        seed(con)
        if d.get("id") is None:
            if not {"name", "points"} <= set(vals):
                raise HTTPException(422, "A new prize needs a name and points")
            key = re.sub(r"[^a-z0-9]+", "_", vals["name"].lower()).strip("_")[:40] or "prize"
            while con.execute("SELECT 1 FROM prizes WHERE key=?", (key,)).fetchone():
                key += "_" + str(_now() % 1000)
            pid = con.insert("INSERT INTO prizes(key,name,description,points,cost_usd,stock,active,sort,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                             (key, vals["name"], vals.get("description", ""), vals["points"], vals.get("cost_usd", 0), vals.get("stock", 0), vals.get("active", 1), 100, _now()))
            con.commit()
            return JSONResponse({"ok": True, "id": pid})
        if not vals:
            raise HTTPException(422, "Nothing to change")
        sets = ",".join(f"{k}=?" for k in vals)
        if con.execute(f"UPDATE prizes SET {sets} WHERE id=?", (*vals.values(), int(d["id"]))).rowcount != 1:
            raise HTTPException(404, "Prize not found")
        con.commit()
    return JSONResponse({"ok": True})


async def admin_decide(request):
    admin = auth.require_admin(request)
    d = await auth._json(request)
    try:
        rid = int(d.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Missing request id")
    action, note = str(d.get("action", "")), " ".join(str(d.get("note", "")).split())[:200]
    if action not in ("shipped", "reject"):
        raise HTTPException(422, "action must be shipped or reject")
    if action == "reject" and len(note) < 3:
        raise HTTPException(422, "Give the member a reason")
    with closing(rewards.db()) as con:
        red = con.execute("SELECT * FROM redemptions WHERE id=?", (rid,)).fetchone()
        if not red:
            raise HTTPException(404, "Request not found")
        if action == "shipped":
            ok = con.execute("UPDATE redemptions SET status='shipped', note=?, decided_at=?, admin_id=? WHERE id=? AND status='requested'",
                             (note or None, _now(), admin["id"], rid)).rowcount == 1
            con.commit()
        else:
            ok = _give_back(con, red, "rejected", admin["id"], note)
    if not ok:
        raise HTTPException(409, "This request has already been decided")
    try:
        from . import commission
        commission._notify(red["user_id"], admin["id"], (f"Your prize ({red['prize_name']}) has been sent. {note}" if action == "shipped"
                                                         else f"Your prize request ({red['prize_name']}) could not be fulfilled: {note}. Your points are back."))
    except Exception as e:
        print(f"[prizes] could not message the member: {type(e).__name__}", flush=True)
    return JSONResponse({"ok": True})


routes = [
    Route("/api/rewards/redeem", api_redeem, methods=["POST"]),
    Route("/api/rewards/redeem/{rid:int}/cancel", api_cancel, methods=["POST"]),
    Route("/api/admin/prizes", admin_view, methods=["GET"]),
    Route("/api/admin/prizes/save", admin_prize, methods=["POST"]),
    Route("/api/admin/prizes/decide", admin_decide, methods=["POST"]),
]
