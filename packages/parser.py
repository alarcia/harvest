"""Parse the notification emails Harvest lives on into structured data.

Amazon.es sends all but one of them; the exception is Pepe y Dalda, the toy
shop that doubles as a delivery address (see EmailKind.STORE_RECEPTION).

Pure function: bytes in, ParsedEmail out. No database, no IMAP. Ingestion
maps the result onto the Package/RawEmail models and the calendar's chip
vocabulary; this module only reads what the email says.

Built for the day Amazon changes a template — fail loudly, never guess:

- The kind is detected from the stable headline phrase in the body (subjects
  get truncated and wrapped in "Fwd:"). An email matching no known kind
  raises ParseError; the caller stores and flags it, never drops it.
- Ids (order, shipment, ASIN) are read from the URLs, which survive copy
  tweaks better than human text.
- Every relative date ("Llega el lunes", "Recogido hoy") is resolved against
  the *original* send time, recovered from the `urn:rtn:msg:<timestamp>`
  token Amazon embeds in every link, or — for senders that embed no such
  token — from the "Date:" line of a Gmail forwarding block. The Date header
  is only the last fallback: on hand-forwarded mail it holds the forward
  time, days after the fact. Amazon writes both the token and that header in
  UTC, so both are converted to local time (`_as_local`) before any day is
  read off them.
- Each kind declares required fields; anything missing raises ParseError
  naming the gap instead of returning half-parsed data.

Deadline semantics: `pickup_before` is the literal "antes del X" day — the
day the package may leave ("Se va"). The last safe day ("Último día") is the
day before; deriving it is the calendar's job, not the parser's.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time
from datetime import timezone as dt_timezone
from decimal import Decimal
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
from enum import Enum
from urllib.parse import unquote

import dateparser
from bs4 import BeautifulSoup
from django.utils.timezone import get_default_timezone


class EmailKind(Enum):
    """One value per Amazon template we know. Model states are coarser:
    ORDERED/ESTIMATE_UPDATED/SHIPPED/OUT_FOR_DELIVERY are all `in_transit`;
    READY_FOR_PICKUP
    and DELIVERY_ATTEMPT both land on `awaiting_pickup` (the second at a
    carrier's office instead of an Amazon point); NO_LONGER_AVAILABLE,
    PICKUP_REMINDER and ADDRESS_UPDATED drive *no* transition (the first is
    misleading, the second is a nag about a package already waiting, the
    third only moves the destination — none changes state);
    REVIEW_PUBLISHED never touches the calendar, but does drive the
    `reviews` app (see `packages.ingest`)."""

    ORDERED = "ordered"
    SHIPPED = "shipped"
    # The user moved an order to a different destination on Amazon's site
    # before it shipped. Same template as ORDERED bar the headline, and it
    # drives no state change — only `pickup_location` matters.
    ADDRESS_UPDATED = "address_updated"
    # "Actualización de la estimación de entrega": Amazon re-dating an order
    # that hasn't shipped. In practice it is how a Vine item ordered in
    # *versión preliminar* announces itself — the product has no Amazon
    # listing yet when it's requested, so no "Pedido" email is ever sent and
    # this is the package's first word. Same Pedido-shaped body bar the total,
    # so ingestion treats it as one (and as Vine, see `packages.ingest`).
    ESTIMATE_UPDATED = "estimate_updated"
    OUT_FOR_DELIVERY = "out_for_delivery"
    READY_FOR_PICKUP = "ready_for_pickup"
    DELIVERY_ATTEMPT = "delivery_attempt"  # failed home delivery, UPS only for
    # now (see _KIND_PATTERNS note) — diverted to the carrier's own office
    PICKED_UP = "picked_up"
    DELIVERED = "delivered"  # home delivery completed (see _KIND_PATTERNS note)
    NO_LONGER_AVAILABLE = "no_longer_available"
    PICKUP_REMINDER = "pickup_reminder"  # "sigue en espera": a nag, no new info
    REVIEW_PUBLISHED = "review_published"
    # The only non-Amazon template: Pepe y Dalda's "Recepción paquete" /
    # "Recepción carta". One email per delivery, sent when it's already on
    # the counter — there is no order/shipped/estimated half of the story —
    # so it lands straight on `awaiting_pickup` with no deadline.
    STORE_RECEPTION = "store_reception"


class ParseError(ValueError):
    """The email couldn't be parsed into a complete ParsedEmail."""


@dataclass(frozen=True)
class Item:
    """One product inside the package. Usually one, but a locker pickup can
    bundle several items (even from *different orders* — see order_ids)."""

    title: str
    asin: str | None
    image_url: str | None


@dataclass(frozen=True)
class ParsedEmail:
    kind: EmailKind
    message_id: str | None
    subject: str
    sent_at: datetime | None  # original send time, not the forward's
    order_id: str | None = None  # the one labelled "Pedido n.º" in the body
    order_ids: frozenset = frozenset()  # all ids seen (body + links); a
    # consolidated locker pickup carries ids of every bundled order
    shipment_id: str | None = None
    shipment_ids: frozenset = frozenset()  # every shipment id seen; a
    # consolidated notification (e.g. two home-delivery orders dropped off in
    # the same visit) can carry more than one, unlike shipment_id's single
    # "the box this specific Enviado is about"
    items: tuple = ()
    pickup_location: str | None = None
    total: Decimal | None = None  # order total exactly as printed. Whether it
    # means "Vine" is not the parser's call — 0.00 is a weak signal (a paid
    # order settled with gift balance also prints 0.00) and the EU import
    # surcharge makes a free item cost money: see models.Config.means_vine.
    estimated_arrival: date | None = None
    estimated_arrival_end: date | None = None  # set only when the email gave a
    # window ("Llegada entre el 24 y el 28 de julio"): estimated_arrival is its
    # start, this its end. None on the usual single-day estimate.
    # The order went through but the payment didn't: Amazon is asking for
    # another card before it commits to a date, so this email carries no
    # arrival at all. True only on the "Pedido" template that says so — it is
    # what lets the estimate be missing without the parse failing.
    payment_review: bool = False
    pickup_before: date | None = None  # the "antes del" day itself
    pickup_code: str | None = None
    barcode_url: str | None = None  # static image scanned at the counter
    temp_password: str | None = None  # home-delivery one-time password
    picked_up_on: date | None = None
    # Pepe y Dalda only. `item_kind` mirrors Package.ItemKind's values
    # ("package"/"letter") so ingestion can assign it straight across;
    # `item_count` is the "Hemos recibido N …" figure (one email is still one
    # trip, so it only ever colours the description).
    item_kind: str | None = None
    item_count: int | None = None
    recipient: str | None = None  # who to name at the counter
    review_id: str | None = None
    review_headline: str | None = None  # the review's own title
    review_rating: int | None = None  # 1-5, decoded from the star image name
    review_excerpt: str | None = None  # truncated body preview only — see
    # Review.text_is_complete: the email never carries the full text

    @property
    def item_title(self):
        return self.items[0].title if self.items else None

    @property
    def asin(self):
        return self.items[0].asin if self.items else None

    @property
    def image_url(self):
        return self.items[0].image_url if self.items else None


# Kind detection: headline phrases as they appear in the body text. Order
# matters — "ya no está disponible" must win over the looser pickup phrases.
# Every pattern matches a full verb phrase, never the bare "Entregado"
# step-tracker label that sits in every email as a progress dot.
_KIND_PATTERNS = [
    (EmailKind.NO_LONGER_AVAILABLE, r"ya no está disponible para (?:su|la) recogida"),
    # A reminder that a package is *still* waiting ("El paquete está a la espera
    # de ser recogido", subject "Recordatorio: Paquete en espera de recogida").
    # Distinct from READY_FOR_PICKUP ("listo para…"): it repeats a pickup we
    # already know about and must not re-open or re-date it.
    (EmailKind.PICKUP_REMINDER,
     r"está a la espera de ser recogido|paquete en espera de recogida"),
    (EmailKind.READY_FOR_PICKUP, r"listo para (?:su|la)?\s*recogida"),
    # UPS-specific on purpose: the carrier name is printed right in this
    # sentence ("Lamentablemente, UPS no ha podido realizar la entrega…").
    # Only UPS is handled for now — a different carrier's equivalent notice
    # must keep tripping the unrecognized-email banner until it's added here.
    (EmailKind.DELIVERY_ATTEMPT, r"ups no ha podido realizar la entrega"),
    (EmailKind.PICKED_UP, r"paquete ha sido recogido"),
    (EmailKind.DELIVERED, r"paquete se ha entregado|paquete ha sido entregado"),
    (EmailKind.REVIEW_PUBLISHED, r"tu reseña está en directo|gracias por su reseña"),
    (EmailKind.OUT_FOR_DELIVERY, r"paquete está en reparto"),
    (EmailKind.SHIPPED, r"paquete se ha enviado"),
    (EmailKind.ORDERED, r"gracias por tu pedido"),
    # "Dirección de envío actualizada" — the destination changed after the
    # order was placed. The body is the Pedido template with a different
    # headline (same venue line above "Pedido n.º", same total, same "Llega
    # el …"), so this must be matched *after* ORDERED to keep an email that
    # says both from being filed as a redirection.
    (EmailKind.ADDRESS_UPDATED, r"direcci[oó]n de env[ií]o actualizada"),
    # "Actualización sobre la fecha estimada de entrega" (body headline; the
    # subject words it "Actualización de la estimación de entrega de tu pedido
    # de Amazon.com con número …", and both are matched so a reworded one
    # still lands). Same reason as ADDRESS_UPDATED for sitting after ORDERED:
    # the body is the Pedido template with the headline swapped.
    (EmailKind.ESTIMATE_UPDATED,
     r"actualizaci[oó]n sobre la fecha estimada de entrega"
     r"|actualizaci[oó]n de la estimaci[oó]n de entrega"),
    # Pepe y Dalda writes both of these by hand, so match either the subject
    # ("Recepción carta", "Recepción de paquete") or the body line ("Hemos
    # recibido 1 carta para ti"). The phrase alone doesn't prove it's *that*
    # shop, but pickup_location is required for this kind and only the shop's
    # own signature ever fills it, so a lookalike from somewhere else fails
    # loudly into the banner — same rule as DELIVERY_ATTEMPT above, one
    # sender at a time.
    (EmailKind.STORE_RECEPTION,
     r"recepci[oó]n\s+(?:de\s+)?(?:paquete|carta)"
     r"|hemos recibido\s+\d+\s+(?:paquete|carta)"),
]

# Fields that must come out of each kind, or the parse fails loudly.
_REQUIRED = {
    # `estimated_arrival` is waived for an order held for payment review, the
    # one case where Amazon knowingly sends a "Pedido" with no date on it (see
    # parse_email). Every other field stands.
    EmailKind.ORDERED: ("order_id", "sent_at", "item_title", "total",
                        "estimated_arrival", "pickup_location"),
    EmailKind.SHIPPED: ("order_id", "sent_at", "estimated_arrival"),
    # The destination line *is* the news here, so it's required; without it
    # the email says nothing and belongs behind the red banner. The arrival
    # is not: the email reprints it as a courtesy, and losing the redirection
    # because Amazon dropped a "Llega el …" line would be the worse trade.
    EmailKind.ADDRESS_UPDATED: ("order_id", "sent_at", "pickup_location"),
    # The same fields as ORDERED bar the total: this template prints the
    # item's list price ("34 99 €" beside the photo), never an order total, so
    # there is no "Total …" line to read and Vine is settled by the kind
    # itself instead (see `packages.ingest`). `item_title` *is* required, for
    # the opposite reason to ADDRESS_UPDATED's leniency: this email creates
    # the package outright, and without the item block the description would
    # fall back to echoing this subject's boilerplate.
    EmailKind.ESTIMATE_UPDATED: ("order_id", "sent_at", "item_title",
                                 "estimated_arrival", "pickup_location"),
    EmailKind.OUT_FOR_DELIVERY: ("order_id", "sent_at", "estimated_arrival"),
    EmailKind.READY_FOR_PICKUP: ("order_id", "sent_at", "pickup_before",
                                 "pickup_code", "pickup_location"),
    # No pickup_before/pickup_code: the email genuinely doesn't carry them —
    # UPS gives neither a deadline nor the office in it, only the tracking
    # link the user has to open by hand (see Package.carrier_tracking_url).
    EmailKind.DELIVERY_ATTEMPT: ("order_id", "sent_at", "pickup_location"),
    EmailKind.PICKED_UP: ("order_id", "picked_up_on"),
    EmailKind.DELIVERED: ("order_id", "sent_at"),
    EmailKind.NO_LONGER_AVAILABLE: ("order_id",),
    EmailKind.PICKUP_REMINDER: (),  # informational nag: recognize it, ignore it
    # item_title/review_id are the matching keys the reviews module needs
    # (audited against fixture 010: both are always present).
    EmailKind.REVIEW_PUBLISHED: ("item_title", "review_id"),
    # No order id, no item, no deadline — this shop's notice carries none of
    # that. `pickup_location` is the shop's own signature block, which is
    # what proves the sender (see _KIND_PATTERNS); `sent_at` is the day it
    # landed on the counter, i.e. the whole calendar entry; `item_kind` says
    # parcel or letter. `recipient` is deliberately optional: the wording
    # ("para ti") doesn't always name anyone.
    EmailKind.STORE_RECEPTION: ("sent_at", "pickup_location", "item_kind"),
}

# Bidi embeddings (Amazon wraps order numbers in RTL marks), zero-widths,
# soft hyphens and the combining-joiner runs Amazon pads preheaders with.
_INVISIBLE = re.compile(
    "[\u00ad\u034f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)

_FWD_PREFIX = re.compile(r"^(?:fwd?|rv|re)\s*:\s*", re.IGNORECASE)
_SENT_TOKEN = re.compile(r"urn:rtn:msg:(\d{14})")
_ORDER_ID = re.compile(r"\d{3}-\d{7}-\d{7}")
_SHIPMENT_ID = re.compile(r"shipmentId=([A-Za-z0-9]+)")
_ASIN_ANY = re.compile(r"/dp/([A-Z0-9]{10})")
_REVIEW_ID = re.compile(r"/review/(R[A-Z0-9]+)")
_TOTAL = re.compile(r"Total\s+(\d+[.,]\d{2})\s*€")
# "Llega el lunes", and the future tense the estimate-update template uses:
# "Llegará el martes, 4 de agosto de 2026". "entre" is excluded so the window
# variant below keeps its own line rather than being read as one long day
# phrase — dateparser would make nonsense of it.
_ARRIVES = re.compile(r"^Llega(?:rá)? (?!entre\b)(.+)$")
# A delivery-window variant of the arrival line: "Llegada entre el 24 de julio
# y el 28 de julio". The start is the estimate proper (that's the day the
# calendar marks); the end is kept alongside it so the card can word the window
# honestly instead of pretending Amazon promised the first day. The later
# Enviado email replaces both with a single firm day.
_ARRIVES_RANGE = re.compile(r"^Llega(?:da|rá) entre el (.+?) y el (.+)$")
_BEFORE = re.compile(r"antes del (.+)$")
_PICKED = re.compile(r"^Recogido (.+)$")
# Searched over the joined text: the value may sit in its own tag (own line).
_PICKUP_CODE = re.compile(r"código de recogida es\s+(\w+)")
_TEMP_PASSWORD = re.compile(r"contraseña temporal es\s+(\w+)")
_ORDER_LINE = re.compile(r"^Pedido n")
# The head of an Amazon venue line. Used to rejoin a destination Amazon broke
# across several lines (see _pickup_location); ingestion classifies points by
# this same prefix, but the parser must stay database-free, so it keeps its own.
_VENUE_HEAD = re.compile(r"^Amazon (?:Locker|Counter) - ")
# How far above "Pedido n.º" that rejoining may reach. Three lines is what the
# real case needs (fixture 180); beyond it sits the step tracker ("Pedido /
# Enviado / En reparto / Entregado"), which must never be read as an address.
_VENUE_MAX_LINES = 4
# "La revisión del pago es necesaria" / "Actualice el método de pago": Amazon
# took the order but hasn't charged it, and the template then prints **no
# arrival line at all** — there is no promise to make until the payment goes
# through. The only known reason a "Pedido" carries no date, so it's matched
# explicitly instead of making the estimate optional everywhere: a template
# that stops saying "Llega el …" for any *other* reason must keep failing
# loudly (see _REQUIRED).
_PAYMENT_REVIEW = re.compile(
    r"revisi[oó]n del pago es necesaria|actualice el m[eé]todo de pago",
    re.IGNORECASE,
)
# Pepe y Dalda's one informative line: "Hemos recibido 1 carta para ti."
_RECEPTION = re.compile(
    r"hemos recibido\s+(\d+)\s+(paquete|carta)s?(?:\s+para\s+([^.,;]+))?",
    re.IGNORECASE,
)
# …and the fallback when they reword it: the subject still says which it is.
_RECEPTION_SUBJECT = re.compile(r"recepci[oó]n\s+(?:de\s+)?(paquete|carta)",
                                re.IGNORECASE)
_ITEM_KINDS = {"paquete": "package", "carta": "letter"}
# "para ti" names nobody: the addressee is whoever the shop emailed, so the
# name comes from the To: line instead (see _addressee).
_PRONOUNS = {"ti", "tí", "vos", "usted", "ustedes", "vosotros", "vosotras",
             "ustedes dos", "vosotros dos"}
_STORE_NAME = "Pepe y Dalda"
# A Gmail forwarding block's own header lines ("Date: jue, 23 jul 2026 a las
# 17:46", "To: Javier Alarcia <"). Present on hand-forwards, absent on the
# automatic ones — which is fine, those keep the original headers intact.
_FWD_HEADER = re.compile(r"^(Date|To|Fecha|Para):\s*(.*)$", re.IGNORECASE)
# Noise between the pickup-point line and "Pedido n.º": opening hours.
_NOISE_LINE = re.compile(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$|^[\w\sñáéíóúü-]+:$",
                         re.IGNORECASE)
# The star rating isn't printed as text — it's the filename of the star-row
# image (audited fixture 010: "star_lightmode_4.png" / the dark-mode twin).
_STAR_RATING = re.compile(r"star_(?:light|dark)mode_(\d)\.png")
_REVIEW_LABEL = "Tu opinión"
_VIEW_FULL_REVIEW = "Vea su reseña completa"


def _text_lines(html):
    """Visible text of the HTML as clean, non-empty lines."""
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    text = _INVISIBLE.sub("", unicodedata.normalize("NFC", text))
    lines = (re.sub(r"\s+", " ", line).strip() for line in text.splitlines())
    return [line for line in lines if line]


def _as_local(moment):
    """A send time as a naive *local* timestamp.

    Amazon stamps its tracking token in UTC — verified 2026-08-02 against
    auto-forwarded mail, where the original header survives alongside it
    (token `20260802114719` ↔ `Date: Sun, 02 Aug 2026 11:47:21 +0000`) — and
    so is the `Date:` header of that same mail. Everything downstream is
    local: the ○ dot's day, the base "Llega el lunes" resolves against, the
    clock in the audit trail. Two hours of drift only shows in summer and
    only after midnight in Madrid, and then it dates the whole entry to the
    day before (fixture 180, ordered at 00:49).

    `sent_at` stays naive throughout — ingestion makes it aware against this
    same zone — so the conversion is done here rather than left to whoever
    reads it. A header with no offset at all ("-0000", meaning unknown) is
    read as UTC, which is what that spelling means.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt_timezone.utc)
    return moment.astimezone(get_default_timezone()).replace(tzinfo=None)


def _resolve_date(phrase, base):
    """'el lunes' / 'hoy' / '13 de julio' → date, relative to base (forward).

    `base` is normally the email's send time, but may be a plain date: the end
    of a delivery window resolves against the *start* of that window rather
    than against the email, so "entre el 30 de diciembre y el 3 de enero"
    can't land its end in the year the window began."""
    if base is None:
        return None
    if not isinstance(base, datetime):
        base = datetime.combine(base, time.min)
    phrase = re.sub(r"^el\s+", "", phrase.strip(), flags=re.IGNORECASE)
    parsed = dateparser.parse(
        phrase,
        languages=["es"],
        settings={"RELATIVE_BASE": base, "PREFER_DATES_FROM": "future"},
    )
    return parsed.date() if parsed else None


def _first_line_search(pattern, lines):
    for line in lines:
        match = pattern.search(line)
        if match:
            return match
    return None


def _first_line_match(pattern, lines):
    match = _first_line_search(pattern, lines)
    return match.group(1) if match else None


def _pickup_location(lines):
    """The bold venue line sits right above "Pedido n.º", bar opening hours.

    Usually one line. Some templates break the *same* address across three
    ("Amazon Counter - Les Mesures," / "AVDA SALORIA, 30" / "BAJO, LA SEU
    D´URGELL, 25700" — fixture 180), and keeping only the last would lose both
    the venue's name and the "Amazon Counter - " prefix ingestion classifies
    it by, filing a counter pickup as a home delivery. So the block is rejoined
    whenever such a head turns up just above it, into exactly the one-line form
    every other template prints (postal code included, which is the dedup key).

    A home address is a single line in every sample so far, and keeps that
    reading: with no head to anchor on, only the nearest non-noise line is
    taken — never the step-tracker labels stacked above it.
    """
    try:
        idx = next(i for i, line in enumerate(lines) if _ORDER_LINE.match(line))
    except StopIteration:
        return None
    block = []
    for line in reversed(lines[:idx]):
        if _NOISE_LINE.match(line):
            continue
        block.insert(0, line)
        if _VENUE_HEAD.match(line):
            return " ".join(block)
        if len(block) >= _VENUE_MAX_LINES:
            break
    if not block:
        return None
    # Venues read "Amazon Counter - Les Mesures, ..."; anything without
    # that shape means the layout moved — better missing than wrong.
    nearest = block[-1]
    return nearest if (" - " in nearest or "," in nearest) else None


def _forwarded_header(lines, name):
    """The value of one header line inside a Gmail forwarding block.

    Only the first few lines of a forward are that block, and Amazon's own
    body never opens with "Date:"/"To:", so the search is capped instead of
    trying to delimit the block precisely. Returns "" when this isn't a
    hand-forward, which is the normal case in production."""
    for line in lines[:12]:
        match = _FWD_HEADER.match(line)
        if match and match.group(1).lower() == name.lower():
            return match.group(2).strip()
    return ""


def _addressee(msg, lines):
    """First name of whoever the original email was addressed to.

    Needed because Pepe y Dalda writes "para ti", naming nobody: the person
    to ask for at the counter is the addressee. On a hand-forward the To:
    header is the Harvest mailbox, so the forwarding block's own To: line
    wins; automatic forwards keep the real one. Returns "" rather than a
    guess when all that's there is a bare address (the field stays editable,
    and a wrong name at the counter is worse than none)."""
    raw = _forwarded_header(lines, "To") or msg.get("To", "")
    # Hand-forwards split "To: Javier Alarcia <jabogood@gmail.com>" across
    # lines: the display name arrives with a dangling "<".
    display = raw.split("<")[0].strip().strip('"')
    if not display or "@" in display:
        return ""
    return display.split()[0]


def _store_signature(lines):
    """Pepe y Dalda's sign-off, which doubles as the shop's address:

        Juguetes Pepe y Dalda
        // c/Regència d'Urgell, 17 // La Seu d'Urgell

    Taken from the *last* line naming the shop — the first one is the "De:"
    sender of a forwarding block — and joined with the address line that
    follows it, since the shop's markup splits the two. Returns None when the
    shop isn't named at all, which is how a lookalike notice from some other
    sender fails loudly instead of being filed here (see _KIND_PATTERNS)."""
    idx = next((i for i in range(len(lines) - 1, -1, -1)
                if _STORE_NAME.lower() in lines[i].lower()), None)
    if idx is None:
        return None
    parts = [lines[idx]]
    if idx + 1 < len(lines) and lines[idx + 1].lstrip().startswith("//"):
        parts.append(lines[idx + 1])
    signature = " ".join(parts)
    return re.sub(r"\s*//\s*", " · ", signature).strip(" ·")[:120]


def _clean_img_url(src):
    """Undo Gmail's image proxy if present: the original URL rides after '#'."""
    return src.split("#", 1)[1] if "#http" in src else src


def _items(soup):
    """Products from the item links: the image alt carries the full title.

    Only anchors whose ref_ contains `fed_asin_title` (lifecycle emails) or
    `cm_rv_eml` (review emails) are the package's own items; the 'Sigue
    comprando' upsells use different ref_ codes. Each item appears twice
    (image link + text link); the text link has no <img>, so iterating
    image-bearing anchors dedupes naturally."""
    items = []
    for anchor in soup.find_all("a", href=True):
        if "fed_asin_title" not in anchor["href"] and "cm_rv_eml" not in anchor["href"]:
            continue
        img = anchor.find("img")
        if not (img and img.get("alt")):
            continue
        match = _ASIN_ANY.search(unquote(anchor["href"]))
        items.append(Item(
            title=_INVISIBLE.sub("", img["alt"]).strip(),
            asin=match.group(1) if match else None,
            image_url=_clean_img_url(img["src"]) if img.get("src") else None,
        ))
    return tuple(items)


def _review_headline_and_excerpt(lines):
    """The review's own headline and its truncated body preview, out of the
    "Tu opinión" block. The excerpt repeats at more than one truncation
    length in the same email (different client/breakpoint renderings of the
    same paragraph) — keep the longest, which carries the most text."""
    try:
        idx = lines.index(_REVIEW_LABEL)
    except ValueError:
        return None, None
    block = lines[idx + 1:]
    try:
        block = block[:block.index(_VIEW_FULL_REVIEW)]
    except ValueError:
        pass
    if not block:
        return None, None
    headline = block[0]
    body_lines = [line for line in block[1:] if line != headline]
    excerpt = max(body_lines, key=len) if body_lines else None
    return headline, excerpt


def _barcode_url(soup):
    img = soup.find("img", alt="Pickup barcode")
    return _clean_img_url(img["src"]) if img and img.get("src") else None


def parse_email(raw):
    """Parse one raw RFC822 message (bytes) into a ParsedEmail.

    Raises ParseError when the template is unknown or a field required for
    its kind can't be read — the caller must store and surface the failure,
    never discard it.
    """
    msg = message_from_bytes(raw, policy=policy.default)
    subject = _FWD_PREFIX.sub("", _INVISIBLE.sub("", msg.get("Subject", "")).strip())
    message_id = msg.get("Message-ID")

    body = msg.get_body(preferencelist=("html", "plain"))
    if body is None:
        raise ParseError(f"No text part found (subject={subject!r})")
    html = body.get_content()
    soup = BeautifulSoup(html, "html.parser")
    lines = _text_lines(html)
    haystack = "\n".join(lines)
    urls = unquote(html)  # %3D→= etc; ids live in link query params

    kind = next(
        (k for k, pattern in _KIND_PATTERNS
         if re.search(pattern, haystack, re.IGNORECASE)
         or re.search(pattern, subject, re.IGNORECASE)),
        None,
    )
    if kind is None:
        raise ParseError(f"Unrecognized email type (subject={subject!r})")

    token = _SENT_TOKEN.search(html)
    forwarded_date = _forwarded_header(lines, "Date")
    if token:
        sent_at = _as_local(datetime.strptime(token.group(1), "%Y%m%d%H%M%S"))
    elif forwarded_date and (parsed_fwd := dateparser.parse(
            forwarded_date, languages=["es"])):
        # A sender that embeds no tracking token (Pepe y Dalda) leaves the
        # forwarding block as the only record of when the email really went
        # out — and on a hand-forward the Date header below is the forward's
        # own, days late. Getting this wrong dates the whole calendar entry.
        # Gmail writes that block in the reader's own timezone, so it needs no
        # conversion, unlike the two branches around it.
        sent_at = parsed_fwd.replace(tzinfo=None)
    elif msg.get("Date"):
        sent_at = _as_local(parsedate_to_datetime(msg["Date"]))
    else:
        sent_at = None

    # The body's "Pedido n.º" is the package's own order; links may add more
    # (a consolidated locker pickup references every bundled order).
    text_ids = _ORDER_ID.findall(haystack)
    url_ids = _ORDER_ID.findall(urls)
    order_id = text_ids[0] if text_ids else (url_ids[0] if url_ids else None)
    shipment_id = match.group(1) if (match := _SHIPMENT_ID.search(urls)) else None
    shipment_ids = frozenset(_SHIPMENT_ID.findall(urls))
    total_raw = _TOTAL.search(haystack)

    arrives = _first_line_match(_ARRIVES, lines)
    window = None if arrives else _first_line_search(_ARRIVES_RANGE, lines)
    if window:
        arrives = window.group(1)
    before = _first_line_match(_BEFORE, lines)
    if before is None and (match := _BEFORE.search(subject)):
        before = match.group(1)
    picked = _first_line_match(_PICKED, lines)
    review_headline, review_excerpt = _review_headline_and_excerpt(lines)
    star_match = _STAR_RATING.search(html)

    # A window's end is resolved against its own start, not against the email:
    # anchored to the send time, "entre el 30 de diciembre y el 3 de enero"
    # would resolve the end before the start. Dropped unless it really is
    # later, so a layout change can only cost us the wording, never invert it.
    estimated_arrival = _resolve_date(arrives, sent_at) if arrives else None
    estimated_arrival_end = (
        _resolve_date(window.group(2), estimated_arrival)
        if window and estimated_arrival else None
    )
    if estimated_arrival_end and estimated_arrival_end <= estimated_arrival:
        estimated_arrival_end = None

    item_kind, item_count, recipient = None, None, None
    pickup_location = _pickup_location(lines)
    if kind is EmailKind.STORE_RECEPTION:
        # A different shape of email entirely: no "Pedido n.º" to hang the
        # venue line off, and the destination is the shop's own signature.
        pickup_location = _store_signature(lines)
        reception = _RECEPTION.search(haystack)
        subject_kind = _RECEPTION_SUBJECT.search(subject) or _RECEPTION_SUBJECT.search(haystack)
        if reception:
            item_count = int(reception.group(1))
            item_kind = _ITEM_KINDS[reception.group(2).lower()]
            named = (reception.group(3) or "").strip()
            recipient = "" if named.lower() in _PRONOUNS else named[:60]
        elif subject_kind:
            item_kind = _ITEM_KINDS[subject_kind.group(1).lower()]
        if not recipient:
            recipient = _addressee(msg, lines)

    parsed = ParsedEmail(
        kind=kind,
        message_id=message_id,
        subject=subject,
        sent_at=sent_at,
        order_id=order_id,
        order_ids=frozenset(text_ids + url_ids),
        shipment_id=shipment_id,
        shipment_ids=shipment_ids,
        items=_items(soup),
        pickup_location=pickup_location,
        total=Decimal(total_raw.group(1).replace(",", ".")) if total_raw else None,
        estimated_arrival=estimated_arrival,
        estimated_arrival_end=estimated_arrival_end,
        payment_review=bool(_PAYMENT_REVIEW.search(haystack)),
        pickup_before=_resolve_date(before, sent_at) if before else None,
        pickup_code=match.group(1) if (match := _PICKUP_CODE.search(haystack)) else None,
        barcode_url=_barcode_url(soup),
        temp_password=match.group(1) if (match := _TEMP_PASSWORD.search(haystack)) else None,
        picked_up_on=_resolve_date(picked, sent_at) if picked else None,
        item_kind=item_kind,
        item_count=item_count,
        recipient=recipient or None,
        review_id=match.group(1) if (match := _REVIEW_ID.search(urls)) else None,
        review_headline=review_headline,
        review_rating=int(star_match.group(1)) if star_match else None,
        review_excerpt=review_excerpt,
    )

    required = _REQUIRED[kind]
    if kind is EmailKind.ORDERED and parsed.payment_review:
        # An order held for payment review prints no "Llega el …" line: there
        # is nothing to promise until the card goes through, and the arrival
        # turns up in the "Enviado" that follows. Losing the order over a date
        # Amazon deliberately withheld would be the worse trade — the same
        # judgement ADDRESS_UPDATED already makes. Waived only here: any other
        # dateless "Pedido" is a template change and still fails loudly.
        required = tuple(name for name in required if name != "estimated_arrival")
    missing = [name for name in required if getattr(parsed, name) is None]
    if missing:
        raise ParseError(
            f"{kind.value} email is missing {', '.join(missing)} "
            f"(subject={subject!r})"
        )
    return parsed
