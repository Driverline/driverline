"""Partner attribution: the two-stage check Deriv support recommended.

Stage 1: carry the partner attribution values through BOTH Deriv login and Deriv sign-up. We never invent or rename them:
  they are read from the partner link saved in Admin (the "Your Deriv IB link" field) exactly as Deriv wrote them: sidc, ONE of
  t / affiliate_token / sidi / ca, and utm_source / utm_medium / utm_campaign. They are added to Deriv's OAuth address (login),
  and for sign-up with prompt=registration (Deriv's documented OAuth sign-up). Set DERIV_SIGNUP_VIA_OAUTH=1 to make the
  "Create a Deriv account" button on the sign-in page use that OAuth sign-up instead of the plain partner link.
Stage 2: a successful OAuth callback proves only that the person authenticated. Every Deriv account we see is stored as
  "provisional". It becomes "confirmed" only after it is checked against Deriv's partner records, either
    * automatically, with Deriv's Partners API (POST /partners/client-tags/check, scope application_read), by signing in as the
      partner from Admin (the token is used once and never stored), or with DERIV_PARTNER_TOKEN if you set one; or
    * by pasting the client IDs from Partners Hub > Active Trader Breakdown into Admin.
By default nothing is blocked. Set PARTNER_GATE=1 to open the Deriv trade button only to confirmed accounts.
"""
import base64
import hashlib
import html
import json
import os
import re
import secrets
import time
from contextlib import closing
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from . import auth, dbx, deriv_login

CHECK_URL = "https://api.derivws.com/partners/client-tags/check"
TOKEN_ALIASES = ("t", "affiliate_token", "sidi", "ca")          # Deriv: equivalent names, use only ONE of them
KEEP = ("sidc",) + TOKEN_ALIASES + ("utm_source", "utm_medium", "utm_campaign")
VAL = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")
STATUSES = ("provisional", "confirmed", "not_tagged")
STATE_TTL = 600
ID_TOKEN = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
USERINFO_URL = "https://auth.deriv.com/userinfo"
BATCH = 100                                                       # Deriv's limit per call

SCHEMA = """
    CREATE TABLE IF NOT EXISTS partner_tags(account_id TEXT PRIMARY KEY, user_id INTEGER, status TEXT NOT NULL DEFAULT 'provisional',
        source TEXT, attribution TEXT, first_seen BIGINT NOT NULL, checked_at BIGINT, note TEXT);
    CREATE INDEX IF NOT EXISTS ix_ptags_status ON partner_tags(status);
    CREATE INDEX IF NOT EXISTS ix_ptags_user ON partner_tags(user_id);
    CREATE TABLE IF NOT EXISTS partner_states(state TEXT PRIMARY KEY, verifier TEXT NOT NULL, user_id INTEGER NOT NULL,
        created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS partner_meta(key TEXT PRIMARY KEY, value TEXT)"""


def _migrate(con):
    con.add_column("partner_states", "probe", "TEXT")
    con.add_column("partner_tags", "client_uuid", "TEXT")
    con.add_column("partner_tags", "deriv_sub", "TEXT")
    con.add_column("partner_tags", "display_id", "TEXT")


def db():
    auth.db().close()
    return dbx.connect("partner", SCHEMA, _migrate)


# ---------------------------------------------------------------- stage 1: the values we carry
def resolved_params() -> dict:
    """The partner values exactly as they appear in the partner link saved in Admin. Nothing renamed, nothing invented."""
    link = deriv_login._cfg()["ib"]
    out, token_done = {}, False
    try:
        pairs = parse_qsl(urlsplit(link).query, keep_blank_values=False)
    except Exception:
        return {}
    for k, v in pairs:
        if k not in KEEP or not VAL.match(v):
            continue
        if k in TOKEN_ALIASES:
            if token_done:
                continue
            token_done = True
        out.setdefault(k, v)
    return out


def oauth_extra(signup: bool) -> dict:
    """Extra query values for Deriv's OAuth address: the partner values on login and sign-up, plus prompt=registration for
    sign-up. DERIV_PARTNER_ON_LOGIN=0 keeps them off the login address."""
    p = resolved_params() if (signup or os.environ.get("DERIV_PARTNER_ON_LOGIN", "1") != "0") else {}
    if signup:
        p["prompt"] = "registration"
    if capture_on():
        p["scope"] = "trade openid"             # later keys win in the address built by deriv_login.start
    return p


UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def capture_on() -> bool:
    """DERIV_CAPTURE_CLIENT_ID=1: also ask Deriv for the unique user ID (scope openid) when members sign in or connect, so each
    member can be matched to the client ID Partners Hub shows. Off by default."""
    return os.environ.get("DERIV_CAPTURE_CLIENT_ID", "0") == "1"


def trade_scope() -> str:
    return "trade openid" if capture_on() else "trade"


async def fetch_identity(token: str):
    """{'client_uuid', 'sub'} from Deriv's userinfo for this token, or None. Only when capture is on; failures never break login."""
    if not capture_on():
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as cl:
            u = await cl.get(USERINFO_URL, headers={"Authorization": f"Bearer {token}", "accept": "application/json"})
        d = u.json() if u.status_code == 200 else {}
    except Exception:
        return None
    cid, sub = d.get("cid"), d.get("sub")
    return {"client_uuid": cid.lower() if isinstance(cid, str) and UUID_RE.match(cid) else None,
            "sub": sub.lower() if isinstance(sub, str) and UUID_RE.match(sub) else None}


def note_identity(ids, ident, source: str = "signin"):
    """Remember which Deriv client UUID belongs to these account IDs (the accounts stay provisional)."""
    ids = [i for i in (ids or []) if i]
    if not ids or not ident or not (ident.get("client_uuid") or ident.get("sub")):
        return
    now = int(time.time())
    with closing(db()) as con:
        for i in ids:
            con.execute("INSERT INTO partner_tags(account_id,user_id,status,source,attribution,first_seen,client_uuid,deriv_sub) "
                        "VALUES(?,NULL,'provisional',?,NULL,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
                        "client_uuid=COALESCE(excluded.client_uuid, partner_tags.client_uuid), "
                        "deriv_sub=COALESCE(excluded.deriv_sub, partner_tags.deriv_sub)",
                        (i, source, now, ident.get("client_uuid"), ident.get("sub")))
        con.commit()


def signup_link(plain_link: str) -> str:
    """What the sign-in page's "Create a Deriv account" button opens."""
    if plain_link and os.environ.get("DERIV_SIGNUP_VIA_OAUTH") == "1":
        return "/auth/deriv/start?mode=signup"
    return plain_link


# ---------------------------------------------------------------- stage 2: provisional until Deriv's partner records agree
def record_accounts(ids, user_id, source: str, request=None):
    ids = [i for i in (ids or []) if i]
    if not ids:
        return
    visit = None
    if request is not None:
        try:
            from . import track
            visit = track.from_request(request)
        except Exception:
            visit = None
    attr = json.dumps({"sent": resolved_params(), "visit": visit})
    now = int(time.time())
    with closing(db()) as con:
        for i in ids:
            con.execute("INSERT INTO partner_tags(account_id,user_id,status,source,attribution,first_seen) "
                        "VALUES(?,?,'provisional',?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
                        "user_id=COALESCE(excluded.user_id, partner_tags.user_id), "
                        "attribution=COALESCE(partner_tags.attribution, excluded.attribution)", (i, user_id, source, attr, now))
        con.commit()


def link_user(uid: int):
    """After sign-up with a Deriv invite: attach the member to the Deriv accounts that were verified for that invite."""
    with closing(deriv_login.db()) as con:
        ids = [r["account_id"] for r in con.execute("SELECT account_id FROM deriv_accounts WHERE user_id=?", (uid,)).fetchall()]
    if not ids:
        return
    with closing(db()) as con:
        for i in ids:
            con.execute("UPDATE partner_tags SET user_id=? WHERE account_id=?", (uid, i))
        con.commit()


def gate_on() -> bool:
    return os.environ.get("PARTNER_GATE", "0") == "1"


def require_tagged(user: dict):
    """Only enforced when PARTNER_GATE=1: the Deriv trade button needs a CONFIRMED partner tag."""
    if not gate_on():
        return
    with closing(db()) as con:
        ok = con.execute("SELECT 1 FROM partner_tags WHERE user_id=? AND status='confirmed'", (user["id"],)).fetchone()
    if not ok:
        raise HTTPException(403, "Trading from TradeLens is for Deriv accounts that are tagged to TradeLens, and we have not "
                                 "confirmed yours yet. This is checked regularly. Please try again soon or contact support.")


# ---------------------------------------------------------------- asking Deriv: is this client tagged to me?
class PartnerAuthError(Exception):
    pass


def parse_clients(data, ids) -> dict:
    """{client id: {'is_tagged', 'resolved', 'display_id'}} read from Deriv's answer, however it nests the results."""
    found = {}
    idset = {str(i) for i in ids}

    def info(x):
        return {"is_tagged": x["is_tagged"], "resolved": x.get("resolved") is not False, "display_id": x.get("display_id")}

    def walk(x, depth=0):
        if depth > 6:
            return
        if isinstance(x, dict):
            if isinstance(x.get("is_tagged"), bool):
                for k in ("client_id", "id", "loginid", "client", "external_id"):
                    if x.get(k) is not None:
                        found[str(x[k])] = info(x)
                        break
            for k, v in x.items():
                if str(k) in idset and isinstance(v, bool):
                    found.setdefault(str(k), {"is_tagged": v, "resolved": True, "display_id": None})
                elif str(k) in idset and isinstance(v, dict) and isinstance(v.get("is_tagged"), bool):
                    found[str(k)] = info(v)
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(x, list):
            for v in x:
                walk(v, depth + 1)
    walk(data)
    if not found:                                    # a bare list of {is_tagged} in the same order as the request
        lists = [v for v in (data.values() if isinstance(data, dict) else [data]) if isinstance(v, list)]
        for lst in lists:
            if len(lst) == len(ids) and all(isinstance(e, dict) and isinstance(e.get("is_tagged"), bool) for e in lst):
                found = {str(i): info(e) for i, e in zip(ids, lst)}
    return {i: found[i] for i in (str(x) for x in ids) if i in found}


def parse_tags(data, ids) -> dict:
    """{client id: True/False} for the ids Deriv recognised."""
    return {k: v["is_tagged"] for k, v in parse_clients(data, ids).items() if v["resolved"]}


async def check_ids(headers: dict, ids: list):
    """Calls Deriv for up to 100 ids. Returns (tags for recognised ids, raw json)."""
    clients, data = await check_clients(headers, ids)
    return {k: v["is_tagged"] for k, v in clients.items() if v["resolved"]}, data


async def check_clients(headers: dict, ids: list):
    """Calls Deriv for up to 100 ids. Returns ({id: {is_tagged, resolved, display_id}}, raw json)."""
    async with httpx.AsyncClient(timeout=30) as cl:
        for attempt in (0, 1):
            r = await cl.post(CHECK_URL, headers={**headers, "Content-Type": "application/json", "accept": "application/json"},
                              json={"client_ids": ids})
            if r.status_code in (502, 504) and attempt == 0:
                continue
            break
    if r.status_code == 404:
        raise PartnerAuthError("The Deriv login used is not a partner account, so Deriv does not know who to check tags for.")
    if r.status_code in (401, 403):
        raise PartnerAuthError("Deriv refused the token. Tick 'Application insights' on the TradeLens app at developers.deriv.com "
                               "so the application_read scope is allowed, then try again.")
    if r.status_code >= 400:
        raise PartnerAuthError(f"Deriv could not check the tags just now (status {r.status_code}). Please try again.")
    data = r.json()
    return parse_clients(data, ids), data


async def check_all(headers: dict) -> dict:
    """Checks every provisional or not-tagged account that has a client UUID against Deriv and stores the result."""
    with closing(db()) as con:
        rows = [(r["account_id"], r["client_uuid"]) for r in con.execute(
            "SELECT account_id, client_uuid FROM partner_tags WHERE status IN ('provisional','not_tagged') "
            "ORDER BY first_seen LIMIT 1000").fetchall()]
    by_uuid: dict = {}
    for acc, u in rows:
        if u:
            by_uuid.setdefault(u, []).append(acc)
    done = {"checked": len(rows), "confirmed": 0, "not_tagged": 0, "unknown": 0,
            "no_client_id": sum(1 for _, u in rows if not u)}
    uuids = list(by_uuid)
    raw0 = None
    now = int(time.time())
    for k in range(0, len(uuids), BATCH):
        chunk = uuids[k:k + BATCH]
        clients, raw = await check_clients(headers, chunk)
        raw0 = raw0 or raw
        with closing(db()) as con:
            for u in chunk:
                c = clients.get(u)
                if not c or not c["resolved"]:
                    done["unknown"] += len(by_uuid[u])
                    continue
                st = "confirmed" if c["is_tagged"] else "not_tagged"
                for acc in by_uuid[u]:
                    done[st] += 1
                    con.execute("UPDATE partner_tags SET status=?, checked_at=?, note=?, display_id=? WHERE account_id=?",
                                (st, now, "Deriv partner API" if c["is_tagged"] else "Deriv partner API says not tagged to you",
                                 c.get("display_id"), acc))
            con.commit()
    with closing(db()) as con:
        con.execute("INSERT INTO partner_meta(key,value) VALUES('last_check',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps({"at": now, **done, "sample": json.dumps(raw0)[:400] if raw0 is not None else None}),))
        con.commit()
    return done


# ---------------------------------------------------------------- "check with Deriv" by signing in as the partner (token used once)
async def partner_start(request):
    user = auth.current_user(request)
    try:
        auth.require_admin(request)
    except HTTPException:
        return deriv_login._again("Only the owner can start a partner check.", 403)
    app_id = deriv_login._cfg()["app_id"]
    if not app_id:
        return deriv_login._again("Deriv is not set up in Admin yet.", 503)
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    now = int(time.time())
    # Optional probe: test specific client IDs against Deriv, and (openid=1) also ask which unique ID Deriv gives for a login.
    ids = [i for i in re.split(r"[\s,;]+", request.query_params.get("probe", "")) if ID_TOKEN.match(i)][:20]
    openid = request.query_params.get("openid") == "1"
    probe = json.dumps({"ids": ids, "openid": openid}) if (ids or openid) else None
    with closing(db()) as con:
        con.execute("DELETE FROM partner_states WHERE created_at<?", (now - STATE_TTL,))
        con.execute("INSERT INTO partner_states(state,verifier,user_id,created_at,probe) VALUES(?,?,?,?,?)",
                    (state, verifier, user["id"], now, probe))
        con.commit()
    url = deriv_login.AUTH_URL + "?" + urlencode({
        "response_type": "code", "client_id": app_id, "redirect_uri": deriv_login._base(request) + "/auth/deriv/callback",
        "scope": "application_read" + (" openid" if openid else ""), "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie("dl_oauth", state, max_age=STATE_TTL, httponly=True, samesite="lax", secure=auth.SECURE, path="/auth/deriv")
    return resp


def owns_state(state: str) -> bool:
    if not state:
        return False
    with closing(db()) as con:
        return bool(con.execute("SELECT 1 FROM partner_states WHERE state=?", (state,)).fetchone())


def _back(**q) -> RedirectResponse:
    return RedirectResponse("/admin.html?" + urlencode(q, quote_via=quote), status_code=303)


async def partner_callback(request):
    q = request.query_params
    state = q.get("state", "")
    with closing(db()) as con:
        r = con.execute("SELECT verifier, user_id, created_at, probe FROM partner_states WHERE state=?", (state,)).fetchone()
        con.execute("DELETE FROM partner_states WHERE state=?", (state,))
        con.commit()
    user = auth.current_user(request)
    if q.get("error"):
        return _back(partner="error", msg="Deriv sign-in was cancelled, or a requested scope is not allowed for the app "
                                          f"({(q.get('error_description') or q.get('error'))[:120]}). Tick 'Application insights' "
                                          "at developers.deriv.com, or run the test again without the unique-ID option.")
    if not (r and q.get("code") and time.time() - r["created_at"] <= STATE_TTL and request.cookies.get("dl_oauth") == state
            and user and user["id"] == r["user_id"]):
        return _back(partner="error", msg="That link expired. Start the check again.")
    app_id = deriv_login._cfg()["app_id"]
    try:
        async with httpx.AsyncClient(timeout=20) as cl:
            t = await cl.post(deriv_login.TOKEN_URL, headers={"accept": "application/json"},
                              data={"grant_type": "authorization_code", "client_id": app_id, "code": q["code"],
                                    "code_verifier": r["verifier"], "redirect_uri": deriv_login._base(request) + "/auth/deriv/callback"})
            t.raise_for_status()
            body = t.json()
            token = body.get("access_token")
        if not token:
            raise ValueError("no token")
        headers = {"Authorization": f"Bearer {token}"}                    # the token lives only inside this request
        if r["probe"]:
            await run_probe(headers, body, json.loads(r["probe"]))
            resp = _back(partner="probe")
            resp.delete_cookie("dl_oauth", path="/auth/deriv")
            return resp
        done = await check_all(headers)
    except PartnerAuthError as e:
        return _back(partner="error", msg=str(e))
    except Exception as e:
        print(f"[partner] check failed: {type(e).__name__}", flush=True)
        return _back(partner="error", msg="We could not complete the check just now. Please try again.")
    resp = _back(partner="done", **done)
    resp.delete_cookie("dl_oauth", path="/auth/deriv")
    return resp


def _jwt_claims(tok: str):
    parts = str(tok).split(".")
    if len(parts) != 3:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except Exception:
        return None


async def run_probe(headers: dict, token_body: dict, opts: dict):
    """Finds out, once, how Deriv identifies people: which unique IDs a login carries, and whether Partners Hub's client IDs (and the
    login's own IDs) are recognised by the tag check. Stores shapes and the owner's own identifiers, never a token."""
    out = {"at": int(time.time()), "scope_openid": bool(opts.get("openid")), "token_keys": sorted(token_body.keys()),
           "token_scope": str(token_body.get("scope", ""))[:80]}
    claims = _jwt_claims(token_body.get("access_token", "")) or _jwt_claims(token_body.get("id_token", ""))
    if claims is not None:
        out["jwt_claims"] = {k: str(v)[:60] for k, v in claims.items()}
    own = {}
    async with httpx.AsyncClient(timeout=20) as cl:
        try:
            u = await cl.get(USERINFO_URL, headers={**headers, "accept": "application/json"})
            ui = u.json() if u.status_code == 200 else {}
            ui = ui if isinstance(ui, dict) else {}
            out["userinfo"] = {"status": u.status_code, "keys": sorted(ui.keys()),
                               "values": {k: (",".join(map(str, v)) if isinstance(v, list) else str(v))[:80] for k, v in ui.items()
                                          if k in ("sub", "cid", "aud", "amr", "iss")}}
            for k in ("cid", "sub"):
                if isinstance(ui.get(k), str) and UUID_RE.match(ui[k]):
                    own[ui[k].lower()] = k
        except Exception as e:
            out["userinfo"] = {"error": type(e).__name__}
    ids = list(dict.fromkeys([*(opts.get("ids") or []), *own]))
    if ids:
        try:
            clients, raw = await check_clients(headers, ids)
            out["check"] = {"clients": {i: {**clients.get(i, {"is_tagged": None, "resolved": False, "display_id": None}),
                                           "own": own.get(i.lower())} for i in ids}, "raw": json.dumps(raw)[:700]}
        except PartnerAuthError as e:
            out["check"] = {"error": str(e)}
    with closing(db()) as con:
        con.execute("INSERT INTO partner_meta(key,value) VALUES('last_probe',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(out),))
        con.commit()


# ---------------------------------------------------------------- admin API
def _pat_headers():
    tok = os.environ.get("DERIV_PARTNER_TOKEN", "").strip()
    return {"Authorization": f"Bearer {tok}", "Deriv-App-ID": deriv_login._cfg()["app_id"]} if tok else None


async def api_check(request):
    auth.require_admin(request)
    h = _pat_headers()
    if not h:
        raise HTTPException(409, "No partner token is set. Use 'Check with Deriv' to sign in as the partner instead.")
    try:
        return JSONResponse(await check_all(h))
    except PartnerAuthError as e:
        raise HTTPException(422, str(e))


async def api_summary(request):
    auth.require_admin(request)
    with closing(db()) as con:
        counts = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) AS n FROM partner_tags GROUP BY status").fetchall()}
        last = con.execute("SELECT value FROM partner_meta WHERE key='last_check'").fetchone()
    p = resolved_params()
    with closing(db()) as con:
        pr = con.execute("SELECT value FROM partner_meta WHERE key='last_probe'").fetchone()
    with closing(db()) as con:
        captured = con.execute("SELECT COUNT(*) AS c FROM partner_tags WHERE client_uuid IS NOT NULL").fetchone()["c"]
    return JSONResponse({"capture_on": capture_on(), "captured": captured, "last_probe": json.loads(pr["value"]) if pr else None, "counts": {s: counts.get(s, 0) for s in STATUSES}, "gate": gate_on(),
                         "signup_via_oauth": os.environ.get("DERIV_SIGNUP_VIA_OAUTH") == "1",
                         "token_set": bool(_pat_headers()), "values_carried": sorted(p.keys()),
                         "last_check": json.loads(last["value"]) if last else None})


async def api_list(request):
    auth.require_admin(request)
    st = request.query_params.get("status", "")
    sql = "SELECT account_id,user_id,status,source,attribution,first_seen,checked_at,note,client_uuid,display_id FROM partner_tags"
    args = ()
    if st in STATUSES:
        sql, args = sql + " WHERE status=?", (st,)
    with closing(db()) as con:
        rows = [dict(r) for r in con.execute(sql + " ORDER BY first_seen DESC LIMIT 60", args).fetchall()]
    with closing(auth.db()) as con:
        for r in rows:
            u = con.execute("SELECT email FROM users WHERE id=?", (r["user_id"],)).fetchone() if r["user_id"] else None
            r["email"] = u["email"] if u else None
            try:
                r["attribution"] = json.loads(r["attribution"] or "null")
            except ValueError:
                r["attribution"] = None
    return JSONResponse({"rows": rows})


async def api_set(request):
    auth.require_admin(request)
    d = await auth._json(request)
    aid, st = str(d.get("account_id", "")), d.get("status")
    if st not in STATUSES or not deriv_login.ID_RE.match(aid):
        raise HTTPException(422, "Invalid request")
    with closing(db()) as con:
        cur = con.execute("UPDATE partner_tags SET status=?, checked_at=?, note=? WHERE account_id=?",
                          (st, int(time.time()), "Set by owner", aid))
        con.commit()
    if not cur.rowcount:
        raise HTTPException(404, "Account not found")
    return JSONResponse({"ok": True})


async def api_paste(request):
    """The Partners Hub route: paste client IDs from Active Trader Breakdown; matching accounts become confirmed."""
    auth.require_admin(request)
    d = await auth._json(request)
    text = str(d.get("text", ""))[:200000]
    found = {m.upper() for m in re.findall(r"[A-Za-z]{0,5}\d{3,12}|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}", text)}
    if not found:
        raise HTTPException(422, "No client IDs found in what you pasted.")
    now = int(time.time())
    matched = []
    with closing(db()) as con:
        for r in con.execute("SELECT account_id FROM partner_tags").fetchall():
            if r["account_id"].upper() in found:
                con.execute("UPDATE partner_tags SET status='confirmed', checked_at=?, note='Matched to Partners Hub list' WHERE account_id=?",
                            (now, r["account_id"]))
                matched.append(r["account_id"])
        con.commit()
    return JSONResponse({"pasted": len(found), "matched": len(matched), "unmatched_sample": sorted(x for x in found - {m.upper() for m in matched} if len(x) >= 5)[:20]})


routes = [
    Route("/auth/deriv/partner/start", partner_start, methods=["GET"]),
    Route("/api/admin/partner/summary", api_summary, methods=["GET"]),
    Route("/api/admin/partner/list", api_list, methods=["GET"]),
    Route("/api/admin/partner/set", api_set, methods=["POST"]),
    Route("/api/admin/partner/paste", api_paste, methods=["POST"]),
    Route("/api/admin/partner/check", api_check, methods=["POST"]),
]
