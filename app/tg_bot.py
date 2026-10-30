"""@TradeLensagent private-chat navigation: a persistent four-button keyboard, answered through Telegram's webhook.

How it works, and why it is safe for the live app:
- Telegram POSTs bot updates to /api/telegram/webhook. We check the secret header Telegram echoes back (set once with setWebhook), then REPLY
  IN THE RESPONSE BODY with the message we want sent (Telegram supports a Bot API method call as the webhook response). The server therefore
  never needs the bot token and makes no outgoing call. The only setting is TELEGRAM_WEBHOOK_SECRET; without it this route does not exist (404).
- It only handles private chats. Channel posts, groups, edits and everything else are ignored, so it cannot interfere with the trade-signal posts
  the Companion EA sends with sendMessage (those never use updates).
- Nothing is stored: no table, no user data, no logging of message text.
The four destinations below are copied verbatim from the brief and are never rebuilt or shortened."""
import hmac
import os
import time

from starlette.responses import JSONResponse
from starlette.routing import Route

APP_URL = "https://tradelens360ai.live/login.html?utm_source=Telegram&utm_medium=paid&utm_campaign=launch&utm_content=hook-a"
RESOURCES_URL = "https://t.me/TradeLensResources"
CHANNEL_URL = "https://t.me/+RgeOG2GryJM3NmI0"
HUB_URL = "https://t.me/+e-cf-gPTZG0xNjk0"

BTN_APP = "\U0001F4CA Open TradeLens App"
BTN_RESOURCES = "\U0001F393 Open TradeLens Resources"
BTN_CHANNEL = "\U0001F4E2 Subscribe to TradeLens Channel"
BTN_HUB = "\U0001F465 Join TradeLens Traders Hub"

KEYBOARD = {"keyboard": [[{"text": BTN_APP}, {"text": BTN_RESOURCES}], [{"text": BTN_CHANNEL}, {"text": BTN_HUB}]],
            "is_persistent": True, "resize_keyboard": True, "one_time_keyboard": False}

WELCOME = ("\U0001F44B Welcome to TradeLens!\n\n"
           "This bot is your shortcut to everything TradeLens: our AI-powered market analysis platform, learning resources, "
           "the official channel and the traders community.\n\n"
           "Use the buttons below to get started.")

ACTIONS = {BTN_APP: ("\U0001F4CA Tap the button below to open the TradeLens app.", APP_URL),
           BTN_RESOURCES: ("\U0001F393 Tap the button below to open TradeLens Resources.", RESOURCES_URL),
           BTN_CHANNEL: ("\U0001F4E2 Tap the button below to subscribe to the TradeLens channel.", CHANNEL_URL),
           BTN_HUB: ("\U0001F465 Tap the button below to join the TradeLens Traders Hub.", HUB_URL)}

_seen: dict[int, float] = {}          # update_id -> time: Telegram re-sends an update it thinks failed; answer each only once


def _fresh(update_id) -> bool:
    now = time.time()
    for k in [k for k, t in _seen.items() if now - t > 600]:
        _seen.pop(k, None)
    if not isinstance(update_id, int) or update_id in _seen:
        return False
    _seen[update_id] = now
    if len(_seen) > 2000:
        _seen.pop(next(iter(_seen)), None)
    return True


def _reply(chat_id: int, text: str, markup: dict) -> JSONResponse:
    return JSONResponse({"method": "sendMessage", "chat_id": chat_id, "text": text, "reply_markup": markup, "disable_web_page_preview": True})


def respond(update: dict):
    """The webhook answer (a dict) for one Telegram update, or None when nothing should be sent."""
    msg = update.get("message") if isinstance(update, dict) else None
    if not isinstance(msg, dict) or (msg.get("chat") or {}).get("type") != "private" or (msg.get("from") or {}).get("is_bot"):
        return None
    text, chat_id = msg.get("text"), msg["chat"].get("id")
    if not isinstance(text, str) or not isinstance(chat_id, int):
        return None
    text = text.strip()
    if text == "/start" or text.startswith("/start ") or text in ("/help", "/menu"):
        return ("sendMessage", chat_id, WELCOME, KEYBOARD)
    if text in ACTIONS:
        say, url = ACTIONS[text]
        return ("sendMessage", chat_id, say, {"inline_keyboard": [[{"text": text, "url": url}]]})   # the reply keyboard stays: it is never removed
    return None


async def webhook(request):
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if not secret:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    sent = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not hmac.compare_digest(sent.encode(), secret.encode()):
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    try:
        update = await request.json()
    except Exception:
        return JSONResponse({})                       # never an error Telegram would retry
    try:
        if not isinstance(update, dict) or not _fresh(update.get("update_id")):
            return JSONResponse({})
        out = respond(update)
        return _reply(out[1], out[2], out[3]) if out else JSONResponse({})
    except Exception as e:
        print(f"[telegram] could not handle an update: {type(e).__name__}", flush=True)
        return JSONResponse({})


routes = [Route("/api/telegram/webhook", webhook, methods=["POST"])]
