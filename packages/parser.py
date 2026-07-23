"""Parse Amazon.es notification emails into structured data.

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
  token Amazon embeds in every link. The Date header is only a fallback: on
  hand-forwarded mail it holds the forward time, days after the fact.
- Each kind declares required fields; anything missing raises ParseError
  naming the gap instead of returning half-parsed data.

Deadline semantics: `pickup_before` is the literal "antes del X" day — the
day the package may leave ("Se va"). The last safe day ("Último día") is the
day before; deriving it is the calendar's job, not the parser's.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
from enum import Enum
from urllib.parse import unquote

import dateparser
from bs4 import BeautifulSoup


class EmailKind(Enum):
    """One value per Amazon template we know. Model states are coarser:
    ORDERED/SHIPPED/OUT_FOR_DELIVERY are all `in_transit`; READY_FOR_PICKUP
    is `awaiting_pickup`; NO_LONGER_AVAILABLE and PICKUP_REMINDER drive *no*
    transition (the first is misleading, the second is a nag about a package
    already waiting — both change nothing); REVIEW_PUBLISHED never touches
    the calendar, but does drive the `reviews` app (see `packages.ingest`)."""

    ORDERED = "ordered"
    SHIPPED = "shipped"
    OUT_FOR_DELIVERY = "out_for_delivery"
    READY_FOR_PICKUP = "ready_for_pickup"
    PICKED_UP = "picked_up"
    DELIVERED = "delivered"  # home delivery completed (see _KIND_PATTERNS note)
    NO_LONGER_AVAILABLE = "no_longer_available"
    PICKUP_REMINDER = "pickup_reminder"  # "sigue en espera": a nag, no new info
    REVIEW_PUBLISHED = "review_published"


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
    total: Decimal | None = None  # order total as printed; 0.00 ⇒ Vine (weak
    # signal: a paid order settled with gift balance also prints 0.00)
    estimated_arrival: date | None = None
    pickup_before: date | None = None  # the "antes del" day itself
    pickup_code: str | None = None
    barcode_url: str | None = None  # static image scanned at the counter
    temp_password: str | None = None  # home-delivery one-time password
    picked_up_on: date | None = None
    review_id: str | None = None
    review_headline: str | None = None  # the review's own title
    review_rating: int | None = None  # 1-5, decoded from the star image name
    review_excerpt: str | None = None  # truncated body preview only — see
    # Review.text_is_complete: the email never carries the full text

    @property
    def is_vine(self):
        return self.total is not None and self.total == 0

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
    (EmailKind.PICKED_UP, r"paquete ha sido recogido"),
    (EmailKind.DELIVERED, r"paquete se ha entregado|paquete ha sido entregado"),
    (EmailKind.REVIEW_PUBLISHED, r"tu reseña está en directo|gracias por su reseña"),
    (EmailKind.OUT_FOR_DELIVERY, r"paquete está en reparto"),
    (EmailKind.SHIPPED, r"paquete se ha enviado"),
    (EmailKind.ORDERED, r"gracias por tu pedido"),
]

# Fields that must come out of each kind, or the parse fails loudly.
_REQUIRED = {
    EmailKind.ORDERED: ("order_id", "sent_at", "item_title", "total",
                        "estimated_arrival", "pickup_location"),
    EmailKind.SHIPPED: ("order_id", "sent_at", "estimated_arrival"),
    EmailKind.OUT_FOR_DELIVERY: ("order_id", "sent_at", "estimated_arrival"),
    EmailKind.READY_FOR_PICKUP: ("order_id", "sent_at", "pickup_before",
                                 "pickup_code", "pickup_location"),
    EmailKind.PICKED_UP: ("order_id", "picked_up_on"),
    EmailKind.DELIVERED: ("order_id", "sent_at"),
    EmailKind.NO_LONGER_AVAILABLE: ("order_id",),
    EmailKind.PICKUP_REMINDER: (),  # informational nag: recognize it, ignore it
    # item_title/review_id are the matching keys the reviews module needs
    # (audited against fixture 010: both are always present).
    EmailKind.REVIEW_PUBLISHED: ("item_title", "review_id"),
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
_ARRIVES = re.compile(r"^Llega (.+)$")
# A delivery-window variant of the arrival line: "Llegada entre el 24 de julio
# y el 28 de julio". We keep only the first (earliest) date as the estimate;
# the later Enviado email replaces it with a single firm day.
_ARRIVES_RANGE = re.compile(r"^Llegada entre el (.+?) y el .+$")
_BEFORE = re.compile(r"antes del (.+)$")
_PICKED = re.compile(r"^Recogido (.+)$")
# Searched over the joined text: the value may sit in its own tag (own line).
_PICKUP_CODE = re.compile(r"código de recogida es\s+(\w+)")
_TEMP_PASSWORD = re.compile(r"contraseña temporal es\s+(\w+)")
_ORDER_LINE = re.compile(r"^Pedido n")
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


def _resolve_date(phrase, base):
    """'el lunes' / 'hoy' / '13 de julio' → date, relative to base (forward)."""
    if base is None:
        return None
    phrase = re.sub(r"^el\s+", "", phrase.strip(), flags=re.IGNORECASE)
    parsed = dateparser.parse(
        phrase,
        languages=["es"],
        settings={"RELATIVE_BASE": base, "PREFER_DATES_FROM": "future"},
    )
    return parsed.date() if parsed else None


def _first_line_match(pattern, lines):
    for line in lines:
        match = pattern.search(line)
        if match:
            return match.group(1)
    return None


def _pickup_location(lines):
    """The bold venue line sits right above "Pedido n.º", bar opening hours."""
    try:
        idx = next(i for i, line in enumerate(lines) if _ORDER_LINE.match(line))
    except StopIteration:
        return None
    for line in reversed(lines[:idx]):
        if _NOISE_LINE.match(line):
            continue
        # Venues read "Amazon Counter - Les Mesures, ..."; anything without
        # that shape means the layout moved — better missing than wrong.
        return line if (" - " in line or "," in line) else None
    return None


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
    if token:
        sent_at = datetime.strptime(token.group(1), "%Y%m%d%H%M%S")
    elif msg.get("Date"):
        sent_at = parsedate_to_datetime(msg["Date"]).replace(tzinfo=None)
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

    arrives = _first_line_match(_ARRIVES, lines) or _first_line_match(_ARRIVES_RANGE, lines)
    before = _first_line_match(_BEFORE, lines)
    if before is None and (match := _BEFORE.search(subject)):
        before = match.group(1)
    picked = _first_line_match(_PICKED, lines)
    review_headline, review_excerpt = _review_headline_and_excerpt(lines)
    star_match = _STAR_RATING.search(html)

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
        pickup_location=_pickup_location(lines),
        total=Decimal(total_raw.group(1).replace(",", ".")) if total_raw else None,
        estimated_arrival=_resolve_date(arrives, sent_at) if arrives else None,
        pickup_before=_resolve_date(before, sent_at) if before else None,
        pickup_code=match.group(1) if (match := _PICKUP_CODE.search(haystack)) else None,
        barcode_url=_barcode_url(soup),
        temp_password=match.group(1) if (match := _TEMP_PASSWORD.search(haystack)) else None,
        picked_up_on=_resolve_date(picked, sent_at) if picked else None,
        review_id=match.group(1) if (match := _REVIEW_ID.search(urls)) else None,
        review_headline=review_headline,
        review_rating=int(star_match.group(1)) if star_match else None,
        review_excerpt=review_excerpt,
    )

    missing = [name for name in _REQUIRED[kind] if getattr(parsed, name) is None]
    if missing:
        raise ParseError(
            f"{kind.value} email is missing {', '.join(missing)} "
            f"(subject={subject!r})"
        )
    return parsed
