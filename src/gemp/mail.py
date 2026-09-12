"""Outbound email, for exactly one purpose: delivering a Judge Pass.

F11 cut SMTP from the alerting path and the reason still stands - nobody reads email
during a presentation. The Judge Pass is the opposite case. Its email is the thing a
judge takes away from the booth and reads afterwards, and it is how they get back in
on another device. So there is one sender, it does one job, and nothing else in the
platform sends mail.

Three ways out, chosen by what is configured, in this order:

* Brevo's HTTPS API, when its key is set;
* Resend's HTTPS API, when its key is set;
* plain SMTP with TLS, when a host, a user and a password are set - which is what a
  Gmail mailbox with an app password needs, and nothing more: no provider account,
  no domain to authenticate. Mail sent through Gmail's own servers from its own
  address is signed by Google, so it arrives as Gmail mail rather than as a third
  party claiming to be it.

All three are the standard library, so none arrives with a dependency.

Unconfigured is a normal state, not an error. With nothing set every send reports
`off` and touches nothing, which is what the test suite and an offline laptop both
want - and a judge is still signed in on the spot either way.
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from gemp.config import Settings

log = logging.getLogger("gemp.mail")

# Literals, never configuration: the only two API hosts this module will ever talk
# to. A target that could be changed from the environment is one typo from mailing
# credentials somewhere else. (The SMTP host IS configuration - that is the point of
# SMTP - but it is only ever spoken to over TLS.)
BREVO_URL = "https://api.brevo.com/v3/smtp/email"
RESEND_URL = "https://api.resend.com/emails"

# Long enough for a slow provider on a bad day, short enough that a black-holed
# connection does not park a worker for a minute. This runs after the response has
# gone, so no judge is ever waiting on it.
TIMEOUT_S = 10.0

# Implicit TLS. Any other port is spoken to in plain text only until STARTTLS, and
# a server that will not upgrade is a failure, never a fallback to plain text.
SMTPS_PORT = 465


@dataclass(frozen=True)
class Outcome:
    """What happened to one message. `detail` is safe to show an administrator:
    the message id on success, the server's stated reason on failure, and never a
    key, a password or the message body."""

    status: str          # "sent", "failed" or "off"
    detail: str = ""


def provider(settings: Settings) -> str:
    """"brevo", "resend", "smtp", or "" when nothing is set.

    The APIs win over SMTP, and Brevo over Resend, so switching is adding one
    setting rather than remembering to blank another.
    """
    if settings.brevo_api_key.get_secret_value():
        return "brevo"
    if settings.resend_api_key.get_secret_value():
        return "resend"
    if settings.smtp_host and settings.smtp_user and settings.smtp_password.get_secret_value():
        return "smtp"
    return ""


def sender(settings: Settings) -> str:
    """Who the mail says it is from. Over SMTP the mailbox itself is a fine default:
    Gmail rewrites any other From to the account that logged in anyway."""
    if settings.mail_from:
        return settings.mail_from
    return settings.smtp_user if provider(settings) == "smtp" else ""


def configured(settings: Settings) -> bool:
    return bool(provider(settings) and sender(settings))


def send(settings: Settings, *, to: str, subject: str, html: str, text: str,
         idempotency_key: str = "") -> Outcome:
    """Send one message. Never raises: every failure comes back as an Outcome."""
    which = provider(settings)
    if not which or not sender(settings):
        return Outcome("off", "no sender configured")

    if which == "smtp":
        return _smtp(settings, to, subject, html, text)

    if which == "brevo":
        url, headers, payload = _brevo(settings, to, subject, html, text)
    else:
        url, headers, payload = _resend(settings, to, subject, html, text,
                                        idempotency_key)
    # API edges commonly refuse urllib's default agent outright.
    headers["User-Agent"] = "gemp-judge-pass/1.0"

    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST",
    )
    try:
        # `url` is one of the two module constants above, both fixed https, so there
        # is no scheme for a caller to choose - the risk the audit rules look for.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # nosec B310
            body = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return _failed(which, f"HTTP {exc.code}: {_reason(exc)}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return _failed(which, f"{type(exc).__name__}: {str(exc)[:120]}")

    ident = (body.get("messageId") or body.get("id") or "") if isinstance(body, dict) else ""
    return Outcome("sent", str(ident)[:64])


def _failed(which: str, detail: str) -> Outcome:
    log.warning("pass email not sent through %s: %s", which, detail)
    return Outcome("failed", detail)


def _smtp(settings: Settings, to: str, subject: str, html: str, text: str) -> Outcome:
    """One message over SMTP, TLS from the first command that matters.

    Port 465 is TLS from the first byte; anything else must upgrade with STARTTLS
    before the login, and `starttls()` raises rather than carrying on in plain text
    if the server declines - so the password never crosses the wire unencrypted.
    """
    name, address = parseaddr(sender(settings))
    message = EmailMessage()
    message["From"] = formataddr((name, address)) if name else address
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(usegmt=True)
    message["Message-ID"] = make_msgid(domain=address.rpartition("@")[2] or None)
    if settings.mail_reply_to:
        message["Reply-To"] = settings.mail_reply_to
    # Plain text first and HTML as the alternative: a client shows the last part it
    # can render, and some can only render the first.
    message.set_content(text)
    message.add_alternative(html, subtype="html")

    context = ssl.create_default_context()
    try:
        if settings.smtp_port == SMTPS_PORT:
            client = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                      timeout=TIMEOUT_S, context=context)
        else:
            client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT_S)
        with client:
            if settings.smtp_port != SMTPS_PORT:
                client.starttls(context=context)
            client.login(settings.smtp_user, settings.smtp_password.get_secret_value())
            client.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        # Gmail's own sentence - "Application-specific password required", or
        # "Username and Password not accepted" - says which of the two setup steps
        # was missed. It never contains the password.
        return _failed("smtp", f"login refused ({exc.smtp_code}): {_smtp_text(exc.smtp_error)}")
    except smtplib.SMTPResponseException as exc:
        return _failed("smtp", f"refused ({exc.smtp_code}): {_smtp_text(exc.smtp_error)}")
    except (smtplib.SMTPException, OSError) as exc:
        return _failed("smtp", f"{type(exc).__name__}: {str(exc)[:120]}")

    return Outcome("sent", str(message["Message-ID"])[:64])


def _smtp_text(raw: bytes | str) -> str:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    return " ".join(text.split())[:160]


def _brevo(settings: Settings, to: str, subject: str, html: str,
           text: str) -> tuple[str, dict, dict]:
    """Brevo's transactional endpoint. The sender is an object, not a string, so
    "GEMP - Team Ecoforge <pass@example.org>" is split into its name and address -
    and the address must be one Brevo has verified, on an authenticated domain."""
    name, address = parseaddr(settings.mail_from)
    brevo_sender = {"email": address or settings.mail_from}
    if name:
        brevo_sender["name"] = name
    payload: dict = {
        "sender": brevo_sender,
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html,
        # A plain-text part beside the HTML. Some clients show only this, and a
        # message with no text part scores worse with spam filters.
        "textContent": text,
        "tags": ["judge-pass"],
    }
    if settings.mail_reply_to:
        _reply_name, reply_address = parseaddr(settings.mail_reply_to)
        payload["replyTo"] = {"email": reply_address or settings.mail_reply_to}
    headers = {
        "api-key": settings.brevo_api_key.get_secret_value(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return BREVO_URL, headers, payload


def _resend(settings: Settings, to: str, subject: str, html: str, text: str,
            idempotency_key: str) -> tuple[str, dict, dict]:
    payload: dict = {
        "from": settings.mail_from,
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text,
    }
    if settings.mail_reply_to:
        payload["reply_to"] = settings.mail_reply_to
    headers = {
        "Authorization": f"Bearer {settings.resend_api_key.get_secret_value()}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        # A retried request carrying the same key is answered, not sent twice.
        headers["Idempotency-Key"] = idempotency_key
    return RESEND_URL, headers, payload


def _reason(exc: urllib.error.HTTPError) -> str:
    """The provider's own words for a refusal. "unauthorized: IP not authorized" or
    "domain is not verified" is worth far more on the administration screen than a
    bare 401 - both API providers put the sentence in `message`."""
    try:
        body = json.loads(exc.read() or b"{}")
        message = (body.get("message") or body.get("code") or body.get("name")
                   if isinstance(body, dict) else "")
    except (ValueError, OSError):
        message = ""
    return str(message or exc.reason)[:160]
