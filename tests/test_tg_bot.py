"""Tests for the @TradeLensagent navigation keyboard (app/tg_bot.py) and its one-off webhook helper (scripts/telegram_webhook.py).
No network and no real bot. Run: python3 tests/test_tg_bot.py"""
import json
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import httpx  # noqa: F401
except ImportError:
    import httpx2 as httpx
    sys.modules["httpx"] = httpx

import os   # noqa: E402
from starlette.applications import Starlette   # noqa: E402
from starlette.testclient import TestClient    # noqa: E402
from app import tg_bot   # noqa: E402
import telegram_webhook as hook   # noqa: E402

SECRET = "test_secret_value_0123456789"
H = {"x-telegram-bot-api-secret-token": SECRET}
APP = TestClient(Starlette(routes=tg_bot.routes))
_n = [1000]


def upd(text, chat_type="private", is_bot=False, chat_id=42, key="message", **extra):
    _n[0] += 1
    return {"update_id": _n[0], key: {"message_id": 1, "from": {"id": chat_id, "is_bot": is_bot}, "chat": {"id": chat_id, "type": chat_type}, "text": text, **extra}}


def post(update, headers=H):
    return APP.post("/api/telegram/webhook", json=update, headers=headers)


class Base(unittest.TestCase):
    def setUp(self):
        os.environ["TELEGRAM_WEBHOOK_SECRET"] = SECRET
        tg_bot._seen.clear()


class KeyboardTests(Base):
    def test_start_sends_the_welcome_and_the_exact_persistent_two_by_two_keyboard(self):
        r = post(upd("/start"))
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertEqual((b["method"], b["chat_id"]), ("sendMessage", 42))
        self.assertIn("Welcome to TradeLens", b["text"])
        for word in ("AI-powered", "learning resources", "official channel", "traders community"):
            self.assertIn(word, b["text"])
        self.assertEqual(b["reply_markup"], {
            "keyboard": [[{"text": "\U0001F4CA Open TradeLens App"}, {"text": "\U0001F393 Open TradeLens Resources"}],
                         [{"text": "\U0001F4E2 Subscribe to TradeLens Channel"}, {"text": "\U0001F465 Join TradeLens Traders Hub"}]],
            "is_persistent": True, "resize_keyboard": True, "one_time_keyboard": False})

    def test_start_with_a_deep_link_payload_and_help_also_show_it(self):
        for t in ("/start abc123", "/help", "/menu", "  /start  "):
            self.assertEqual(post(upd(t)).json().get("reply_markup", {}).get("is_persistent"), True, t)

    def test_each_button_replies_with_one_inline_url_button_to_the_exact_destination(self):
        expected = {
            "\U0001F4CA Open TradeLens App": "https://tradelens360ai.live/login.html?utm_source=Telegram&utm_medium=paid&utm_campaign=launch&utm_content=hook-a",
            "\U0001F393 Open TradeLens Resources": "https://t.me/TradeLensResources",
            "\U0001F4E2 Subscribe to TradeLens Channel": "https://t.me/+RgeOG2GryJM3NmI0",
            "\U0001F465 Join TradeLens Traders Hub": "https://t.me/+e-cf-gPTZG0xNjk0"}
        for label, url in expected.items():
            b = post(upd(label)).json()
            self.assertEqual(b["method"], "sendMessage", label)
            self.assertEqual(b["reply_markup"], {"inline_keyboard": [[{"text": label, "url": url}]]}, label)
            self.assertNotIn("Welcome", b["text"])                                   # no welcome message every time a button is pressed
            self.assertLess(len(b["text"]), 90)
        self.assertEqual(len(expected), 4)

    def test_the_reply_keyboard_is_never_removed(self):
        for label in tg_bot.ACTIONS:
            self.assertNotIn("remove_keyboard", json.dumps(post(upd(label)).json()))

    def test_the_app_url_keeps_every_utm_parameter(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(tg_bot.APP_URL).query)
        self.assertEqual({k: v[0] for k, v in q.items()}, {"utm_source": "Telegram", "utm_medium": "paid", "utm_campaign": "launch", "utm_content": "hook-a"})
        self.assertTrue(tg_bot.APP_URL.startswith("https://tradelens360ai.live/login.html?"))

    def test_only_exact_button_text_counts(self):
        for t in ("open tradelens app", "\U0001F4CA Open TradeLens App please", "Open TradeLens App", "hello", "", "/unknown"):
            self.assertEqual(post(upd(t)).json(), {}, t)

    def test_groups_channels_bots_edits_and_non_text_are_ignored(self):
        self.assertEqual(post(upd("/start", chat_type="supergroup", chat_id=-100)).json(), {})
        self.assertEqual(post(upd("/start", chat_type="channel", chat_id=-101)).json(), {})
        self.assertEqual(post(upd("/start", is_bot=True)).json(), {})
        self.assertEqual(post(upd("/start", key="edited_message")).json(), {})
        self.assertEqual(post(upd("/start", key="channel_post")).json(), {})
        u = upd("x")
        del u["message"]["text"]
        self.assertEqual(post(u).json(), {})
        self.assertEqual(post({"update_id": 5, "callback_query": {}}).json(), {})

    def test_a_retried_update_is_answered_only_once(self):
        u = upd("\U0001F4CA Open TradeLens App")
        self.assertEqual(post(u).json()["method"], "sendMessage")
        self.assertEqual(post(u).json(), {})
        self.assertEqual(post(upd("/start")).json()["method"], "sendMessage")        # a new update is fine


class SecurityTests(Base):
    def test_without_the_secret_setting_the_route_does_not_exist(self):
        os.environ.pop("TELEGRAM_WEBHOOK_SECRET")
        self.assertEqual(post(upd("/start")).status_code, 404)

    def test_wrong_or_missing_secret_is_refused_and_nothing_is_answered(self):
        for h in ({}, {"x-telegram-bot-api-secret-token": "nope"}, {"x-telegram-bot-api-secret-token": SECRET + "x"}, {"x-telegram-bot-api-secret-token": ""}):
            r = post(upd("/start"), headers=h)
            self.assertEqual(r.status_code, 403)
            self.assertNotIn("method", r.text)

    def test_bad_bodies_never_cause_an_error_telegram_would_retry(self):
        for body in (b"not json", b"[]", b"null", b'{"update_id": "x"}'):
            r = APP.post("/api/telegram/webhook", content=body, headers={**H, "content-type": "application/json"})
            self.assertEqual((r.status_code, r.json()), (200, {}), body)
        self.assertEqual(APP.get("/api/telegram/webhook").status_code, 405)

    def test_the_server_code_never_touches_the_bot_token_or_stores_anything(self):
        src = (ROOT / "app" / "tg_bot.py").read_text()
        for bad in ("TELEGRAM_BOT_TOKEN", "api.telegram.org", "getUpdates", "sqlite", "INSERT", "open("):
            self.assertNotIn(bad, src, bad)
        main = (ROOT / "app" / "main.py").read_text()
        self.assertIn("*tg_bot.routes", main)

    def test_the_trade_signal_sender_is_untouched(self):
        mq5 = (ROOT / "DriverlineCompanion.mq5").read_text()
        self.assertIn("/sendMessage", mq5)
        self.assertNotIn("getUpdates", mq5)                                         # the EA never consumes updates, so a webhook cannot compete with it


class FakeTg:
    def __init__(self):
        self.url, self.calls = "", []


FAKE = FakeTg()
TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnop"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        p = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
        method = self.path.split("/")[-1]
        FAKE.calls.append((method, p))
        res = {"getWebhookInfo": {"url": FAKE.url, "pending_update_count": 3}, "setWebhook": True, "deleteWebhook": True}[method]
        if method == "setWebhook":
            FAKE.url = p["url"]
        raw = json.dumps({"ok": True, "result": res}).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


SRV = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=SRV.serve_forever, daemon=True).start()


def tearDownModule():
    SRV.shutdown()


def run(*argv, env=None):
    out = []
    e = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_WEBHOOK_SECRET": SECRET, "TELEGRAM_API_BASE": f"http://127.0.0.1:{SRV.server_port}"}
    e.update(env or {})
    code = hook.main(list(argv), env=e, out=out.append)
    return code, "\n".join(out)


class WebhookHelperTests(unittest.TestCase):
    def setUp(self):
        FAKE.url, FAKE.calls = "", []

    def test_info_is_read_only(self):
        code, out = run("info")
        self.assertEqual(code, 0)
        self.assertIn("none", out)
        self.assertEqual([c[0] for c in FAKE.calls], ["getWebhookInfo"])

    def test_set_registers_our_url_with_the_secret_and_only_message_updates(self):
        code, out = run("set")
        self.assertEqual(code, 0, out)
        (_, p), = [c for c in FAKE.calls if c[0] == "setWebhook"]
        self.assertEqual((p["url"], p["secret_token"], json.loads(p["allowed_updates"]), p["drop_pending_updates"]),
                         ("https://tradelens360ai.live/api/telegram/webhook", SECRET, ["message"], "false"))
        self.assertNotIn(SECRET, out + TOKEN.join(out))
        self.assertNotIn(TOKEN, out)

    def test_an_existing_different_webhook_is_not_replaced_without_permission(self):
        FAKE.url = "https://somewhere-else.example/hook"
        code, out = run("set")
        self.assertEqual(code, 1)
        self.assertIn("not replacing", out.lower())
        self.assertNotIn("setWebhook", [c[0] for c in FAKE.calls])
        self.assertEqual(run("set", "--replace")[0], 0)

    def test_inputs_are_validated_and_delete_works(self):
        self.assertEqual(run("set", env={"TELEGRAM_WEBHOOK_SECRET": "short"})[0], 1)
        self.assertEqual(run("set", env={"TELEGRAM_BOT_TOKEN": "bad"})[0], 1)
        self.assertEqual(run("set", "--url", "http://x.example/h")[0], 1)
        self.assertEqual([c for c in FAKE.calls if c[0] == "setWebhook"], [])
        self.assertEqual(run("delete")[0], 0)
        code, out = run("secret")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^[A-Za-z0-9_-]{32,}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
