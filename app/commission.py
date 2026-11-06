"""Referral commission (Phase 3): a referrer receives 10% of the Deriv commission TradeLens earns on the trading of the members they referred.

How it works (nothing here talks to Deriv; the owner imports the report TradeLens already receives from Deriv):
  1. Admin > Rewards > Commission: choose the month, paste or load the commission report (CSV) from Deriv Partners Hub, pick which column holds
     the client ID and which holds the commission (the format of Deriv's report is NOT assumed: the columns are chosen by the admin), PREVIEW,
     then APPLY. Import only after Deriv has paid that month, because members can withdraw what they earn.
  2. A report row earns a share only when ALL of these hold: its client ID is a Deriv account stored in partner_tags that Deriv's partner records
     CONFIRM as tagged to TradeLens (status "confirmed": this covers accounts created through TradeLens and accounts tagged to us), the account
     belongs to a TradeLens member, and that member was referred by another member. Everything else stays 100% with TradeLens and is only counted.
  3. Rows are stored per (month, client), so re-importing a month (a corrected report) replaces that month and the balances follow.
  4. Balances: earned = sum of shares, paid = payouts marked paid, reserved = payout requests waiting. Money is kept in whole cents.
  5. Payout is MANUAL: the member saves a USDT address (TRC20 or BEP20) and asks to withdraw (minimum $10). The admin pays from outside the app,
     then marks the request paid with the transaction hash, or rejects it with a reason. The app never moves money.
Members see their commission only when REWARDS_ON=1 and REWARDS_COMMISSION_ON=1. The admin tools work whenever the admin opens them.
Tables are new and additive (migrations/2026_11_rewards_phase3.sql)."""
import asyncio
import csv
import io
import os
import re
import time
from contextlib import closing
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, dbx

SCHEMA = """
    CREATE TABLE IF NOT EXISTS commission_rows(id {ID}, period TEXT NOT NULL, client_id TEXT NOT NULL, referred_id INTEGER NOT NULL,
        referrer_id INTEGER NOT NULL, commission_cents BIGINT NOT NULL, pct INTEGER NOT NULL, share_cents BIGINT NOT NULL,
        import_id INTEGER, created_at BIGINT NOT NULL, UNIQUE(period, client_id));
    CREATE INDEX IF NOT EXISTS ix_comm_referrer ON commission_rows(referrer_id, period);
    CREATE TABLE IF NOT EXISTS commission_imports(id {ID}, period TEXT NOT NULL, admin_id INTEGER, filename TEXT, rows_total INTEGER NOT NULL,
        rows_shared INTEGER NOT NULL, commission_cents BIGINT NOT NULL, share_cents BIGINT NOT NULL, replaced INTEGER NOT NULL DEFAULT 0,
        created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS payout_details(user_id INTEGER PRIMARY KEY, network TEXT NOT NULL, address TEXT NOT NULL, updated_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS payout_requests(id {ID}, user_id INTEGER NOT NULL, amount_cents BIGINT NOT NULL, network TEXT NOT NULL,
        address TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'requested', txid TEXT, note TEXT, created_at BIGINT NOT NULL,
        decided_at BIGINT, admin_id INTEGER);
    CREATE INDEX IF NOT EXISTS ix_payout_user ON payout_requests(user_id, id);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_payout_open ON payout_requests(user_id) WHERE status='requested'"""

PCT = 10                       # percent of the Deriv commission shared with the referrer
MIN_PAYOUT_CENTS = 1000        # $10
MAX_CSV_CHARS = 3_000_000
MAX_ROWS = 50_000
PERIOD_RE = re.compile(r"^20\d\d-(0[1-9]|1[0-2])$")
NETWORKS = {"trc20": re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$"), "bep20": re.compile(r"^0x[a-fA-F0-9]{40}$")}
ID_HINTS = ("client", "loginid", "login id", "account", "user id", "id")
AMOUNT_HINTS = ("commission", "earning", "payout", "amount", "usd")


def commission_on() -> bool:
    from . import rewards
    return rewards.enabled() and os.environ.get("REWARDS_COMMISSION_ON", "").strip() == "1"


def db():
    auth.db().close()
    return dbx.connect("commission", SCHEMA)


def _now() -> int:
    return int(time.time())


def usd(cents) -> float:
    return round(int(cents or 0) / 100.0, 2)


# ------------------------------------------------------------------ reading the report
def to_cents(text) -> int:
    """'$1,234.56' -> 123456, '(2.50)' -> -250. Raises ValueError for anything that is not a plain amount."""
    t = str(text if text is not None else "").strip().replace(",", "").replace("$", "").replace("USD", "").replace(" ", "")
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    if not t or t.lower() in ("nan", "inf", "-inf", "infinity"):
        raise ValueError("not an amount")
    try:
        d = Decimal(t)
    except InvalidOperation:
        raise ValueError("not an amount")
    if not d.is_finite():
        raise ValueError("not an amount")
    if neg:
        d = -d
    if abs(d) > Decimal("10000000"):
        raise ValueError("too large")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_report(text: str):
    """(headers, rows as dicts). Detects , ; tab | as the separator. Raises HTTPException for a file that cannot be used."""
    text = str(text or "")
    if not text.strip():
        raise HTTPException(422, "Paste or load the commission report first")
    if len(text) > MAX_CSV_CHARS:
        raise HTTPException(413, "That file is too large (3 MB limit)")
    text = text.lstrip("﻿")
    sample = text[:4096]
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delim = ","
    rdr = csv.reader(io.StringIO(text), delimiter=delim)
    try:
        headers = [h.strip() for h in next(rdr)]
    except StopIteration:
        raise HTTPException(422, "The report is empty")
    if len(headers) < 2 or len(set(headers)) != len(headers) or any(not h for h in headers):
        raise HTTPException(422, "The first line must be the column names, each different and not empty")
    rows = []
    for i, r in enumerate(rdr):
        if not any(c.strip() for c in r):
            continue
        if len(r) > len(headers):          # an unquoted amount like 1,000.00 would be read as 1 and shift the columns: never guess with money
            raise HTTPException(422, f"Line {i + 2} has {len(r)} values but the header has {len(headers)}. Amounts such as 1,000.00 must be inside quotes, "
                                     "or load the original file from Deriv instead of retyping it.")
        if len(rows) >= MAX_ROWS:
            raise HTTPException(413, f"More than {MAX_ROWS} rows: split the report by month")
        rows.append({h: (r[j].strip() if j < len(r) else "") for j, h in enumerate(headers)})
    if not rows:
        raise HTTPException(422, "The report has a header but no rows")
    return headers, rows


def guess_columns(headers):
    low = [h.lower() for h in headers]
    pick = lambda hints, skip=None: next((headers[i] for h in hints for i, l in enumerate(low) if h in l and headers[i] != skip), None)
    amount = pick(AMOUNT_HINTS)
    return {"id_col": pick(ID_HINTS, amount), "amount_col": amount}


# ------------------------------------------------------------------ who is eligible
def _tag_index():
    """upper-case client key -> (account_id, user_id, status), over every way Deriv may name the client."""
    from . import partner
    out = {}
    with closing(partner.db()) as con:
        for r in con.execute("SELECT account_id, user_id, status, display_id, client_uuid FROM partner_tags").fetchall():
            for k in (r["account_id"], r["display_id"], r["client_uuid"]):
                if k:
                    out[str(k).strip().upper()] = (r["account_id"], r["user_id"], r["status"])
    return out


def analyse(text: str, id_col: str = None, amount_col: str = None) -> dict:
    """Read the report and work out each client's share. Writes nothing."""
    headers, rows = parse_report(text)
    if not id_col or not amount_col:
        return {"needs_columns": True, "headers": headers, "guess": guess_columns(headers), "rows": len(rows)}
    if id_col not in headers or amount_col not in headers or id_col == amount_col:
        raise HTTPException(422, "Choose two different columns from the report")
    totals, bad = {}, 0
    for r in rows:
        cid = r[id_col].strip().upper()
        try:
            c = to_cents(r[amount_col])
        except ValueError:
            bad += 1
            continue
        if not cid:
            bad += 1
            continue
        totals[cid] = totals.get(cid, 0) + c
    from . import rewards
    tags = _tag_index()
    with closing(rewards.db()) as con:
        refs = {r["referred_id"]: r["referrer_id"] for r in con.execute("SELECT referred_id, referrer_id FROM referrals").fetchall()}
    shared, reasons, unmatched = [], {"not_ours": 0, "no_member": 0, "not_confirmed": 0, "no_referrer": 0}, []
    total_all = sum(totals.values())
    for cid, cents in sorted(totals.items()):
        t = tags.get(cid)
        if not t:
            reasons["not_ours"] += 1
            if len(unmatched) < 10:
                unmatched.append(cid)
        elif not t[1]:
            reasons["no_member"] += 1
        elif t[2] != "confirmed":
            reasons["not_confirmed"] += 1
        elif t[1] not in refs:
            reasons["no_referrer"] += 1
        else:
            shared.append({"client_id": t[0], "referred_id": t[1], "referrer_id": refs[t[1]], "commission_cents": cents,
                           "share_cents": int(round(cents * PCT / 100.0))})
    return {"needs_columns": False, "rows": len(rows), "bad_rows": bad, "clients": len(totals), "report_total_cents": total_all,
            "shared": shared, "share_total_cents": sum(s["share_cents"] for s in shared),
            "shared_commission_cents": sum(s["commission_cents"] for s in shared), "reasons": reasons, "unmatched_sample": unmatched}


def _emails(ids):
    ids = list(ids)
    if not ids:
        return {}
    with closing(auth.db()) as a:
        return {r["id"]: r["email"] for r in a.execute(f"SELECT id,email FROM users WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()}


def _summary(a: dict, period: str) -> dict:
    if a.get("needs_columns"):
        return a
    by = {}
    for s in a["shared"]:
        x = by.setdefault(s["referrer_id"], {"clients": 0, "share_cents": 0})
        x["clients"] += 1
        x["share_cents"] += s["share_cents"]
    em = _emails(by)
    with closing(db()) as con:
        prev = con.execute("SELECT COUNT(*) AS n, COALESCE(SUM(share_cents),0) AS s FROM commission_rows WHERE period=?", (period,)).fetchone()
    return {"needs_columns": False, "period": period, "rows": a["rows"], "bad_rows": a["bad_rows"], "clients": a["clients"],
            "report_total": usd(a["report_total_cents"]), "shared_clients": len(a["shared"]), "shared_commission": usd(a["shared_commission_cents"]),
            "share_total": usd(a["share_total_cents"]), "percent": PCT, "reasons": a["reasons"], "unmatched_sample": a["unmatched_sample"],
            "referrers": sorted(({"email": em.get(k, "?"), "clients": v["clients"], "share": usd(v["share_cents"])} for k, v in by.items()),
                                key=lambda x: -x["share"])[:50],
            "already_imported": {"rows": prev["n"], "share": usd(prev["s"])} if prev["n"] else None}


def apply(a: dict, period: str, admin_id: int, filename: str, replace: bool) -> dict:
    now = _now()
    with closing(db()) as con:
        n = con.execute("SELECT COUNT(*) AS n FROM commission_rows WHERE period=?", (period,)).fetchone()["n"]
        if n and not replace:
            raise HTTPException(409, f"{period} was already imported. Tick \"replace\" to correct it with the new report.")
        try:
            if n:
                con.execute("DELETE FROM commission_rows WHERE period=?", (period,))
            iid = con.insert("INSERT INTO commission_imports(period,admin_id,filename,rows_total,rows_shared,commission_cents,share_cents,replaced,created_at) "
                             "VALUES(?,?,?,?,?,?,?,?,?)", (period, admin_id, (filename or "")[:120], a["rows"], len(a["shared"]),
                                                          a["shared_commission_cents"], a["share_total_cents"], 1 if n else 0, now))
            for s in a["shared"]:
                con.execute("INSERT INTO commission_rows(period,client_id,referred_id,referrer_id,commission_cents,pct,share_cents,import_id,created_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?)", (period, s["client_id"], s["referred_id"], s["referrer_id"], s["commission_cents"], PCT,
                                                         s["share_cents"], iid, now))
            con.commit()
        except Exception:
            con.raw.rollback()
            raise
    return {"ok": True, "import_id": iid, "period": period, "shared_clients": len(a["shared"]), "share_total": usd(a["share_total_cents"]), "replaced": bool(n)}


# ------------------------------------------------------------------ balances and payouts
def balances(uid: int, con=None) -> dict:
    own = con is None
    con = con or db()
    try:
        e = con.execute("SELECT COALESCE(SUM(share_cents),0) AS s FROM commission_rows WHERE referrer_id=?", (uid,)).fetchone()["s"]
        p = con.execute("SELECT COALESCE(SUM(amount_cents),0) AS s FROM payout_requests WHERE user_id=? AND status='paid'", (uid,)).fetchone()["s"]
        r = con.execute("SELECT COALESCE(SUM(amount_cents),0) AS s FROM payout_requests WHERE user_id=? AND status='requested'", (uid,)).fetchone()["s"]
    finally:
        if own:
            con.close()
    return {"earned": int(e), "paid": int(p), "reserved": int(r), "available": int(e) - int(p) - int(r)}


def mask_addr(a: str) -> str:
    return (a[:4] + "..." + a[-4:]) if a and len(a) > 10 else ""


def view(uid: int) -> dict:
    if not commission_on():
        return {"on": False}
    b = balances(uid)
    with closing(db()) as con:
        periods = [{"period": r["period"], "clients": r["n"], "share": usd(r["s"])} for r in con.execute(
            "SELECT period, COUNT(*) AS n, SUM(share_cents) AS s FROM commission_rows WHERE referrer_id=? GROUP BY period ORDER BY period DESC LIMIT 12", (uid,)).fetchall()]
        det = con.execute("SELECT network, address FROM payout_details WHERE user_id=?", (uid,)).fetchone()
        reqs = [{"id": r["id"], "amount": usd(r["amount_cents"]), "status": r["status"], "note": r["note"], "txid": r["txid"], "at": r["created_at"]}
                for r in con.execute("SELECT * FROM payout_requests WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,)).fetchall()]
    return {"on": True, "percent": PCT, "earned": usd(b["earned"]), "paid": usd(b["paid"]), "reserved": usd(b["reserved"]),
            "available": usd(max(0, b["available"])), "min_payout": usd(MIN_PAYOUT_CENTS), "periods": periods,
            "details": {"network": det["network"], "address": mask_addr(det["address"])} if det else None, "requests": reqs,
            "can_request": b["available"] >= MIN_PAYOUT_CENTS and bool(det) and not any(r["status"] == "requested" for r in reqs)}


def _notify(uid: int, admin_id: int, body: str):
    try:
        from . import inbox
        with closing(inbox.db()) as con:
            con.insert("INSERT INTO inbox_messages(user_id,sender,admin_id,body,created_at) VALUES(?,?,?,?,?)", (uid, "admin", admin_id, body[:1000], _now()))
            con.commit()
    except Exception as e:
        print(f"[commission] could not message the member: {type(e).__name__}", flush=True)


# ------------------------------------------------------------------ member API
async def api_save_details(request):
    u = auth.require_user(request)
    if not commission_on():
        raise HTTPException(404, "Not available")
    d = await auth._json(request)
    net, addr = str(d.get("network", "")).strip().lower(), str(d.get("address", "")).strip()
    if net not in NETWORKS or not NETWORKS[net].match(addr):
        raise HTTPException(422, "Enter a valid USDT address for the network you chose (TRC20 starts with T, BEP20 with 0x)")
    with closing(db()) as con:
        if con.execute("SELECT 1 FROM payout_requests WHERE user_id=? AND status='requested'", (u["id"],)).fetchone():
            raise HTTPException(409, "You have a payout waiting. Wait until it is paid before changing your address.")
        con.execute("INSERT INTO payout_details(user_id,network,address,updated_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                    "network=excluded.network, address=excluded.address, updated_at=excluded.updated_at", (u["id"], net, addr, _now()))
        con.commit()
    return JSONResponse({"ok": True})


async def api_request_payout(request):
    u = auth.require_user(request)
    if not commission_on():
        raise HTTPException(404, "Not available")
    with closing(db()) as con:
        det = con.execute("SELECT network, address FROM payout_details WHERE user_id=?", (u["id"],)).fetchone()
        if not det:
            raise HTTPException(409, "Save your USDT address first")
        b = balances(u["id"], con)
        if b["available"] < MIN_PAYOUT_CENTS:
            raise HTTPException(409, f"The minimum payout is ${usd(MIN_PAYOUT_CENTS):g}")
        try:
            rid = con.insert("INSERT INTO payout_requests(user_id,amount_cents,network,address,status,created_at) VALUES(?,?,?,?,'requested',?)",
                             (u["id"], b["available"], det["network"], det["address"], _now()))
            con.commit()
        except Exception as e:
            con.raw.rollback()
            if dbx.is_duplicate(e):
                raise HTTPException(409, "You already have a payout waiting")
            raise
    return JSONResponse({"ok": True, "id": rid, "amount": usd(b["available"])})


# ------------------------------------------------------------------ admin API
async def _read(request):
    d = await auth._json(request)
    period = str(d.get("period", "")).strip()
    if not PERIOD_RE.match(period):
        raise HTTPException(422, "Choose the month the report is for (for example 2026-10)")
    return d, period


async def admin_preview(request):
    auth.require_admin(request)
    d, period = await _read(request)
    a = await asyncio.to_thread(analyse, d.get("csv"), d.get("id_col") or None, d.get("amount_col") or None)
    return JSONResponse(await asyncio.to_thread(_summary, a, period))


async def admin_apply(request):
    admin = auth.require_admin(request)
    d, period = await _read(request)
    if d.get("confirm") is not True:
        raise HTTPException(422, "Confirm that the amounts are in USD and that Deriv has paid this month")
    a = await asyncio.to_thread(analyse, d.get("csv"), d.get("id_col") or None, d.get("amount_col") or None)
    if a.get("needs_columns"):
        raise HTTPException(422, "Choose the client and commission columns first")
    return JSONResponse(await asyncio.to_thread(apply, a, period, admin["id"], str(d.get("filename") or ""), d.get("replace") is True))


def overview() -> dict:
    with closing(db()) as con:
        imports = [dict(r) for r in con.execute("SELECT id,period,rows_total,rows_shared,commission_cents,share_cents,replaced,created_at FROM commission_imports ORDER BY id DESC LIMIT 12").fetchall()]
        per = con.execute("SELECT referrer_id, SUM(share_cents) AS s FROM commission_rows GROUP BY referrer_id").fetchall()
        reqs = [dict(r) for r in con.execute("SELECT * FROM payout_requests ORDER BY CASE status WHEN 'requested' THEN 0 ELSE 1 END, id DESC LIMIT 40").fetchall()]
        rows = []
        for r in per:
            b = balances(r["referrer_id"], con)
            rows.append({"user_id": r["referrer_id"], **b})
    em = _emails({r["user_id"] for r in rows} | {r["user_id"] for r in reqs})
    owed = sum(max(0, r["available"]) + r["reserved"] for r in rows)
    return {"percent": PCT, "min_payout": usd(MIN_PAYOUT_CENTS), "on": commission_on(),
            "totals": {"earned": usd(sum(r["earned"] for r in rows)), "paid": usd(sum(r["paid"] for r in rows)), "owed": usd(owed)},
            "imports": [{"id": i["id"], "period": i["period"], "rows": i["rows_total"], "shared": i["rows_shared"], "commission": usd(i["commission_cents"]),
                         "share": usd(i["share_cents"]), "replaced": bool(i["replaced"]), "at": i["created_at"]} for i in imports],
            "referrers": sorted(({"email": em.get(r["user_id"], "?"), "earned": usd(r["earned"]), "paid": usd(r["paid"]), "reserved": usd(r["reserved"]),
                                  "available": usd(r["available"]), "overpaid": r["available"] < 0} for r in rows), key=lambda x: -x["earned"])[:50],
            "requests": [{"id": r["id"], "email": em.get(r["user_id"], "?"), "amount": usd(r["amount_cents"]), "network": r["network"], "address": r["address"],
                          "status": r["status"], "txid": r["txid"], "note": r["note"], "at": r["created_at"]} for r in reqs]}


async def admin_view(request):
    auth.require_admin(request)
    return JSONResponse(await asyncio.to_thread(overview))


async def admin_decide(request):
    admin = auth.require_admin(request)
    d = await auth._json(request)
    try:
        rid = int(d.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Missing request id")
    action, note = str(d.get("action", "")), str(d.get("note", "")).strip()[:200]
    txid = str(d.get("txid", "")).strip()
    if action == "paid":
        if not 6 <= len(txid) <= 120:
            raise HTTPException(422, "Enter the transaction hash of the payment")
    elif action == "reject":
        if len(note) < 3:
            raise HTTPException(422, "Give the member a reason")
    else:
        raise HTTPException(422, "action must be paid or reject")
    with closing(db()) as con:
        row = con.execute("SELECT user_id, amount_cents FROM payout_requests WHERE id=?", (rid,)).fetchone()
        if not row:
            raise HTTPException(404, "Request not found")
        cur = con.execute("UPDATE payout_requests SET status=?, txid=?, note=?, decided_at=?, admin_id=? WHERE id=? AND status='requested'",
                          ("paid" if action == "paid" else "rejected", txid if action == "paid" else None, note or None, _now(), admin["id"], rid))
        con.commit()
        if cur.rowcount != 1:
            raise HTTPException(409, "This request has already been decided")
    _notify(row["user_id"], admin["id"], f"Your commission payout of ${usd(row['amount_cents']):.2f} has been sent. Transaction: {txid}" if action == "paid"
            else f"Your commission payout request of ${usd(row['amount_cents']):.2f} was not paid: {note}. The amount is back in your balance.")
    return JSONResponse({"ok": True})


routes = [
    Route("/api/rewards/payout-details", api_save_details, methods=["POST"]),
    Route("/api/rewards/payout-request", api_request_payout, methods=["POST"]),
    Route("/api/admin/commission", admin_view, methods=["GET"]),
    Route("/api/admin/commission/preview", admin_preview, methods=["POST"]),
    Route("/api/admin/commission/apply", admin_apply, methods=["POST"]),
    Route("/api/admin/commission/payout", admin_decide, methods=["POST"]),
]
