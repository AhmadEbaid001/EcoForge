"""Outbound email, for exactly one purpose: delivering a Judge Pass.

F11 cut SMTP from the alerting path and the reason still stands - nobody reads email
during a presentation. The Judge Pass is the opposite case. Its email is the thing a
judge takes away from the booth and reads afterwards, and it is how they get back in
on another device. So there is one sender, it does one job, and nothing else in the
platform sends mail.

It speaks a provider's HTTPS API rather than SMTP, and the reason decided it: a
host's outbound mail ports are the ones most often blocked, while outbound HTTPS is
the one connection every server already makes. Two providers are understood - Brevo
and Resend - chosen by which API key is set, Brevo first. Both are the standard
library's urllib, so neither arrives with a dependency.

Unconfigured is a normal state, not an error. With no API key every send reports
`off` and touches nothing, which is what the test suite and an offline laptop both
want - and a judge is still signed in on the spot either way.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.utils import parseaddr

from gemp.config import Settings

log = logging.getLogger("gemp.mail")

# Literals, never configuration: the only two hosts this module will ever talk to. A
# target that could be changed from the environment is one typo from mailing
# credentials somewhere else.
BREVO_URL = "https://api.brevo.com/v3/smtp/email"
RESEND_URL = "https://api.resend.com/emails"

# Long enough for a slow API on a bad day, short enough that a black-holed
# connection does not park a worker for a minute. This runs after the response has
# gone, so no judge is ever waiting on it.
TIMEOUT_S = 10.0


@dataclass(frozen=True)
class Outcome:
    """What happened to one message. `detail` is safe to show an administrator:
    the provider's message id on success, its stated reason on failure, and never
    the key or the message body."""

    status: str          # "sent", "failed" or "off"
    detail: str = ""


def provider(settings: Settings) -> str:
    """"brevo", "resend", or "" when neither key is set.

    Brevo wins when both are, so switching providers is adding one key rather than
    remembering to blank the other.
    """
    if settings.brevo_api_key.get_secret_value():
        return "brevo"
    if settings.resend_api_key.get_secret_value():
        return "resend"
    return ""


def configured(settings: Settings) -> bool:
    return bool(provider(settings) and settings.mail_from)


def send(settings: Settings, *, to: str, subject: str, html: str, text: str,
         idempotency_key: str = "") -> Outcome:
    """Send one message. Never raises: every failure comes back as an Outcome."""
    which = provider(settings)
    if not which or not settings.mail_from:
        return Outcome("off", "no sender configured")

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
        outcome = Outcome("failed", f"HTTP {exc.code}: {_reason(exc)}")
        log.warning("pass email refused by %s: %s", which, outcome.detail)
        return outcome
    except (urllib.error.URLError, OSError, ValueError) as exc:
        outcome = Outcome("failed", f"{type(exc).__name__}: {str(exc)[:120]}")
        log.warning("pass email could not be sent through %s: %s", which, outcome.detail)
        return outcome

    ident = (body.get("messageId") or body.get("id") or "") if isinstance(body, dict) else ""
    return Outcome("sent", str(ident)[:64])


def _brevo(settings: Settings, to: str, subject: str, html: str,
           text: str) -> tuple[str, dict, dict]:
    """Brevo's transactional endpoint. The sender is an object, not a string, so
    "GEMP - Team Ecoforge <pass@example.org>" is split into its name and address -
    and the address must be one Brevo has verified, on an authenticated domain."""
    name, address = parseaddr(settings.mail_from)
    sender = {"email": address or settings.mail_from}
    if name:
        sender["name"] = name
    payload: dict = {
        "sender": sender,
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
    bare 401 - both providers put the sentence in `message`."""
    try:
        body = json.loads(exc.read() or b"{}")
        message = (body.get("message") or body.get("code") or body.get("name")
                   if isinstance(body, dict) else "")
    except (ValueError, OSError):
        message = ""
    return str(message or exc.reason)[:160]
