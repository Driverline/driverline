"""Tests for scripts/telegram_access_button.py against a fake Telegram Bot API (no network, no real bot, no token). Run: python3 tests/test_telegram_access.py"""
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import telegram_access_button as tab   # noqa: E402

TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnop"
CH, GR, BOT = -1001, -1002, 777


class Fake:
    """A tiny Telegram: one channel linked to one group, message storage, and per-chat bot rights."""
    def __init__(self):
        self.msgs = {CH: {}, GR: {}}
        self.pinned = {CH: None, GR: None}
        self.next = 100
        self.rights = {CH: {"status": "administrator", "can_post_messages": True, "can_edit_messages": True},
                       GR: {"status": "administrator", "can_pin_messages": True}}
        self.linked = GR
        self.log = []

    def handle(self, method, p):
        self.log.append(method)
        chat = int(p["chat_id"]) if p.get("chat_id", "").lstrip("-").isdigit() else {"@chan": CH, "@grp": GR}.get(p.get("chat_id"))
        if method == "getMe":
            return {"id": BOT, "username": "TradeLensagent"}
        if chat is None:
            raise KeyError((400, "Bad Request: chat not found"))
        if method == "getChat":
            r = {"id": chat, "type": "channel" if chat == CH else "supergroup", "title": "Channel" if chat == CH else "Group"}
            if chat == CH and self.linked:
                r["linked_chat_id"] = self.linked
            if self.pinned[chat]:
                r["pinned_message"] = self.msgs[chat][self.pinned[chat]]
            return r
        if method == "getChatMember":
            return {"user": {"id": BOT}, **self.rights[chat]}
        if method == "sendMessage":
            if chat == CH and not self.rights[CH].get("can_post_messages") and self.rights[CH]["status"] != "creator":
                raise KeyError((403, "Forbidden: not enough rights to send messages"))
            self.next += 1
            m = {"message_id": self.next, "text": p["text"], "reply_markup": json.loads(p["reply_markup"])}
            self.msgs[chat][self.next] = m
            return m
        if method == "editMessageText":
            m = self.msgs[chat].get(int(p["message_id"]))
            if not m:
                raise KeyError((400, "Bad Request: message to edit not found"))
            new = {"message_id": m["message_id"], "text": p["text"], "reply_markup": json.loads(p["reply_markup"])}
            if new == m:
                raise KeyError((400, "Bad Request: message is not modified: specified new message content and reply markup are exactly the same"))
            self.msgs[chat][m["message_id"]] = new
            return new
        if method == "pinChatMessage":
            need = "can_edit_messages" if chat == CH else "can_pin_messages"
            if not self.rights[chat].get(need) and self.rights[chat]["status"] != "creator":
                raise KeyError((400, "Bad Request: not enough rights to pin a message"))
            self.pinned[chat] = int(p["message_id"])
            return True
        raise KeyError((404, "Not Found"))


FAKE = Fake()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        p = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
        method = self.path.split("/")[-1]
        if self.path.split("/")[1] != "bot" + TOKEN:
            code, body = 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
        else:
            try:
                code, body = 200, {"ok": True, "result": FAKE.handle(method, p)}
            except KeyError as e:
                c, d = e.args[0]
                code, body = c, {"ok": False, "error_code": c, "description": d}
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


SRV = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=SRV.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{SRV.server_port}"


def tearDownModule():
    SRV.shutdown()


def run(*argv, token=TOKEN, state=None):
    out = []
    code = tab.main(["--state", str(state)] + list(argv), env={"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_API_BASE": BASE}, out=out.append)
    return code, "\n".join(out)


class Base(unittest.TestCase):
    def setUp(self):
        global FAKE
        FAKE.__init__()
        self.state = Path(tempfile.mkdtemp()) / "state.json"


class AccessButtonTests(Base):
    def test_check_only_posts_nothing(self):
        code, out = run("--channel", "@chan", state=self.state)
        self.assertEqual(code, 0, out)
        self.assertEqual((FAKE.msgs[CH], FAKE.msgs[GR]), ({}, {}))
        self.assertNotIn("sendMessage", FAKE.log)
        self.assertIn("CHECK ONLY", out)
        self.assertIn("would publish", out)
        self.assertFalse(self.state.exists())

    def test_apply_publishes_the_exact_message_and_button_in_both_places_and_pins(self):
        code, out = run("--channel", "@chan", "--apply", state=self.state)
        self.assertEqual(code, 0, out)
        for chat in (CH, GR):
            (m,) = FAKE.msgs[chat].values()
            self.assertEqual(m["text"], "Welcome to TradeLens!\n\nAccess our AI-powered market analysis platform to explore market insights and potential trade setups.")
            self.assertEqual(m["reply_markup"], {"inline_keyboard": [[{"text": "\U0001F4CA Open TradeLens App", "url": "https://tradelens360ai.live/"}]]})
            self.assertEqual(FAKE.pinned[chat], m["message_id"])
        st = json.loads(self.state.read_text())
        self.assertEqual({int(k) for k in st}, {CH, GR})
        self.assertNotIn(TOKEN, self.state.read_text() + out)                     # the token is never stored or printed

    def test_running_again_never_duplicates(self):
        run("--channel", "@chan", "--apply", state=self.state)
        for _ in range(3):
            code, out = run("--channel", "@chan", "--apply", state=self.state)
            self.assertEqual(code, 0, out)
        self.assertEqual((len(FAKE.msgs[CH]), len(FAKE.msgs[GR])), (1, 1))
        self.assertEqual(FAKE.log.count("sendMessage"), 2)

    def test_lost_state_file_adopts_the_pinned_message_instead_of_duplicating(self):
        run("--channel", "@chan", "--apply", state=self.state)
        self.state.unlink()
        code, out = run("--channel", "@chan", "--apply", state=self.state)
        self.assertEqual(code, 0, out)
        self.assertEqual((len(FAKE.msgs[CH]), len(FAKE.msgs[GR])), (1, 1))
        self.assertIn("already pinned", out)
        self.assertEqual(len(json.loads(self.state.read_text())), 2)

    def test_deleted_message_is_published_again_once(self):
        run("--channel", "@chan", "--apply", state=self.state)
        FAKE.msgs[CH].clear()
        FAKE.pinned[CH] = None
        code, out = run("--channel", "@chan", "--apply", state=self.state)
        self.assertEqual((code, len(FAKE.msgs[CH]), len(FAKE.msgs[GR])), (0, 1, 1), out)
        self.assertEqual(FAKE.pinned[CH], list(FAKE.msgs[CH])[0])

    def test_a_changed_url_updates_the_existing_message_in_place(self):
        run("--channel", "@chan", "--apply", state=self.state)
        code, out = run("--channel", "@chan", "--apply", "--url", "https://tradelens360ai.live/?src=tg", state=self.state)
        self.assertEqual((code, len(FAKE.msgs[CH])), (0, 1), out)
        self.assertEqual(list(FAKE.msgs[CH].values())[0]["reply_markup"]["inline_keyboard"][0][0]["url"], "https://tradelens360ai.live/?src=tg")

    def test_repin_restores_the_pin_but_a_normal_run_leaves_other_pins_alone(self):
        run("--channel", "@chan", "--apply", state=self.state)
        FAKE.msgs[CH][999] = {"message_id": 999, "text": "Other announcement"}
        FAKE.pinned[CH] = 999
        run("--channel", "@chan", "--apply", state=self.state)
        self.assertEqual(FAKE.pinned[CH], 999)
        code, out = run("--channel", "@chan", "--apply", "--repin", state=self.state)
        self.assertEqual(code, 0, out)
        self.assertEqual(FAKE.pinned[CH], int(list(json.loads(self.state.read_text()).values())[0]["message_id"]))


class PermissionTests(Base):
    def test_missing_pin_right_is_reported_exactly_and_the_message_is_still_published_once(self):
        FAKE.rights[GR] = {"status": "administrator", "can_pin_messages": False}
        code, out = run("--channel", "@chan", "--apply", state=self.state)
        self.assertEqual(code, 1)
        self.assertIn("group: 'Pin messages' right", out)
        self.assertIn("--repin", out)
        self.assertEqual((len(FAKE.msgs[CH]), len(FAKE.msgs[GR]), FAKE.pinned[GR]), (1, 1, None))
        self.assertEqual(FAKE.pinned[CH], list(FAKE.msgs[CH])[0])                 # the channel half still completed
        FAKE.rights[GR]["can_pin_messages"] = True                                # the admin grants the right
        code, out = run("--channel", "@chan", "--apply", "--repin", state=self.state)
        self.assertEqual((code, len(FAKE.msgs[GR]), FAKE.pinned[GR]), (0, 1, list(FAKE.msgs[GR])[0]), out)

    def test_channel_rights_are_named_and_nothing_is_posted_without_the_post_right(self):
        FAKE.rights[CH] = {"status": "administrator", "can_post_messages": False, "can_edit_messages": False}
        code, out = run("--channel", "@chan", "--channel-only", "--apply", state=self.state)
        self.assertEqual(code, 1)
        self.assertIn("'Post messages' right", out)
        self.assertIn("'Edit messages of others' right", out)
        self.assertEqual(FAKE.msgs[CH], {})

    def test_a_plain_member_bot_is_told_to_become_administrator(self):
        FAKE.rights[GR] = {"status": "member"}
        code, out = run("--channel", "@chan", "--group-only", "--apply", state=self.state)
        self.assertEqual(code, 1)
        self.assertIn("administrator", out)

    def test_linked_group_not_found_is_reported_not_guessed(self):
        FAKE.linked = None
        code, out = run("--channel", "@chan", "--apply", state=self.state)
        self.assertEqual(code, 1)
        self.assertIn("NOT FOUND", out)
        self.assertIn("--group", out)
        self.assertEqual(FAKE.msgs[GR], {})
        self.assertEqual(len(FAKE.msgs[CH]), 1)                                   # the channel is still done
        code, out = run("--channel", "@chan", "--group", "@grp", "--apply", state=self.state)
        self.assertEqual((code, len(FAKE.msgs[GR]), len(FAKE.msgs[CH])), (0, 1, 1), out)


class SafetyTests(Base):
    def test_token_is_required_validated_and_never_leaked(self):
        for bad in ("", "not-a-token"):
            code, out = run("--channel", "@chan", token=bad, state=self.state)
            self.assertEqual(code, 1)
            self.assertNotIn(bad or "zzz", out.replace("TELEGRAM_BOT_TOKEN", "")) if bad else None
        wrong = "999999999:AAWrongTokenWrongTokenWrongToken12"
        code, out = run("--channel", "@chan", "--apply", token=wrong, state=self.state)
        self.assertEqual(code, 1)
        self.assertNotIn(wrong, out)
        self.assertEqual(FAKE.msgs[CH], {})

    def test_unreachable_telegram_never_prints_the_token(self):
        out = []
        code = tab.main(["--channel", "@chan", "--apply", "--state", str(self.state)], env={"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_API_BASE": "http://127.0.0.1:1"}, out=out.append)
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, "\n".join(out))

    def test_a_group_is_not_accepted_as_the_channel(self):
        code, out = run("--channel", "@grp", "--apply", state=self.state)
        self.assertEqual(code, 1)
        self.assertEqual(FAKE.msgs[GR], {})

    def test_only_https_urls(self):
        code, out = run("--channel", "@chan", "--apply", "--url", "http://tradelens360ai.live/", state=self.state)
        self.assertEqual((code, FAKE.msgs[CH]), (1, {}))

    def test_the_script_uses_no_server_code_and_no_extra_dependencies(self):
        src = (ROOT / "scripts" / "telegram_access_button.py").read_text()
        imports = {l.split()[1].split(".")[0] for l in src.splitlines() if l.startswith(("import ", "from "))}
        self.assertTrue(imports <= {"argparse", "json", "os", "re", "sys", "urllib", "pathlib"}, imports)
        for f in (ROOT / "app").glob("*.py"):
            self.assertNotIn("api.telegram.org", f.read_text(), f.name)          # the web app is untouched: no bot, no webhook, no polling
        self.assertNotIn("InpTgToken", src)


class GroupCopyTests(Base):
    def test_the_automatic_copy_of_the_channel_post_in_the_group_is_not_mistaken_for_our_message(self):
        run("--channel", "@chan", "--channel-only", "--apply", state=self.state)
        (cm,) = FAKE.msgs[CH].values()
        FAKE.next += 1
        FAKE.msgs[GR][FAKE.next] = {"message_id": FAKE.next, "text": cm["text"], "reply_markup": cm["reply_markup"], "is_automatic_forward": True,
                                    "forward_origin": {"type": "channel"}}
        FAKE.pinned[GR] = FAKE.next
        copy_id = FAKE.next
        code, out = run("--channel", "@chan", "--group-only", "--apply", state=self.state)
        self.assertEqual(code, 0, out)
        own = [m for m in FAKE.msgs[GR].values() if not m.get("is_automatic_forward")]
        self.assertEqual(len(own), 1, out)
        self.assertEqual(FAKE.pinned[GR], own[0]["message_id"])
        self.assertNotEqual(FAKE.pinned[GR], copy_id)
        code, out = run("--channel", "@chan", "--group-only", "--apply", state=self.state)           # and again: no second native message
        self.assertEqual(len([m for m in FAKE.msgs[GR].values() if not m.get("is_automatic_forward")]), 1, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
