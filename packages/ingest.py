"""Ingestion pipeline: inbox → RawEmail → parser → Package.

Two halves, deliberately separate: scan_inbox() talks IMAP and feeds raw
bytes in; process_message() stores, parses and applies — that half is what
the tests exercise, fixture bytes in, database rows out.

Rules that matter:

- The raw email is stored *before* parsing, always. History gets reprocessed
  the day the parser improves.
- Idempotent by Message-ID: the same email seen twice changes nothing. Gmail
  preserves the original Message-ID on auto-forward, and a hand-forward gets
  the forwarding Gmail's own one — both stable. Idempotency is by the DB, never
  by IMAP flags, so trashing processed mail (below) can't break it.
- A failed parse is recorded on the RawEmail (the calendar shows it as a red
  banner) and never aborts the scan of the remaining messages.
- "Ya no está disponible" is misleading (proven repeatedly): it drives no
  state change. "Se ha recogido" is treated as final truth: it confirms
  picked_up, and a pickup empties the whole point.
- Home deliveries (location is not an Amazon Locker/Counter) create no rows:
  the calendar tracks trips to pickup points. The raw email stays stored, so
  the decision is reversible by reprocessing.
- Once GMAIL_TRASH_PROCESSED is on, successfully-processed emails are moved to
  Gmail's Trash (30-day grace). Parse failures are left in the inbox so an
  unhandled email is doubly visible (inbox + red banner). The whole run is
  logged with timestamps for `docker logs`.
"""

import hashlib
import imaplib
import logging
import re
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Package, PickupPoint, RawEmail
from .parser import EmailKind, ParseError, parse_email

logger = logging.getLogger("packages.ingest")

_LOCATION = re.compile(r"^Amazon (Locker|Counter) - ")

# States only move forward; a late or re-forwarded email never regresses one.
# PICKED_UP and DELIVERED are both terminal (a pickup and a home delivery).
_RANK = {
    Package.State.IN_TRANSIT: 0,
    Package.State.AWAITING_PICKUP: 1,
    Package.State.PICKED_UP: 2,
    Package.State.DELIVERED: 2,
    Package.State.RETURNED: 2,
}


def _pickup_point(location):
    """PickupPoint for a destination line. "Amazon Locker/Counter - …" is a
    real pickup point; any other named place is a home/relative address (a
    HOME point, delivered and done, no trip). None only when there's no
    location at all. Dedup is by the exact string — Amazon spells the same
    venue differently across templates, so near-duplicates are expected and
    benign (the calendar only colors by amazon-vs-store)."""
    if not location:
        return None
    match = _LOCATION.match(location)
    if not match:
        point, _ = PickupPoint.objects.get_or_create(
            name=location[:120], defaults={"kind": PickupPoint.Kind.HOME},
        )
        return point
    kind = (PickupPoint.Kind.AMAZON_LOCKER if match.group(1) == "Locker"
            else PickupPoint.Kind.AMAZON_COUNTER)
    point, _ = PickupPoint.objects.get_or_create(
        name=location[:120], defaults={"kind": kind},
    )
    return point


_COUNT_ONLY = re.compile(r"^\d+\s+productos?$", re.IGNORECASE)


def _description(parsed):
    """A human name for the package. Item links carry the real product title;
    picked-up and home-delivery emails often have none. Their subject sometimes
    names the product ("Se ha recogido Cargador…") and sometimes only counts it
    ("Se han recogido 2 productos", "Entregado: 1 producto") — naming nothing.
    Return "" in the count-only case so the calendar shows a "desconocido"
    placeholder instead of echoing the boilerplate; an empty description is not
    user data, so a later email that does carry the item can still fill it in."""
    if parsed.items:
        return " + ".join(item.title for item in parsed.items)[:255]
    remainder = re.sub(r"^Se han? recogido\s+", "", parsed.subject).strip()
    remainder = re.sub(r"^Entregado:\s*", "", remainder, flags=re.IGNORECASE).strip()
    # Delivered subjects append "| N.º de pedido XXX-…"; drop it so what's left
    # is either a product name or just a count.
    remainder = re.sub(r"\s*\|?\s*N\.?º de pedido.*$", "", remainder,
                       flags=re.IGNORECASE).strip()
    return "" if not remainder or _COUNT_ONLY.match(remainder) else remainder[:255]


def _find_packages(parsed):
    """Packages this email talks about. The shipment id is the strongest key;
    order numbers come second — matching *any* seen id covers consolidated
    locker pickups, which bundle items from several orders."""
    if parsed.shipment_id:
        found = list(Package.objects.filter(shipment_id=parsed.shipment_id))
        if found:
            return found
    ids = set(parsed.order_ids) | ({parsed.order_id} if parsed.order_id else set())
    if ids:
        return list(Package.objects.filter(order_id__in=ids))
    return []


def _fill(pkg, parsed):
    """Copy fields the email knows and the package doesn't. Never overwrites
    user-edited data with blanks. Cost/Vine is deliberately *not* here — it
    follows a lifecycle rule of its own (see _apply_cost)."""
    if not pkg.description:
        pkg.description = _description(parsed)
    if not pkg.asin and parsed.asin:
        pkg.asin = parsed.asin
    if not pkg.image_url and parsed.image_url:
        pkg.image_url = parsed.image_url
    if parsed.estimated_arrival:
        pkg.estimated_arrival = parsed.estimated_arrival


def _apply_cost(pkg, parsed, *, authoritative):
    """Set cost and the Vine flag.

    A Vine order and a paid order settled with Amazon balance BOTH print
    "Total 0.00€" on the *Pedido* email — indistinguishable there. Only the
    *Enviado* email prints the real amount (the colchón: 0.00€ ordered,
    19.98€ shipped). So: assume Vine from a 0.00€ order, then let the shipped
    email confirm or refute it. The shipped total is `authoritative` and a
    later-processed Pedido (re-forward, out-of-order delivery) must not
    clobber it — guarded by shipped_on."""
    if parsed.total is None:
        return
    if authoritative or not pkg.shipped_on:
        pkg.cost = parsed.total
        pkg.is_vine = parsed.total == 0


def _apply(parsed):
    """Map one parsed email onto the packages table.

    Returns (package, note) — package may be None when the email correctly
    leads to no row (reviews, unmatched return notices, or a placeless email).
    """
    sent_on = parsed.sent_at.date() if parsed.sent_at else None
    kind = parsed.kind
    matches = _find_packages(parsed)
    point = _pickup_point(parsed.pickup_location)

    if kind in (EmailKind.ORDERED, EmailKind.SHIPPED, EmailKind.OUT_FOR_DELIVERY):
        pkg = None
        if matches:
            # A shipped notice for an order that already has a *different*
            # shipment is a split order: that box is a new package (new trip).
            if (kind == EmailKind.SHIPPED and parsed.shipment_id
                    and all(p.shipment_id and p.shipment_id != parsed.shipment_id
                            for p in matches)):
                pkg = None
            else:
                unshipped = [p for p in matches if not p.shipment_id]
                pkg = (unshipped or matches)[0]
        if pkg is None:
            if point is None:
                return None, "Sin destino identificable: ignorado"
            pkg = Package(
                pickup_point=point,
                order_id=parsed.order_id or "",
                state=Package.State.IN_TRANSIT,
            )
        if point is not None:
            pkg.pickup_point = point
        if kind == EmailKind.ORDERED:
            pkg.ordered_on = sent_on
        else:
            pkg.shipped_on = pkg.shipped_on or sent_on
        if parsed.shipment_id:
            pkg.shipment_id = parsed.shipment_id
        _fill(pkg, parsed)
        was_vine = pkg.is_vine
        _apply_cost(pkg, parsed, authoritative=(kind == EmailKind.SHIPPED))
        pkg.save()
        if kind == EmailKind.SHIPPED and was_vine and not pkg.is_vine:
            return pkg, f"Coste real {parsed.total}€ en el envío: desmarcado como Vine"
        return pkg, ""

    if kind == EmailKind.READY_FOR_PICKUP:
        if not matches:
            if point is None:
                return None, "Listo para recogida sin punto Amazon: ignorado"
            matches = [Package(
                pickup_point=point,
                order_id=parsed.order_id or "",
                shipment_id=parsed.shipment_id or "",
            )]
        for pkg in matches:
            if _RANK[pkg.state] >= _RANK[Package.State.PICKED_UP]:
                continue  # already resolved; a re-forward must not reopen it
            pkg.state = Package.State.AWAITING_PICKUP
            pkg.actual_arrival = sent_on
            pkg.deadline = parsed.pickup_before  # read, never calculated
            if point is not None:
                pkg.pickup_point = point  # where to actually go, authoritative
            if parsed.pickup_code:
                pkg.pickup_code = parsed.pickup_code
            if parsed.barcode_url:
                pkg.barcode_url = parsed.barcode_url
            _fill(pkg, parsed)
            pkg.save()
        note = ("" if len(matches) == 1
                else f"Recogida consolidada: {len(matches)} paquetes actualizados")
        return matches[0], note

    if kind == EmailKind.PICKED_UP:
        picked_day = parsed.picked_up_on or sent_on
        # One scan at the counter/locker hands over *everything* waiting there
        # — the terminal releases every available package at once. And the
        # email is unreliable about what it covers: "Se han recogido 4
        # productos" names a single order. So a pickup confirms the named
        # order(s) *and* sweeps every package still awaiting at that same point.
        targets = [p for p in matches if p.state != Package.State.RETURNED]
        matched_pks = {p.pk for p in targets}
        if point is not None:
            swept = (Package.objects
                     .filter(pickup_point=point, state=Package.State.AWAITING_PICKUP)
                     .exclude(pk__in=matched_pks))
            targets.extend(swept)
        if not targets:
            if point is None:
                return None, "Recogido sin paquete conocido ni punto Amazon: ignorado"
            # Never seen this package and nothing was waiting: keep a lone
            # picked row so the pickup still shows on the calendar.
            pkg = Package(
                pickup_point=point,
                order_id=parsed.order_id or "",
                state=Package.State.PICKED_UP,
                picked_up_on=picked_day,
            )
            _fill(pkg, parsed)
            pkg.save()
            return pkg, ""
        for pkg in targets:
            pkg.state = Package.State.PICKED_UP
            pkg.picked_up_on = picked_day
            _fill(pkg, parsed)
            pkg.save()
        extra = len(targets) - len(matched_pks)
        note = "" if extra <= 0 else f"Recogida en bloque: +{extra} paquete(s) del mismo punto"
        return targets[0], note

    if kind == EmailKind.DELIVERED:
        # Home delivery reaching its destination: terminal, no pickup trip.
        delivered_day = sent_on  # "Entregado hoy" ≈ the email's send day
        if not matches:
            if point is None:
                return None, "Entregado sin pedido conocido ni destino: ignorado"
            # Default IN_TRANSIT state so the transition below actually runs.
            matches = [Package(pickup_point=point, order_id=parsed.order_id or "")]
        for pkg in matches:
            if _RANK[pkg.state] >= _RANK[Package.State.DELIVERED]:
                continue  # already terminal; a re-forward must not reopen it
            pkg.state = Package.State.DELIVERED
            pkg.actual_arrival = delivered_day
            if point is not None:
                pkg.pickup_point = point
            _fill(pkg, parsed)
            pkg.save()
        return matches[0], ""

    if kind == EmailKind.NO_LONGER_AVAILABLE:
        # The misleading one: the package usually is still there. Record only.
        pkg = matches[0] if matches else None
        return pkg, "Aviso de devolución registrado; sin cambios (correo engañoso)"

    if kind == EmailKind.PICKUP_REMINDER:
        # A nag that a package is still waiting: the deadline and everything
        # else came from the original "listo para recogida". Record only —
        # re-dating or re-opening from a reminder would be wrong.
        pkg = matches[0] if matches else None
        return pkg, "Recordatorio de recogida: sin cambios"

    if kind == EmailKind.REVIEW_PUBLISHED:
        return None, "Reseña publicada: sin acción de calendario"

    return None, f"Sin regla para {kind.value}"  # unreachable; belt and braces


def process_message(raw):
    """One raw RFC822 message (bytes) through the whole pipeline.

    Returns (RawEmail, created): created=False means the Message-ID was
    already in the database and nothing was touched.
    """
    msg = message_from_bytes(raw, policy=policy.default)
    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id:
        message_id = "sha256:" + hashlib.sha256(raw).hexdigest()
    existing = RawEmail.objects.filter(message_id=message_id).first()
    if existing is not None:
        return existing, False

    received_at = None
    if msg.get("Date"):
        try:
            received_at = parsedate_to_datetime(msg["Date"])
        except ValueError:
            pass
        if received_at is not None and timezone.is_naive(received_at):
            received_at = timezone.make_aware(received_at)
    record = RawEmail.objects.create(  # stored before parsing, always
        message_id=message_id,
        subject=(msg.get("Subject") or "")[:255],
        received_at=received_at,
        raw=raw.decode("utf-8", "replace"),
    )
    try:
        parsed = parse_email(raw)
        record.kind = parsed.kind.value
        if parsed.sent_at:
            record.received_at = timezone.make_aware(
                parsed.sent_at, timezone.get_default_timezone()
            )
        with transaction.atomic():
            package, note = _apply(parsed)
        record.package = package
        record.note = note
        record.processed = True
    except ParseError as exc:
        record.parse_error = str(exc)
    except Exception as exc:  # a crash is still never a dropped email
        record.parse_error = f"{type(exc).__name__}: {exc}"
    record.save()
    return record, True


def _default_connection():
    # A 30 s socket timeout so a stuck mailbox raises instead of hanging the
    # worker silently — a hang would look like "colgado" in the logs.
    return imaplib.IMAP4_SSL(settings.GMAIL_IMAP_HOST, timeout=30)


def _trash(conn, uid, record, stats):
    """Move a processed email to Gmail's Trash via the locale-independent
    X-GM-LABELS extension (adding \\Trash removes it from the inbox). Wrapped
    so a mailbox hiccup logs and continues — trashing must never lose data,
    and the raw email is already saved either way."""
    try:
        status, _ = conn.uid("STORE", uid, "+X-GM-LABELS", "\\Trash")
    except Exception:
        logger.exception("No se pudo mover a la papelera: %r", record.subject)
        return
    if status == "OK":
        stats["trashed"] += 1
        logger.info("Papelera ← %r", record.subject)
    else:
        logger.warning("Papelera rechazada (%s): %r", status, record.subject)


def scan_inbox(connection_factory=_default_connection):
    """Sweep the whole INBOX once and ingest anything new.

    New mail is detected by Message-ID against the database, never by IMAP
    flags. With GMAIL_TRASH_PROCESSED on, processed emails are moved to Trash
    (failures stay put); otherwise the mailbox is opened read-only and left
    untouched. Every action is logged with a timestamp.
    """
    if not (settings.GMAIL_IMAP_USER and settings.GMAIL_IMAP_APP_PASSWORD):
        raise RuntimeError("GMAIL_IMAP_USER / GMAIL_IMAP_APP_PASSWORD are not set.")

    trash = settings.GMAIL_TRASH_PROCESSED
    stats = {"messages": 0, "new": 0, "failed": 0, "trashed": 0}
    with connection_factory() as conn:
        conn.login(settings.GMAIL_IMAP_USER, settings.GMAIL_IMAP_APP_PASSWORD)
        status, _ = conn.select("INBOX", readonly=not trash)
        if status != "OK":
            raise RuntimeError("Could not open INBOX")
        status, data = conn.uid("search", None, "ALL")
        if status != "OK":
            raise RuntimeError("UID SEARCH failed")
        uids = data[0].split()
        logger.info(
            "INBOX abierta (%s): %d mensaje(s)",
            "lectura/escritura, papelera activa" if trash else "solo lectura",
            len(uids),
        )
        for uid in uids:
            # PEEK so reading a message never flips its \Seen flag; the only
            # deliberate mailbox change is the Trash move below.
            status, msg_data = conn.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or msg_data[0] is None:
                logger.warning("UID %s: fetch falló, se omite", uid.decode())
                stats["failed"] += 1
                continue
            stats["messages"] += 1
            record, created = process_message(msg_data[0][1])
            when = timezone.localtime(record.received_at).strftime("%d %b %H:%M") \
                if record.received_at else "fecha ?"
            if created and record.parse_error:
                stats["failed"] += 1
                logger.warning(
                    "SIN PARSEAR [%s] %r → %s (se deja en la bandeja)",
                    when, record.subject[:70], record.parse_error,
                )
            elif created:
                stats["new"] += 1
                detail = f" · {record.note}" if record.note else ""
                logger.info(
                    "PROCESADO [%s] %r → %s%s",
                    when, record.subject[:70], record.kind or "?", detail,
                )
            else:
                logger.debug("Ya en base de datos: %r", record.subject[:70])
            if trash and record.processed:
                _trash(conn, uid, record, stats)
    logger.info(
        "Escaneo completado: %d en bandeja, %d nuevos, %d sin parsear, %d a papelera",
        len(uids), stats["new"], stats["failed"], stats["trashed"],
    )
    return stats
