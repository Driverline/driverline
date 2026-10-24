"""Checks your NOWPayments setup from your own machine. Prints no secrets and changes nothing in TradeLens.

  cd ~/driverline
  read -rs NOWPAYMENTS_API_KEY; export NOWPAYMENTS_API_KEY        # type or paste the key, press Enter (nothing is shown)
  python scripts/nowpayments_check.py                  # is the API reachable with this key, is USDT (TRC20) offered
  python scripts/nowpayments_check.py --create 3       # also asks NOWPayments for ONE test payment of 3 USD and reads it back

Optional: NOWPAYMENTS_API_BASE (default is the live API; set it to the sandbox address to test there),
          NOWPAYMENTS_PAY_CURRENCY (default usdttrc20).
--create makes a deposit address that belongs to no TradeLens order. Do not pay it: it simply expires. It shows the amount
NOWPayments quotes for that price and whether that price is above the minimum for the coin."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DRIVERLINE_BASE_URL", "https://tradelens360ai.live")
os.environ.setdefault("NOWPAYMENTS_IPN_SECRET", "check-only")        # not used by this script
from app import nowpay  # noqa: E402


async def main():
    ok = True
    print("API address      :", nowpay.api_base())
    print("Pay currency     :", nowpay.pay_currency())
    if not os.environ.get("NOWPAYMENTS_API_KEY"):
        print("NOWPAYMENTS_API_KEY is not set in this terminal. Set it first (see the top of this file).")
        return 1
    try:
        st = await nowpay._request("GET", "/status")
        print("API status       :", st.get("message") if isinstance(st, dict) else st)
    except nowpay.ProviderError as e:
        print("API status       : FAILED:", e, e.detail)
        print("A 401/403 here usually means the key is wrong, or it is a sandbox key used against the live address (or the reverse).")
        return 1
    try:
        cur = await nowpay._request("GET", "/currencies")
        lst = cur.get("currencies") if isinstance(cur, dict) else cur
        listed = isinstance(lst, list) and nowpay.pay_currency() in {str(x).lower() for x in lst}
        print(f"{nowpay.pay_currency()} offered :", "yes" if listed else "NO (enable USDT TRC20 in your NOWPayments dashboard, or check the code)")
        ok = ok and listed
    except nowpay.ProviderError as e:
        print("Currency list    : FAILED:", e, e.detail)
        ok = False
    if "--create" in sys.argv:
        usd = float(sys.argv[sys.argv.index("--create") + 1])
        try:
            p = await nowpay.create_provider_payment("TLP-CHECK-" + str(os.getpid()), usd, "TradeLens setup check")
            print(f"Test payment     : {usd} USD would be {p['pay_amount']} {p['pay_currency']} on {p['network']}")
            back = await nowpay._request("GET", f"/payment/{p['payment_id']}")
            print("Read back        : status", back.get("payment_status"), "| payment id", back.get("payment_id"))
            print("(Do not pay this address. It belongs to no order and will expire.)")
        except nowpay.ProviderError as e:
            print("Test payment     : FAILED:", e, e.detail)
            print("If this says the amount is too small, that price is below NOWPayments' minimum for this coin.")
            ok = False
    print("RESULT           :", "all good" if ok else "needs attention")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
