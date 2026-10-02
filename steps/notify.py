"""Email a short alert when a run needs a person's attention.

ingest.py runs unattended (nightly cron) and is silent when everything
went fine, so this is only called when something didn't: files moved to
error/, FileMaker records or shard updates that failed, or a crash.

The email is only a signal. It never carries details — no file names,
paths, host or database names, error messages — since an inbox (and the
phones it syncs to) is a much wider audience than the server. The details
stay in the logs on the server.

Only sent over TLS with a verified certificate; if the mail server can't
offer that, nothing is sent.

Configured from .env; a no-op unless both SMTP_HOST and NOTIFY_EMAIL are
set, so the pipeline runs the same with or without email.
  SMTP_HOST, SMTP_PORT (default 25)  — a mail relay, or the recipients'
      own mail server (its MX host), which accepts mail for its own
      domain without a login
  NOTIFY_EMAIL  — recipient(s), comma-separated
  NOTIFY_FROM   — sender address (default: the first recipient)
"""

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage


def alert(summary: str) -> bool:
    """Send the alert email, summary being one detail-free sentence (e.g.
    "The latest run had 3 problem(s)."); True if sent. Never raises — a
    failed alert is printed (ending up in the cron log) instead of hiding
    the problem it was about."""
    host = os.getenv("SMTP_HOST")
    recipients = [a.strip() for a in os.getenv("NOTIFY_EMAIL", "").split(",") if a.strip()]
    if not host or not recipients:
        return False

    msg = EmailMessage()
    msg["Subject"] = "scan-ingest: something went wrong"
    msg["From"] = os.getenv("NOTIFY_FROM") or recipients[0]
    msg["To"] = ", ".join(recipients)
    msg.set_content(f"{summary}\n\nDetails are in the logs on the server: "
                    "cron.log, and errors_/filemaker_warnings_/"
                    "shard_warnings_<date>.log.\n")

    try:
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "25")), timeout=30) as smtp:
            smtp.ehlo()
            if not smtp.has_extn("starttls"):
                raise smtplib.SMTPException("server offers no TLS (STARTTLS); not sending")
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException, ValueError) as e:
        print(f"Could not send alert email via {host}: {e}", file=sys.stderr)
        return False
