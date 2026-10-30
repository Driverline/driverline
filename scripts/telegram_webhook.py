#!/usr/bin/env python3
"""One-off helper for the @TradeLensagent navigation keyboard webhook. Standard library only; nothing is ever printed that contains a secret.

    export TELEGRAM_BOT_TOKEN=...            # same bot the EA uses (type it with: read -s TELEGRAM_BOT_TOKEN)
    export TELEGRAM_WEBHOOK_SECRET=...       # the SAME value you put in Render as TELEGRAM_WEBHOOK_SECRET
    python3 scripts/telegram_webhook.py secret   # prints a fresh random secret to use (to your own screen only)
    python3 scripts/telegram_webhook.py info     # what Telegram currently has (read only)
    python3 scripts/telegram_webhook.py set      # register the webhook (refuses to replace a different one unless --replace)
    python3 scripts/telegram_webhook.py delete   # remove it (rollback)

Only run `set` AFTER the new version is deployed on Render with TELEGRAM_WEBHOOK_SECRET set. Registering a webhook switches Telegram from
getUpdates to webhooks for this bot: the Companion EA only sends messages, so signal posting is unaffected, but anything else that polls the
bot with getUpdates would stop receiving updates. `info` shows whether a webhook already exists."""
import argparse
import os
import re
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from telegram_access_button import Api, TgError   # noqa: E402  (same stdlib-only Bot API client)

URL = "https://tradelens360ai.live/api/telegram/webhook"


def main(argv=None, env=None, out=print):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["secret", "info", "set", "delete"])
    ap.add_argument("--url", default=URL)
    ap.add_argument("--replace", action="store_true", help="replace a different webhook that already exists")
    a = ap.parse_args(argv)
    if a.command == "secret":
        out(secrets.token_urlsafe(32))
        return 0
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
        out("Set TELEGRAM_BOT_TOKEN first (read -s TELEGRAM_BOT_TOKEN; export TELEGRAM_BOT_TOKEN).")
        return 1
    api = Api(token, env.get("TELEGRAM_API_BASE"))
    try:
        info = api.call("getWebhookInfo")
        cur = info.get("url") or ""
        if a.command == "info":
            out(f"Current webhook: {cur or 'none'}")
            out(f"Pending updates: {info.get('pending_update_count', 0)}")
            if info.get("last_error_message"):
                out(f"Last Telegram delivery error: {info['last_error_message']}")
            return 0
        if a.command == "delete":
            api.call("deleteWebhook")
            out("Webhook removed.")
            return 0
        secret = (env.get("TELEGRAM_WEBHOOK_SECRET") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", secret):
            out("Set TELEGRAM_WEBHOOK_SECRET (16-256 letters, digits, _ or -), the same value as in Render. `secret` prints a good one.")
            return 1
        if not a.url.startswith("https://"):
            out("The URL must start with https://")
            return 1
        if cur and cur != a.url and not a.replace:
            out(f"A different webhook is already registered: {cur}\nNot replacing it. Re-run with --replace if you are sure.")
            return 1
        api.call("setWebhook", url=a.url, secret_token=secret, allowed_updates=["message"], drop_pending_updates=False)
        out(f"Webhook registered: {a.url}")
        return 0
    except TgError as e:
        out(f"Telegram refused: {e.description}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
