"""Checks your Paystack setup from your own machine. Prints no secrets and changes nothing in TradeLens or on Paystack.

  cd ~/driverline
  read -rs PAYSTACK_SECRET_KEY; export PAYSTACK_SECRET_KEY        # type or paste the key, press Enter (nothing is shown)
  python scripts/paystack_check.py

It tells you whether the key looks like a test or a live key, and whether Paystack accepts it (it looks up a reference that cannot
exist: a valid key answers "not found", a wrong key is refused). No transaction is created and no money moves."""
import asyncio
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DRIVERLINE_BASE_URL", "https://tradelens360ai.live")
os.environ.setdefault("PAYSTACK_ALLOW_LIVE", "1")            # this script only reads; it never starts a payment
from app import paystack  # noqa: E402


async def main():
    if not os.environ.get("PAYSTACK_SECRET_KEY"):
        print("PAYSTACK_SECRET_KEY is not set in this terminal. Set it first (see the top of this file).")
        return 1
    print("Key mode         :", paystack.key_mode() or "NOT A PAYSTACK SECRET KEY (must start sk_test_ or sk_live_)")
    print("Webhook URL      :", paystack.webhook_url())
    print("Callback URL     :", paystack.callback_url())
    try:
        await paystack._request("GET", "/transaction/verify/tl-connection-check-" + secrets.token_hex(4))
        print("Paystack answer  : unexpected success")
        return 0
    except paystack.ProviderError as e:
        if paystack._reference_not_found(e):
            print("Paystack answer  : key accepted (reference not found, as expected)")
            return 0
        print(f"Paystack answer  : key refused or Paystack unreachable ({e} {e.detail})")
        return 1


sys.exit(asyncio.run(main()))
