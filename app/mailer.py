"""Sends email through any SMTP provider. Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM.
Without SMTP_HOST the reset link is printed in the server log instead."""
import asyncio
import os
import smtplib
import ssl
from email.message import EmailMessage


def configured() -> bool:
    return bool(os.environ.get("SMTP_HOST"))


def _send(to: str, subject: str, body: str):
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587))
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = os.environ.get("SMTP_FROM") or user, to, subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
            if user:
                s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls(context=ctx)
            if user:
                s.login(user, pw)
            s.send_message(msg)


async def send_reset(to: str, link: str) -> bool:
    body = ("Someone asked to reset your Driverline password.\n\n"
            f"Open this link within one hour to choose a new one:\n{link}\n\n"
            "If this wasn't you, ignore this email and your password stays the same.")
    if not configured():
        print(f"[mail not configured] Password reset link for {to}: {link}", flush=True)
        return False
    try:
        await asyncio.to_thread(_send, to, "Reset your Driverline password", body)
        return True
    except Exception as e:
        print(f"[mail error] {e}", flush=True)
        return False
