"""Signal alerts in Telegram for members who linked TradeLens Connect.

OFF unless CONNECT_ALERTS=1 (and Connect itself is on). It runs right AFTER the existing web-push alerts, with the same rules:
the member must be entitled to notifications, must have chosen alert styles, must be outside quiet hours, must not have reached
their daily limit, and gets at most one alert per signal. The existing signal_notifs table is shared, so a member whose web push
already went out is skipped, and one without a working web push gets the Telegram message instead. The text never contains
trade levels; it points back to TradeLens, exactly like the web push. Nothing here changes any existing function or table.
"""
import os
from contextlib import closing
from datetime import datetime, timezone

import httpx


def enabled() -> bool:
    from . import connect
    return os.environ.get("CONNECT_ALERTS") == "1" and bool(os.environ.get("CONNECT_SECRET")) and bool(connect._bot_token())


def _send(chat_id: int, text: str) -> bool:
    """True when Telegram accepted the message. A member who blocked the bot is simply skipped. The URL (it holds the token) is never logged."""
    from . import connect
    try:
        r = httpx.post(f"https://api.telegram.org/bot{connect._bot_token()}/sendMessage",
                       json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}, timeout=10.0)
    except httpx.HTTPError as e:
        print(f"[connect-alerts] send failed: {type(e).__name__}", flush=True)
        return False
    return r.status_code == 200


def notify_new(signal_ids: list, sender=None) -> int:
    from . import connect, credits, push
    from . import signal_feed as F
    connect.db().close()      # makes sure the link table exists
    sender, sent = sender or _send, 0
    now = datetime.now(timezone.utc)
    with closing(F.db()) as con:
        links = [dict(r) for r in con.execute(
            "SELECT l.user_id, l.telegram_id, u.tier FROM telegram_links l JOIN users u ON u.id=l.user_id "
            "WHERE l.revoked_at IS NULL AND u.blocked=0").fetchall()]
        for sid in signal_ids:
            sig = con.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone()
            if not sig:
                continue
            for link in links:
                uid = link["user_id"]
                user = {"id": uid, "tier": link["tier"]}
                if not credits.entitled(user, "notify"):
                    continue
                p = F._prefs(con, uid)
                if sig["style"] not in (p.get("notify_styles") or "").split(",") or push._quiet(p, now):
                    continue
                if F.budget(con, user)["paused"]:
                    continue
                today = F._today()
                done = con.execute("SELECT COUNT(*) AS c FROM signal_notifs WHERE user_id=? AND day=?", (uid, today)).fetchone()["c"]
                if done >= p["limit"] or con.execute("SELECT 1 FROM signal_notifs WHERE user_id=? AND signal_id=?", (uid, sid)).fetchone():
                    continue
                name = F.INSTRUMENTS.get(sig["instrument"], ("", sig["instrument"]))[1]
                text = (f"{sig['style'].capitalize()} setup: {name}\n"
                        f"{'BUY' if sig['direction'] > 0 else 'SELL'} setup, risk {sig['risk']}. Open TradeLens to see the levels. "
                        "Track and close your trades; never leave them unattended.")
                try:
                    ok = sender(link["telegram_id"], text)
                except Exception as e:
                    print(f"[connect-alerts] send failed: {type(e).__name__}", flush=True)
                    continue
                if not ok:
                    continue
                con.execute("INSERT INTO signal_notifs(user_id,signal_id,day) VALUES(?,?,?)", (uid, sid, today))
                con.commit()
                sent += 1
    return sent
