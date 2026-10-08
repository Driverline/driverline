"""Web push notifications for paid members. Keys are created once and kept in the database, so no secret has to be
pasted anywhere. Needs the `pywebpush` package (installed on the host, not needed on the phone)."""
import base64
import json
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, dbx

SCHEMA = """
    CREATE TABLE IF NOT EXISTS push_subs(id {ID}, user_id INTEGER NOT NULL, endpoint TEXT UNIQUE NOT NULL,
        p256dh TEXT NOT NULL, auth TEXT NOT NULL, created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)"""


def db():
    auth.db().close()
    return dbx.connect("push", SCHEMA)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def keys() -> dict:
    """The server's VAPID key pair (created on first use)."""
    with closing(db()) as con:
        rows = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM app_settings WHERE key IN ('vapid_public','vapid_private')").fetchall()}
        if len(rows) == 2:
            return {"public": rows["vapid_public"], "private": rows["vapid_private"]}
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        priv = ec.generate_private_key(ec.SECP256R1())
        pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        pair = {"public": _b64(pub), "private": _b64(priv.private_numbers().private_value.to_bytes(32, "big"))}
        for k, v in (("vapid_public", pair["public"]), ("vapid_private", pair["private"])):
            con.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING", (k, v))
        con.commit()
        return pair


def _send(sub: dict, payload: dict) -> bool:
    """True if delivered, False if the subscription is gone. Raises on temporary trouble."""
    from pywebpush import WebPushException, webpush
    try:
        webpush({"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                data=json.dumps(payload), vapid_private_key=keys()["private"],
                vapid_claims={"sub": "mailto:" + (__import__("os").environ.get("DRIVERLINE_ADMIN_EMAIL") or "admin@example.com")})
        return True
    except WebPushException as e:
        if getattr(e.response, "status_code", 0) in (404, 410):
            return False
        raise


def _quiet(p: dict, now: datetime) -> bool:
    qs, qe = p.get("quiet_start"), p.get("quiet_end")
    if qs is None or qe is None or qs == qe:
        return False
    h = (now + timedelta(minutes=p.get("tz_offset") or 0)).hour
    return (qs <= h < qe) if qs < qe else (h >= qs or h < qe)


def notify_new(signal_ids: list, sender=None):
    """Push each new signal to paid members who asked for it, who are not paused, and who are inside their caps."""
    from . import signal_feed as F
    db().close()   # makes sure the subscription table exists
    sender, sent = sender or _send, 0
    now = datetime.now(timezone.utc)
    with closing(F.db()) as con:
        subs = con.execute("SELECT s.*, u.tier FROM push_subs s JOIN users u ON u.id=s.user_id WHERE u.blocked=0 "
                           "AND u.tier IN ('member','admin')").fetchall()
        for sid in signal_ids:
            sig = con.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone()
            if not sig:
                continue
            for sub in [dict(s) for s in subs]:
                uid = sub["user_id"]
                p = F._prefs(con, uid)
                if sig["style"] not in (p.get("notify_styles") or "").split(",") or _quiet(p, now):
                    continue
                if F.budget(con, {"id": uid, "tier": sub["tier"]})["paused"]:
                    continue                      # the member's own daily limit is reached: no more alerts
                today = F._today()
                done = con.execute("SELECT COUNT(*) AS c FROM signal_notifs WHERE user_id=? AND day=?", (uid, today)).fetchone()["c"]
                if done >= p["limit"] or con.execute("SELECT 1 FROM signal_notifs WHERE user_id=? AND signal_id=?", (uid, sid)).fetchone():
                    continue
                name = F.INSTRUMENTS.get(sig["instrument"], ("", sig["instrument"]))[1]
                body = {"title": f"{sig['style'].capitalize()} setup: {name}",
                        "body": f"{'BUY' if sig['direction'] > 0 else 'SELL'} setup, risk {sig['risk']}. Open TradeLens to see the levels. Track and close your trades; never leave them unattended.",
                        "url": "/signals.html", "tag": f"sig-{sid}"}
                try:
                    alive = sender(sub, body)
                except Exception as e:
                    print(f"[push] send failed: {type(e).__name__}", flush=True)
                    continue
                if not alive:
                    con.execute("DELETE FROM push_subs WHERE id=?", (sub["id"],))
                    continue
                con.execute("INSERT INTO signal_notifs(user_id,signal_id,day) VALUES(?,?,?)", (uid, sid, today))
                con.commit()
                sent += 1
    return sent


# ---------------------------------------------------------------- API
async def public_key(request):
    auth.require_user(request)
    try:
        return JSONResponse({"key": keys()["public"]})
    except Exception as e:
        print(f"[push] keys unavailable: {type(e).__name__}", flush=True)
        raise HTTPException(503, "Notifications are not available on this server yet")


async def subscribe(request):
    user = auth.require_user(request)
    if user["tier"] not in ("member", "admin"):
        raise HTTPException(403, "Notifications are for paid members")
    d = await auth._json(request)
    k = d.get("keys") or {}
    if not (isinstance(d.get("endpoint"), str) and k.get("p256dh") and k.get("auth")):
        raise HTTPException(422, "Invalid subscription")
    with closing(db()) as con:
        con.execute("INSERT INTO push_subs(user_id,endpoint,p256dh,auth,created_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, p256dh=excluded.p256dh, auth=excluded.auth",
                    (user["id"], d["endpoint"], k["p256dh"], k["auth"], int(time.time())))
        con.commit()
    return JSONResponse({"ok": True})


async def unsubscribe(request):
    user = auth.require_user(request)
    d = await auth._json(request)
    with closing(db()) as con:
        con.execute("DELETE FROM push_subs WHERE user_id=? AND endpoint=?", (user["id"], str(d.get("endpoint", ""))))
        con.commit()
    return JSONResponse({"ok": True})


async def test(request):
    user = auth.require_user(request)
    with closing(db()) as con:
        subs = [dict(r) for r in con.execute("SELECT * FROM push_subs WHERE user_id=?", (user["id"],)).fetchall()]
    if not subs:
        raise HTTPException(409, "This device is not subscribed yet")
    ok = 0
    for s in subs:
        try:
            ok += bool(_send(s, {"title": "TradeLens notifications are on",
                                 "body": "You will be alerted about setups you chose, within your daily limit.", "url": "/signals.html", "tag": "test"}))
        except Exception as e:
            print(f"[push] test failed: {type(e).__name__}", flush=True)
    if not ok:
        raise HTTPException(502, "Could not deliver the test notification")
    return JSONResponse({"ok": True})


routes = [
    Route("/api/push/key", public_key, methods=["GET"]),
    Route("/api/push/subscribe", subscribe, methods=["POST"]),
    Route("/api/push/unsubscribe", unsubscribe, methods=["POST"]),
    Route("/api/push/test", test, methods=["POST"]),
]
