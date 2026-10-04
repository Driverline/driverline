"""Admin commands. Run from the project root, for example:
  python -m app.manage create-admin you@example.com
  python -m app.manage invite --tier free --uses 20
  python -m app.manage set-tier friend@example.com member
  python -m app.manage block friend@example.com
  python -m app.manage reset-password friend@example.com
  python -m app.manage users
  python -m app.manage claim-trades you@example.com   (adopt trades logged before accounts existed)
"""
import argparse
import getpass
import secrets
from contextlib import closing

from . import auth


def main():
    ap = argparse.ArgumentParser(prog="manage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-admin"); p.add_argument("email")
    p = sub.add_parser("invite"); p.add_argument("--tier", choices=["free", "member"], default="free")
    p.add_argument("--uses", type=int, default=1); p.add_argument("--code")
    p = sub.add_parser("set-tier"); p.add_argument("email"); p.add_argument("tier", choices=["free", "member", "admin"])
    for n in ("block", "unblock", "claim-trades", "reset-password"):
        p = sub.add_parser(n); p.add_argument("email")
    sub.add_parser("users")
    a = ap.parse_args()

    with closing(auth.db()) as con:
        def user_id(email):
            r = con.execute("SELECT id FROM users WHERE email=?", (email.lower(),)).fetchone()
            if not r:
                raise SystemExit(f"No such user: {email}")
            return r["id"]

        if a.cmd == "create-admin":
            pw = getpass.getpass("Password (min 8 chars): ")
            if len(pw) < 8:
                raise SystemExit("Password too short")
            if getpass.getpass("Repeat password: ") != pw:
                raise SystemExit("Passwords differ")
            con.execute("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,'admin',?) "
                        "ON CONFLICT(email) DO UPDATE SET pw_hash=excluded.pw_hash, tier='admin', blocked=0",
                        (a.email.lower(), auth.hash_pw(pw), auth._now()))
            print("Admin ready:", a.email.lower())
        elif a.cmd == "invite":
            code = a.code or secrets.token_urlsafe(6)
            con.execute("INSERT INTO invites(code,tier,uses_left) VALUES(?,?,?) "
                        "ON CONFLICT(code) DO UPDATE SET tier=excluded.tier, uses_left=excluded.uses_left",
                        (code, a.tier, a.uses))
            print(f"Invite code: {code}  (tier {a.tier}, {a.uses} use(s))")
        elif a.cmd == "set-tier":
            con.execute("UPDATE users SET tier=? WHERE id=?", (a.tier, user_id(a.email)))
            print(a.email, "is now", a.tier)
        elif a.cmd in ("block", "unblock"):
            con.execute("UPDATE users SET blocked=? WHERE id=?", (int(a.cmd == "block"), user_id(a.email)))
            print(a.email, a.cmd + "ed")
        elif a.cmd == "claim-trades":
            uid = user_id(a.email)
            try:
                n = con.execute("UPDATE trades SET user_id=? WHERE user_id=0", (uid,)).rowcount
            except Exception:
                n = 0
            print(f"{n} older trade(s) assigned to {a.email}")
        elif a.cmd == "reset-password":
            pw = getpass.getpass("New password for them (min 8 chars): ")
            if len(pw) < 8:
                raise SystemExit("Password too short")
            con.execute("UPDATE users SET pw_hash=? WHERE id=?", (auth.hash_pw(pw), user_id(a.email)))
            print("Password reset for", a.email, "- ask them to sign in with it")
        elif a.cmd == "users":
            for r in con.execute("SELECT email,tier,blocked,created_at FROM users ORDER BY id"):
                print(f"{r['email']:35} {r['tier']:7} {'BLOCKED' if r['blocked'] else '':8} {r['created_at']}")
        con.commit()


if __name__ == "__main__":
    main()
