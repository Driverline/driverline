"""TradeLens partner referral programme: Master Partner -> TradeLens sub-partners -> referred TradeLens members.

TradeLens keeps ONLY its own records here: who invited whom inside TradeLens, which Deriv referral link belongs to which TradeLens partner,
and a record of the screens and consents a member went through before being sent to Deriv. It does not control, check or claim Deriv's
official attribution. Deriv alone decides partner approval, client tagging, partner relationships and commissions. A TradeLens
registration or referral row is NEVER proof of Deriv client attribution. There is no Deriv API or webhook here: verification is manual
(an admin records what Deriv Partners Hub shows, with the source and the date).

Two separate member journeys (never mixed):
  A. Trader   - a member who wants a NEW Deriv trading account is sent to the Deriv trader referral link of the TradeLens partner who
                introduced them (or to the owner's own "house" link when nobody did). Only when that partner is approved AND their link is
                verified. Otherwise the hand-off is refused with an explanation. The Master Partner link is NEVER used as a fallback.
  B. Partner  - a member who wants to become a Deriv partner is sent to the Master Partner link (configured in Admin, never hard-coded),
                or, when they are already a Deriv client tagged under TradeLens, may skip it. The application waits for Deriv's approval and
                an admin's manual verification before the member can operate as a sub-partner.

Referral attribution policy: FIRST VALID TOUCH AT SIGNUP. The code arrives as ?ref=CODE on /login.html, track.js keeps it in a 30-day
cookie (first touch wins) and signup attaches it once. It is checked on the server (it must belong to an approved partner), it is never
re-attached or overwritten later, and nobody can be attributed to themselves.

Stage gate (Admin > Partner programme > Mode): off | review | public.
  off     only admins can see anything; no referral is recorded and the old universal Deriv link is not shown or used.
  review  admins and the listed reviewer accounts (read-only admin view, e-mails masked) can use every screen; referrals are recorded.
  public  every signed-in member. Refused until a launch record has been entered in Admin: either Deriv's written approval and its
          conditions, or the owner's own dated decision to launch without Deriv approval. It must never say Deriv approved when it did not.
Tables are new and additive (migrations/2026_11_partners.sql); nothing existing is edited."""
import asyncio
import json
import re
import secrets
import time
from contextlib import closing
from urllib.parse import urlsplit

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, dbx

SCHEMA = """
    CREATE TABLE IF NOT EXISTS tl_partners(id {ID}, user_id INTEGER UNIQUE, partner_code TEXT UNIQUE NOT NULL, ref_code TEXT UNIQUE NOT NULL,
        is_house INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, applied_via TEXT, deriv_partner_id TEXT, deriv_referral_link TEXT,
        link_status TEXT NOT NULL DEFAULT 'unverified', link_verified_at BIGINT, verification_source TEXT, verification_date TEXT,
        consent_at BIGINT, approved_at BIGINT, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_tl_partner_house ON tl_partners(is_house) WHERE is_house=1;
    CREATE TABLE IF NOT EXISTS tl_partner_referrals(referred_id INTEGER PRIMARY KEY, partner_id INTEGER NOT NULL, code TEXT NOT NULL,
        attribution_status TEXT NOT NULL DEFAULT 'attributed', created_at BIGINT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_tl_partner_ref_partner ON tl_partner_referrals(partner_id);
    CREATE TABLE IF NOT EXISTS tl_partner_events(id {ID}, partner_id INTEGER, user_id INTEGER, kind TEXT NOT NULL, detail TEXT,
        created_at BIGINT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_tl_partner_ev ON tl_partner_events(partner_id, kind);
    CREATE TABLE IF NOT EXISTS tl_partner_consents(id {ID}, user_id INTEGER NOT NULL, kind TEXT NOT NULL, version TEXT NOT NULL,
        partner_id INTEGER, created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS tl_partner_verifications(id {ID}, partner_id INTEGER NOT NULL, kind TEXT NOT NULL, subject TEXT,
        source TEXT NOT NULL, verified_on TEXT NOT NULL, note TEXT, admin_id INTEGER NOT NULL, created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS tl_partner_audit(id {ID}, admin_id INTEGER, partner_id INTEGER, action TEXT NOT NULL, detail TEXT,
        created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS tl_partner_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)"""

MODES = ("off", "review", "public")
STATUSES = ("application_initiated", "awaiting_deriv_approval", "approved", "rejected", "suspended")
STATUS_LABEL = {"none": "Not applied", "application_initiated": "Application initiated", "awaiting_deriv_approval": "Awaiting Deriv approval",
                "approved": "Approved", "rejected": "Rejected", "suspended": "Suspended"}
CONSENT_VERSION = "2026-11-v2"
COOKIE = "dl_pr"
REF_RE = re.compile(r"^[A-Za-z0-9]{6,12}$")
DERIV_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{3,40}$")
DATE_RE = re.compile(r"^20\d\d-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
DERIV_HOSTS = {"t.deriv.link", "deriv.link", "deriv.com", "www.deriv.com"}
MAX_REVIEWERS = 20

TRADER_DISCLOSURE = [
    "You are about to leave TradeLens and open Deriv's own website. TradeLens is independent of Deriv and is not owned, operated or endorsed by Deriv unless Deriv says so in writing.",
    "This link belongs to the TradeLens partner shown below, who is a Deriv partner. If you open an account through it, that partner and/or TradeLens may receive a commission from Deriv. It does not change what you pay. TradeLens does not share that commission with you and gives no points or rewards for it.",
    "Deriv alone decides whether an account is tagged to a partner. A TradeLens referral is not proof of tagging and tagging is not guaranteed.",
    "Deriv's own terms, fees and checks apply to the account. Trading leveraged products and synthetic indices is high risk, you can lose all your money, and nothing in TradeLens is advice or a promise of profit.",
    "If you already have a Deriv account, this does not change how it is tagged. TradeLens never changes the attribution of an existing account.",
    "We keep a record that you chose to continue (date, version of this notice and the partner) for our referral records. How we handle your personal data is described in the TradeLens Privacy Notice (/privacy.html).",
]
PARTNER_DISCLOSURE = [
    "Deriv runs its own independent partner application and approval process. Signing up to TradeLens is not Deriv partner approval, and TradeLens cannot approve you.",
    "Deriv partner commissions are set and paid by Deriv under Deriv's terms. TradeLens does not guarantee that you will be approved or that you will earn anything.",
    "To apply you will be taken to Deriv through TradeLens's Master Partner link. TradeLens may receive a commission from Deriv on partners it introduces. This is a financial interest in your decision.",
    "TradeLens gives no points, prizes or payments for becoming a partner, for introducing Deriv clients, for trading volume or for partner commissions.",
    "If you are already a Deriv client tagged under TradeLens and have become a Deriv partner, you do not need to apply again through the link: Deriv has told us such a client becomes a sub-partner. Please confirm this with Deriv. Choose the second option below.",
    "You can operate as a TradeLens partner only after Deriv has approved you and a TradeLens admin has verified it by hand. We store your application status, the Deriv partner ID you enter and this consent. How we handle your personal data is described in the TradeLens Privacy Notice (/privacy.html).",
]

_rl: dict = {}


def _now() -> int:
    return int(time.time())


def db():
    auth.db().close()
    return dbx.connect("partners", SCHEMA)


# ------------------------------------------------------------------ settings, mode, access
def _settings(con) -> dict:
    return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM tl_partner_settings").fetchall()}


def _set(con, key: str, value: str):
    con.execute("INSERT INTO tl_partner_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def mode() -> str:
    try:
        with closing(db()) as con:
            m = _settings(con).get("mode", "off")
    except Exception as e:
        print(f"[partners] mode: {type(e).__name__}", flush=True)
        return "off"
    return m if m in MODES else "off"


def legacy_link_allowed() -> bool:
    """The old universal Deriv link (setting deriv_ib_link) is only shown or used while the programme is public. See deriv_login._cfg()."""
    return mode() == "public"


def _reviewers(s: dict) -> set:
    return {e.strip().lower() for e in (s.get("review_emails") or "").split(",") if e.strip()}


def is_reviewer(user: dict, s: dict = None) -> bool:
    if s is None:
        with closing(db()) as con:
            s = _settings(con)
    return str(user.get("email", "")).lower() in _reviewers(s)


def has_access(user: dict) -> bool:
    if user.get("tier") == "admin":
        return True
    with closing(db()) as con:
        s = _settings(con)
    m = s.get("mode", "off")
    if m == "public":
        return True
    return m == "review" and is_reviewer(user, s)


def _need_access(request) -> dict:
    u = auth.require_user(request)
    if not has_access(u):
        raise HTTPException(404, "Not available")
    return u


def _need_reviewer(request) -> dict:
    """Admin, or a listed reviewer: read-only."""
    u = auth.require_user(request)
    if u.get("tier") == "admin" or is_reviewer(u):
        return u
    raise HTTPException(404, "Not available")


# ------------------------------------------------------------------ validation
def valid_deriv_url(url) -> str:
    """An official-looking Deriv https address, or HTTPException. Hosts can be extended with PARTNER_LINK_HOSTS (comma separated)."""
    import os
    u = str(url or "").strip()
    extra = {h.strip().lower() for h in os.environ.get("PARTNER_LINK_HOSTS", "").split(",") if h.strip()}
    if not u or len(u) > 300 or any(c.isspace() or ord(c) < 32 for c in u):
        raise HTTPException(422, "Enter the full Deriv link (https://...)")
    try:
        p = urlsplit(u)
        host, port = (p.hostname or "").lower(), p.port
    except ValueError:
        raise HTTPException(422, "That does not look like a valid link")
    if p.scheme != "https" or p.username or p.password or port not in (None, 443) or host not in (DERIV_HOSTS | extra):
        raise HTTPException(422, "Only official Deriv https links are accepted (" + ", ".join(sorted(DERIV_HOSTS | extra)) + ")")
    return u


def _clean(text, limit=200) -> str:
    return " ".join(str(text or "").split())[:limit]


def _ratelimit(key: str, n: int, per: int = 60):
    now = time.time()
    hits = [t for t in _rl.get(key, []) if now - t < per]
    if len(hits) >= n:
        raise HTTPException(429, "Too many attempts. Please wait a minute.")
    hits.append(now)
    _rl[key] = hits
    if len(_rl) > 5000:
        _rl.clear()


def _event(con, partner_id, user_id, kind: str, detail=None):
    con.execute("INSERT INTO tl_partner_events(partner_id,user_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                (partner_id, user_id, kind, json.dumps(detail) if detail is not None else None, _now()))


def _audit(con, admin_id, partner_id, action: str, detail=None):
    con.execute("INSERT INTO tl_partner_audit(admin_id,partner_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                (admin_id, partner_id, action, json.dumps(detail) if detail is not None else None, _now()))


def _consent(con, uid: int, kind: str, partner_id=None):
    con.execute("INSERT INTO tl_partner_consents(user_id,kind,version,partner_id,created_at) VALUES(?,?,?,?,?)",
                (uid, kind, CONSENT_VERSION, partner_id, _now()))


def _need_consent(d: dict):
    if d.get("consent") is not True or d.get("version") != CONSENT_VERSION:
        raise HTTPException(422, "Please read the notice and tick the box to continue")


# ------------------------------------------------------------------ partner records
def _new_ref(con) -> str:
    for _ in range(30):
        code = "TL" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not con.execute("SELECT 1 FROM tl_partners WHERE ref_code=?", (code,)).fetchone():
            return code
    raise RuntimeError("could not make a referral code")


def _create_partner(con, user_id, status: str, applied_via=None, is_house=0, link=None, deriv_id=None) -> int:
    now = _now()
    pid = con.insert("INSERT INTO tl_partners(user_id,partner_code,ref_code,is_house,status,applied_via,deriv_referral_link,deriv_partner_id,"
                     "consent_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (user_id, "tmp-" + secrets.token_hex(6), _new_ref(con), is_house, status, applied_via, link, deriv_id, now, now, now))
    con.execute("UPDATE tl_partners SET partner_code=? WHERE id=?", (f"TLP{pid:04d}", pid))
    return pid


def _by_user(con, uid: int):
    return con.execute("SELECT * FROM tl_partners WHERE user_id=?", (uid,)).fetchone()


def _base(request) -> str:
    from . import rewards
    return rewards._base(request)


def _own_view(p, request) -> dict:
    approved = p["status"] == "approved"
    return {"partner_id": p["partner_code"], "status": p["status"], "status_label": STATUS_LABEL.get(p["status"], p["status"]),
            "referral_code": p["ref_code"] if approved else None,
            "referral_url": f"{_base(request)}/login.html?ref={p['ref_code']}" if approved else None,
            "deriv_partner_id": p["deriv_partner_id"], "has_trader_link": bool(p["deriv_referral_link"]),
            "link_status": p["link_status"], "is_house": bool(p["is_house"]), "applied_via": p["applied_via"],
            "can_refer_traders": approved and p["link_status"] == "verified" and bool(p["deriv_referral_link"])}


def _referrer_of(con, uid: int):
    """The partner who introduced this member (first valid touch), or None."""
    r = con.execute("SELECT partner_id FROM tl_partner_referrals WHERE referred_id=?", (uid,)).fetchone()
    return con.execute("SELECT * FROM tl_partners WHERE id=?", (r["partner_id"],)).fetchone() if r else None


def _house(con):
    return con.execute("SELECT * FROM tl_partners WHERE is_house=1").fetchone()


def _resolve_trader_partner(con, uid: int):
    """(partner row, why_not). The introducing partner if there is one, otherwise the owner's house record. Never the Master link."""
    ref = _referrer_of(con, uid)
    p, who = (ref, "your TradeLens partner") if ref else (_house(con), "TradeLens")
    if p is None:
        return None, "No Deriv referral link has been approved for you yet."
    if p["status"] != "approved":
        return p, f"{who} is not an active partner right now, so we cannot send you to a partner link."
    if p["link_status"] != "verified" or not p["deriv_referral_link"]:
        return p, f"The Deriv link of {who} has not been verified yet, so we cannot send you to it."
    return p, None


# ------------------------------------------------------------------ attribution at signup (called from auth.signup)
def on_signup(uid: int, request):
    """First valid referral code wins. Unknown or inactive codes are logged and ignored; a member is never attributed twice."""
    if mode() == "off":
        return
    code = str(request.cookies.get(COOKIE, "") or "").strip().upper()
    if not REF_RE.match(code):
        return
    with closing(db()) as con:
        p = con.execute("SELECT * FROM tl_partners WHERE ref_code=?", (code,)).fetchone()
        if not p:
            _event(con, None, uid, "code_rejected", {"reason": "unknown"})
        elif p["status"] != "approved":
            _event(con, p["id"], uid, "code_rejected", {"reason": "partner_not_active"})
        elif p["user_id"] == uid:
            _event(con, p["id"], uid, "self_referral_blocked")
        else:
            cur = con.execute("INSERT INTO tl_partner_referrals(referred_id,partner_id,code,attribution_status,created_at) "
                              "VALUES(?,?,?,'attributed',?) ON CONFLICT(referred_id) DO NOTHING", (uid, p["id"], code, _now()))
            _event(con, p["id"], uid, "signup_attributed" if cur.rowcount == 1 else "duplicate_ignored")
        con.commit()


async def public_check(request):
    """Is this referral code usable? Answers only yes or no, and only once the programme is switched on."""
    _ratelimit("chk:" + (request.client.host if request.client else "?"), 30)
    code = str(request.query_params.get("code", "")).strip().upper()
    if mode() == "off":
        return JSONResponse({"active": False, "valid": False})
    if not REF_RE.match(code):
        return JSONResponse({"active": True, "valid": False})
    with closing(db()) as con:
        p = con.execute("SELECT status FROM tl_partners WHERE ref_code=?", (code,)).fetchone()
    return JSONResponse({"active": True, "valid": bool(p and p["status"] == "approved")})


# ------------------------------------------------------------------ member API
async def api_access(request):
    u = auth.require_user(request)
    return JSONResponse({"access": has_access(u)})


def _disclosures() -> dict:
    return {"version": CONSENT_VERSION, "trader": TRADER_DISCLOSURE, "partner": PARTNER_DISCLOSURE}


def _has_deriv_account(uid: int) -> bool:
    try:
        from . import deriv_login
        with closing(deriv_login.db()) as con:
            return bool(con.execute("SELECT 1 FROM deriv_accounts WHERE user_id=?", (uid,)).fetchone())
    except Exception:
        return False


async def api_me(request):
    u = _need_access(request)

    def work():
        with closing(db()) as con:
            own = _by_user(con, u["id"])
            part, why = _resolve_trader_partner(con, u["id"])
            ref = _referrer_of(con, u["id"])
            s = _settings(con)
        return {"mode": s.get("mode", "off"), "review": is_reviewer(u, s) or u["tier"] == "admin" and s.get("mode") != "public",
                "disclosures": _disclosures(), "partner": _own_view(own, request) if own else None,
                "status_label": STATUS_LABEL["none"] if not own else STATUS_LABEL.get(own["status"]),
                "master_link_set": bool(s.get("master_link")),
                "trader": {"referred_by_partner": bool(ref), "available": why is None, "reason": why,
                           "partner_ref": part["partner_code"] if part and why is None else None,
                           "via": "partner" if ref else "house", "has_deriv_account": _has_deriv_account(u["id"])}}
    return JSONResponse(await asyncio.to_thread(work))


async def api_trader_handoff(request):
    """Journey A. Consent first, then the verified Deriv link of the introducing partner. Never the Master Partner link."""
    u = _need_access(request)
    d = await auth._json(request)
    _need_consent(d)
    if d.get("new_account") is not True:
        raise HTTPException(422, "Please confirm that you want to open a NEW Deriv account")
    _ratelimit(f"th:{u['id']}", 10)

    def work():
        with closing(db()) as con:
            p, why = _resolve_trader_partner(con, u["id"])
            if why:
                _event(con, p["id"] if p else None, u["id"], "trader_handoff_blocked", {"reason": why})
                con.commit()
                return None, why
            _consent(con, u["id"], "trader_handoff", p["id"])
            _event(con, p["id"], u["id"], "trader_handoff", {"via": "house" if p["is_house"] else "partner", "consent": CONSENT_VERSION})
            con.commit()
            return p["deriv_referral_link"], None
    url, why = await asyncio.to_thread(work)
    if why:
        raise HTTPException(409, why)
    return JSONResponse({"redirect": url})


async def api_become_start(request):
    """Journey B, step 1: consent, then either the Master Partner link or, for an already-tagged client, straight to awaiting approval."""
    u = _need_access(request)
    d = await auth._json(request)
    _need_consent(d)
    path = d.get("path")
    if path not in ("master_link", "already_tagged"):
        raise HTTPException(422, "Choose how you want to continue")
    _ratelimit(f"bp:{u['id']}", 10)

    def work():
        with closing(db()) as con:
            s = _settings(con)
            own = _by_user(con, u["id"])
            if own and own["status"] in ("approved", "suspended", "awaiting_deriv_approval") and not (own["status"] == "awaiting_deriv_approval" and path == "master_link"):
                raise HTTPException(409, "You already have a partner application: " + STATUS_LABEL[own["status"]])
            if path == "master_link" and not s.get("master_link"):
                raise HTTPException(409, "The partner application link is not available yet")
            status = "application_initiated" if path == "master_link" else "awaiting_deriv_approval"
            if own:
                con.execute("UPDATE tl_partners SET status=?, applied_via=?, consent_at=?, updated_at=? WHERE id=?",
                            (status, path, _now(), _now(), own["id"]))
                pid = own["id"]
            else:
                pid = _create_partner(con, u["id"], status, path)
            _consent(con, u["id"], "partner_application", pid)
            _event(con, pid, u["id"], "master_handoff" if path == "master_link" else "already_tagged_declared",
                   {"consent": CONSENT_VERSION, "review_account": u["tier"] == "admin" or is_reviewer(u, s)})
            code = con.execute("SELECT partner_code FROM tl_partners WHERE id=?", (pid,)).fetchone()["partner_code"]
            con.commit()
            return (s.get("master_link") if path == "master_link" else None), code
    url, code = await asyncio.to_thread(work)
    if u["tier"] != "admin":
        from . import admin_alerts
        admin_alerts.partner(u["id"], code, "started")
    return JSONResponse({"redirect": url, "status": "application_initiated" if url else "awaiting_deriv_approval"})


async def api_become_submit(request):
    """Journey B, step 2: the member says they have applied at Deriv and gives their Deriv partner ID. It waits for manual verification."""
    u = _need_access(request)
    d = await auth._json(request)
    did = str(d.get("deriv_partner_id", "")).strip()
    if did and not DERIV_ID_RE.match(did):
        raise HTTPException(422, "The Deriv partner ID may only contain letters, numbers, dots, dashes and underscores (3 to 40 characters)")

    def work():
        with closing(db()) as con:
            own = _by_user(con, u["id"])
            if not own or own["status"] not in ("application_initiated", "awaiting_deriv_approval", "rejected"):
                raise HTTPException(409, "Start your application first")
            con.execute("UPDATE tl_partners SET status='awaiting_deriv_approval', deriv_partner_id=COALESCE(?, deriv_partner_id), updated_at=? WHERE id=?",
                        (did or None, _now(), own["id"]))
            _event(con, own["id"], u["id"], "application_submitted", {"deriv_partner_id_given": bool(did)})
            con.commit()
            return own["partner_code"]
    code = await asyncio.to_thread(work)
    if u["tier"] != "admin":
        from . import admin_alerts
        admin_alerts.partner(u["id"], code, "submitted")
    return JSONResponse({"ok": True, "status": "awaiting_deriv_approval"})


def _dashboard(u: dict, request) -> dict:
    with closing(db()) as con:
        p = _by_user(con, u["id"])
        if not p:
            return {"partner": None}
        ids = [r["referred_id"] for r in con.execute("SELECT referred_id FROM tl_partner_referrals WHERE partner_id=?", (p["id"],)).fetchall()]
        hand = con.execute("SELECT COUNT(DISTINCT user_id) AS n FROM tl_partner_events WHERE partner_id=? AND kind='trader_handoff'",
                           (p["id"],)).fetchone()["n"]
        tagged = con.execute("SELECT COUNT(*) AS n FROM tl_partner_verifications WHERE partner_id=? AND kind='client_tag'", (p["id"],)).fetchone()["n"]
    onboarded = 0
    if ids:
        with closing(auth.db()) as a:
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                onboarded += a.execute(f"SELECT COUNT(DISTINCT user_id) AS n FROM usage WHERE n>0 AND user_id IN ({','.join('?' * len(chunk))})",
                                       chunk).fetchone()["n"]
    return {"partner": _own_view(p, request),
            "counts": {"tradelens_registrations": len(ids), "completed_onboarding": onboarded, "deriv_handoffs_started": hand,
                       "deriv_account_registrations": None, "deriv_confirmed_tagging": tagged},
            "notes": {"tradelens_registrations": "Members who joined TradeLens through your referral link. This is a TradeLens record only.",
                      "completed_onboarding": "Of those, members who have run at least one analysis.",
                      "deriv_handoffs_started": "Members who chose to continue to Deriv through your link from TradeLens. A click is not an account.",
                      "deriv_account_registrations": "TradeLens cannot see Deriv sign-ups, so no figure is shown.",
                      "deriv_confirmed_tagging": "Clients an admin has checked by hand against Deriv Partners Hub. Deriv's own records decide."},
            "explain": "Deriv manages all official partner commissions directly under its own programme. TradeLens does not calculate, estimate, "
                       "hold or pay any Deriv commission, and a TradeLens referral is not proof that Deriv has tagged a client to you."}


async def api_dashboard(request):
    u = _need_access(request)
    return JSONResponse(await asyncio.to_thread(_dashboard, u, request))


# ------------------------------------------------------------------ admin API (reviewers read, admins change)
def _emails(ids, mask: bool) -> dict:
    ids = list({i for i in ids if i})
    if not ids:
        return {}
    from . import rewards
    with closing(auth.db()) as a:
        rows = a.execute(f"SELECT id,email FROM users WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return {r["id"]: (rewards.mask(r["email"]) if mask else r["email"]) for r in rows}


def _row(p, em: dict) -> dict:
    return {"id": p["id"], "partner_id": p["partner_code"], "referral_code": p["ref_code"], "email": em.get(p["user_id"], "?"),
            "is_house": bool(p["is_house"]), "status": p["status"], "status_label": STATUS_LABEL.get(p["status"], p["status"]),
            "deriv_partner_id": p["deriv_partner_id"], "deriv_referral_link": p["deriv_referral_link"], "link_status": p["link_status"],
            "applied_via": p["applied_via"], "verification_source": p["verification_source"], "verification_date": p["verification_date"],
            "created_at": p["created_at"], "updated_at": p["updated_at"]}


async def admin_whoami(request):
    u = _need_reviewer(request)
    return JSONResponse({"admin": u["tier"] == "admin", "read_only": u["tier"] != "admin", "mode": mode()})


async def admin_list(request):
    u = _need_reviewer(request)
    q = _clean(request.query_params.get("q", ""), 80).lower()

    def work():
        with closing(db()) as con:
            rows = con.execute("SELECT * FROM tl_partners ORDER BY id DESC LIMIT 500").fetchall()
        em = _emails([r["user_id"] for r in rows], u["tier"] != "admin")
        out = [_row(r, em) for r in rows]
        if q:
            real = _emails([r["user_id"] for r in rows], False) if u["tier"] == "admin" else {}
            out = [x for x, r in zip(out, rows) if q in x["partner_id"].lower() or q in x["referral_code"].lower()
                   or q in str(real.get(r["user_id"], "")).lower() or q in str(x["deriv_partner_id"] or "").lower()]
        return out[:200]
    return JSONResponse({"partners": await asyncio.to_thread(work)})


async def admin_detail(request):
    u = _need_reviewer(request)
    pid = request.path_params["pid"]
    mask = u["tier"] != "admin"

    def work():
        with closing(db()) as con:
            p = con.execute("SELECT * FROM tl_partners WHERE id=?", (pid,)).fetchone()
            if not p:
                raise HTTPException(404, "Partner not found")
            refs = con.execute("SELECT referred_id, attribution_status, created_at FROM tl_partner_referrals WHERE partner_id=? "
                               "ORDER BY created_at DESC LIMIT 200", (pid,)).fetchall()
            events = con.execute("SELECT user_id, kind, detail, created_at FROM tl_partner_events WHERE partner_id=? ORDER BY id DESC LIMIT 100", (pid,)).fetchall()
            ver = con.execute("SELECT * FROM tl_partner_verifications WHERE partner_id=? ORDER BY id DESC LIMIT 100", (pid,)).fetchall()
            aud = con.execute("SELECT admin_id, action, detail, created_at FROM tl_partner_audit WHERE partner_id=? ORDER BY id DESC LIMIT 100", (pid,)).fetchall()
        em = _emails([p["user_id"]] + [r["referred_id"] for r in refs] + [e["user_id"] for e in events], mask)
        return {"partner": _row(p, em),
                "referred": [{"email": em.get(r["referred_id"], "?"), "status": r["attribution_status"], "at": r["created_at"]} for r in refs],
                "events": [{"email": em.get(e["user_id"], "-"), "kind": e["kind"], "detail": e["detail"], "at": e["created_at"]} for e in events],
                "verifications": [{"kind": v["kind"], "subject": v["subject"], "source": v["source"], "verified_on": v["verified_on"], "note": v["note"],
                                   "at": v["created_at"]} for v in ver],
                "audit": [{"admin_id": a["admin_id"], "action": a["action"], "detail": a["detail"], "at": a["created_at"]} for a in aud]}
    return JSONResponse(await asyncio.to_thread(work))


def _verif_fields(d: dict) -> tuple:
    source, on = _clean(d.get("source"), 120), str(d.get("verified_on", "")).strip()
    if len(source) < 3:
        raise HTTPException(422, "Say where you checked it (for example: Deriv Partners Hub, Active Trader Breakdown)")
    if not DATE_RE.match(on):
        raise HTTPException(422, "Give the date you checked it as YYYY-MM-DD")
    return source, on, _clean(d.get("note"), 300)


async def admin_status(request):
    """approve / reject / suspend / activate. Every change is audited. Approval needs a Deriv partner ID and the verification source and date."""
    admin = auth.require_admin(request)
    pid = request.path_params["pid"]
    d = await auth._json(request)
    action = str(d.get("action", ""))
    if action not in ("approve", "reject", "suspend", "activate"):
        raise HTTPException(422, "action must be approve, reject, suspend or activate")

    def work():
        with closing(db()) as con:
            p = con.execute("SELECT * FROM tl_partners WHERE id=?", (pid,)).fetchone()
            if not p:
                raise HTTPException(404, "Partner not found")
            st, now = p["status"], _now()
            allowed = {"approve": ("application_initiated", "awaiting_deriv_approval"), "reject": ("application_initiated", "awaiting_deriv_approval", "suspended"),
                       "suspend": ("approved",), "activate": ("suspended",)}[action]
            if st not in allowed:
                raise HTTPException(409, f"A partner who is '{STATUS_LABEL[st]}' cannot be {action}d")
            note = _clean(d.get("note"), 300)
            if action in ("reject", "suspend", "activate") and len(note) < 3:
                raise HTTPException(422, "Give a reason (kept in the audit history)")
            before = {"status": st}
            if action == "approve":
                source, on, vnote = _verif_fields(d)
                did = str(d.get("deriv_partner_id") or p["deriv_partner_id"] or "").strip()
                if not DERIV_ID_RE.match(did):
                    raise HTTPException(422, "Record the member's Deriv partner ID first (3 to 40 letters, numbers, dots, dashes, underscores)")
                con.execute("UPDATE tl_partners SET status='approved', deriv_partner_id=?, verification_source=?, verification_date=?, approved_at=?, updated_at=? WHERE id=?",
                            (did, source, on, now, now, pid))
                con.execute("INSERT INTO tl_partner_verifications(partner_id,kind,subject,source,verified_on,note,admin_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                            (pid, "partner_approval", did, source, on, vnote, admin["id"], now))
            else:
                new = {"reject": "rejected", "suspend": "suspended", "activate": "approved"}[action]
                con.execute("UPDATE tl_partners SET status=?, updated_at=? WHERE id=?", (new, now, pid))
            after = con.execute("SELECT status FROM tl_partners WHERE id=?", (pid,)).fetchone()["status"]
            _audit(con, admin["id"], pid, action, {"before": before["status"], "after": after, "note": note})
            con.commit()
            return after
    return JSONResponse({"ok": True, "status": await asyncio.to_thread(work)})


async def admin_link(request):
    """Add or change the partner's Deriv trader referral link. Changing it resets its verification."""
    admin = auth.require_admin(request)
    pid = request.path_params["pid"]
    d = await auth._json(request)
    url = valid_deriv_url(d.get("deriv_referral_link"))
    did = str(d.get("deriv_partner_id", "")).strip()
    if did and not DERIV_ID_RE.match(did):
        raise HTTPException(422, "Invalid Deriv partner ID")

    def work():
        with closing(db()) as con:
            p = con.execute("SELECT * FROM tl_partners WHERE id=?", (pid,)).fetchone()
            if not p:
                raise HTTPException(404, "Partner not found")
            other = con.execute("SELECT 1 FROM tl_partners WHERE deriv_referral_link=? AND id<>?", (url, pid)).fetchone()
            if other:
                raise HTTPException(409, "Another partner already uses this link")
            con.execute("UPDATE tl_partners SET deriv_referral_link=?, deriv_partner_id=COALESCE(?, deriv_partner_id), link_status='unverified', "
                        "link_verified_at=NULL, updated_at=? WHERE id=?", (url, did or None, _now(), pid))
            _audit(con, admin["id"], pid, "set_link", {"before": p["deriv_referral_link"], "after": url, "link_status": "unverified"})
            con.commit()
    await asyncio.to_thread(work)
    return JSONResponse({"ok": True, "link_status": "unverified"})


async def admin_verify_link(request):
    admin = auth.require_admin(request)
    pid = request.path_params["pid"]
    d = await auth._json(request)
    source, on, note = _verif_fields(d)

    def work():
        with closing(db()) as con:
            p = con.execute("SELECT * FROM tl_partners WHERE id=?", (pid,)).fetchone()
            if not p:
                raise HTTPException(404, "Partner not found")
            if not p["deriv_referral_link"]:
                raise HTTPException(409, "Add the Deriv link first")
            now = _now()
            con.execute("UPDATE tl_partners SET link_status='verified', link_verified_at=?, updated_at=? WHERE id=?", (now, now, pid))
            con.execute("INSERT INTO tl_partner_verifications(partner_id,kind,subject,source,verified_on,note,admin_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (pid, "link", p["deriv_referral_link"], source, on, note, admin["id"], now))
            _audit(con, admin["id"], pid, "verify_link", {"source": source, "verified_on": on})
            con.commit()
    await asyncio.to_thread(work)
    return JSONResponse({"ok": True, "link_status": "verified"})


async def admin_verify_client(request):
    """Record, by hand, that Deriv Partners Hub shows a client tagged to this partner. TradeLens never checks this itself."""
    admin = auth.require_admin(request)
    pid = request.path_params["pid"]
    d = await auth._json(request)
    source, on, note = _verif_fields(d)
    subject = _clean(d.get("client"), 60)
    if len(subject) < 3:
        raise HTTPException(422, "Enter the Deriv client ID or reference you checked")

    def work():
        with closing(db()) as con:
            if not con.execute("SELECT 1 FROM tl_partners WHERE id=?", (pid,)).fetchone():
                raise HTTPException(404, "Partner not found")
            now = _now()
            con.execute("INSERT INTO tl_partner_verifications(partner_id,kind,subject,source,verified_on,note,admin_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (pid, "client_tag", subject, source, on, note, admin["id"], now))
            _audit(con, admin["id"], pid, "verify_client", {"client": subject, "source": source, "verified_on": on})
            con.commit()
    await asyncio.to_thread(work)
    return JSONResponse({"ok": True})


async def admin_house(request):
    """Create the owner's own 'house' partner record (used for members nobody introduced). It still needs the normal approval and link verification."""
    admin = auth.require_admin(request)
    d = await auth._json(request)
    url = valid_deriv_url(d.get("deriv_referral_link"))
    did = str(d.get("deriv_partner_id", "")).strip() or None
    if did and not DERIV_ID_RE.match(did):
        raise HTTPException(422, "Invalid Deriv partner ID")

    def work():
        with closing(db()) as con:
            if _house(con):
                raise HTTPException(409, "The house partner already exists")
            if _by_user(con, admin["id"]):
                raise HTTPException(409, "This admin account already has a partner record")
            pid = _create_partner(con, admin["id"], "awaiting_deriv_approval", "house", 1, url, did)
            _audit(con, admin["id"], pid, "create_house", {"link": url})
            con.commit()
            return pid
    return JSONResponse({"ok": True, "id": await asyncio.to_thread(work)})


async def admin_settings(request):
    u = _need_reviewer(request)
    is_admin = u["tier"] == "admin"
    if request.method == "POST":
        admin = auth.require_admin(request)
        d = await auth._json(request)

        def save():
            with closing(db()) as con:
                s, changes = _settings(con), {}
                if "master_link" in d:
                    new = valid_deriv_url(d["master_link"]) if str(d["master_link"]).strip() else ""
                    changes["master_link"] = {"before": s.get("master_link", ""), "after": new}
                    _set(con, "master_link", new)
                if "review_emails" in d:
                    mails = [e.strip().lower() for e in str(d["review_emails"] or "").replace("\n", ",").split(",") if e.strip()]
                    if len(mails) > MAX_REVIEWERS or any(not auth.EMAIL_RE.match(e) for e in mails):
                        raise HTTPException(422, f"Up to {MAX_REVIEWERS} valid e-mail addresses, separated by commas")
                    changes["review_emails"] = {"before": s.get("review_emails", ""), "after": ",".join(mails)}
                    _set(con, "review_emails", ",".join(mails))
                if "approval_record" in d:
                    rec = _clean(d["approval_record"], 600)
                    changes["approval_record"] = {"before": s.get("approval_record", ""), "after": rec}
                    _set(con, "approval_record", rec)
                    _set(con, "approval_recorded_at", str(_now()))
                    _set(con, "approval_recorded_by", str(admin["id"]))
                s = _settings(con)
                if "mode" in d:
                    m = str(d["mode"])
                    if m not in MODES:
                        raise HTTPException(422, "mode must be off, review or public")
                    if m == "public" and len(s.get("approval_record", "")) < 10:
                        raise HTTPException(409, "Enter the launch record first (Deriv's written approval, or the owner's dated decision to launch without Deriv approval). The programme cannot be made public without it")
                    changes["mode"] = {"before": s.get("mode", "off"), "after": m}
                    _set(con, "mode", m)
                _audit(con, admin["id"], None, "settings", changes)
                con.commit()
        await asyncio.to_thread(save)

    def read():
        with closing(db()) as con:
            s = _settings(con)
        out = {"mode": s.get("mode", "off"), "master_link": s.get("master_link", ""), "approval_record": s.get("approval_record", ""),
               "approval_recorded_at": int(s["approval_recorded_at"]) if s.get("approval_recorded_at") else None}
        if is_admin:
            out["review_emails"] = s.get("review_emails", "")
            try:
                from . import credits
                with closing(credits.db()) as c:
                    out["legacy_link"] = credits.settings(c).get("deriv_ib_link", "")
            except Exception:
                out["legacy_link"] = ""
        return out
    return JSONResponse(await asyncio.to_thread(read))


routes = [
    Route("/api/public/partner-ref", public_check, methods=["GET"]),
    Route("/api/partners/access", api_access, methods=["GET"]),
    Route("/api/partners/me", api_me, methods=["GET"]),
    Route("/api/partners/trader/handoff", api_trader_handoff, methods=["POST"]),
    Route("/api/partners/become/start", api_become_start, methods=["POST"]),
    Route("/api/partners/become/submit", api_become_submit, methods=["POST"]),
    Route("/api/partners/dashboard", api_dashboard, methods=["GET"]),
    Route("/api/admin/partners/whoami", admin_whoami, methods=["GET"]),
    Route("/api/admin/partners/settings", admin_settings, methods=["GET", "POST"]),
    Route("/api/admin/partners/house", admin_house, methods=["POST"]),
    Route("/api/admin/partners", admin_list, methods=["GET"]),
    Route("/api/admin/partners/{pid:int}", admin_detail, methods=["GET"]),
    Route("/api/admin/partners/{pid:int}/status", admin_status, methods=["POST"]),
    Route("/api/admin/partners/{pid:int}/link", admin_link, methods=["POST"]),
    Route("/api/admin/partners/{pid:int}/verify-link", admin_verify_link, methods=["POST"]),
    Route("/api/admin/partners/{pid:int}/verify-client", admin_verify_client, methods=["POST"]),
]
