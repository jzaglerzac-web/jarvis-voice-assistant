"""
Jarvis — mail access.
Reading opens mailboxes read-only (EXAMINE / BODY.PEEK): Jarvis never marks mails
as read, moves or deletes anything. Sending (send_mail) only happens after the user
confirmed the draft; see PendingMail handling in server.py.
"""

import email
import email.header
import email.utils
import imaplib
import time
from datetime import datetime, timedelta

GROUP_NAMES = {"arbeit": "Arbeit", "projekt": "Projekt", "privat": "Privat"}


def _decode(value) -> str:
    if not value:
        return ""
    parts = []
    for text, charset in email.header.decode_header(value):
        if isinstance(text, bytes):
            text = text.decode(charset or "utf-8", errors="replace")
        parts.append(text)
    return "".join(parts).strip()


def _snippet(msg, length=300) -> str:
    """Start of the plain-text body (falls back to stripped HTML)."""
    body = ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if ctype in ("text/plain", "text/html") and not part.get_filename():
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if ctype == "text/html":
                import re
                text = re.sub(r"<[^>]+>", " ", text)
            body = text
            if ctype == "text/plain":
                break
    return " ".join(body.split())[:length]


def _read_account(acc: dict, days: int, unread_only: bool, limit: int) -> list:
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    with imaplib.IMAP4_SSL(acc["imap_host"], int(acc.get("imap_port", 993)), timeout=20) as imap:
        imap.login(acc.get("user") or acc["email"], acc["password"])
        imap.select("INBOX", readonly=True)
        criteria = f'(SINCE "{since}"{" UNSEEN" if unread_only else ""})'
        _, data = imap.search(None, criteria)
        ids = data[0].split()[-limit:]
        mails = []
        for mid in reversed(ids):
            _, msg_data = imap.fetch(mid, "(BODY.PEEK[])")
            msg = email.message_from_bytes(msg_data[0][1])
            date = email.utils.parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
            mails.append({
                "account": acc["email"],
                "date": date.strftime("%d.%m. %H:%M") if date else "",
                "from": _decode(msg["From"]),
                "subject": _decode(msg["Subject"]),
                "snippet": _snippet(msg),
            })
        return mails


def smtp_host(acc: dict) -> str:
    """SMTP server of an account: explicit smtp_host, Gmail, otherwise the IMAP host (All-Inkl)."""
    if acc.get("smtp_host"):
        return acc["smtp_host"]
    return "smtp.gmail.com" if acc["imap_host"] == "imap.gmail.com" else acc["imap_host"]


def send_mail(acc: dict, to: str, subject: str, body: str) -> None:
    """Send a plain-text mail from acc and file a copy in its Sent folder.
    Only call this after the user has confirmed the exact draft."""
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = acc["email"]
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid()
    msg.set_content(body)

    with smtplib.SMTP_SSL(smtp_host(acc), int(acc.get("smtp_port", 465)), timeout=30) as smtp:
        smtp.login(acc.get("user") or acc["email"], acc["password"])
        smtp.send_message(msg)

    # Gmail files sent mail itself; other providers need an IMAP copy to show it in "Gesendet"
    if acc["imap_host"] == "imap.gmail.com":
        return
    try:
        with imaplib.IMAP4_SSL(acc["imap_host"], int(acc.get("imap_port", 993)), timeout=20) as imap:
            imap.login(acc.get("user") or acc["email"], acc["password"])
            sent = None
            _, folders = imap.list()
            for line in folders or []:
                text = line.decode(errors="replace")
                if "\\Sent" in text or text.rstrip('"').endswith(("Sent", "Gesendet", "Gesendete Elemente")):
                    sent = text.split(' "/" ')[-1].split(' "." ')[-1].strip()
                    break
            if sent:
                imap.append(sent, "\\Seen", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
    except Exception as e:
        print(f"[jarvis] Kopie in Gesendet nicht moeglich: {e}", flush=True)


def read_mails(accounts: list, request: str = "", days: int = 3, limit: int = 8) -> str:
    """Mails as text for Jarvis. request may name a group (arbeit/projekt/privat),
    an address part (e.g. "rechnung") and "alle" to include already read mails."""
    request = request.lower()
    unread_only = "alle" not in request
    chosen = [a for a in accounts
              if (a.get("group") and a["group"].lower() in request) or a["email"].split("@")[0].lower() in request]
    if not chosen:
        chosen = accounts
    lines = []
    for acc in chosen:
        if not acc.get("password") or "YOUR_" in acc.get("password", ""):
            lines.append(f"[{acc['email']}] kein Passwort eingetragen")
            continue
        try:
            mails = _read_account(acc, days, unread_only, limit)
        except Exception as e:
            lines.append(f"[{acc['email']}] nicht erreichbar: {e}")
            continue
        group = GROUP_NAMES.get(acc.get("group", "").lower(), acc.get("group", ""))
        if not mails:
            lines.append(f"[{group} {acc['email']}] keine {'ungelesenen ' if unread_only else ''}Mails der letzten {days} Tage")
        for m in mails:
            lines.append(f"[{group} {m['account']}] {m['date']} von {m['from']}: {m['subject']} — {m['snippet']}")
    return "\n".join(lines)
