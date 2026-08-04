"""
Sends password-reset emails via plain SMTP - works with a free Gmail
account (using an "app password", not your real Gmail password) or any
other SMTP provider. No paid service required.

If SMTP isn't configured, send_reset_email() just returns False instead
of crashing, so the rest of the app keeps working - the forgot-password
page will tell the person email isn't set up yet.

To go live: set these environment variables:
  SMTP_HOST      e.g. smtp.gmail.com
  SMTP_PORT      e.g. 587
  SMTP_USER      the email address that sends the messages
  SMTP_PASSWORD  an app password (Gmail: Google Account -> Security ->
                 2-Step Verification -> App passwords)
  APP_BASE_URL   e.g. https://meeting-ledger.onrender.com (used to build
                 the reset link - without this, links default to
                 http://127.0.0.1:5000 for local testing)
"""
import os
import smtplib
from email.mime.text import MIMEText


def is_configured():
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD"))


def send_reset_email(to_email, reset_url):
    if not is_configured():
        return False

    msg = MIMEText(
        f"Someone (hopefully you) asked to reset the Meeting Ledger password for this email.\n\n"
        f"Reset it here: {reset_url}\n\n"
        f"This link works for 1 hour. If you didn't request this, you can ignore this email."
    )
    msg["Subject"] = "Reset your Meeting Ledger password"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = to_email

    try:
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as server:
            server.starttls()
            server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            server.sendmail(os.environ["SMTP_USER"], [to_email], msg.as_string())
        return True
    except Exception:
        return False


def send_reminder_email(to_email, subject, body):
    """Generic sender used for the meeting reminder - the caller builds
    the (already-translated) subject/body text since this module doesn't
    know about languages."""
    if not is_configured():
        return False

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = to_email

    try:
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as server:
            server.starttls()
            server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            server.sendmail(os.environ["SMTP_USER"], [to_email], msg.as_string())
        return True
    except Exception:
        return False


def base_url():
    return os.environ.get("APP_BASE_URL", "http://127.0.0.1:5000")
