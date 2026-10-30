#!/usr/bin/env python3
"""One-off, idempotent setup: a pinned "Open TradeLens App" button in the TradeLens Telegram channel and its linked discussion group.

Why a script and not part of the web app: the TradeLens server has no Telegram code and holds no bot token. Trade signals are posted to Telegram by
the Driverline Companion EA on the admin's MT5 terminal. This script only calls Telegram's Bot API, once, from wherever you run it. It adds no
server process, no webhook, no polling, no database table and no dependency (standard library only), and it never touches signal publishing.

Usage (the token is read from the environment, never from the command line, a file, or the output):

    export TELEGRAM_BOT_TOKEN='...'            # the same bot the EA uses (@TradeLensagent)
    python3 scripts/telegram_access_button.py --channel @YourChannel            # CHECK ONLY: reports what it would do, posts nothing
    python3 scripts/telegram_access_button.py --channel @YourChannel --apply    # create, then pin, the two messages
    python3 scripts/telegram_access_button.py --channel @YourChannel --apply    # run again any time: it creates no duplicates

Options: --group <id or @name> (only if the linked group cannot be found automatically), --repin (pin our messages again), --state <file>,
--url, --channel-only / --group-only. Exit code 0 = everything done, 1 = something needs your attention (the exact reason is printed).
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

URL = "https://tradelens360ai.live/"
BUTTON = "\U0001F4CA Open TradeLens App"
TEXT = ("Welcome to TradeLens!\n\n"
        "Access our AI-powered market analysis platform to explore market insights and potential trade setups.")
DEFAULT_STATE = Path.home() / ".tradelens_telegram_access.json"


class TgError(Exception):
    def __init__(self, code, description):
        super().__init__(f"Telegram {code}: {description}")
        self.code, self.description = code, description


def _redact(text, token):
    text = str(text)
    return text.replace(token, "<token>") if token else text


class Api:
    def __init__(self, token, base=None):
        self.token = token
        self.base = (base or os.environ.get("TELEGRAM_API_BASE") or "https://api.telegram.org").rstrip("/")

    def call(self, method, **params):
        data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list, bool)) else v) for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(f"{self.base}/bot{self.token}/{method}", data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode())
            except Exception:
                body = {"ok": False, "error_code": e.code, "description": "HTTP error"}
        except Exception as e:                                       # network trouble: never echo the URL (it contains the token)
            raise TgError(0, "could not reach Telegram: " + _redact(type(e).__name__, self.token))
        if not body.get("ok"):
            raise TgError(body.get("error_code", 0), _redact(body.get("description", "unknown error"), self.token))
        return body["result"]


def keyboard(url):
    return {"inline_keyboard": [[{"text": BUTTON, "url": url}]]}


def matches(msg, url):
    """Is this Telegram message our access message (same text, same button target)?"""
    if not msg or (msg.get("text") or "").strip() != TEXT:
        return False
    if msg.get("is_automatic_forward") or msg.get("forward_origin") or msg.get("forward_from_chat"):
        return False         # Telegram's automatic copy of a channel post into the linked group is not our own message: it cannot be edited or pinned by us
    rows = (msg.get("reply_markup") or {}).get("inline_keyboard") or []
    return any(b.get("url") == url for row in rows for b in row)


def load_state(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(p)


def permissions(api, chat, bot_id, kind):
    """(can_post, can_pin, missing) for the bot in this chat. `missing` lists the exact rights to grant."""
    try:
        m = api.call("getChatMember", chat_id=chat, user_id=bot_id)
    except TgError as e:
        return False, False, [f"the bot must be added to this {kind} as an administrator ({e.description})"]
    status = m.get("status")
    if status == "creator":
        return True, True, []
    if status != "administrator":
        return (kind == "group" and status == "member"), False, [f"make the bot an administrator of the {kind} (it is currently '{status}')"]
    missing = []
    if kind == "channel":
        can_post, can_pin = m.get("can_post_messages") is True, m.get("can_edit_messages") is True
        if not can_post:
            missing.append("channel: 'Post messages' right")
        if not can_pin:
            missing.append("channel: 'Edit messages of others' right (Telegram requires it to pin in a channel)")
    else:
        can_post, can_pin = True, m.get("can_pin_messages") is True
        if not can_pin:
            missing.append("group: 'Pin messages' right")
    return can_post, can_pin, missing


def ensure(api, label, chat, bot_id, kind, url, state, args, out):
    """Create (once), update and pin the access message in one chat. Returns True when fully done."""
    key = str(chat)
    can_post, can_pin, missing = permissions(api, chat, bot_id, kind)
    for m in missing:
        out(f"  [{label}] needs: {m}")
    mid = (state.get(key) or {}).get("message_id")
    chat_info = api.call("getChat", chat_id=chat)
    pinned = chat_info.get("pinned_message")
    created = False
    if mid:                                                          # stored id: confirm it still exists and carries the right content
        if not args.apply:
            out(f"  [{label}] already published (message {mid}); would confirm it is current")
        else:
            try:
                api.call("editMessageText", chat_id=chat, message_id=mid, text=TEXT, reply_markup=keyboard(url), disable_web_page_preview=True)
                out(f"  [{label}] message {mid} updated")
            except TgError as e:
                if "not modified" in e.description.lower():
                    out(f"  [{label}] message {mid} is already current")
                elif "not found" in e.description.lower() or "can't be edited" in e.description.lower():
                    out(f"  [{label}] stored message {mid} no longer exists; it will be published again")
                    mid = None
                else:
                    out(f"  [{label}] could not update message {mid}: {e.description}")
                    return False
    if not mid and pinned and matches(pinned, url):                  # state file lost, but our message is still pinned: adopt it, do not duplicate
        mid = pinned["message_id"]
        out(f"  [{label}] found our message already pinned (message {mid}); reusing it")
        state[key] = {"message_id": mid, "kind": kind}
        if args.apply:
            save_state(args.state, state)
    if not mid:
        if not can_post:
            out(f"  [{label}] cannot publish yet (see above)")
            return False
        if not args.apply:
            out(f"  [{label}] would publish the access message and pin it")
            return not missing
        try:
            sent = api.call("sendMessage", chat_id=chat, text=TEXT, reply_markup=keyboard(url), disable_web_page_preview=True)
        except TgError as e:
            out(f"  [{label}] could not publish: {e.description}")
            return False
        mid, created = sent["message_id"], True
        state[key] = {"message_id": mid, "kind": kind}
        save_state(args.state, state)                                # saved immediately, before pinning, so a later failure cannot cause a duplicate
        out(f"  [{label}] published (message {mid})")
    already_pinned = bool(pinned) and pinned.get("message_id") == mid
    if args.apply and (created or args.repin) and not already_pinned:
        try:
            api.call("pinChatMessage", chat_id=chat, message_id=mid, disable_notification=True)
            out(f"  [{label}] pinned")
        except TgError as e:
            out(f"  [{label}] could not pin: {e.description}. Grant the right listed above, then run again with --repin")
            return False
    elif already_pinned:
        out(f"  [{label}] is pinned")
    elif args.apply:
        out(f"  [{label}] not pinned (run with --repin to pin it; later pins by other admins are left alone)")
    return not missing


def main(argv=None, env=None, out=print):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description="Publish and pin the 'Open TradeLens App' button in the Telegram channel and linked group.")
    ap.add_argument("--channel", required=True, help="channel @username or numeric id (-100...)")
    ap.add_argument("--group", help="discussion group id or @name; normally found automatically from the channel")
    ap.add_argument("--apply", action="store_true", help="actually publish and pin (without it, only checks and reports)")
    ap.add_argument("--repin", action="store_true", help="pin our messages again")
    ap.add_argument("--state", default=str(DEFAULT_STATE), help="where message ids are remembered")
    ap.add_argument("--url", default=URL)
    ap.add_argument("--channel-only", action="store_true")
    ap.add_argument("--group-only", action="store_true")
    args = ap.parse_args(argv)
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
        out("Set TELEGRAM_BOT_TOKEN in your shell first (export TELEGRAM_BOT_TOKEN='...'). It is never printed or stored by this script.")
        return 1
    if not args.url.startswith("https://"):
        out("The URL must start with https://")
        return 1
    api = Api(token, env.get("TELEGRAM_API_BASE"))
    out("TradeLens access button: " + ("APPLY" if args.apply else "CHECK ONLY (nothing will be posted; add --apply)"))
    try:
        me = api.call("getMe")
        out(f"Bot: @{me.get('username')}")
        channel = api.call("getChat", chat_id=args.channel)
    except TgError as e:
        out(f"Cannot continue: {e.description}")
        return 1
    state = load_state(args.state)
    ok = True
    if not args.group_only:
        if channel.get("type") != "channel":
            out(f"{args.channel} is not a channel (it is a {channel.get('type')}).")
            return 1
        out(f"Channel: {channel.get('title')}")
        ok = ensure(api, "channel", channel["id"], me["id"], "channel", args.url, state, args, out) and ok
    group_id = args.group or channel.get("linked_chat_id")
    if not args.channel_only:
        if not group_id:
            out("Linked group: NOT FOUND. Telegram did not report a discussion group for this channel (the bot must be a member of the channel's group, "
                "or the channel has no linked group). Re-run with --group <id or @name> once you know it. I will not guess a chat id.")
            ok = False
        else:
            try:
                group = api.call("getChat", chat_id=group_id)
            except TgError as e:
                out(f"Linked group {group_id}: cannot be read ({e.description}). Add the bot to the group as an administrator.")
                return 1
            out(f"Group: {group.get('title')}")
            ok = ensure(api, "group", group["id"], me["id"], "group", args.url, state, args, out) and ok
    out("Done." if ok else "Finished with items that need your attention (listed above).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
