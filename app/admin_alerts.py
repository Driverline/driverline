"""Alerts for the admins: something happened that an admin has to act on.

  * payment        a member says they paid (manual USDT, Naira transfer) or a NOWPayments payment was credited
  * mt5_request    a member sent MT5 details for support to connect
  * copy_on        a member switched Copy trading on
  * auto_on        a member switched AutoTrading on
  * partner        a member started or submitted a Deriv partner application

Each alert is one row (shown in Admin > Messages, with an unread badge) and a push notification to every admin device that has push
switched on. Both are best effort: notify() NEVER raises, so a problem here can never break a payment, a request or an application.
A repeat of the same event (same kind and ref) is ignored, so a double click or a webhook retry does not alert twice.
No passwords, keys or MT5 details are ever put in an alert: only who, what and where to go.
Additive: own table, nothing existing is edited. Mirrored by migrations/2026_11_admin_alerts.sql."""
import threading
import time
from contextlib import closing

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, dbx

SCHEMA = """
    CREATE TABLE IF NOT EXISTS admin_alerts(id {ID}, kind TEXT NOT NULL, user_id INTEGER, title TEXT NOT NULL, body TEXT NOT NULL,
        link TEXT, ref TEXT, created_at BIGINT NOT NULL, read_at BIGINT);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_admin_alert_ref ON admin_alerts(kind, ref);
    CREATE INDEX IF NOT EXISTS ix_admin_alert_unread ON admin_alerts(read_at, id)"""

KINDS = {"payment": "Payment", "mt5_request": "MT5 details", "copy_on": "Copy trading", "auto_on": "AutoTrading", "partner": "Partner"}
PAGE = 30
KEEP_DAYS = 90


def db():
    auth.db().close()
    return dbx.connect("admin_alerts", SCHEMA)


def _now() -> int:
    return int(time.time())


def _who(user_id) -> str:
    try:
        with closing(auth.db()) as con:
            r = con.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
        return r["email"] if r else f"member {user_id}"
    except Exception:
        return f"member {user_id}"


def _admin_ids() -> list:
    with closing(auth.db()) as con:
        return [r["id"] for r in con.execute("SELECT id FROM users WHERE tier='admin' AND blocked=0").fetchall()]


def _push(alert_id: int, title: str, body: str, link: str, sender=None):
    """Push to every admin device. Dead subscriptions are removed. Never raises."""
    try:
        from . import push
        sender = sender or push._send
        ids = _admin_ids()
        if not ids:
            return 0
        with closing(push.db()) as con:
            subs = [dict(s) for s in con.execute(
                f"SELECT * FROM push_subs WHERE user_id IN ({','.join('?' * len(ids))})", ids).fetchall()]
        sent, gone = 0, []
        for sub in subs:
            try:
                if sender(sub, {"title": title, "body": body, "url": link or "/admin-messages.html", "tag": f"admin-alert-{alert_id}"}):
                    sent += 1
                else:
                    gone.append(sub["id"])
            except Exception as e:
                print(f"[admin-alerts] push failed: {type(e).__name__}", flush=True)
        if gone:
            with closing(push.db()) as con:
                for i in gone:
                    con.execute("DELETE FROM push_subs WHERE id=?", (i,))
                con.commit()
        return sent
    except Exception as e:
        print(f"[admin-alerts] push error: {type(e).__name__}", flush=True)
        return 0


def notify(kind: str, title: str, body: str, user_id=None, ref=None, link="/admin-messages.html", sender=None, wait=False):
    """Record one alert and push it. Returns the alert id, or None (a repeat, or a failure). Never raises."""
    try:
        title, body = str(title)[:120], str(body)[:400]
        with closing(db()) as con:
            if ref is not None and con.execute("SELECT 1 FROM admin_alerts WHERE kind=? AND ref=?", (kind, str(ref))).fetchone():
                return None
            try:
                aid = con.insert("INSERT INTO admin_alerts(kind,user_id,title,body,link,ref,created_at) VALUES(?,?,?,?,?,?,?)",
                                 (kind, user_id, title, body, link, None if ref is None else str(ref), _now()))
                con.commit()
            except Exception as e:
                if dbx.is_duplicate(e):          # two requests raced: the other one already alerted
                    con.raw.rollback()
                    return None
                raise
        t = threading.Thread(target=_push, args=(aid, title, body, link, sender), daemon=True)
        t.start()
        if wait:
            t.join(10)
        return aid
    except Exception as e:
        print(f"[admin-alerts] could not record an alert: {type(e).__name__}", flush=True)
        return None


# ---- the events (called from the code that handles them; each is one short line there)
def payment(user_id, product, amount, how, order_ref):
    notify("payment", f"Payment: {how}", f"{_who(user_id)} · {product} · ${amount:g} · ref {order_ref}", user_id, f"{how}:{order_ref}",
           "/admin-payments.html")


def mt5_request(user_id, request_id, login):
    notify("mt5_request", "MT5 details sent", f"{_who(user_id)} sent MT5 account {login} for setup", user_id, request_id, "/admin-mt5.html")


def feature_on(kind, user_id, what, key):
    try:                                                  # an admin trying their own account is not news
        with closing(auth.db()) as con:
            r = con.execute("SELECT tier FROM users WHERE id=?", (user_id,)).fetchone()
        if r and r["tier"] == "admin":
            return
    except Exception:
        pass
    notify(kind, f"{what} switched on", f"{_who(user_id)} switched {what} on", user_id, key,
           "/admin-copy.html")


def partner(user_id, partner_code, stage):
    notify("partner", "Partner application " + ("started" if stage == "started" else "submitted"),
           f"{_who(user_id)} · {partner_code} · needs checking in Partners Hub", user_id, f"{partner_code}:{stage}:{_now() // 3600}",
           "/admin-partners.html")


# ---- admin API
def unread_count() -> int:
    with closing(db()) as con:
        return con.execute("SELECT COUNT(*) AS c FROM admin_alerts WHERE read_at IS NULL").fetchone()["c"]


def _row(r) -> dict:
    return {"id": r["id"], "kind": r["kind"], "label": KINDS.get(r["kind"], r["kind"]), "title": r["title"], "body": r["body"],
            "link": r["link"], "created_at": r["created_at"], "read": r["read_at"] is not None}


async def admin_list(request):
    auth.require_admin(request)
    try:
        before = int(request.query_params.get("before") or 0)
    except ValueError:
        before = 0
    with closing(db()) as con:
        con.execute("DELETE FROM admin_alerts WHERE created_at<? AND read_at IS NOT NULL", (_now() - KEEP_DAYS * 86400,))
        con.commit()
        if before:
            rows = con.execute("SELECT * FROM admin_alerts WHERE id<? ORDER BY id DESC LIMIT ?", (before, PAGE + 1)).fetchall()
        else:
            rows = con.execute("SELECT * FROM admin_alerts ORDER BY id DESC LIMIT ?", (PAGE + 1,)).fetchall()
        unread = con.execute("SELECT COUNT(*) AS c FROM admin_alerts WHERE read_at IS NULL").fetchone()["c"]
    more = len(rows) > PAGE
    rows = list(reversed(rows[:PAGE]))                     # oldest first, newest last, like a chat
    return JSONResponse({"alerts": [_row(r) for r in rows], "has_more": more, "unread": unread})


async def admin_read(request):
    auth.require_admin(request)
    d = await auth._json(request)
    with closing(db()) as con:
        if d.get("all") is True:
            con.execute("UPDATE admin_alerts SET read_at=? WHERE read_at IS NULL", (_now(),))
        else:
            ids = d.get("ids")
            if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids) or len(ids) > 200:
                raise HTTPException(422, "Send the ids to mark as read, or all: true")
            for i in ids:
                con.execute("UPDATE admin_alerts SET read_at=? WHERE id=? AND read_at IS NULL", (_now(), i))
        con.commit()
    return JSONResponse({"ok": True, "unread": unread_count()})


routes = [
    Route("/api/admin/alerts", admin_list, methods=["GET"]),
    Route("/api/admin/alerts/read", admin_read, methods=["POST"]),
]
