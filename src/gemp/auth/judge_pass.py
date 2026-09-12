"""The Judge Pass: a one-day viewer account, issued at the booth.

A judge is handed a card. Every card carries the same QR code, which opens a page
asking for an email address and nothing else. Confirming it creates a viewer account
named after that address, signs the browser in on the spot, and emails a generated
password so the judge can come back on any device until the pass closes.

One shared code rather than one per card is a deliberate trade. A per-card code is a
key: whoever holds it gets exactly one account. A shared code is only a door, and
cards leave the booth in pockets and on photographs - so the limits that matter live
here rather than in how many cards were printed:

* a closing time, after which the page issues nothing and every pass account stops
  working, so the card a judge takes home is a souvenir and not an open door;
* one pass per address, so a second attempt signs nobody in and sends nothing;
* a ceiling on passes in any 24 hours, far above a booth's worth, so a leaked link
  cannot turn the sender into a spam cannon and get the sending account suspended;
* an off switch an administrator can reach from a phone.

Per-address rate limiting is deliberately absent. Every request reaches the app from
nginx's private address, so a per-IP limit would treat the whole venue as one person
- see `service._distinguishing_ip`.

The account is a viewer and nothing more. It has an ordinary scrypt password and
ordinary sessions; its one new property is `access_expires_at`, which `service`
enforces at sign-in and on every request. The password is generated here, hashed
into the account, handed to the mail sender in memory, and never stored or logged.
"""

from __future__ import annotations

import contextlib
import hmac
import html
import logging
import re
import secrets
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gemp import mail
from gemp.auth import service
from gemp.config import Settings
from gemp.db import AppSettingRow, JudgePassRow, UserRow

log = logging.getLogger("gemp.auth.judge_pass")

# Printed on paper and typed by nobody, so there is no reason for it to be short. A
# short one is guessable, and a guessable one is an open registration form.
MIN_CODE_LENGTH = 16

# The ceiling is counted over a rolling window rather than a calendar day, so it has
# no midnight to reason about in two time zones.
WINDOW = timedelta(hours=24)

SETTING_KEY = "judge_pass"
JUDGE_ROLE = "viewer"
JUDGE_DISPLAY_NAME = "Judge"

# The username column's width, because the address IS the username.
MAX_EMAIL_LENGTH = 64

# Deliberately loose. The mailbox is the real validator - the pass only works if the
# email arrives - so this rejects what cannot be an address and nothing more.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

LANGS = frozenset({"en", "ar"})

EXISTS_MESSAGE = ("This email already has a Judge Pass. Sign in with the details "
                  "that were sent to it.")

_REFUSALS = {
    "closed": ("closed", 410, "Judge Passes for this event have closed."),
    "paused": ("paused", 403, "Judge Passes are paused right now. Ask the team at the booth."),
    "full": ("full", 429,
             "Today's Judge Passes have all been issued. Ask the team at the booth."),
    "unconfigured": ("invalid", 404, "This pass link is not valid."),
}


class Refused(Exception):
    """A pass that will not be issued. `reason` is what the page switches on."""

    def __init__(self, reason: str, status: int, message: str):
        super().__init__(message)
        self.reason = reason
        self.status = status
        self.message = message


# --- configuration ------------------------------------------------------------


def problem(settings: Settings) -> str:
    """Why no pass can be issued at all, or "" when one can. Shown to administrators."""
    code = settings.judge_pass_code.get_secret_value()
    if not code:
        return "GEMP_JUDGE_PASS_CODE is not set, so Judge Passes are off."
    if len(code) < MIN_CODE_LENGTH:
        return (f"GEMP_JUDGE_PASS_CODE is shorter than {MIN_CODE_LENGTH} characters, "
                "so Judge Passes are off. A short code is a guessable one.")
    until = settings.judge_pass_until
    if until is None:
        return ("GEMP_JUDGE_PASS_UNTIL is not set or is not a date, so Judge Passes "
                "are off. Write it as 2026-09-14T23:59:59+03:00.")
    if until.tzinfo is None:
        return ("GEMP_JUDGE_PASS_UNTIL has no UTC offset, so Judge Passes are off. "
                "Write it as 2026-09-14T23:59:59+03:00.")
    return ""


def code_matches(settings: Settings, presented: str) -> bool:
    """Constant-time. False whenever the feature is not fully configured."""
    if problem(settings):
        return False
    expected = settings.judge_pass_code.get_secret_value()
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def link(settings: Settings) -> str:
    """What the QR code on the card encodes. In the fragment, so the code never
    reaches a server log or a Referer header."""
    base = settings.public_url.rstrip("/")
    return f"{base}/#/pass/{settings.judge_pass_code.get_secret_value()}"


def sign_in_url(settings: Settings) -> str:
    """Where the button in the email lands: straight on the sign-in form."""
    base = settings.public_url.rstrip("/")
    return f"{base}/#/signin" if base else ""


# --- the off switch and the ceiling ---------------------------------------------


def enabled(session: Session) -> bool:
    row = session.get(AppSettingRow, SETTING_KEY)
    return bool((row.value or {}).get("enabled", True)) if row else True


def set_enabled(session: Session, value: bool, *, by: str) -> None:
    row = session.get(AppSettingRow, SETTING_KEY)
    now = datetime.now(UTC)
    if row is None:
        session.add(AppSettingRow(key=SETTING_KEY, value={"enabled": value},
                                  updated_at=now, updated_by=by))
    else:
        # A new dict rather than a mutation: the ORM does not see an in-place change
        # to a JSON value, and the switch would silently not persist.
        row.value = {"enabled": value}
        row.updated_at = now
        row.updated_by = by
    session.flush()


def issued_in_window(session: Session, now: datetime | None = None) -> int:
    since = (now or datetime.now(UTC)) - WINDOW
    return int(session.execute(
        select(func.count()).select_from(JudgePassRow)
        .where(JudgePassRow.created_at > since)
    ).scalar_one())


def holders(session: Session) -> list[tuple[JudgePassRow, UserRow]]:
    """Every pass issued, newest first, beside the account it opened."""
    return list(session.execute(
        select(JudgePassRow, UserRow)
        .join(UserRow, UserRow.id == JudgePassRow.user_id)
        .order_by(JudgePassRow.created_at.desc())
    ).tuples())


def state(session: Session, settings: Settings, now: datetime | None = None) -> str:
    """open, closed, paused, full - or unconfigured. In that order of precedence:
    a closed event says closed even while paused, because that is the answer that
    stays true."""
    if problem(settings):
        return "unconfigured"
    now = now or datetime.now(UTC)
    if now >= settings.judge_pass_until:
        return "closed"
    if not enabled(session):
        return "paused"
    if issued_in_window(session, now) >= settings.judge_pass_daily_max:
        return "full"
    return "open"


# --- issuing --------------------------------------------------------------------


def normalise_email(raw: str) -> str | None:
    """Lower-cased and trimmed, or None when it cannot be an address that fits."""
    email = (raw or "").strip().lower()
    if not email or len(email) > MAX_EMAIL_LENGTH or not _EMAIL.match(email):
        return None
    return email


# Short, common, unambiguous words, so the password can be read off a phone screen
# and typed on another device without a copy-paste. Three words and two digits from
# this list is about 26 bits - modest, and enough for a view-only account that dies
# within the day behind a five-attempt lockout per username.
WORDS = (
    "acacia", "amber", "anchor", "aspen", "bamboo", "basalt", "beacon", "birch",
    "breeze", "cactus", "canal", "cedar", "cinder", "citrus", "clover", "cobalt",
    "comet", "copper", "coral", "cotton", "crane", "dahlia", "delta", "desert",
    "dune", "ember", "falcon", "fennel", "fern", "flint", "galaxy", "garnet",
    "glacier", "granite", "harbor", "hazel", "heron", "horizon", "indigo", "iris",
    "island", "ivory", "jasmine", "juniper", "kestrel", "lagoon", "lantern", "lemon",
    "lotus", "lunar", "magnet", "maple", "marble", "meadow", "meteor", "mint",
    "mosaic", "nectar", "nile", "oasis", "olive", "onyx", "orbit", "palm",
    "papyrus", "pebble", "pepper", "pine", "planet", "prism", "quartz", "raven",
    "reef", "ripple", "river", "saffron", "sage", "sand", "shore", "sierra",
    "signal", "silver", "solar", "spark", "spruce", "summit", "sunrise", "tide",
    "timber", "topaz", "tulip", "tundra", "turbine", "valley", "velvet", "violet",
    "walnut", "willow", "wind", "zephyr", "zinc",
)


def generate_password() -> str:
    """e.g. cedar-nile-solar-47. From `secrets`, never `random`."""
    words = "-".join(secrets.choice(WORDS) for _ in range(3))
    return f"{words}-{secrets.randbelow(90) + 10}"


@dataclass(frozen=True)
class Issued:
    user: UserRow
    row: JudgePassRow
    password: str        # in memory only, for the one email that carries it


def issue(session: Session, settings: Settings, *, email: str, lang: str,
          now: datetime | None = None) -> Issued:
    """Create the account and its pass row, or raise `Refused`. Does not commit.

    `email` must already have been through `normalise_email`.
    """
    now = now or datetime.now(UTC)
    current = state(session, settings, now)
    if current != "open":
        reason, status, message = _REFUSALS[current]
        raise Refused(reason, status, message)

    # An existing account of any kind, not only an existing pass: an address that
    # is already somebody's username must not become a second way into it.
    taken = session.execute(
        select(UserRow.id).where(UserRow.username == email)
    ).first() or session.execute(
        select(JudgePassRow.id).where(JudgePassRow.email == email)
    ).first()
    if taken:
        raise Refused("exists", 409, EXISTS_MESSAGE)

    password = generate_password()
    user = service.create_user(
        session, username=email, password=password, role=JUDGE_ROLE,
        display_name=JUDGE_DISPLAY_NAME,
        # System-generated and dead by the end of the day. A forced change would put
        # a second form between a judge and the product and protect nothing.
        must_change_password=False,
    )
    # Stored in UTC, not in the offset it was written with. PostgreSQL converts
    # either way, but SQLite keeps the wall-clock digits and drops the offset, and
    # everything reads a naive value back as UTC - so 23:59+03:00 came back as
    # 23:59 UTC and the pass on a laptop demonstration closed three hours late.
    # Measured on the rig: the strip said 12:02 for a pass that closed at 09:02.
    user.access_expires_at = settings.judge_pass_until.astimezone(UTC)
    row = JudgePassRow(
        id=str(uuid.uuid4()), user_id=user.id, email=email,
        lang=lang if lang in LANGS else "en", created_at=now,
        mail_status="queued" if mail.configured(settings) else "off", mail_detail="",
    )
    session.add(row)
    session.flush()
    return Issued(user=user, row=row, password=password)


# --- the email ------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    subject: str
    html: str
    text: str


def deliver(open_session: Callable[[], Iterator[Session]], pass_id: str, to: str,
            message: Message, settings: Settings) -> None:
    """Send the pass email and record what happened. Runs after the response.

    Never raises. A background task that throws has nowhere to report to, and the
    judge it concerns is already signed in; the outcome goes on the pass row, where
    the administration screen shows it.
    """
    outcome = mail.send(settings, to=to, subject=message.subject, html=message.html,
                        text=message.text, idempotency_key=f"judge-pass-{pass_id}")
    try:
        generator = open_session()
        session = next(generator)
        try:
            row = session.get(JudgePassRow, pass_id)
            if row is not None:
                row.mail_status = outcome.status
                row.mail_detail = outcome.detail[:200]
                row.mail_at = datetime.now(UTC)
                session.flush()
        finally:
            with contextlib.suppress(StopIteration):
                next(generator, None)
    except Exception:  # noqa: BLE001 - see the docstring
        log.exception("could not record the outcome of pass email %s", pass_id)


MONTHS_EN = ("January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December")
MONTHS_AR = ("يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو",
             "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر")


def until_text(until: datetime, lang: str) -> str:
    """The closing time in the offset it was configured with, which is Cairo's."""
    hhmm = until.strftime("%H:%M")
    if lang == "ar":
        return f"الساعة {hhmm} يوم {until.day} {MONTHS_AR[until.month - 1]} {until.year} بتوقيت القاهرة"
    return f"{hhmm} on {until.day} {MONTHS_EN[until.month - 1]} {until.year}, Cairo time"


# The figures are the poster's, measured on fifty buildings at a 50 M EGP budget,
# with the poster's own caveat beneath them. An email is not the place for a number
# the judge has not already been shown.
COPY = {
    "en": {
        "subject": "Your GEMP Judge Pass",
        "eyebrow": "Judge Pass · RoboDam 2026",
        "title": "Thank you for visiting Team Ecoforge.",
        "intro": "Your pass opened GEMP on the phone you scanned it with. These details "
                 "bring you back on any device, as often as you like, until the pass "
                 "closes.",
        "user_label": "Username",
        "pw_label": "Password",
        "valid_label": "Valid until",
        "button": "Sign in to GEMP",
        "or": "Or open {url} and choose Sign in.",
        "numbers": "Three figures from the portfolio",
        "n1v": "41.5 M EGP", "n1u": "a year",
        "n1": "saved across fifty public buildings on a 50 M EGP budget",
        "n2v": "+8.2 M EGP", "n2u": "a year",
        "n2": "more than the strongest heuristic, for identical spend",
        "n3v": "5,750 t", "n3u": "CO₂e a year",
        "n3": "of emissions avoided, 170,000 t over thirty years",
        "caveat": "Readings are simulated and saving fractions are literature values, so "
                  "these figures describe the decision model rather than the buildings' "
                  "measured behaviour.",
        "look": "Worth a look",
        "l1t": "Allocation map", "l1": "which buildings get funded, and why each measure won.",
        "l2t": "Evidence", "l2": "every claim in our paper, re-measured against the running "
                                 "system.",
        "l3t": "Integrity", "l3": "re-walk any building's signed chain of readings.",
        "sign": "Team Ecoforge · Ahmed Ebaid and Nada Wagdy",
        "footer": "You received this because this address was entered on a GEMP Judge "
                  "Pass at RoboDam 2026. The pass and its account close automatically at "
                  "{until}. Reply to this email to reach the team.",
    },
    "ar": {
        "subject": "بطاقة المحكّم الخاصة بك في GEMP",
        "eyebrow": "بطاقة محكّم · روبودام 2026",
        "title": "شكرًا لزيارتك فريق Ecoforge.",
        "intro": "فتحت بطاقتك منصة GEMP على الهاتف الذي مسحتها به. بهذه البيانات تعود "
                 "من أي جهاز، وكلما أردت، حتى تنتهي صلاحية البطاقة.",
        "user_label": "اسم المستخدم",
        "pw_label": "كلمة المرور",
        "valid_label": "صالحة حتى",
        "button": "تسجيل الدخول إلى GEMP",
        "or": "أو افتح {url} واختر «تسجيل الدخول».",
        "numbers": "ثلاثة أرقام من المحفظة",
        "n1v": "41.5 مليون جنيه", "n1u": "سنويًّا",
        "n1": "وفورات عبر خمسين مبنى عامًّا بميزانية 50 مليون جنيه",
        "n2v": "8.2 مليون جنيه", "n2u": "إضافية سنويًّا",
        "n2": "أكثر من أقوى طريقة تقريبية، بالإنفاق نفسه",
        "n3v": "5,750 طنًّا", "n3u": "من ثاني أكسيد الكربون المكافئ سنويًّا",
        "n3": "من الانبعاثات يتم تجنّبها، و170,000 طن على مدى ثلاثين عامًا",
        "caveat": "القراءات محاكاة ونسب التوفير مأخوذة من الأدبيات، لذا تصف هذه الأرقام "
                  "نموذج القرار لا السلوك المقاس للمباني.",
        "look": "يستحق نظرة",
        "l1t": "خريطة التخصيص", "l1": "أيّ المباني تُموَّل، ولماذا فاز كل إجراء.",
        "l2t": "الأدلة", "l2": "كل ادعاء في ورقتنا، مُعاد قياسه على النظام العامل.",
        "l3t": "سلامة البيانات", "l3": "أعد التحقق من سلسلة القراءات الموقّعة لأي مبنى.",
        "sign": "فريق Ecoforge · أحمد عبيد وندى وجدي",
        "footer": "وصلتك هذه الرسالة لأن هذا العنوان أُدخل في بطاقة محكّم لمنصة GEMP في "
                  "روبودام 2026. تُغلق البطاقة وحسابها تلقائيًا في {until}. للتواصل مع "
                  "الفريق، ردّ على هذه الرسالة.",
    },
}

# The platform's own light palette, so the email reads as the product it came from.
NAVY, MINT = "#003663", "#85e199"
INK, INK2, INK3 = "#0d1a26", "#44525e", "#6b7883"
GROUND, LINE, FACE = "#f3f4f7", "#e4e7ec", "#fafbfc"

SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
SANS_AR = "'Segoe UI',Tahoma,'Noto Sans Arabic',Arial,sans-serif"
MONO = "Consolas,Menlo,'Courier New',monospace"


def compose(lang: str, *, email: str, password: str, until: datetime,
            sign_in_url: str) -> Message:
    """The pass email, in the language the judge used on the pass page."""
    lang = lang if lang in LANGS else "en"
    c = COPY[lang]
    when = until_text(until, lang)
    return Message(
        subject=c["subject"],
        html=_html(lang, c, email=email, password=password, when=when, url=sign_in_url),
        text=_text(c, email=email, password=password, when=when, url=sign_in_url),
    )


def _html(lang: str, c: dict, *, email: str, password: str, when: str, url: str) -> str:
    """Tables and inline styles, because that is what every mail client renders.
    No external images: many clients block them, and the mark is drawn in HTML."""
    rtl = lang == "ar"
    direction = "rtl" if rtl else "ltr"
    start = "right" if rtl else "left"
    family = SANS_AR if rtl else SANS
    e = html.escape

    def type_(size: int, weight: int = 400, color: str = INK2, line: float = 1.55,
              face: str = family) -> str:
        return (f"font-family:{face};font-size:{size}px;font-weight:{weight};"
                f"line-height:{line};color:{color};")

    label = type_(11, 700, INK3, 1.4) + "letter-spacing:.08em;text-transform:uppercase;"
    creds = [
        (c["user_label"], email, type_(16, 600, INK, 1.45, MONO), "ltr"),
        (c["pw_label"], password, type_(18, 700, NAVY, 1.45, MONO), "ltr"),
        (c["valid_label"], when, type_(15, 600, INK, 1.45), direction),
    ]
    cred_rows = "".join(
        f'<tr><td style="padding:14px 18px;'
        f'{"" if i == 0 else f"border-top:1px solid {LINE};"}">'
        f'<div style="{label}">{e(name)}</div>'
        f'<div dir="{value_dir}" style="{style}margin-top:4px;word-break:break-all;'
        f'text-align:{start};">{e(value)}</div></td></tr>'
        for i, (name, value, style, value_dir) in enumerate(creds)
    )

    figures = [(c["n1v"], c["n1u"], c["n1"]), (c["n2v"], c["n2u"], c["n2"]),
               (c["n3v"], c["n3u"], c["n3"])]
    figure_rows = "".join(
        f'<tr><td style="padding:12px 0;border-top:1px solid {LINE};">'
        f'<span style="{type_(22, 700, NAVY, 1.2)}">{e(value)}</span> '
        f'<span style="{type_(13, 600, INK2, 1.2)}">{e(unit)}</span>'
        f'<div style="{type_(13)}margin-top:3px;">{e(note)}</div></td></tr>'
        for value, unit, note in figures
    )

    looks = [(c["l1t"], c["l1"]), (c["l2t"], c["l2"]), (c["l3t"], c["l3"])]
    look_rows = "".join(
        f'<tr><td style="width:10px;padding:8px 0;vertical-align:top;">'
        f'<div style="width:8px;height:8px;margin-top:7px;background:{MINT};'
        f'border-radius:2px;font-size:0;line-height:0;">&nbsp;</div></td>'
        f'<td style="padding:8px 0;padding-{start}:12px;{type_(14)}">'
        f'<strong style="color:{INK};font-weight:700;">{e(title)}</strong> &mdash; '
        f'{e(body)}</td></tr>'
        for title, body in looks
    )

    button = ""
    fallback = ""
    if url:
        button = (
            f'<tr><td style="padding:8px 32px 4px;text-align:{start};">'
            f'<a href="{e(url)}" style="display:inline-block;background:{NAVY};'
            f"color:#ffffff;text-decoration:none;font-family:{family};font-size:15px;"
            f'font-weight:700;line-height:1;padding:15px 26px;border-radius:10px;">'
            f'{e(c["button"])}</a></td></tr>'
        )
        # A URL has no break opportunities, and one that cannot wrap sets the width
        # of the whole message: on a 380px phone it pushed the card half off the
        # screen, and in Arabic, which anchors to the right, off the side a reader
        # starts from. Measured on a headless render before this line existed.
        fallback = (
            f'<tr><td style="padding:10px 32px 26px;word-break:break-all;'
            f'overflow-wrap:anywhere;{type_(12, 400, INK3, 1.5)}">'
            f'{e(c["or"]).format(url=f"<span dir=ltr>{e(url)}</span>")}</td></tr>'
        )

    footer = e(c["footer"]).format(until=e(when))

    return f"""<!doctype html>
<html lang="{lang}" dir="{direction}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only">
<title>{e(c["subject"])}</title>
</head>
<body style="margin:0;padding:0;background:{GROUND};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:{GROUND};" dir="{direction}">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"
       style="width:100%;max-width:600px;background:#ffffff;border:1px solid {LINE};
              border-radius:18px;overflow:hidden;text-align:{start};">
  <tr><td style="background:{NAVY};padding:28px 32px 30px;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
      <td style="width:40px;height:40px;background:#ffffff;border-radius:10px;
                 text-align:center;vertical-align:middle;">
        <span style="{type_(22, 800, NAVY, 1, SANS)}">G</span></td>
      <td style="padding-{start}:12px;{type_(20, 800, "#ffffff", 1, SANS)}
                 letter-spacing:.06em;">GEMP</td>
    </tr></table>
    <div style="margin-top:24px;{type_(11, 700, MINT, 1.4)}letter-spacing:.12em;
                text-transform:uppercase;">{e(c["eyebrow"])}</div>
    <h1 style="margin:8px 0 0;{type_(26, 700, "#ffffff", 1.25)}">{e(c["title"])}</h1>
  </td></tr>
  <tr><td style="height:4px;background:{MINT};font-size:0;line-height:0;">&nbsp;</td></tr>
  <tr><td style="padding:28px 32px 6px;{type_(15, 400, INK2, 1.6)}">{e(c["intro"])}</td></tr>
  <tr><td style="padding:14px 32px 16px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
           style="background:{FACE};border:1px solid {LINE};border-radius:12px;">
      {cred_rows}
    </table>
  </td></tr>
  {button}
  {fallback}
  <tr><td style="padding:6px 32px 0;">
    <div style="{label}">{e(c["numbers"])}</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
           style="margin-top:10px;">{figure_rows}</table>
    <div style="{type_(12, 400, INK3, 1.5)}margin-top:8px;">{e(c["caveat"])}</div>
  </td></tr>
  <tr><td style="padding:24px 32px 8px;">
    <div style="{label}">{e(c["look"])}</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
           style="margin-top:6px;">{look_rows}</table>
  </td></tr>
  <tr><td style="padding:16px 32px 30px;{type_(14, 700, INK, 1.5)}">{e(c["sign"])}</td></tr>
</table>
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"
       style="width:100%;max-width:600px;">
  <tr><td style="padding:16px 32px;text-align:center;{type_(11, 400, INK3, 1.55)}">
    {footer}</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
"""


def _text(c: dict, *, email: str, password: str, when: str, url: str) -> str:
    lines = [
        c["title"], "", c["intro"], "",
        f'{c["user_label"]}: {email}',
        f'{c["pw_label"]}: {password}',
        f'{c["valid_label"]}: {when}', "",
    ]
    if url:
        lines += [f'{c["button"]}: {url}', ""]
    lines += [
        c["numbers"],
        f'- {c["n1v"]} {c["n1u"]}: {c["n1"]}',
        f'- {c["n2v"]} {c["n2u"]}: {c["n2"]}',
        f'- {c["n3v"]} {c["n3u"]}: {c["n3"]}',
        c["caveat"], "",
        c["look"],
        f'- {c["l1t"]}: {c["l1"]}',
        f'- {c["l2t"]}: {c["l2"]}',
        f'- {c["l3t"]}: {c["l3"]}', "",
        c["sign"], "",
        c["footer"].format(until=when),
    ]
    return "\n".join(lines) + "\n"
