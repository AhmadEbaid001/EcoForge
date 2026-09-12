"""Outbound email, for exactly one purpose: delivering a Judge Pass.

F11 cut SMTP from the alerting path and the reason still stands - nobody reads email
during a presentation. The Judge Pass is the opposite case. Its email is the thing a
judge takes away from the booth and reads afterwards, and it is how they get back in
on another device. So there is one sender, it does one job, and nothing else in the
platform sends mail.

It speaks Resend's HTTPS API rather than SMTP, and the reason decided it: a host's
outbound mail ports are the ones most often blocked, while outbound HTTPS is the one
connection every server already makes. It is the standard library's urllib, so it
arrives with no new dependency.

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

from gemp.config import Settings

log = logging.getLogger("gemp.mail")

# A literal, never configuration: the one host this module will ever talk to. A
# target that could be changed from the environment is one typo from mailing
# credentials somewhere else.
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


def configured(settings: Settings) -> bool:
    return bool(settings.resend_api_key.get_secret_value() and settings.mail_from)


def send(settings: Settings, *, to: str, subject: str, html: str, text: str,
         idempotency_key: str = "") -> Outcome:
    """Send one message. Never raises: every failure comes back as an Outcome."""
    if not configured(settings):
        return Outcome("off", "no sender configured")

    payload: dict = {
        "from": settings.mail_from,
        "to": [to],
        "subject": subject,
        "html": html,
        # A plain-text part beside the HTML. Some clients show only this, and a
        # message with no text part scores worse with spam filters.
        "text": text,
    }
    if settings.mail_reply_to:
        payload["reply_to"] = settings.mail_reply_to

    headers = {
        "Authorization": f"Bearer {settings.resend_api_key.get_secret_value()}",
        "Content-Type": "application/json",
        # API edges commonly refuse urllib's default agent outright.
        "User-Agent": "gemp-judge-pass/1.0",
    }
    if idempotency_key:
        # A retried request carrying the same key is answered, not sent twice.
        headers["Idempotency-Key"] = idempotency_key

    request = urllib.request.Request(
        RESEND_URL, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        # RESEND_URL is a module constant with a fixed https scheme, so there is no
        # scheme for a caller to choose - the risk the audit rules below look for.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # nosec B310
            body = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        outcome = Outcome("failed", f"HTTP {exc.code}: {_reason(exc)}")
        log.warning("pass email refused by the provider: %s", outcome.detail)
        return outcome
    except (urllib.error.URLError, OSError, ValueError) as exc:
        outcome = Outcome("failed", f"{type(exc).__name__}: {str(exc)[:120]}")
        log.warning("pass email could not be sent: %s", outcome.detail)
        return outcome

    return Outcome("sent", str(body.get("id", "") if isinstance(body, dict) else "")[:64])


def _reason(exc: urllib.error.HTTPError) -> str:
    """The provider's own words for a refusal - "domain is not verified" is worth
    far more on the administration screen than a bare 403."""
    try:
        body = json.loads(exc.read() or b"{}")
        message = body.get("message") or body.get("name") if isinstance(body, dict) else ""
    except (ValueError, OSError):
        message = ""
    return str(message or exc.reason)[:160]
