"""Bot commands /app /status /unlink (app/tg_bot.py) - additive: nothing changes unless Connect is on. Run: python3 tests/test_connect_bot.py"""
import os
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.update(DRIVERLINE_SECRET="s" * 40)
os.environ.pop("DATABASE_URL", None)

from app import auth, dbx                                          # noqa: E402

dbx.SQLITE_PATH = Path(tempfile.mkdtemp()) / "t.db"
from app import connect, tg_bot                                    # noqa: E402

URL = "https://example.test/tg/"
_n = [0]


def upd(text, uid=900):
    return {"update_id": 1, "message": {"message_id": 1, "from": {"id": uid, "is_bot": False}, "chat": {"id": uid, "type": "private"}, "text": text}}


def linked(uid):
    _n[0] += 1
    with closing(auth.db()) as con:
        u = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)", (f"b{_n[0]}@t.example", "h", "free", "2026-01-01"))
        con.commit()
    with closing(connect.db()) as con:
        con.insert("INSERT INTO telegram_links(user_id,telegram_id,telegram_username,source,linked_at,last_seen_at) VALUES(?,?,?,?,?,?)",
                   (u, uid, "x", "bot", int(time.time()), int(time.time())))
        con.commit()


class BotConnectTests(unittest.TestCase):
    def on(self):
        p = patch.dict(os.environ, {"CONNECT_SECRET": "c" * 40, "CONNECT_MINIAPP_URL": URL})
        p.start()
        self.addCleanup(p.stop)

    def test_commands_do_nothing_while_connect_is_off(self):
        os.environ.pop("CONNECT_SECRET", None)
        for t in ("/app", "/status", "/unlink", "/unlink confirm"):
            self.assertIsNone(tg_bot.respond(upd(t)), t)

    def test_start_is_unchanged_when_connect_is_on(self):
        self.on()
        r = tg_bot.respond(upd("/start"))
        self.assertEqual((r[2], r[3]), (tg_bot.WELCOME, tg_bot.KEYBOARD))

    def test_app_gives_a_web_app_button(self):
        self.on()
        r = tg_bot.respond(upd("/app"))
        self.assertEqual(r[3], {"inline_keyboard": [[{"text": "Open TradeLens Connect", "web_app": {"url": URL}}]]})

    def test_app_needs_an_https_url(self):
        self.on()
        os.environ["CONNECT_MINIAPP_URL"] = "http://insecure"
        self.assertIsNone(tg_bot.respond(upd("/app")))

    def test_status_unlinked_and_linked(self):
        self.on()
        self.assertIn("not linked", tg_bot.respond(upd("/status", 901))[2])
        linked(902)
        t = tg_bot.respond(upd("/status", 902))[2]
        self.assertIn("Linked to TradeLens", t)
        self.assertIn("***@", t)

    def test_unlink_needs_confirm(self):
        self.on()
        linked(903)
        self.assertIn("/unlink confirm", tg_bot.respond(upd("/unlink", 903))[2])
        self.assertIsNotNone(connect.link_status(903))
        self.assertIn("Unlinked", tg_bot.respond(upd("/unlink confirm", 903))[2])
        self.assertIsNone(connect.link_status(903))

    def test_the_bot_file_still_obeys_the_old_rules(self):
        src = (ROOT / "app" / "tg_bot.py").read_text()
        for bad in ("TELEGRAM_BOT_TOKEN", "api.telegram.org", "getUpdates", "sqlite", "INSERT", "open("):
            self.assertNotIn(bad, src, bad)


if __name__ == "__main__":
    unittest.main()
