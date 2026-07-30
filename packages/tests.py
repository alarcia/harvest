"""Parser regression net: every fixture is a real Amazon.es email.

The .eml files under tests/fixtures/ are the archetypes of every known
communication, dumped read-only from the dedicated inbox (`imap_dump`). The
day Amazon changes a template, these tests are what says which extraction
broke — keep one fixture per template, and add one whenever a new template
shows up.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from django.db import IntegrityError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from reviews.models import Review

from .ingest import (
    MANUAL_SCAN_TIMEOUT,
    _sync_review_for_vine,
    backfill_reviews,
    process_message,
    reprocess_failures,
    scan_inbox,
    scan_now,
)
from .models import Config, Package, PickupPoint, RawEmail
from .parser import EmailKind, ParseError, _resolve_date, parse_email
from .views import _estimate_line, _estimate_note

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_bytes()


_PEPE_SIGNATURE = ("Juguetes Pepe y Dalda // c/Regència d'Urgell, 17 "
                   "// La Seu d'Urgell")


def _pepe_email(subject, body_line, *, forwarded=True,
                to="Javier Alarcia <jabogood@gmail.com>",
                signature=_PEPE_SIGNATURE):
    """A Pepe y Dalda notice, shaped like the real one (fixture 142).

    Only one real sample exists so far — a letter, hand-forwarded, addressed
    with "para ti" — so the variations the user says are coming (parcels,
    ones for his wife) and the automatic-forward shape are synthesized from
    that template rather than guessed at when they arrive. `forwarded` draws
    the Gmail quote block a hand-forward carries; without it the message is
    what the automatic forwarding delivers: original headers, no block.
    """
    msg = EmailMessage()
    msg["Subject"] = f"Fwd: {subject}" if forwarded else subject
    msg["Message-ID"] = f"<pepe-{abs(hash((subject, body_line, to)))}@example.com>"
    msg["To"] = "Viner Harvest <viner2552@gmail.com>" if forwarded else to
    # The forward's own date: two days after the shop sent it, which is
    # exactly why the block below has to win over this header.
    msg["Date"] = ("Sat, 25 Jul 2026 15:35:08 +0200" if forwarded
                   else "Thu, 23 Jul 2026 17:46:00 +0200")
    block = (
        "<div>---------- Forwarded message ---------</div>"
        "<div class='gmail_attr'>De: <strong>Pepe y Dalda</strong> "
        "&lt;pepeydalda@gmail.com&gt;<br>"
        "Date: jue, 23 jul 2026 a las 17:46<br>"
        f"Subject: {subject}<br>"
        f"To: {to.replace('<', '&lt;').replace('>', '&gt;')}<br></div>"
    ) if forwarded else ""
    msg.set_content(
        f"<div>{block}<div><div>{body_line}</div>"
        "<div>(Os recordamos el PAGO EN EFECTIVO al recoger los paquetes)</div>"
        f"<div><b>{signature.split('//')[0].strip()}</b>"
        f"<font> // {' // '.join(signature.split('//')[1:]).strip()}</font></div>"
        "<div>Horario:</div><div>Lunes cerrado.</div></div></div>",
        subtype="html",
    )
    return msg.as_bytes()


class FakeIMAP:
    """Enough of imaplib.IMAP4_SSL to drive scan_inbox in tests. Records
    STORE calls so a test can assert exactly which UIDs were trashed."""

    def __init__(self, messages):
        self.messages = dict(messages)  # {uid:int -> raw:bytes}
        self.stored = []  # [(uid:int, item:str, value:str)]
        self.readonly = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        return ("OK", [b""])

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        return ("OK", [str(len(self.messages)).encode()])

    def uid(self, command, *args):
        cmd = command.upper()
        if cmd == "SEARCH":
            return ("OK", [b" ".join(str(u).encode() for u in self.messages)])
        if cmd == "FETCH":
            uid = int(args[0])
            return ("OK", [(b"1 (BODY[] {})", self.messages[uid])])
        if cmd == "STORE":
            self.stored.append((int(args[0]), args[1], args[2]))
            return ("OK", [b""])
        return ("OK", [b""])


class ParseEmailTests(SimpleTestCase):
    def test_ordered(self):
        parsed = parse_email(fixture("006-fwd-pedido-cargador-inalambrico.eml"))
        self.assertEqual(parsed.kind, EmailKind.ORDERED)
        self.assertEqual(parsed.order_id, "403-0477954-5913111")
        self.assertEqual(parsed.sent_at.date(), date(2026, 7, 1))
        # "Llega el lunes", sent Wednesday July 1st -> Monday July 6th.
        self.assertEqual(parsed.estimated_arrival, date(2026, 7, 6))
        self.assertEqual(parsed.total, Decimal("0.00"))
        self.assertEqual(parsed.asin, "B0GXK1FPTY")
        self.assertTrue(
            parsed.item_title.startswith("Cargador Inalámbrico Magnético 25W")
        )
        self.assertTrue(
            parsed.pickup_location.startswith("Amazon Counter - Les Mesures")
        )
        self.assertTrue(parsed.subject.startswith("Pedido:"))  # "Fwd: " stripped

    def test_shipped(self):
        parsed = parse_email(fixture("007-fwd-enviado-cargador-inalambrico.eml"))
        self.assertEqual(parsed.kind, EmailKind.SHIPPED)
        self.assertEqual(parsed.order_id, "403-0477954-5913111")
        self.assertEqual(parsed.shipment_id, "TnzBz0Vk4")
        self.assertEqual(parsed.sent_at.date(), date(2026, 7, 2))
        self.assertEqual(parsed.estimated_arrival, date(2026, 7, 6))
        self.assertEqual(parsed.total, Decimal("0.00"))

    def test_ready_for_pickup(self):
        parsed = parse_email(
            fixture("008-fwd-paquete-listo-para-recogida-recoger-en-amazon-counter-le.eml")
        )
        self.assertEqual(parsed.kind, EmailKind.READY_FOR_PICKUP)
        self.assertEqual(parsed.order_id, "403-0477954-5913111")
        self.assertEqual(parsed.shipment_id, "TnzBz0Vk4")
        self.assertEqual(parsed.sent_at.date(), date(2026, 7, 6))
        # "Recoge antes del 13 de julio" -> the literal "antes del" day; the
        # calendar derives the last safe day (the 12th) itself.
        self.assertEqual(parsed.pickup_before, date(2026, 7, 13))
        self.assertEqual(parsed.pickup_code, "376126")
        self.assertIn("Les Mesures", parsed.pickup_location)
        self.assertTrue(
            parsed.barcode_url.startswith(
                "https://m.media-amazon.com/images/G/01/barcodes/"
            )
        )

    def test_picked_up(self):
        parsed = parse_email(
            fixture("009-fwd-se-ha-recogido-cargador-inalambrico-magnetico-25w-con-us.eml")
        )
        self.assertEqual(parsed.kind, EmailKind.PICKED_UP)
        self.assertEqual(parsed.order_id, "403-0477954-5913111")
        # "Recogido hoy", sent July 8th.
        self.assertEqual(parsed.picked_up_on, date(2026, 7, 8))

    def test_review_published(self):
        parsed = parse_email(
            fixture("010-fwd-gracias-por-su-resena-de-cargador-inalambrico-mag-en-ama.eml")
        )
        self.assertEqual(parsed.kind, EmailKind.REVIEW_PUBLISHED)
        self.assertEqual(parsed.asin, "B0GXK1FPTY")
        self.assertEqual(parsed.review_id, "R1IUNF3PY66WHI")

    def test_no_longer_available(self):
        parsed = parse_email(
            fixture("011-fwd-ya-no-esta-disponible-para-su-recogida-lvjkes-bolso-band.eml")
        )
        # The misleading one: parsed and recognized, but ingestion must never
        # auto-expire or mark returned on it (the package is usually still there).
        self.assertEqual(parsed.kind, EmailKind.NO_LONGER_AVAILABLE)
        self.assertEqual(parsed.order_id, "403-2373187-4267548")
        self.assertEqual(parsed.shipment_id, "TnVb0WV5H")

    def test_out_for_delivery(self):
        parsed = parse_email(
            fixture("012-fwd-llega-hoy-necesitas-una-contrasena-temporal-para-tu-entr.eml")
        )
        self.assertEqual(parsed.kind, EmailKind.OUT_FOR_DELIVERY)
        self.assertEqual(parsed.order_id, "403-4159988-6701146")
        # "Llega hoy", sent May 12th.
        self.assertEqual(parsed.estimated_arrival, date(2026, 5, 12))
        self.assertEqual(parsed.temp_password, "273030")
        # A home delivery, not a pickup point — the destination line still
        # lands in pickup_location; whether it becomes a row is ingestion's call.
        self.assertIsNotNone(parsed.pickup_location)
        self.assertFalse(parsed.pickup_location.startswith("Amazon"))

    def test_ordered_paid_to_locker(self):
        # A non-Vine purchase — and its email *still* prints "Total 0.00€"
        # (settled with gift balance?), so the total alone can't prove Vine.
        parsed = parse_email(fixture("016-fwd-pedido-intex-64761-colchon.eml"))
        self.assertEqual(parsed.kind, EmailKind.ORDERED)
        self.assertEqual(parsed.order_id, "408-3509044-1782749")
        self.assertEqual(parsed.sent_at.date(), date(2026, 6, 20))
        # "Llega mañana", sent June 20th.
        self.assertEqual(parsed.estimated_arrival, date(2026, 6, 21))
        self.assertEqual(parsed.total, Decimal("0.00"))
        self.assertTrue(
            parsed.pickup_location.startswith("Amazon Locker - plato")
        )
        self.assertEqual(len(parsed.items), 1)
        self.assertEqual(parsed.asin, "B07XQNPKPB")
        self.assertEqual(
            parsed.image_url,
            "https://m.media-amazon.com/images/I/61LHU0-P3OL._SS90_.jpg",
        )

    def test_ordered_arrival_window(self):
        # Newer Pedido template gives a delivery window instead of a single
        # day: "Llegada entre el 24 de julio y el 28 de julio". The start is
        # the estimate proper; the end rides alongside it so the card can word
        # the window instead of pretending the first day was a promise.
        parsed = parse_email(
            fixture("023-fwd-pedido-veebmys-correa-movil-llegada-entre-fechas.eml")
        )
        self.assertEqual(parsed.kind, EmailKind.ORDERED)
        self.assertEqual(parsed.order_id, "404-4372144-5150738")
        self.assertEqual(parsed.sent_at.date(), date(2026, 7, 20))
        self.assertEqual(parsed.estimated_arrival, date(2026, 7, 24))
        self.assertEqual(parsed.estimated_arrival_end, date(2026, 7, 28))
        self.assertTrue(
            parsed.pickup_location.startswith("Amazon Counter - Les Mesures")
        )

    def test_single_day_arrival_has_no_window(self):
        # The common template names one day: nothing to word as a range, so
        # the end stays None rather than echoing the start.
        parsed = parse_email(fixture("006-fwd-pedido-cargador-inalambrico.eml"))
        self.assertEqual(parsed.estimated_arrival, date(2026, 7, 6))
        self.assertIsNone(parsed.estimated_arrival_end)

    def test_arrival_window_end_resolves_against_its_start(self):
        # A window may cross a month — or the new year. Resolving its end
        # against the email's send time would put "3 de enero" back in the
        # year the window began; anchoring it to the start can't.
        sent = datetime(2026, 12, 28, 9, 0)
        self.assertEqual(_resolve_date("30 de diciembre", sent), date(2026, 12, 30))
        self.assertEqual(
            _resolve_date("3 de enero", date(2026, 12, 30)), date(2027, 1, 3)
        )

    def test_ready_for_pickup_locker_consolidated(self):
        # One locker slot, two items, and two order numbers: the body says
        # "Pedido n.º 407-..." while the links reference 404-....
        parsed = parse_email(
            fixture("017-fwd-paquete-listo-para-recogida-recoger-en-amazon-locker-ceb.eml")
        )
        self.assertEqual(parsed.kind, EmailKind.READY_FOR_PICKUP)
        self.assertEqual(parsed.order_id, "407-2753653-0825928")
        self.assertEqual(
            parsed.order_ids,
            frozenset({"407-2753653-0825928", "404-1931433-1428321"}),
        )
        self.assertEqual(parsed.shipment_id, "TnnxzlRZ9")
        self.assertEqual(parsed.pickup_before, date(2026, 7, 16))
        self.assertEqual(parsed.pickup_code, "488940")
        self.assertTrue(
            parsed.pickup_location.startswith("Amazon Locker - cebolla")
        )
        self.assertEqual(
            [item.title[:14] for item in parsed.items],
            ["Bonsenkitchen ", "XOKUWU Funda c"],
        )
        self.assertTrue(
            parsed.barcode_url.startswith(
                "https://m.media-amazon.com/images/G/01/barcodes/"
            )
        )

    def test_shipped_paid_order_reveals_real_price(self):
        # The colchón's *Enviado* email is the first to print the real amount
        # (its Pedido, fixture 016, said 0.00€) — this is what refutes Vine.
        parsed = parse_email(fixture("019-fwd-enviado-intex-64761-colchon.eml"))
        self.assertEqual(parsed.kind, EmailKind.SHIPPED)
        self.assertEqual(parsed.order_id, "408-3509044-1782749")
        self.assertEqual(parsed.total, Decimal("19.98"))
        self.assertEqual(parsed.shipment_id, "TgvslGX9H")

    def test_shipped_vine_with_eu_import_surcharge(self):
        # A Vine item from a seller outside the EU: the Pedido printed 0.00€,
        # the Enviado prints the bare import surcharge (3.63€) — still free
        # in the sense that matters, still owes a review. See Config.means_vine.
        parsed = parse_email(
            fixture("106-enviado-ones-funda-magnetica-para-galaxy-s26-recargo-ue.eml"))
        self.assertEqual(parsed.kind, EmailKind.SHIPPED)
        self.assertEqual(parsed.order_id, "404-2171566-7826720")
        self.assertEqual(parsed.total, Decimal("3.63"))

    def test_picked_up_multi_product(self):
        # "Se han recogido 4 productos": same body headline as the single
        # case, but names only one order though four were handed over.
        parsed = parse_email(fixture("018-fwd-se-han-recogido-4-productos.eml"))
        self.assertEqual(parsed.kind, EmailKind.PICKED_UP)
        self.assertEqual(parsed.order_id, "404-5168905-2457920")
        self.assertEqual(parsed.picked_up_on, date(2026, 7, 4))
        self.assertIn("Les Mesures", parsed.pickup_location)

    def test_shipped_home_delivery(self):
        # Real auto-forwarded email (no "Fwd:"): shipped to a home address,
        # not an Amazon pickup point.
        parsed = parse_email(fixture("020-enviado-kalvica-11-pares-pendientes.eml"))
        self.assertEqual(parsed.kind, EmailKind.SHIPPED)
        self.assertEqual(parsed.order_id, "404-5385257-6763515")
        # Destination is a home address, not an Amazon pickup point.
        self.assertIsNotNone(parsed.pickup_location)
        self.assertFalse(parsed.pickup_location.startswith("Amazon"))
        # "Llega el viernes", sent 2026-07-13 (Monday) -> Friday the 17th.
        self.assertEqual(parsed.estimated_arrival, date(2026, 7, 17))

    def test_delivered_home(self):
        # Real "Entregado" email: "¡Tu paquete se ha entregado!" to a home
        # address — the terminal state for home deliveries. Confirms the
        # DELIVERED headline that was previously only inferred.
        parsed = parse_email(
            fixture("021-fwd-entregado-1-producto-n-o-de-pedido-404-7963783-4668345.eml")
        )
        self.assertEqual(parsed.kind, EmailKind.DELIVERED)
        self.assertEqual(parsed.order_id, "404-7963783-4668345")
        self.assertEqual(parsed.sent_at.date(), date(2026, 7, 13))  # "Entregado hoy"
        # Delivered to a home address, not an Amazon pickup point.
        self.assertIsNotNone(parsed.pickup_location)
        self.assertFalse(parsed.pickup_location.startswith("Amazon"))

    def test_pickup_reminder(self):
        # "Recordatorio: Paquete en espera de recogida" — a nag that a package
        # is still waiting. Recognized as its own kind so it never trips the
        # unknown-email alarm, and ingestion drives no transition from it.
        parsed = parse_email(
            fixture("022-recordatorio-paquete-en-espera-de-recogida.eml")
        )
        self.assertEqual(parsed.kind, EmailKind.PICKUP_REMINDER)
        self.assertEqual(parsed.order_id, "407-2753653-0825928")

    def test_delivery_attempt_ups(self):
        # "Intento de entrega" — UPS couldn't hand a home delivery over and
        # held it at its own office. The email carries the destination and
        # order/shipment ids but, unlike READY_FOR_PICKUP, neither a deadline
        # nor the office itself — only Amazon's own tracking link (real UPS
        # tracking number is nowhere in the email, confirmed against the raw
        # source; the user reads it off Amazon's own order page by hand).
        parsed = parse_email(
            fixture("107-intento-de-entrega-ones-funda-magnetica-para.eml")
        )
        self.assertEqual(parsed.kind, EmailKind.DELIVERY_ATTEMPT)
        self.assertEqual(parsed.order_id, "404-2171566-7826720")
        self.assertEqual(parsed.shipment_id, "TCl8Nkz09")
        self.assertEqual(parsed.sent_at.date(), date(2026, 7, 23))
        self.assertIsNone(parsed.pickup_before)
        self.assertIsNone(parsed.pickup_code)
        self.assertFalse(parsed.pickup_location.startswith("Amazon"))
        self.assertTrue(parsed.item_title.startswith("ONES Funda Magnética"))
        self.assertEqual(parsed.asin, "B0GL7ZD86T")

    def test_delivery_attempt_other_carrier_stays_unrecognized(self):
        # Only UPS is handled for now (user's explicit ask, 2026-07-23): a
        # different carrier's equivalent notice must keep tripping the
        # unrecognized-email banner rather than being guessed at.
        msg = EmailMessage()
        msg["Subject"] = 'Intento de entrega: "Otro producto..."'
        msg["Message-ID"] = "<seur-attempt@example.com>"
        msg.set_content(
            "<p>Se ha intentado realizar tu entrega</p>"
            "<p>Lamentablemente, SEUR no ha podido realizar la entrega y te "
            "la ha dejado en su oficina para que la recojas.</p>",
            subtype="html",
        )
        with self.assertRaisesMessage(ParseError, "Unrecognized email type"):
            parse_email(msg.as_bytes())

    def test_store_reception_letter(self):
        # Pepe y Dalda, the toy shop that doubles as a delivery address: one
        # email, sent when the thing is already on the counter. No order id,
        # no product, no deadline — what it does carry is parcel-or-letter,
        # who to ask for, and the day, which is the whole calendar entry.
        parsed = parse_email(fixture("142-fwd-recepcion-carta.eml"))
        self.assertEqual(parsed.kind, EmailKind.STORE_RECEPTION)
        self.assertEqual(parsed.item_kind, "letter")
        self.assertEqual(parsed.item_count, 1)
        self.assertEqual(parsed.recipient, "Javier")
        self.assertIn("Pepe y Dalda", parsed.pickup_location)
        self.assertIn("Regència d'Urgell", parsed.pickup_location)
        self.assertIsNone(parsed.order_id)
        self.assertIsNone(parsed.pickup_before)
        # The shop embeds no tracking token, so the send time is recovered
        # from the forwarding block — the Date header is the forward's own,
        # two days late, and would file the whole entry on the wrong day.
        self.assertEqual(parsed.sent_at.date(), date(2026, 7, 23))

    def test_store_reception_parcel_for_someone_else(self):
        # No fixture yet — every real one so far is a letter addressed with
        # "para ti" (user, 2026-07-25: parcels and ones for his wife are
        # coming). Synthesized from the real template's wording so the two
        # unseen variations are covered before they land: the plural count,
        # and a recipient the email names outright instead of "ti".
        parsed = parse_email(_pepe_email(
            "Recepción de paquete",
            "Hola: Hemos recibido 2 paquetes para Marina.",
        ))
        self.assertEqual(parsed.kind, EmailKind.STORE_RECEPTION)
        self.assertEqual(parsed.item_kind, "package")
        self.assertEqual(parsed.item_count, 2)
        self.assertEqual(parsed.recipient, "Marina")

    def test_store_reception_falls_back_to_the_addressee(self):
        # "para ti" names nobody, so the person to ask for at the counter is
        # whoever the shop emailed — read off the To: header when there's no
        # forwarding block to read it from (i.e. an automatic forward).
        parsed = parse_email(_pepe_email(
            "Recepción carta",
            "Hola: Hemos recibido 1 carta para ti.",
            forwarded=False,
            to="Marina Alarcia <marina@example.com>",
        ))
        self.assertEqual(parsed.recipient, "Marina")
        self.assertEqual(parsed.sent_at.date(), date(2026, 7, 23))

    def test_store_reception_without_a_name_leaves_the_recipient_empty(self):
        # A bare address is not a name: better an empty field (editable, and
        # the card simply drops the row) than asking for "Jabogood" at the
        # counter.
        parsed = parse_email(_pepe_email(
            "Recepción carta",
            "Hola: Hemos recibido 1 carta para ti.",
            forwarded=False,
            to="jabogood@example.com",
        ))
        self.assertIsNone(parsed.recipient)
        self.assertEqual(parsed.item_kind, "letter")

    def test_reception_lookalike_from_another_sender_fails_loudly(self):
        # The reception phrase alone doesn't prove it's that shop. Another
        # store wording it the same way must trip the banner rather than be
        # filed under Pepe y Dalda — same one-sender-at-a-time rule as the
        # UPS delivery attempt.
        with self.assertRaisesMessage(ParseError, "missing") as ctx:
            parse_email(_pepe_email(
                "Recepción de paquete",
                "Hola: Hemos recibido 1 paquete para ti.",
                forwarded=False,
                signature="Papelería Vilaró // c/Mayor, 3",
            ))
        self.assertIn("pickup_location", str(ctx.exception))

    def test_unknown_template_fails_loudly(self):
        msg = EmailMessage()
        msg["Subject"] = "Oferta especial solo hoy"
        msg["Message-ID"] = "<junk@example.com>"
        msg.set_content("Grandes descuentos", subtype="html")
        with self.assertRaisesMessage(ParseError, "Unrecognized email type"):
            parse_email(msg.as_bytes())

    def test_recognized_template_missing_fields_fails_loudly(self):
        # A ready-for-pickup where Amazon moved the deadline and code out of
        # reach: the parse must fail naming the gaps, not half-succeed.
        msg = EmailMessage()
        msg["Subject"] = "Paquete listo para recogida"
        msg["Message-ID"] = "<incomplete@example.com>"
        msg["Date"] = "Mon, 6 Jul 2026 10:47:22 +0200"
        msg.set_content(
            "<p>El paquete está listo para su recogida</p>", subtype="html"
        )
        with self.assertRaisesMessage(ParseError, "missing") as ctx:
            parse_email(msg.as_bytes())
        self.assertIn("pickup_before", str(ctx.exception))
        self.assertIn("pickup_code", str(ctx.exception))


class IngestTests(TestCase):
    """Fixture bytes in, database rows out — no IMAP involved."""

    def test_full_lifecycle_collapses_into_one_package(self):
        for name in (
            "006-fwd-pedido-cargador-inalambrico.eml",
            "007-fwd-enviado-cargador-inalambrico.eml",
            "008-fwd-paquete-listo-para-recogida-recoger-en-amazon-counter-le.eml",
            "009-fwd-se-ha-recogido-cargador-inalambrico-magnetico-25w-con-us.eml",
        ):
            record, created = process_message(fixture(name))
            self.assertTrue(created)
            self.assertTrue(record.processed, record.parse_error)

        self.assertEqual(Package.objects.count(), 1)
        pkg = Package.objects.get()
        self.assertEqual(pkg.state, Package.State.PICKED_UP)
        self.assertEqual(pkg.order_id, "403-0477954-5913111")
        self.assertEqual(pkg.shipment_id, "TnzBz0Vk4")
        self.assertEqual(pkg.ordered_on, date(2026, 7, 1))
        self.assertEqual(pkg.shipped_on, date(2026, 7, 2))
        self.assertEqual(pkg.actual_arrival, date(2026, 7, 6))
        self.assertEqual(pkg.deadline, date(2026, 7, 13))
        self.assertEqual(pkg.picked_up_on, date(2026, 7, 8))
        self.assertEqual(pkg.pickup_code, "376126")
        self.assertTrue(pkg.is_vine)
        self.assertTrue(pkg.description.startswith("Cargador Inalámbrico"))
        self.assertTrue(pkg.image_url.startswith("https://m.media-amazon.com/"))
        self.assertTrue(pkg.barcode_url.startswith("https://m.media-amazon.com/"))
        # The "Se ha recogido" email is treated as final truth: the pickup
        # got confirmed without any manual step.
        self.assertEqual(pkg.pickup_point.kind, PickupPoint.Kind.AMAZON_COUNTER)

    def test_ordered_window_stores_both_ends(self):
        record, _ = process_message(
            fixture("023-fwd-pedido-veebmys-correa-movil-llegada-entre-fechas.eml")
        )
        self.assertTrue(record.processed, record.parse_error)
        pkg = Package.objects.get()
        self.assertEqual(pkg.estimated_arrival, date(2026, 7, 24))
        self.assertEqual(pkg.estimated_arrival_end, date(2026, 7, 28))

    def test_shipping_notice_clears_a_delivery_window(self):
        # A "Pedido" that gave a window, then the "Enviado", which always
        # names one firm day. The window has to go with it: left behind, the
        # card would keep offering a range the newer date contradicts.
        point = PickupPoint.objects.create(
            name="Amazon Counter - Les Mesures", kind=PickupPoint.Kind.AMAZON_COUNTER
        )
        Package.objects.create(
            pickup_point=point, order_id="407-2023163-0562738",
            state=Package.State.IN_TRANSIT, description="EHEYCIGA Escalera",
            estimated_arrival=date(2026, 7, 16),
            estimated_arrival_end=date(2026, 7, 20),
        )
        record, _ = process_message(
            fixture("046-enviado-eheyciga-escalera-perros-4.eml")
        )
        self.assertTrue(record.processed, record.parse_error)
        pkg = Package.objects.get()
        self.assertEqual(pkg.estimated_arrival, date(2026, 7, 18))
        self.assertIsNone(pkg.estimated_arrival_end)

    def test_idempotent_by_message_id(self):
        raw = fixture("006-fwd-pedido-cargador-inalambrico.eml")
        _, first = process_message(raw)
        _, second = process_message(raw)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(Package.objects.count(), 1)
        self.assertEqual(RawEmail.objects.count(), 1)

    def test_ready_alone_creates_awaiting_package(self):
        # The Locker Cebolla case: the first email the app ever sees for
        # this package is already the pickup notice.
        record, _ = process_message(
            fixture("017-fwd-paquete-listo-para-recogida-recoger-en-amazon-locker-ceb.eml")
        )
        self.assertTrue(record.processed, record.parse_error)
        pkg = Package.objects.get()
        self.assertEqual(pkg.state, Package.State.AWAITING_PICKUP)
        self.assertEqual(pkg.deadline, date(2026, 7, 16))
        self.assertEqual(pkg.pickup_code, "488940")
        self.assertEqual(pkg.pickup_point.kind, PickupPoint.Kind.AMAZON_LOCKER)
        self.assertIn("Bonsenkitchen", pkg.description)
        self.assertIn("XOKUWU", pkg.description)  # both bundled items named

    def test_store_reception_creates_an_awaiting_package(self):
        # Pepe y Dalda's whole lifecycle in one email: it's already on the
        # counter, so the row starts (and stays) in awaiting_pickup, with no
        # deadline — the shop just holds it, charging a little more as the
        # days pass, which Harvest deliberately doesn't model.
        record, _ = process_message(fixture("142-fwd-recepcion-carta.eml"))
        self.assertTrue(record.processed, record.parse_error)
        pkg = Package.objects.get()
        self.assertEqual(pkg.state, Package.State.AWAITING_PICKUP)
        self.assertEqual(pkg.pickup_point.kind, PickupPoint.Kind.PEPE_Y_DALDA)
        self.assertEqual(pkg.actual_arrival, date(2026, 7, 23))
        self.assertIsNone(pkg.deadline)
        self.assertEqual(pkg.item_kind, Package.ItemKind.LETTER)
        self.assertEqual(pkg.recipient, "Javier")
        self.assertEqual(pkg.description, "Carta para Javier")
        # No order number to build one from, so no Amazon link on the card.
        self.assertEqual(pkg.amazon_tracking_url, "")

    def test_store_receptions_are_separate_packages_at_one_point(self):
        # Nothing in these emails identifies a delivery, so each notice is
        # its own row — one thing to go and fetch — but they all share the
        # single shop row, which dedups by kind and survives a reworded
        # signature.
        process_message(fixture("142-fwd-recepcion-carta.eml"))
        process_message(_pepe_email(
            "Recepción de paquete",
            "Hola: Hemos recibido 2 paquetes para Marina.",
            signature="Pepe y Dalda // Regència d'Urgell 17",
        ))
        self.assertEqual(Package.objects.count(), 2)
        point = PickupPoint.objects.get(kind=PickupPoint.Kind.PEPE_Y_DALDA)
        self.assertEqual(point.packages.count(), 2)
        parcel = Package.objects.get(item_kind=Package.ItemKind.PACKAGE)
        self.assertEqual(parcel.description, "2 paquetes para Marina")
        self.assertEqual(parcel.recipient, "Marina")

    def test_delivery_attempt_transitions_existing_home_package(self):
        # Real 2026-07-23 case: a home delivery already tracked in_transit
        # (its Pedido/Enviado already seen) that UPS couldn't hand over —
        # same package, now awaiting pickup at UPS's office instead of
        # landing at home.
        home = PickupPoint.objects.create(
            name="Rosa - Can Salgot (lliça D'amunt), Barcelona",
            kind=PickupPoint.Kind.HOME,
        )
        pkg = Package.objects.create(
            pickup_point=home, order_id="404-2171566-7826720",
            shipment_id="TCl8Nkz09", description="ONES Funda Magnética",
            state=Package.State.IN_TRANSIT,
            estimated_arrival=date(2026, 8, 5),
        )
        record, _ = process_message(
            fixture("107-intento-de-entrega-ones-funda-magnetica-para.eml")
        )
        self.assertTrue(record.processed, record.parse_error)
        self.assertEqual(Package.objects.count(), 1)  # same package
        pkg.refresh_from_db()
        self.assertEqual(pkg.state, Package.State.AWAITING_PICKUP)
        self.assertEqual(pkg.actual_arrival, date(2026, 7, 23))
        self.assertEqual(pkg.carrier, "UPS")
        self.assertEqual(pkg.carrier_tracking_number, "")  # not in the email
        self.assertEqual(pkg.carrier_tracking_url, "")  # blank until filled in
        self.assertEqual(pkg.pickup_point.kind, PickupPoint.Kind.CARRIER)
        self.assertEqual(pkg.pickup_point.name, "UPS")
        self.assertIsNone(pkg.deadline)  # never provided; never guessed
        # Amazon's own order-tracking page — simplified from the email's
        # click-tracking redirect, built from the order/shipment ids alone —
        # is the only way left to check status, so it's always offered here.
        self.assertEqual(
            pkg.amazon_tracking_url,
            "https://www.amazon.es/progress-tracker/package"
            "?_encoding=UTF8&orderId=404-2171566-7826720"
            "&packageIndex=0&shipmentId=TCl8Nkz09",
        )

    def test_delivery_attempt_alone_creates_awaiting_package(self):
        # The email arrives before any Pedido/Enviado ever did (edge case,
        # out-of-order forwarding) — still lands as an awaiting-pickup row.
        record, _ = process_message(
            fixture("107-intento-de-entrega-ones-funda-magnetica-para.eml")
        )
        self.assertTrue(record.processed, record.parse_error)
        pkg = Package.objects.get()
        self.assertEqual(pkg.state, Package.State.AWAITING_PICKUP)
        self.assertEqual(pkg.pickup_point.kind, PickupPoint.Kind.CARRIER)
        self.assertEqual(pkg.order_id, "404-2171566-7826720")
        self.assertTrue(pkg.description.startswith("ONES Funda Magnética"))

    def test_amazon_tracking_url_offered_for_every_amazon_package(self):
        # It's no longer a carrier-only escape hatch: any Amazon package, in
        # any state, links to its own tracker (it opens the Amazon app).
        point = PickupPoint.objects.create(
            name="Amazon Counter - Test", kind=PickupPoint.Kind.AMAZON_COUNTER,
        )
        pkg = Package.objects.create(
            pickup_point=point, order_id="404-1111111-1111111",
            shipment_id="AAAA111111", state=Package.State.AWAITING_PICKUP,
        )
        self.assertEqual(
            pkg.amazon_tracking_url,
            "https://www.amazon.es/progress-tracker/package"
            "?_encoding=UTF8&orderId=404-1111111-1111111"
            "&packageIndex=0&shipmentId=AAAA111111",
        )

    def test_amazon_tracking_url_falls_back_to_the_order_page(self):
        # Emails that carry only an order number (a consolidated pickup, a
        # Pedido not yet shipped) leave no shipment id to pin the box, so the
        # link degrades to the order page instead of disappearing.
        point = PickupPoint.objects.create(
            name="Amazon Locker - Test", kind=PickupPoint.Kind.AMAZON_LOCKER,
        )
        pkg = Package.objects.create(
            pickup_point=point, order_id="407-2753653-0825928",
            state=Package.State.PICKED_UP,
        )
        self.assertEqual(
            pkg.amazon_tracking_url,
            "https://www.amazon.es/gp/your-account/order-details"
            "?_encoding=UTF8&orderID=407-2753653-0825928",
        )

    def test_alt_store_package_has_no_amazon_link(self):
        # Manual, non-Amazon rows: no order number, nothing to link to.
        point = PickupPoint.objects.create(
            name="Tienda alternativa", kind=PickupPoint.Kind.ALT_STORE,
        )
        pkg = Package.objects.create(
            pickup_point=point, state=Package.State.AWAITING_PICKUP,
        )
        self.assertEqual(pkg.amazon_tracking_url, "")

    def test_delivery_attempt_other_carrier_email_is_flagged(self):
        # Mirrors test_unparseable_email_is_stored_and_flagged: a non-UPS
        # delivery-attempt notice must surface the red banner, same as any
        # other email type ingestion doesn't yet understand.
        msg = EmailMessage()
        msg["Subject"] = 'Intento de entrega: "Otro producto..."'
        msg["Message-ID"] = "<seur-attempt-ingest@example.com>"
        msg.set_content(
            "<p>Se ha intentado realizar tu entrega</p>"
            "<p>Lamentablemente, SEUR no ha podido realizar la entrega y te "
            "la ha dejado en su oficina para que la recojas.</p>",
            subtype="html",
        )
        record, created = process_message(msg.as_bytes())
        self.assertTrue(created)
        self.assertFalse(record.processed)
        self.assertIn("Unrecognized", record.parse_error)
        self.assertEqual(Package.objects.count(), 0)

    def test_return_notice_drives_no_transition(self):
        process_message(
            fixture("008-fwd-paquete-listo-para-recogida-recoger-en-amazon-counter-le.eml")
        )
        record, _ = process_message(
            fixture("011-fwd-ya-no-esta-disponible-para-su-recogida-lvjkes-bolso-band.eml")
        )
        self.assertTrue(record.processed)
        self.assertIn("engañoso", record.note)
        # The unrelated awaiting package is untouched, and the notice's own
        # order (never seen before) creates nothing.
        self.assertEqual(Package.objects.count(), 1)
        self.assertEqual(
            Package.objects.get().state, Package.State.AWAITING_PICKUP
        )

    def test_home_delivery_tracked_as_in_transit(self):
        # A home delivery ("En reparto" to a relative's address) is now
        # tracked: an in_transit package at a HOME point, no pickup trip.
        record, _ = process_message(
            fixture("012-fwd-llega-hoy-necesitas-una-contrasena-temporal-para-tu-entr.eml")
        )
        self.assertTrue(record.processed)
        pkg = Package.objects.get()
        self.assertEqual(pkg.state, Package.State.IN_TRANSIT)
        self.assertEqual(pkg.pickup_point.kind, PickupPoint.Kind.HOME)
        self.assertEqual(pkg.estimated_arrival, date(2026, 5, 12))  # "Llega hoy"

    def test_home_delivery_shipped_tracked_in_transit(self):
        # A real auto-forwarded "Enviado" email shipped to a home address.
        process_message(fixture("020-enviado-kalvica-11-pares-pendientes.eml"))
        pkg = Package.objects.get()
        self.assertEqual(pkg.state, Package.State.IN_TRANSIT)
        self.assertEqual(pkg.pickup_point.kind, PickupPoint.Kind.HOME)
        self.assertEqual(pkg.estimated_arrival, date(2026, 7, 17))  # "Llega el viernes"
        self.assertTrue(pkg.description.startswith("KALVICA"))

    def test_delivered_transitions_existing_home_package(self):
        # A home package already in transit; the real "Entregado" email for
        # its order takes it to the terminal state — same row, not a new one.
        point = PickupPoint.objects.create(
            name="Home address", kind=PickupPoint.Kind.HOME,
        )
        Package.objects.create(
            pickup_point=point, order_id="404-7963783-4668345",
            description="A home-delivered item", state=Package.State.IN_TRANSIT,
        )
        process_message(
            fixture("021-fwd-entregado-1-producto-n-o-de-pedido-404-7963783-4668345.eml")
        )
        self.assertEqual(Package.objects.count(), 1)  # same package
        pkg = Package.objects.get()
        self.assertEqual(pkg.state, Package.State.DELIVERED)
        self.assertEqual(pkg.actual_arrival, date(2026, 7, 13))  # "Entregado hoy"

    def test_delivered_email_alone_creates_delivered_package(self):
        record, _ = process_message(
            fixture("021-fwd-entregado-1-producto-n-o-de-pedido-404-7963783-4668345.eml")
        )
        self.assertTrue(record.processed, record.parse_error)
        pkg = Package.objects.get()
        self.assertEqual(pkg.state, Package.State.DELIVERED)
        self.assertEqual(pkg.pickup_point.kind, PickupPoint.Kind.HOME)
        self.assertEqual(pkg.actual_arrival, date(2026, 7, 13))

    def test_delivered_home_consolidates_two_orders(self):
        # A consolidated notification can print more than one order and
        # shipment id (proven for PICKED_UP by the real fixture 018, which
        # names two orders but only one shipment id) — the same template
        # habit is plausible for a consolidated DELIVERED. The parser only
        # ever captured a single `shipment_id` (the first one seen in the
        # HTML), and the old matching narrowed to just that shipment —
        # silently leaving whichever order's shipment id wasn't first still
        # `in_transit`. Both must transition, regardless of which shipment
        # came first. (The real 2026-07-18 incident this whole area of code
        # was fixed for turned out to be the harder sibling case below,
        # where the second order's id never appears in the email at all —
        # this test guards the "id present but not first" half of it.)
        home = PickupPoint.objects.create(
            name="Rosa - Can Salgot, Barcelona", kind=PickupPoint.Kind.HOME,
        )
        first = Package.objects.create(
            pickup_point=home, order_id="404-1111111-1111111",
            shipment_id="AAAA111111", description="6-in-1 Hot Air Brush & Hair Dryer",
            state=Package.State.IN_TRANSIT,
        )
        second = Package.objects.create(
            pickup_point=home, order_id="404-2222222-2222222",
            shipment_id="BBBB222222", description="Otro producto",
            state=Package.State.IN_TRANSIT,
        )
        msg = EmailMessage()
        msg["Subject"] = "Entregado: 2 productos"
        msg["Message-ID"] = "<two-orders-delivered@example.com>"
        msg.set_content(
            "<h2>¡Tu paquete se ha entregado!</h2>"
            "<p>Entregado hoy</p>"
            "<p>El pedido ha sido entregado en la dirección indicada.</p>"
            "<p>Rosa - Can Salgot, Barcelona</p>"
            "<p>Pedido n.º 404-1111111-1111111</p>"
            '<a href="https://www.amazon.es/gp/r.html?M=urn:rtn:msg:20260718150100'
            '&U=https%3A%2F%2Fwww.amazon.es%2Fprogress-tracker%2Fpackage%3ForderId'
            '%3D404-1111111-1111111%26shipmentId%3DAAAA111111">Seguimiento</a>'
            "<p>Pedido n.º 404-2222222-2222222</p>"
            '<a href="https://www.amazon.es/gp/r.html?M=urn:rtn:msg:20260718150100'
            '&U=https%3A%2F%2Fwww.amazon.es%2Fprogress-tracker%2Fpackage%3ForderId'
            '%3D404-2222222-2222222%26shipmentId%3DBBBB222222">Seguimiento</a>',
            subtype="html",
        )
        record, _ = process_message(msg.as_bytes())
        self.assertTrue(record.processed, record.parse_error)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.state, Package.State.DELIVERED)
        self.assertEqual(second.state, Package.State.DELIVERED)
        self.assertEqual(first.actual_arrival, date(2026, 7, 18))
        self.assertEqual(second.actual_arrival, date(2026, 7, 18))
        self.assertIn("2 paquetes", record.note)

    def test_delivered_home_rescues_unlisted_sibling_by_asin(self):
        # The actual 2026-07-18 incident, replayed from the real emails
        # (fixtures 013/046/059/064): two independent home orders — a dog
        # ramp (407-2023163-0562738) and a hair dryer bought minutes apart —
        # delivered by Amazon in the same visit. The consolidated "En
        # reparto"/"Entregado" emails picture *both* items but only ever
        # print the dog ramp's own "Pedido n.º" and tracking link; the hair
        # dryer's order id never appears in either email's text at all, so
        # no amount of order/shipment id matching can find it. Its ASIN
        # (B0H33JF6HM, from the photo link) and shared destination are the
        # only thread back to its package — confirmed by hand in the admin
        # afterwards, this test is what keeps it from recurring.
        process_message(fixture("013-pedido-eheyciga-escalera-perros-4.eml"))
        process_message(fixture("046-enviado-eheyciga-escalera-perros-4.eml"))
        dog_ramp = Package.objects.get(order_id="407-2023163-0562738")
        home = dog_ramp.pickup_point
        self.assertEqual(home.kind, PickupPoint.Kind.HOME)

        hair_dryer = Package.objects.create(
            pickup_point=home, order_id="407-1111111-1111111",
            asin="B0H33JF6HM", description="6-in-1 Hot Air Brush & Hair Dryer",
            state=Package.State.IN_TRANSIT,
        )

        # "En reparto" (OUT_FOR_DELIVERY) only ever touches the named order —
        # by design (see _find_packages) it must NOT rescue the sibling yet.
        process_message(
            fixture("059-en-reparto-6-in-1-hot-air-brush-hair-y-1-productos-mas.eml")
        )
        hair_dryer.refresh_from_db()
        self.assertEqual(hair_dryer.state, Package.State.IN_TRANSIT)

        record, _ = process_message(
            fixture("064-entregado-6-in-1-hot-air-brush-hair-y-1-producto-mas.eml")
        )
        self.assertTrue(record.processed, record.parse_error)

        dog_ramp.refresh_from_db()
        hair_dryer.refresh_from_db()
        self.assertEqual(dog_ramp.state, Package.State.DELIVERED)
        self.assertEqual(hair_dryer.state, Package.State.DELIVERED)
        self.assertEqual(hair_dryer.actual_arrival, date(2026, 7, 18))

    def test_ready_for_pickup_consolidates_two_orders(self):
        # Same root cause as the home-delivery case above, one step earlier
        # in the lifecycle: a "listo para recogida" notification can also
        # bundle boxes from two different orders/shipments arriving at the
        # same locker/counter together.
        point = PickupPoint.objects.create(
            name="Amazon Locker - Test, Barcelona",
            kind=PickupPoint.Kind.AMAZON_LOCKER,
        )
        first = Package.objects.create(
            pickup_point=point, order_id="404-3333333-3333333",
            shipment_id="CCCC333333", description="Producto uno",
            state=Package.State.IN_TRANSIT,
        )
        second = Package.objects.create(
            pickup_point=point, order_id="404-4444444-4444444",
            shipment_id="DDDD444444", description="Producto dos",
            state=Package.State.IN_TRANSIT,
        )
        msg = EmailMessage()
        msg["Subject"] = "Paquete listo para recogida"
        msg["Message-ID"] = "<two-orders-ready@example.com>"
        msg.set_content(
            "<p>El paquete está listo para su recogida</p>"
            "<p>antes del 20 de julio</p>"
            "<p>El código de recogida es 123456</p>"
            "<p>Amazon Locker - Test, Barcelona</p>"
            "<p>Pedido n.º 404-3333333-3333333</p>"
            '<a href="https://www.amazon.es/gp/r.html?M=urn:rtn:msg:20260718090000'
            '&U=https%3A%2F%2Fwww.amazon.es%2Fprogress-tracker%2Fpackage%3ForderId'
            '%3D404-3333333-3333333%26shipmentId%3DCCCC333333">Seguimiento</a>'
            "<p>Pedido n.º 404-4444444-4444444</p>"
            '<a href="https://www.amazon.es/gp/r.html?M=urn:rtn:msg:20260718090000'
            '&U=https%3A%2F%2Fwww.amazon.es%2Fprogress-tracker%2Fpackage%3ForderId'
            '%3D404-4444444-4444444%26shipmentId%3DDDDD444444">Seguimiento</a>',
            subtype="html",
        )
        record, _ = process_message(msg.as_bytes())
        self.assertTrue(record.processed, record.parse_error)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.state, Package.State.AWAITING_PICKUP)
        self.assertEqual(second.state, Package.State.AWAITING_PICKUP)
        self.assertIn("2 paquetes", record.note)

    def test_review_creates_no_row(self):
        record, _ = process_message(
            fixture("010-fwd-gracias-por-su-resena-de-cargador-inalambrico-mag-en-ama.eml")
        )
        self.assertTrue(record.processed)
        self.assertEqual(Package.objects.count(), 0)

    def test_unparseable_email_is_stored_and_flagged(self):
        msg = EmailMessage()
        msg["Subject"] = "Oferta especial solo hoy"
        msg["Message-ID"] = "<junk@example.com>"
        msg.set_content("Grandes descuentos", subtype="html")
        record, created = process_message(msg.as_bytes())
        self.assertTrue(created)
        self.assertFalse(record.processed)
        self.assertIn("Unrecognized", record.parse_error)
        self.assertEqual(Package.objects.count(), 0)

    def test_paid_order_assumed_vine_then_refuted_by_shipped(self):
        # Pedido prints 0.00€ → assumed Vine; the Enviado prints 19.98€ →
        # refuted. Both emails are the same order (408-…).
        process_message(fixture("016-fwd-pedido-intex-64761-colchon.eml"))
        pkg = Package.objects.get()
        self.assertTrue(pkg.is_vine)
        self.assertEqual(pkg.cost, Decimal("0.00"))

        record, _ = process_message(fixture("019-fwd-enviado-intex-64761-colchon.eml"))
        self.assertEqual(Package.objects.count(), 1)  # same package, not a new one
        pkg.refresh_from_db()
        self.assertFalse(pkg.is_vine)
        self.assertEqual(pkg.cost, Decimal("19.98"))
        self.assertEqual(pkg.shipment_id, "TgvslGX9H")
        self.assertIn("Vine", record.note)

    def test_eu_import_surcharge_keeps_vine(self):
        # Real case (order 404-2171566-7826720): Pedido 0.00€ → assumed Vine;
        # the Enviado prints 3.63€, which is only the EU import duty the
        # non-EU seller passes on — the item itself is still free, so the
        # package stays Vine (and keeps owing a review) instead of being
        # refuted like the colchón above.
        process_message(fixture("095-pedido-ones-funda-magnetica-para-galaxy-s26.eml"))
        pkg = Package.objects.get()
        self.assertTrue(pkg.is_vine)

        record, _ = process_message(
            fixture("106-enviado-ones-funda-magnetica-para-galaxy-s26-recargo-ue.eml"))
        self.assertEqual(Package.objects.count(), 1)
        pkg.refresh_from_db()
        self.assertTrue(pkg.is_vine)
        self.assertEqual(pkg.cost, Decimal("3.63"))  # what was really charged
        self.assertIn("Recargo UE", record.note)
        self.assertEqual(Review.objects.filter(package=pkg).count(), 0)  # in transit, review created on pickup

    def test_eu_import_surcharge_amount_comes_from_config(self):
        # The figure is legislation: it changes, so it lives in the database.
        # Raise it and the old amount stops meaning Vine.
        config = Config.load()
        config.eu_import_surcharge = Decimal("4.50")
        config.save()
        process_message(
            fixture("106-enviado-ones-funda-magnetica-para-galaxy-s26-recargo-ue.eml"))
        pkg = Package.objects.get()
        self.assertFalse(pkg.is_vine)
        self.assertEqual(pkg.cost, Decimal("3.63"))

    def test_shipped_first_out_of_order_does_not_get_reflagged(self):
        # Enviado processed before its Pedido (re-forward / racing delivery):
        # the real price must survive the later 0.00€ Pedido.
        process_message(fixture("019-fwd-enviado-intex-64761-colchon.eml"))
        process_message(fixture("016-fwd-pedido-intex-64761-colchon.eml"))
        pkg = Package.objects.get()
        self.assertFalse(pkg.is_vine)
        self.assertEqual(pkg.cost, Decimal("19.98"))

    def test_genuine_vine_stays_vine_through_shipping(self):
        process_message(fixture("006-fwd-pedido-cargador-inalambrico.eml"))
        process_message(fixture("007-fwd-enviado-cargador-inalambrico.eml"))
        pkg = Package.objects.get()
        self.assertTrue(pkg.is_vine)  # shipped email also 0.00€
        self.assertEqual(pkg.cost, Decimal("0.00"))

    def test_pickup_sweeps_whole_point(self):
        # A package is awaiting at the Les Mesures counter (its Ready email).
        process_message(
            fixture("008-fwd-paquete-listo-para-recogida-recoger-en-amazon-counter-le.eml")
        )
        cargador = Package.objects.get()
        self.assertEqual(cargador.state, Package.State.AWAITING_PICKUP)

        # "Se han recogido 4 productos" names a *different* order (404-…) and
        # never mentions the cargador — but everything at that counter goes
        # home in one scan, so the cargador is marked picked too.
        record, _ = process_message(
            fixture("018-fwd-se-han-recogido-4-productos.eml")
        )
        cargador.refresh_from_db()
        self.assertEqual(cargador.state, Package.State.PICKED_UP)
        self.assertEqual(cargador.picked_up_on, date(2026, 7, 4))
        self.assertIn("bloque", record.note)

    def test_pickup_does_not_sweep_a_different_point(self):
        # The Locker Cebolla package must be untouched by a Counter pickup.
        process_message(
            fixture("017-fwd-paquete-listo-para-recogida-recoger-en-amazon-locker-ceb.eml")
        )
        process_message(fixture("018-fwd-se-han-recogido-4-productos.eml"))
        cebolla = Package.objects.get(pickup_point__name__startswith="Amazon Locker - cebolla")
        self.assertEqual(cebolla.state, Package.State.AWAITING_PICKUP)

    def test_same_venue_across_templates_shares_one_pickup_point(self):
        # The "Pedido" line and the "Entregado" line spell the Les Mesures
        # counter differently (comma placement, city vs. province name) but
        # are the same physical counter (postal code 25700). They must
        # collapse into one PickupPoint, not two — both so the "Add package"
        # dropdown doesn't show duplicates and so a later pickup-sweep at
        # this counter (matched by PickupPoint FK) catches every package
        # waiting there, whichever template created its row.
        process_message(fixture("006-fwd-pedido-cargador-inalambrico.eml"))
        process_message(
            fixture("008-fwd-paquete-listo-para-recogida-recoger-en-amazon-counter-le.eml")
        )
        self.assertEqual(
            PickupPoint.objects.filter(kind=PickupPoint.Kind.AMAZON_COUNTER).count(), 1
        )

    def test_pickup_reminder_drives_no_transition(self):
        # The cebolla locker package is awaiting pickup (its Ready email).
        process_message(
            fixture("017-fwd-paquete-listo-para-recogida-recoger-en-amazon-locker-ceb.eml")
        )
        # A reminder about that very package arrives days later — a nag, no new
        # information. It must leave the state and deadline exactly as they were.
        record, _ = process_message(
            fixture("022-recordatorio-paquete-en-espera-de-recogida.eml")
        )
        self.assertTrue(record.processed, record.parse_error)
        self.assertIn("Recordatorio", record.note)
        self.assertEqual(Package.objects.count(), 1)  # no new row
        pkg = Package.objects.get()
        self.assertEqual(pkg.state, Package.State.AWAITING_PICKUP)
        self.assertEqual(pkg.deadline, date(2026, 7, 16))  # unchanged

    def test_reprocess_failures_reparses_a_now_known_template(self):
        # An email that failed under an older parser is stuck: the idempotent
        # scan never retries it. Simulate that stale failure, then reprocess.
        raw = fixture("022-recordatorio-paquete-en-espera-de-recogida.eml")
        from email import message_from_bytes, policy
        mid = message_from_bytes(raw, policy=policy.default).get("Message-ID")
        RawEmail.objects.create(
            message_id=mid, subject="Recordatorio: Paquete en espera…",
            raw=raw.decode("utf-8", "replace"),
            parse_error="Unrecognized email type", processed=False,
        )

        total, fixed = reprocess_failures()
        self.assertEqual((total, fixed), (1, 1))
        record = RawEmail.objects.get(message_id=mid)
        self.assertEqual(record.parse_error, "")  # banner clears
        self.assertTrue(record.processed)
        self.assertEqual(record.kind, "pickup_reminder")

    def test_reprocess_leaves_genuine_failures_flagged(self):
        # A truly unknown email stays flagged after a reprocess — never silently
        # cleared just because we retried it.
        msg = EmailMessage()
        msg["Subject"] = "Oferta especial solo hoy"
        msg["Message-ID"] = "<still-junk@example.com>"
        msg.set_content("Grandes descuentos", subtype="html")
        process_message(msg.as_bytes())

        total, fixed = reprocess_failures()
        self.assertEqual((total, fixed), (1, 0))
        self.assertTrue(
            RawEmail.objects.get(message_id="<still-junk@example.com>").parse_error
        )

    def test_nameless_delivered_email_leaves_description_blank(self):
        # "Entregado: 1 producto | N.º de pedido …" names no product and there
        # are no item links: the description is left blank (the calendar shows a
        # "desconocido" placeholder) rather than echoing the subject boilerplate.
        process_message(
            fixture("021-fwd-entregado-1-producto-n-o-de-pedido-404-7963783-4668345.eml")
        )
        pkg = Package.objects.get()
        self.assertEqual(pkg.description, "")

    # ---- R1: reviews follow the packages that owe them ----

    def test_vine_pedido_does_not_create_pending_review_until_pickup(self):
        process_message(fixture("016-fwd-pedido-intex-64761-colchon.eml"))
        pkg = Package.objects.get()
        self.assertTrue(pkg.is_vine)
        self.assertEqual(Review.objects.count(), 0)  # in transit, no review yet

        # Mark picked up -> review is created now
        pkg.state = Package.State.PICKED_UP
        pkg.picked_up_on = date(2026, 7, 10)
        pkg.save()
        _sync_review_for_vine(pkg)

        review = Review.objects.get()
        self.assertEqual(review.package_id, pkg.pk)
        self.assertEqual(review.status, Review.Status.PENDING)
        self.assertEqual(review.asin, pkg.asin)
        self.assertEqual(review.product_title, pkg.description)
        self.assertEqual(review.due_on, date(2026, 8, 9))

    def test_vine_refuted_deletes_untouched_pending_review(self):
        process_message(fixture("016-fwd-pedido-intex-64761-colchon.eml"))
        pkg = Package.objects.get()
        pkg.state = Package.State.PICKED_UP
        pkg.picked_up_on = date(2026, 7, 10)
        pkg.save()
        _sync_review_for_vine(pkg)
        self.assertEqual(Review.objects.count(), 1)

        process_message(fixture("019-fwd-enviado-intex-64761-colchon.eml"))
        self.assertEqual(Review.objects.count(), 0)  # discarded, never Vine after all

    def test_vine_refuted_keeps_review_the_user_already_touched(self):
        process_message(fixture("016-fwd-pedido-intex-64761-colchon.eml"))
        pkg = Package.objects.get()
        pkg.state = Package.State.PICKED_UP
        pkg.picked_up_on = date(2026, 7, 10)
        pkg.save()
        _sync_review_for_vine(pkg)
        review = Review.objects.get()
        review.notes = "Ya lo estoy probando"
        review.save()

        process_message(fixture("019-fwd-enviado-intex-64761-colchon.eml"))
        # Refuted as Vine, but the row survives — it's the user's now.
        self.assertEqual(Review.objects.count(), 1)
        pkg.refresh_from_db()
        self.assertFalse(pkg.is_vine)

    def test_genuine_vine_review_not_duplicated_across_lifecycle(self):
        process_message(fixture("006-fwd-pedido-cargador-inalambrico.eml"))
        process_message(fixture("007-fwd-enviado-cargador-inalambrico.eml"))
        self.assertEqual(Review.objects.count(), 0)  # still in transit

        process_message(fixture("008-fwd-paquete-listo-para-recogida-recoger-en-amazon-counter-le.eml"))
        self.assertEqual(Review.objects.count(), 0)  # awaiting pickup

        process_message(fixture("009-fwd-se-ha-recogido-cargador-inalambrico-magnetico-25w-con-us.eml"))
        self.assertEqual(Review.objects.count(), 1)  # created on pickup!

    def test_pickup_sets_review_due_on_30_days_out(self):
        for name in (
            "006-fwd-pedido-cargador-inalambrico.eml",
            "007-fwd-enviado-cargador-inalambrico.eml",
            "008-fwd-paquete-listo-para-recogida-recoger-en-amazon-counter-le.eml",
            "009-fwd-se-ha-recogido-cargador-inalambrico-magnetico-25w-con-us.eml",
        ):
            process_message(fixture(name))
        review = Review.objects.get()
        self.assertEqual(review.due_on, date(2026, 8, 7))  # picked up 2026-07-08 + 30

    def test_review_published_creates_review_when_none_pending(self):
        record, _ = process_message(
            fixture("010-fwd-gracias-por-su-resena-de-cargador-inalambrico-mag-en-ama.eml")
        )
        self.assertTrue(record.processed, record.parse_error)
        review = Review.objects.get()
        self.assertEqual(review.status, Review.Status.PUBLISHED)
        self.assertEqual(review.asin, "B0GXK1FPTY")
        self.assertEqual(review.review_id, "R1IUNF3PY66WHI")
        self.assertEqual(review.title, "Carga rápido y los imanes agarran bien")
        self.assertEqual(review.rating, 4)
        self.assertTrue(review.text.endswith("ideal para..."))
        self.assertFalse(review.text_is_complete)  # only ever an excerpt
        self.assertIsNone(review.package)  # no matching package was ever seen

    def test_review_published_closes_matching_pending_review(self):
        process_message(fixture("006-fwd-pedido-cargador-inalambrico.eml"))
        pkg = Package.objects.get()
        pkg.state = Package.State.PICKED_UP
        pkg.picked_up_on = date(2026, 7, 8)
        pkg.save()
        _sync_review_for_vine(pkg)
        review = Review.objects.get()
        self.assertEqual(review.status, Review.Status.PENDING)

        process_message(
            fixture("010-fwd-gracias-por-su-resena-de-cargador-inalambrico-mag-en-ama.eml")
        )
        self.assertEqual(Review.objects.count(), 1)  # same row, not a second one
        review.refresh_from_db()
        self.assertEqual(review.status, Review.Status.PUBLISHED)
        self.assertEqual(review.package_id, pkg.pk)
        self.assertEqual(review.review_id, "R1IUNF3PY66WHI")

    def test_review_published_prefers_approved_over_pending(self):
        pending = Review.objects.create(
            product_title="otro", asin="B0GXK1FPTY", status=Review.Status.PENDING,
        )
        approved = Review.objects.create(
            product_title="Cargador Inalámbrico (mío)", asin="B0GXK1FPTY",
            status=Review.Status.APPROVED, title="Mi propio título", rating=5,
        )
        process_message(
            fixture("010-fwd-gracias-por-su-resena-de-cargador-inalambrico-mag-en-ama.eml")
        )
        approved.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(approved.status, Review.Status.PUBLISHED)
        self.assertEqual(approved.title, "Mi propio título")  # never overwritten
        self.assertEqual(pending.status, Review.Status.PENDING)  # untouched

    def test_review_published_is_idempotent(self):
        process_message(
            fixture("010-fwd-gracias-por-su-resena-de-cargador-inalambrico-mag-en-ama.eml")
        )
        self.assertEqual(Review.objects.count(), 1)
        # A stray re-forward of the very same confirmation must not duplicate it.
        msg = EmailMessage()
        msg["Subject"] = "Re-fwd"
        msg["Message-ID"] = "<second-copy@example.com>"
        raw = fixture("010-fwd-gracias-por-su-resena-de-cargador-inalambrico-mag-en-ama.eml")
        from email import message_from_bytes, policy
        body = message_from_bytes(raw, policy=policy.default).get_body(
            preferencelist=("html", "plain"))
        msg.set_content(body.get_content(), subtype="html")
        record, _ = process_message(msg.as_bytes())
        self.assertEqual(Review.objects.count(), 1)
        self.assertIn("ya registrada", record.note)

    def test_home_delivery_creates_pending_review_for_vine(self):
        point = PickupPoint.objects.create(name="Mi Casa", kind=PickupPoint.Kind.HOME)
        pkg = Package.objects.create(
            pickup_point=point, description="Producto Vine Domicilio", asin="B0HOMEVINE",
            is_vine=True, state=Package.State.IN_TRANSIT,
        )
        self.assertEqual(Review.objects.count(), 0)

        # Process a home delivery email (DELIVERED)
        msg = EmailMessage()
        msg["Subject"] = "Entregado: Producto Vine Domicilio"
        msg["Date"] = "Wed, 15 Jul 2026 10:00:00 +0200"
        msg["Message-ID"] = "<delivered-home@example.com>"
        msg.set_content(f"Entregado hoy\nASIN: B0HOMEVINE\n{point.name}")
        
        # Or test via model transition + _sync_review_for_vine directly
        pkg.state = Package.State.DELIVERED
        pkg.actual_arrival = date(2026, 7, 15)
        pkg.save()
        _sync_review_for_vine(pkg)

        review = Review.objects.get(package=pkg)
        self.assertEqual(review.status, Review.Status.PENDING)
        self.assertEqual(review.due_on, date(2026, 8, 14))  # 15 Jul + 30 days

    def test_confirm_pickup_creates_pending_review_for_vine(self):
        point = PickupPoint.objects.create(name="UPS Office", kind=PickupPoint.Kind.CARRIER)
        pkg = Package.objects.create(
            pickup_point=point, description="Producto Carrier Vine", asin="B0CARRIERVINE",
            is_vine=True, state=Package.State.AWAITING_PICKUP, actual_arrival=date(2026, 7, 10),
        )
        self.assertEqual(Review.objects.count(), 0)

        # User confirms manual pickup via confirm_pickup view
        response = self.client.post(
            reverse("confirm_pickup", args=[pkg.pk]),
            {"picked_up_on": "2026-07-12"},
        )
        self.assertEqual(response.status_code, 200)

        pkg.refresh_from_db()
        self.assertEqual(pkg.state, Package.State.PICKED_UP)
        review = Review.objects.get(package=pkg)
        self.assertEqual(review.status, Review.Status.PENDING)
        self.assertEqual(review.due_on, date(2026, 8, 11))  # 12 Jul + 30 days


class BackfillReviewsTests(TestCase):
    """The one-off that catches real data up to R1 (2026-07-23): Vine
    packages ingested before the hooks existed, and review_published emails
    that were correctly parsed but, under the old no-op handler, produced
    nothing."""

    def test_backfills_pending_review_for_pre_existing_vine_package(self):
        point = PickupPoint.objects.create(
            name="Amazon Locker - Test", kind=PickupPoint.Kind.AMAZON_LOCKER,
        )
        pkg = Package.objects.create(
            pickup_point=point, description="Viejo paquete Vine", asin="B0OLDVINE1",
            is_vine=True, state=Package.State.PICKED_UP,
            picked_up_on=date(2026, 6, 1),
        )
        result = backfill_reviews()
        self.assertEqual(result["packages"], 1)
        review = Review.objects.get(package=pkg)
        self.assertEqual(review.status, Review.Status.PENDING)
        self.assertEqual(review.due_on, date(2026, 7, 1))  # picked up + 30

    def test_backfill_is_idempotent(self):
        point = PickupPoint.objects.create(
            name="Amazon Locker - Test", kind=PickupPoint.Kind.AMAZON_LOCKER,
        )
        Package.objects.create(
            pickup_point=point, description="Viejo paquete Vine", is_vine=True,
            state=Package.State.PICKED_UP, picked_up_on=date(2026, 6, 1),
        )
        backfill_reviews()
        result = backfill_reviews()
        self.assertEqual(result["packages"], 0)  # already has one, second pass no-ops
        self.assertEqual(Review.objects.count(), 1)

    def test_backfill_replays_stranded_review_published_emails(self):
        raw = fixture("010-fwd-gracias-por-su-resena-de-cargador-inalambrico-mag-en-ama.eml")
        RawEmail.objects.create(
            message_id="<stranded@example.com>", subject="Gracias por tu reseña",
            raw=raw.decode("utf-8", "replace"), processed=True,
            kind="review_published", note="Reseña publicada: sin acción de calendario",
        )
        result = backfill_reviews()
        self.assertEqual(result["emails"], 1)
        review = Review.objects.get()
        self.assertEqual(review.status, Review.Status.PUBLISHED)
        self.assertEqual(review.review_id, "R1IUNF3PY66WHI")
        record = RawEmail.objects.get(message_id="<stranded@example.com>")
        self.assertNotIn("sin acción", record.note)

    def test_backfill_replay_is_idempotent(self):
        raw = fixture("010-fwd-gracias-por-su-resena-de-cargador-inalambrico-mag-en-ama.eml")
        RawEmail.objects.create(
            message_id="<stranded-2@example.com>", subject="Gracias por tu reseña",
            raw=raw.decode("utf-8", "replace"), processed=True,
            kind="review_published", note="Reseña publicada: sin acción de calendario",
        )
        backfill_reviews()
        backfill_reviews()
        self.assertEqual(Review.objects.count(), 1)


def _junk_email(subject="Newsletter", mid="<junk-scan@example.com>"):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["Message-ID"] = mid
    msg.set_content("<p>Nada que procesar</p>", subtype="html")
    return msg.as_bytes()


@override_settings(
    GMAIL_IMAP_USER="viner@example.com",
    GMAIL_IMAP_APP_PASSWORD="app-password",
)
class ScanInboxTests(TestCase):
    """scan_inbox against a fake mailbox: idempotency and the Trash policy."""

    @override_settings(GMAIL_TRASH_PROCESSED=True)
    def test_processed_trashed_failures_kept(self):
        good = fixture("006-fwd-pedido-cargador-inalambrico.eml")
        bad = _junk_email()
        fake = FakeIMAP([(11, good), (22, bad)])

        stats = scan_inbox(connection_factory=lambda: fake)

        self.assertEqual(stats["new"], 1)  # only the parseable one
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["trashed"], 1)
        # Read-write session, and only the processed UID got the \Trash label.
        self.assertFalse(fake.readonly)
        trashed = [uid for uid, item, _ in fake.stored if item == "+X-GM-LABELS"]
        self.assertEqual(trashed, [11])
        # The unparseable one stays in the inbox and is flagged for the banner.
        self.assertTrue(
            RawEmail.objects.get(message_id="<junk-scan@example.com>").parse_error
        )

    @override_settings(GMAIL_TRASH_PROCESSED=True)
    def test_idempotent_scan_trashes_leftover_without_reprocessing(self):
        good = fixture("006-fwd-pedido-cargador-inalambrico.eml")
        # First scan ingests and trashes; imagine the trash didn't take and the
        # message is still there on the next sweep.
        scan_inbox(connection_factory=lambda: FakeIMAP([(11, good)]))
        second = FakeIMAP([(11, good)])
        stats = scan_inbox(connection_factory=lambda: second)

        self.assertEqual(stats["new"], 0)  # not reprocessed
        self.assertEqual(Package.objects.count(), 1)  # no duplicate
        # Still swept out of the inbox on the retry.
        self.assertEqual(
            [uid for uid, item, _ in second.stored if item == "+X-GM-LABELS"], [11]
        )

    @override_settings(GMAIL_TRASH_PROCESSED=False)
    def test_readonly_mode_never_touches_mailbox(self):
        good = fixture("006-fwd-pedido-cargador-inalambrico.eml")
        fake = FakeIMAP([(11, good)])

        stats = scan_inbox(connection_factory=lambda: fake)

        self.assertEqual(stats["new"], 1)
        self.assertEqual(stats["trashed"], 0)
        self.assertTrue(fake.readonly)
        self.assertEqual(fake.stored, [])  # nothing moved or flagged

    def test_two_sweeps_at_the_same_instant_apply_an_email_once(self):
        """The web's "procesar ahora" button and the worker's loop can now
        collide on the very same email. The Message-ID pre-check can't see an
        insert another connection hasn't committed yet, so the unique
        constraint is the real guard — and the loser must bail out *before*
        parsing: a Pepe y Dalda notice matches on nothing and always creates a
        row, so applying it twice means two packages for one letter."""
        raw = _pepe_email("Recepción carta", "Hemos recibido 1 carta para ti")
        real_create = RawEmail.objects.create

        def racing_create(**kwargs):
            # The other sweep gets there between our pre-check and our insert.
            real_create(**kwargs)
            raise IntegrityError("UNIQUE constraint failed: packages_rawemail.message_id")

        with patch.object(RawEmail.objects, "create", side_effect=racing_create):
            record, created = process_message(raw)

        self.assertFalse(created)  # not ours to parse
        self.assertEqual(RawEmail.objects.count(), 1)
        self.assertEqual(record, RawEmail.objects.get())
        self.assertEqual(Package.objects.count(), 0)  # applied by the winner only


@override_settings(
    GMAIL_IMAP_USER="viner@example.com",
    GMAIL_IMAP_APP_PASSWORD="app-password",
    # The two full-page renders below draw the topbar's {% static %} logo,
    # which needs a collectstatic manifest this environment doesn't have.
    STORAGES={
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class ManualIngestTests(TestCase):
    """The topbar's ⟳ — an inbox sweep on demand, for the email that just
    landed and shouldn't have to wait for the worker's next cycle.

    The sweep itself is the worker's, already covered by ScanInboxTests. What's
    under test here is the button: that it's reachable from both sections, that
    it never runs on a GET, the one-line answer it leaves, and whether it asks
    the view behind it to refresh."""

    def test_the_calendar_offers_the_button(self):
        html = self.client.get(reverse("home")).content.decode()
        self.assertIn(reverse("ingest_now"), html)
        self.assertIn('id="ingest-status"', html)

    def test_the_reviews_page_offers_it_too(self):
        # Shared topbar, and an ingest sweep can create or close a review.
        html = self.client.get(reverse("reviews_list")).content.decode()
        self.assertIn(reverse("ingest_now"), html)

    def test_a_get_never_sweeps(self):
        # A sweep talks IMAP and moves mail to Trash, so it stays behind POST:
        # no prefetch, crawl or stray link ever triggers one.
        with patch("packages.views.scan_now") as scan:
            response = self.client.get(reverse("ingest_now"))
        self.assertEqual(response.status_code, 405)
        scan.assert_not_called()

    def test_new_mail_is_reported_and_the_view_refreshes(self):
        stats = {"messages": 3, "new": 2, "failed": 0, "trashed": 2}
        with patch("packages.views.scan_now", return_value=stats):
            response = self.client.post(reverse("ingest_now"))
        self.assertContains(response, "2 correos nuevos")
        # The grid behind the topbar is now stale: same trigger the manual
        # pickup fires, so it refetches itself in place.
        self.assertEqual(response["HX-Trigger"], "package-updated")

    def test_a_single_email_is_counted_in_the_singular(self):
        stats = {"messages": 1, "new": 1, "failed": 0, "trashed": 1}
        with patch("packages.views.scan_now", return_value=stats):
            response = self.client.post(reverse("ingest_now"))
        self.assertContains(response, "1 correo nuevo")

    def test_nothing_new_says_so_and_leaves_the_view_alone(self):
        stats = {"messages": 0, "new": 0, "failed": 0, "trashed": 0}
        with patch("packages.views.scan_now", return_value=stats):
            response = self.client.post(reverse("ingest_now"))
        self.assertContains(response, "Sin correos nuevos")
        self.assertFalse(response.has_header("HX-Trigger"))  # nothing changed

    def test_unparseable_mail_shows_up_on_the_pill_and_refreshes(self):
        # The red banner spells out what broke; the pill just points at it,
        # which is why the refresh has to happen for a failure too.
        stats = {"messages": 2, "new": 1, "failed": 1, "trashed": 1}
        with patch("packages.views.scan_now", return_value=stats):
            response = self.client.post(reverse("ingest_now"))
        self.assertContains(response, "1 correo nuevo · 1 sin procesar")
        self.assertContains(response, "bad")  # in danger red
        self.assertEqual(response["HX-Trigger"], "package-updated")

    def test_a_mailbox_that_is_down_never_breaks_the_page(self):
        with patch("packages.views.scan_now", side_effect=OSError("timed out")):
            response = self.client.post(reverse("ingest_now"))
        self.assertContains(response, "No se pudo leer el buzón")
        self.assertFalse(response.has_header("HX-Trigger"))

    @override_settings(GMAIL_IMAP_USER="", GMAIL_IMAP_APP_PASSWORD="")
    def test_without_credentials_it_says_so_instead_of_trying(self):
        with patch("packages.views.scan_now") as scan:
            response = self.client.post(reverse("ingest_now"))
        scan.assert_not_called()
        self.assertContains(response, "Buzón sin configurar")

    def test_scan_now_is_the_worker_sweep_with_a_tighter_timeout(self):
        # Same scan_inbox, so the same ingestion — only the socket timeout
        # differs, so a stuck mailbox can't outlive gunicorn's own 30 s.
        fake = FakeIMAP([(11, fixture("006-fwd-pedido-cargador-inalambrico.eml"))])
        with patch("packages.ingest.imaplib.IMAP4_SSL", return_value=fake) as imap:
            stats = scan_now()
        self.assertEqual(stats["new"], 1)
        self.assertEqual(Package.objects.count(), 1)
        self.assertEqual(imap.call_args.kwargs["timeout"], MANUAL_SCAN_TIMEOUT)
        self.assertLess(MANUAL_SCAN_TIMEOUT, 30)


class EstimateWordingTests(SimpleTestCase):
    """The card's "Llegada estimada" sentence and the chip's parenthetical,
    pinned at fixed dates so the actual Spanish is under test.

    Both read the same two fields (the estimate and, when the email gave a
    window, its end) against today, and neither may ever claim more than the
    email did — the whole point is that the user doesn't drive to a counter on
    the strength of a date Amazon already missed."""

    START, END = date(2026, 7, 24), date(2026, 7, 28)

    def _pkg(self, start=START, end=None):
        return Package(estimated_arrival=start, estimated_arrival_end=end)

    def test_single_day_still_ahead(self):
        line = _estimate_line(self._pkg(), date(2026, 7, 22))
        self.assertEqual(line, "viernes 24 de julio")
        self.assertEqual(_estimate_note(self._pkg(), date(2026, 7, 22)), "")

    def test_single_day_missed(self):
        today = date(2026, 7, 25)
        self.assertEqual(_estimate_line(self._pkg(), today),
                         "se esperaba el viernes 24 de julio · con retraso")
        self.assertEqual(_estimate_note(self._pkg(), today), "con retraso")

    def test_window_still_ahead_names_both_ends(self):
        pkg = self._pkg(end=self.END)
        self.assertEqual(
            _estimate_line(pkg, date(2026, 7, 22)),
            "entre el viernes 24 de julio y el martes 28 de julio",
        )
        # On the first day of the window it's still a promise, not a delay.
        self.assertEqual(_estimate_line(pkg, self.START),
                         "entre el viernes 24 de julio y el martes 28 de julio")
        self.assertEqual(_estimate_note(pkg, self.START), "")

    def test_window_already_running_counts_from_today(self):
        pkg = self._pkg(end=self.END)
        today = date(2026, 7, 25)
        self.assertEqual(_estimate_line(pkg, today), "entre hoy y el martes 28 de julio")
        self.assertEqual(_estimate_note(pkg, today), "hasta el 28 jul")

    def test_window_overrun_switches_to_the_past_tense(self):
        pkg = self._pkg(end=self.END)
        today = date(2026, 7, 29)
        self.assertEqual(
            _estimate_line(pkg, today),
            "se esperaba entre el viernes 24 de julio y el martes 28 de julio · con retraso",
        )
        self.assertEqual(_estimate_note(pkg, today), "con retraso")

    def test_window_across_two_months_keeps_the_month_on_both_ends(self):
        # "entre el 28 y el 2" would read as a day already gone: the month is
        # never dropped, in the sentence or in the chip's parenthetical.
        pkg = self._pkg(date(2026, 7, 28), date(2026, 8, 2))
        today = date(2026, 7, 30)
        self.assertEqual(_estimate_line(pkg, today), "entre hoy y el domingo 2 de agosto")
        self.assertEqual(_estimate_note(pkg, today), "hasta el 2 ago")

    def test_no_estimate_says_nothing(self):
        self.assertEqual(_estimate_line(Package(), date(2026, 7, 25)), "")
        self.assertEqual(_estimate_note(Package(), date(2026, 7, 25)), "")


class CalendarViewTests(TestCase):
    """The calendar's rendering rules: the unknown-item placeholder and the one
    consolidated chip that stands in for a day's whole pickup haul."""

    def _point(self, name, kind):
        return PickupPoint.objects.create(name=name, kind=kind)

    def _picked(self, point, description, day):
        return Package.objects.create(
            pickup_point=point, state=Package.State.PICKED_UP,
            picked_up_on=day, description=description,
        )

    def test_carrier_pickup_gets_the_action_needed_chip(self):
        # A UPS delivery attempt (PickupPoint.Kind.CARRIER) has no known
        # deadline, but must never look like the calm, business-as-usual
        # "waiting" chip a normal Amazon/alt-store pickup gets — it needs an
        # active trip today (user, 2026-07-24).
        carrier = self._point("UPS", PickupPoint.Kind.CARRIER)
        pkg = Package.objects.create(
            pickup_point=carrier, state=Package.State.AWAITING_PICKUP,
            description="ONES Funda Magnética", carrier="UPS",
        )
        html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
        self.assertIn(b'is-action_needed"', html)
        self.assertNotIn(b'is-waiting"', html)
        self.assertIn("Recoger ya".encode(), html)

        detail = self.client.get(reverse("package_detail", args=[pkg.pk])).content
        self.assertIn("Recoger ya en el transportista".encode(), detail)

    def test_alt_store_awaiting_pickup_keeps_the_calm_waiting_chip(self):
        # Regression guard: the alt store also has no deadline, but it's mild
        # (a per-package €1, more after a while) — it must keep the original
        # passive "waiting" chip, not the carrier's louder one.
        store = self._point("Tienda de juguetes", PickupPoint.Kind.ALT_STORE)
        Package.objects.create(
            pickup_point=store, state=Package.State.AWAITING_PICKUP,
            description="Juguete",
        )
        html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
        self.assertIn(b'is-waiting"', html)
        self.assertNotIn(b'is-action_needed"', html)

    def test_pepe_y_dalda_chip_walks_forward_in_its_own_colour(self):
        # No deadline to go red about, so the urgency is the chip itself:
        # redrawn on today every day it isn't collected, with a note saying
        # how long it's been sitting there (user, 2026-07-25). And its own
        # source colour — Pepe y Dalda is a category beside Amazon and
        # "Otros", not a flavour of either.
        today = timezone.localdate()
        shop = self._point("Juguetes Pepe y Dalda · c/Regència d'Urgell, 17",
                           PickupPoint.Kind.PEPE_Y_DALDA)
        Package.objects.create(
            pickup_point=shop, state=Package.State.AWAITING_PICKUP,
            description="Carta para Marina", recipient="Marina",
            item_kind=Package.ItemKind.LETTER,
            actual_arrival=today - timedelta(days=3),
        )
        html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
        # The chip itself, not just the stylesheet that travels with it: its
        # own hue, and not the "Otros" bucket's.
        self.assertIn(b'class="pkg src-pepe is-waiting', html)
        self.assertNotIn(b'class="pkg src-store', html)
        # The day count — except on a Monday, when the shop is shut and the
        # note says so instead (its own test, below). Reading `today` and
        # then asserting a fixed note made this fail every Monday.
        self.assertIn(b"Listo (cerrado hoy)" if today.weekday() == 0
                       else b"Listo (3 d\xc3\xadas)", html)
        # Drawn on today, not on the day it arrived three days ago.
        day = self.client.get(
            reverse("day_detail", args=[today.isoformat()])).content
        self.assertIn("Carta para Marina".encode(), day)

    def test_freshly_arrived_deadline_less_package_says_no_days(self):
        # Day one: the chip's own position says everything, so the note stays
        # out of the way until there's something to nag about.
        today = timezone.localdate()
        shop = self._point("Pepe y Dalda", PickupPoint.Kind.PEPE_Y_DALDA)
        Package.objects.create(
            pickup_point=shop, state=Package.State.AWAITING_PICKUP,
            description="Paquete para Javier", actual_arrival=today,
        )
        html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
        self.assertIn(b"src-pepe", html)
        self.assertNotIn(b"d\xc3\xadas)", html)

    def _pepe_waiting(self, arrived_days_ago=2):
        shop = self._point("Juguetes Pepe y Dalda · c/Regència d'Urgell, 17",
                           PickupPoint.Kind.PEPE_Y_DALDA)
        return Package.objects.create(
            pickup_point=shop, state=Package.State.AWAITING_PICKUP,
            description="Carta para Marina", recipient="Marina",
            item_kind=Package.ItemKind.LETTER,
            actual_arrival=timezone.localdate() - timedelta(days=arrived_days_ago),
        )

    def _on_monday(self, view="month"):
        """The board as it looks on a Monday. The Pepe chip always rides on
        today, so the shop's closing day is reached by moving "today", not by
        navigating the calendar."""
        monday = timezone.localdate()
        monday += timedelta(days=(7 - monday.weekday()) % 7 or 7)
        return monday

    def test_pepe_y_dalda_chip_warns_on_the_monday_it_is_shut(self):
        # The trap day (user, 2026-07-25): the chip still says "Listo", so
        # without a mark on it the calendar plans a trip to a shuttered door.
        # The day count gives way — how long it's waited is a nudge, "you
        # can't fetch it today" is a fact, and only one fits on a chip.
        pkg = self._pepe_waiting()
        monday = self._on_monday()
        with patch("packages.views.timezone.localdate", return_value=monday):
            html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
            card = self.client.get(
                reverse("package_detail", args=[pkg.pk])).content
        # The chip's own class, not the stylesheet that travels with it.
        self.assertIn(b"is-waiting pkg-closed", html)
        self.assertIn(b"Listo (cerrado hoy)", html)
        self.assertNotIn(b"d\xc3\xadas)", html)  # the count stepped aside
        self.assertIn("⚠ Hoy es lunes: Pepe y Dalda está cerrado.".encode(), card)

    def test_pepe_y_dalda_chip_is_calm_the_rest_of_the_week(self):
        pkg = self._pepe_waiting()
        tuesday = self._on_monday() + timedelta(days=1)
        with patch("packages.views.timezone.localdate", return_value=tuesday):
            html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
            card = self.client.get(
                reverse("package_detail", args=[pkg.pk])).content
        self.assertNotIn(b"is-waiting pkg-closed", html)
        self.assertNotIn(b"cerrado hoy", html)
        # The card still says when the shop shuts — quietly, no warning box.
        self.assertIn("Pepe y Dalda cierra los domingos y los lunes.".encode(), card)
        self.assertNotIn(b'class="modal-note warn"', card)

    def test_a_collected_package_gets_no_closing_warning(self):
        # Nothing left to plan: the shop's hours are trivia on a done row.
        shop = self._point("Juguetes Pepe y Dalda", PickupPoint.Kind.PEPE_Y_DALDA)
        monday = self._on_monday()
        pkg = Package.objects.create(
            pickup_point=shop, state=Package.State.PICKED_UP,
            description="Carta para Marina", picked_up_on=monday)
        with patch("packages.views.timezone.localdate", return_value=monday):
            html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
            card = self.client.get(
                reverse("package_detail", args=[pkg.pk])).content
        self.assertNotIn(b"is-waiting pkg-closed", html)
        self.assertNotIn(b"cerrado", card)

    def test_a_monday_never_warns_about_any_other_point(self):
        # The closing day belongs to one shop, not to the calendar.
        counter = self._point("Amazon Counter - Les Mesures",
                              PickupPoint.Kind.AMAZON_COUNTER)
        store = self._point("Otra tienda", PickupPoint.Kind.ALT_STORE)
        monday = self._on_monday()
        for point in (counter, store):
            Package.objects.create(
                pickup_point=point, state=Package.State.AWAITING_PICKUP,
                description="Algo", actual_arrival=monday - timedelta(days=1))
        with patch("packages.views.timezone.localdate", return_value=monday):
            html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
        self.assertNotIn(b"is-waiting pkg-closed", html)
        self.assertIn(b"Listo (1 d\xc3\xada)", html)

    def test_pepe_y_dalda_card_names_the_type_and_the_recipient(self):
        # The two things the notice carries that nothing else does — and the
        # recipient is what has to be said out loud at the counter.
        shop = self._point("Juguetes Pepe y Dalda · c/Regència d'Urgell, 17",
                           PickupPoint.Kind.PEPE_Y_DALDA)
        pkg = Package.objects.create(
            pickup_point=shop, state=Package.State.AWAITING_PICKUP,
            description="Carta para Marina", recipient="Marina",
            item_kind=Package.ItemKind.LETTER,
        )
        html = self.client.get(
            reverse("package_detail", args=[pkg.pk])).content
        self.assertIn(b"Tipo", html)
        self.assertIn(b"Carta", html)
        self.assertIn(b"Destinatario", html)
        self.assertIn(b"Marina", html)
        # The shop's own signature is the point label, address included.
        self.assertIn("Regència".encode(), html)

    def test_amazon_card_never_asks_parcel_or_letter(self):
        # Every non-Pepe row would answer "Paquete", which is noise.
        counter = self._point("Amazon Counter - Les Mesures",
                              PickupPoint.Kind.AMAZON_COUNTER)
        pkg = Package.objects.create(
            pickup_point=counter, state=Package.State.AWAITING_PICKUP,
            description="Funda de móvil",
        )
        html = self.client.get(
            reverse("package_detail", args=[pkg.pk])).content
        self.assertNotIn(b"<b>Tipo</b>", html)

    def test_same_day_pickups_collapse_into_one_chip(self):
        # Two things picked up the same day, at different points: the month view
        # has no room for a chip each, so they become one "N productos" recap
        # chip that opens the day's consolidated card.
        today = timezone.localdate()
        counter = self._point("Amazon Counter - Les Mesures",
                              PickupPoint.Kind.AMAZON_COUNTER)
        locker = self._point("Amazon Locker - cebolla",
                             PickupPoint.Kind.AMAZON_LOCKER)
        self._picked(counter, "Mantel de flores", today)
        self._picked(locker, "Funda de móvil", today)

        html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
        self.assertEqual(html.count(b'is-picked"'), 1)  # one chip, not two
        self.assertIn(b"2 productos", html)
        self.assertIn(reverse("picked_detail", args=[today.isoformat()]).encode(), html)

    def test_single_pickup_keeps_its_own_chip(self):
        today = timezone.localdate()
        counter = self._point("Amazon Counter - Les Mesures",
                              PickupPoint.Kind.AMAZON_COUNTER)
        pkg = self._picked(counter, "Mantel de flores", today)

        html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
        self.assertEqual(html.count(b'is-picked"'), 1)
        # A lone pickup still opens its own single-package card, not the recap.
        self.assertIn(reverse("package_detail", args=[pkg.pk]).encode(), html)
        self.assertNotIn(
            reverse("picked_detail", args=[today.isoformat()]).encode(), html)

    def test_picked_detail_lists_every_item_of_the_day(self):
        today = timezone.localdate()
        counter = self._point("Amazon Counter - Les Mesures",
                              PickupPoint.Kind.AMAZON_COUNTER)
        locker = self._point("Amazon Locker - cebolla",
                             PickupPoint.Kind.AMAZON_LOCKER)
        self._picked(counter, "Mantel de flores", today)
        self._picked(locker, "Funda de móvil", today)

        html = self.client.get(
            reverse("picked_detail", args=[today.isoformat()])).content
        self.assertIn(b"Mantel de flores", html)
        self.assertIn("Funda de móvil".encode(), html)

    def _delivered(self, point, description, day):
        return Package.objects.create(
            pickup_point=point, state=Package.State.DELIVERED,
            actual_arrival=day, description=description,
        )

    def test_same_day_same_address_deliveries_collapse_into_one_chip(self):
        # Two boxes landing at the same home the same day fold into one
        # "N productos" recap chip, same as the pickup recap.
        today = timezone.localdate()
        home = self._point("Rosa - Can Salgot", PickupPoint.Kind.HOME)
        self._delivered(home, "Mantel de flores", today)
        self._delivered(home, "Funda de móvil", today)

        html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
        self.assertEqual(html.count(b'is-delivered"'), 1)
        self.assertIn(b"2 productos", html)
        self.assertIn(
            reverse("delivered_detail", args=[today.isoformat(), home.pk]).encode(), html)

    def test_same_day_different_address_deliveries_stay_separate(self):
        # Two homes getting packages the same day is rare, and each is a
        # different person to tell what arrived — unlike pickups, these must
        # NOT fold into a single recap chip.
        today = timezone.localdate()
        home1 = self._point("Rosa - Can Salgot", PickupPoint.Kind.HOME)
        home2 = self._point("Padres - Mataró", PickupPoint.Kind.HOME)
        pkg1 = self._delivered(home1, "Mantel de flores", today)
        pkg2 = self._delivered(home2, "Funda de móvil", today)

        html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
        self.assertEqual(html.count(b'is-delivered"'), 2)
        self.assertIn(reverse("package_detail", args=[pkg1.pk]).encode(), html)
        self.assertIn(reverse("package_detail", args=[pkg2.pk]).encode(), html)
        self.assertNotIn(b"productos", html)

    def test_delivered_detail_lists_every_item_of_the_address(self):
        today = timezone.localdate()
        home = self._point("Rosa - Can Salgot", PickupPoint.Kind.HOME)
        self._delivered(home, "Mantel de flores", today)
        self._delivered(home, "Funda de móvil", today)

        html = self.client.get(
            reverse("delivered_detail", args=[today.isoformat(), home.pk])).content
        self.assertIn(b"Mantel de flores", html)
        self.assertIn("Funda de móvil".encode(), html)
        self.assertIn(b"Can Salgot", html)

    def test_unknown_item_shows_placeholder_not_boilerplate(self):
        # A delivered package whose only name is the "N productos | N.º de
        # pedido …" subject boilerplate: the chip shows a clean placeholder and
        # never leaks the order number as if it were a product name.
        today = timezone.localdate()
        home = self._point("Rosa - Can Salgot", PickupPoint.Kind.HOME)
        Package.objects.create(
            pickup_point=home, state=Package.State.DELIVERED, actual_arrival=today,
            description="Entregado: 1 producto | N.º de pedido 404-7963783-4668345",
        )
        html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
        self.assertIn("Producto desconocido".encode(), html)
        self.assertNotIn(b"404-7963783-4668345", html)

    def test_ship_and_arrive_today_merges_into_one_shipped_chip(self):
        # "Enviado hoy, llega hoy": shipping fact and estimated arrival on the
        # same day become a single "Enviado (llega hoy)" chip — one mark, but
        # the arrival is still spelled out where the user looks for it.
        today = timezone.localdate()
        home = self._point("Rosa - Can Salgot", PickupPoint.Kind.HOME)
        Package.objects.create(
            pickup_point=home, state=Package.State.IN_TRANSIT,
            description="ivvi Pill Pockets", shipped_on=today,
            estimated_arrival=today,
        )
        html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
        self.assertEqual(html.count(b'is-shipped"'), 1)
        self.assertEqual(html.count(b'is-estimated"'), 0)
        self.assertIn(b"(llega hoy)", html)

    def test_ship_today_arrive_later_keeps_both_marks(self):
        # The normal case: shipped today, arrives in a few days — the dot and
        # the dashed box sit on different days, both worth showing.
        today = timezone.localdate()
        home = self._point("Rosa - Can Salgot", PickupPoint.Kind.HOME)
        Package.objects.create(
            pickup_point=home, state=Package.State.IN_TRANSIT,
            description="Colchón", shipped_on=today,
            estimated_arrival=today + timedelta(days=3),
        )
        html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
        self.assertEqual(html.count(b'is-shipped"'), 1)
        self.assertEqual(html.count(b'is-estimated"'), 1)

    def test_missed_estimate_rides_on_today(self):
        # Amazon named a day and missed it. The package is still on its way,
        # so the dashed box moves to today instead of sitting in the past,
        # where it would be both false and out of sight (the board is read
        # forwards). The note keeps it from reading as "llega hoy".
        today = timezone.localdate()
        home = self._point("Rosa - Can Salgot", PickupPoint.Kind.HOME)
        promised = today - timedelta(days=2)
        Package.objects.create(
            pickup_point=home, state=Package.State.IN_TRANSIT,
            description="Veebmys Correa", estimated_arrival=promised,
        )
        stale = self.client.get(
            reverse("day_detail", args=[promised.isoformat()])).content
        self.assertNotIn(b"is-estimated", stale)

        html = self.client.get(
            reverse("day_detail", args=[today.isoformat()])).content
        self.assertIn(b"is-estimated", html)
        self.assertIn("(con retraso)".encode(), html)

    def test_running_window_rides_on_today_with_its_end(self):
        # Inside a delivery window: the chip is on today, but says how much
        # slack is left, so a trip isn't planned on the strength of it.
        today = timezone.localdate()
        counter = self._point("Amazon Counter - Les Mesures",
                              PickupPoint.Kind.AMAZON_COUNTER)
        end = today + timedelta(days=3)
        Package.objects.create(
            pickup_point=counter, state=Package.State.IN_TRANSIT,
            description="Veebmys Correa", estimated_arrival=today - timedelta(days=1),
            estimated_arrival_end=end,
        )
        html = self.client.get(
            reverse("day_detail", args=[today.isoformat()])).content.decode()
        self.assertIn("is-estimated", html)
        self.assertIn(f"(hasta el {date_format(end, 'j b')})", html)

    def test_deadline_preview_moves_with_a_missed_estimate(self):
        # The forecast hangs off the arrival day we currently believe. Left on
        # the missed estimate it would paint red dashed boxes in the past —
        # the same incongruence, one step down the chain.
        today = timezone.localdate()
        counter = self._point("Amazon Counter - Les Mesures",
                              PickupPoint.Kind.AMAZON_COUNTER)
        Package.objects.create(
            pickup_point=counter, state=Package.State.IN_TRANSIT,
            description="Veebmys Correa", estimated_arrival=today - timedelta(days=10),
        )
        stale = self.client.get(reverse(
            "day_detail", args=[(today - timedelta(days=3)).isoformat()])).content
        self.assertNotIn(b"is-leaves_estimated", stale)

        html = self.client.get(reverse(
            "day_detail", args=[(today + timedelta(days=7)).isoformat()])).content
        self.assertIn(b"is-leaves_estimated", html)

    def test_shipped_today_with_a_missed_estimate_keeps_two_chips(self):
        # The "Enviado (llega hoy)" merge is about a promise landing on the
        # day it shipped. An estimate that only reached today by slipping is a
        # weaker statement and keeps its own chip to say so.
        today = timezone.localdate()
        home = self._point("Rosa - Can Salgot", PickupPoint.Kind.HOME)
        Package.objects.create(
            pickup_point=home, state=Package.State.IN_TRANSIT,
            description="Colchón", shipped_on=today,
            estimated_arrival=today - timedelta(days=1),
        )
        html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
        self.assertEqual(html.count(b'is-shipped"'), 1)
        self.assertEqual(html.count(b'is-estimated"'), 1)
        self.assertNotIn("(llega hoy)".encode(), html)

    def test_package_detail_spells_out_the_delivery_window(self):
        today = timezone.localdate()
        counter = self._point("Amazon Counter - Les Mesures",
                              PickupPoint.Kind.AMAZON_COUNTER)
        pkg = Package.objects.create(
            pickup_point=counter, state=Package.State.IN_TRANSIT,
            description="Veebmys Correa", estimated_arrival=today + timedelta(days=1),
            estimated_arrival_end=today + timedelta(days=5),
        )
        html = self.client.get(
            reverse("package_detail", args=[pkg.pk])).content.decode()
        self.assertIn("Llegada estimada", html)
        self.assertIn(f"y el {date_format(today + timedelta(days=5), r'l j \d\e F')}",
                      html)

    def test_in_transit_counter_previews_deadline_and_leaves(self):
        # Before the real "Entregado" email, an in-transit Counter package
        # forecasts its last-safe day and "se va" day from the estimated
        # arrival plus the observed 7-day grace (see _PREVIEW_GRACE_DAYS).
        today = timezone.localdate()
        counter = self._point("Amazon Counter - Les Mesures",
                              PickupPoint.Kind.AMAZON_COUNTER)
        arrival = today + timedelta(days=2)
        Package.objects.create(
            pickup_point=counter, state=Package.State.IN_TRANSIT,
            description="Cargador", estimated_arrival=arrival,
        )
        last_safe = arrival + timedelta(days=6)
        leaves = arrival + timedelta(days=7)

        html = self.client.get(
            reverse("day_detail", args=[last_safe.isoformat()])).content
        self.assertIn(b'is-deadline_estimated"', html)
        self.assertIn("Último día (estimado)".encode(), html)

        html = self.client.get(
            reverse("day_detail", args=[leaves.isoformat()])).content
        self.assertIn(b'is-leaves_estimated"', html)
        self.assertIn("Se va (estimado)".encode(), html)

    def test_in_transit_locker_previews_deadline_and_leaves(self):
        # Same forecast, but a Locker's observed grace is 3 days, not 7.
        today = timezone.localdate()
        locker = self._point("Amazon Locker - cebolla",
                             PickupPoint.Kind.AMAZON_LOCKER)
        arrival = today + timedelta(days=2)
        Package.objects.create(
            pickup_point=locker, state=Package.State.IN_TRANSIT,
            description="Funda de móvil", estimated_arrival=arrival,
        )
        last_safe = arrival + timedelta(days=2)
        leaves = arrival + timedelta(days=3)

        html = self.client.get(
            reverse("day_detail", args=[last_safe.isoformat()])).content
        self.assertIn(b'is-deadline_estimated"', html)

        html = self.client.get(
            reverse("day_detail", args=[leaves.isoformat()])).content
        self.assertIn(b'is-leaves_estimated"', html)

    def test_home_and_alt_store_get_no_deadline_preview(self):
        # Neither ever has a real deadline, so forecasting one would be
        # actively misleading — no preview chips at all, on any day.
        today = timezone.localdate()
        home = self._point("Rosa - Can Salgot", PickupPoint.Kind.HOME)
        store = self._point("Juguetería", PickupPoint.Kind.ALT_STORE)
        arrival = today + timedelta(days=2)
        Package.objects.create(
            pickup_point=home, state=Package.State.IN_TRANSIT,
            description="Mantel", estimated_arrival=arrival,
        )
        Package.objects.create(
            pickup_point=store, state=Package.State.IN_TRANSIT,
            description="Puzzle", estimated_arrival=arrival,
        )
        for offset in range(0, 10):
            day = arrival + timedelta(days=offset)
            html = self.client.get(
                reverse("day_detail", args=[day.isoformat()])).content
            self.assertNotIn(b"is-deadline_estimated", html)
            self.assertNotIn(b"is-leaves_estimated", html)

    def test_preview_disappears_once_the_real_deadline_lands(self):
        # Once the "Entregado" email arrives (awaiting_pickup, real deadline
        # set), the forecast is superseded, not stacked alongside the real
        # waiting/deadline/leaves marks.
        today = timezone.localdate()
        counter = self._point("Amazon Counter - Les Mesures",
                              PickupPoint.Kind.AMAZON_COUNTER)
        Package.objects.create(
            pickup_point=counter, state=Package.State.AWAITING_PICKUP,
            description="Cargador", estimated_arrival=today - timedelta(days=1),
            actual_arrival=today, deadline=today + timedelta(days=7),
        )
        html = self.client.get(
            reverse("day_detail", args=[today.isoformat()])).content
        self.assertNotIn(b"is-deadline_estimated", html)
        self.assertNotIn(b"is-leaves_estimated", html)

    def test_package_detail_shows_preview_deadline_while_in_transit(self):
        today = timezone.localdate()
        counter = self._point("Amazon Counter - Les Mesures",
                              PickupPoint.Kind.AMAZON_COUNTER)
        arrival = today + timedelta(days=2)
        pkg = Package.objects.create(
            pickup_point=counter, state=Package.State.IN_TRANSIT,
            description="Cargador", estimated_arrival=arrival,
        )
        html = self.client.get(
            reverse("package_detail", args=[pkg.pk])).content.decode()
        self.assertIn("previsión", html)

    def test_defaults_to_fortnight_view(self):
        # No explicit view: defaults to fortnight agenda for both desktop and phone.
        phone = self.client.get(
            reverse("home"), HTTP_HX_REQUEST="true",
            HTTP_USER_AGENT="Mozilla/5.0 (Linux; Android 16; SM-S936B) Mobile Safari")
        self.assertIn(b"view-fortnight", phone.content)
        desktop = self.client.get(reverse("home"), HTTP_HX_REQUEST="true")
        self.assertIn(b"view-fortnight", desktop.content)
        explicit = self.client.get(reverse("home") + "?view=month",
                                   HTTP_HX_REQUEST="true", HTTP_USER_AGENT="Mobile")
        self.assertIn(b"view-month", explicit.content)

    def test_day_cell_opens_the_day_modal(self):
        # A day with chips carries the day-detail URL: the whole cell is the
        # tap target that blows the day up into the modal.
        today = timezone.localdate()
        counter = self._point("Amazon Counter - Les Mesures",
                              PickupPoint.Kind.AMAZON_COUNTER)
        self._picked(counter, "Mantel de flores", today)
        html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
        self.assertIn(reverse("day_detail", args=[today.isoformat()]).encode(), html)

    def test_day_detail_lists_chips_with_a_way_back(self):
        # The day modal names the day's packages, and each chip's URL carries
        # from_day so the package card can draw its ‹ back to the day.
        today = timezone.localdate()
        counter = self._point("Amazon Counter - Les Mesures",
                              PickupPoint.Kind.AMAZON_COUNTER)
        pkg = self._picked(counter, "Mantel de flores", today)
        html = self.client.get(
            reverse("day_detail", args=[today.isoformat()])).content
        self.assertIn("Mantel de flores".encode(), html)
        want = f"{reverse('package_detail', args=[pkg.pk])}?from_day={today.isoformat()}"
        self.assertIn(want.encode(), html)

    def test_day_detail_rejects_a_bad_date(self):
        self.assertEqual(self.client.get("/day/not-a-date/").status_code, 404)

    def test_package_detail_from_day_offers_the_way_back(self):
        today = timezone.localdate()
        counter = self._point("Amazon Counter - Les Mesures",
                              PickupPoint.Kind.AMAZON_COUNTER)
        pkg = self._picked(counter, "Mantel de flores", today)
        with_back = self.client.get(reverse("package_detail", args=[pkg.pk]),
                                    {"from_day": today.isoformat()}).content
        self.assertIn(reverse("day_detail", args=[today.isoformat()]).encode(),
                      with_back)
        bare = self.client.get(reverse("package_detail", args=[pkg.pk])).content
        self.assertNotIn(b"modal-prev", bare)

    def test_shipped_sorts_before_estimated_on_a_shared_day(self):
        # Two different packages marking the same day: the certain "Enviado"
        # must read before the "Estimado" guess.
        today = timezone.localdate()
        home = self._point("Rosa - Can Salgot", PickupPoint.Kind.HOME)
        Package.objects.create(
            pickup_point=home, state=Package.State.IN_TRANSIT,
            description="Recién enviado", shipped_on=today,
        )
        Package.objects.create(
            pickup_point=home, state=Package.State.IN_TRANSIT,
            description="Solo estimado", estimated_arrival=today,
        )
        html = self.client.get(
            reverse("home"), HTTP_HX_REQUEST="true").content.decode()
        self.assertLess(html.index("is-shipped"), html.index("is-estimated"))

    @override_settings(STORAGES={
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })  # the full-page branch renders the topbar's {% static %} logo, which
        # needs a collectstatic manifest this dev/test environment doesn't have
    def test_history_restore_request_gets_the_full_page_not_a_fragment(self):
        # htmx tags a post-cache-miss browser-back request with HX-Request
        # too, but replaces the *whole document* with the response — serving
        # it the bare #app-view fragment renders as raw, chromeless HTML.
        fragment = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
        self.assertNotIn(b"<!doctype html>", fragment)

        restored = self.client.get(
            reverse("home"), HTTP_HX_REQUEST="true",
            HTTP_HX_HISTORY_RESTORE_REQUEST="true").content
        self.assertIn(b"<!doctype html>", restored)
        self.assertIn(b"app-topbar", restored)


class ManualPickupTests(TestCase):
    """The manual "ya lo he recogido" confirmation: the way out for the one
    pickup no email ever closes — a home delivery that failed and got
    diverted to a carrier's office, which leaves Amazon's lifecycle for good
    ("Entregado" on their side, nothing more ever sent)."""

    def setUp(self):
        self.today = timezone.localdate()
        self.carrier = PickupPoint.objects.create(
            name="UPS", kind=PickupPoint.Kind.CARRIER)
        self.pkg = Package.objects.create(
            pickup_point=self.carrier, state=Package.State.AWAITING_PICKUP,
            description="ONES Funda Magnética", carrier="UPS",
            actual_arrival=self.today - timedelta(days=3),
        )

    def _elsewhere(self, kind):
        point = PickupPoint.objects.create(name=f"Punto {kind}", kind=kind)
        return Package.objects.create(
            pickup_point=point, state=Package.State.AWAITING_PICKUP,
            description="Otro paquete")

    def test_carrier_pickup_offers_the_button(self):
        html = self.client.get(
            reverse("package_detail", args=[self.pkg.pk])).content
        self.assertIn(reverse("confirm_pickup", args=[self.pkg.pk]).encode(), html)

    def test_pepe_y_dalda_pickup_offers_the_button(self):
        # The shop's other no-email half: it says when something arrives and
        # never again, so the row only closes when the user says so.
        shop = PickupPoint.objects.create(
            name="Juguetes Pepe y Dalda", kind=PickupPoint.Kind.PEPE_Y_DALDA)
        pkg = Package.objects.create(
            pickup_point=shop, state=Package.State.AWAITING_PICKUP,
            description="Carta para Marina",
            actual_arrival=self.today - timedelta(days=2))
        html = self.client.get(reverse("package_detail", args=[pkg.pk])).content
        self.assertIn(reverse("confirm_pickup", args=[pkg.pk]).encode(), html)

        response = self.client.post(
            reverse("confirm_pickup", args=[pkg.pk]),
            {"picked_up_on": (self.today - timedelta(days=1)).isoformat()})
        pkg.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(pkg.state, Package.State.PICKED_UP)
        self.assertEqual(pkg.picked_up_on, self.today - timedelta(days=1))

    def test_every_other_pickup_stays_email_driven(self):
        # Scoped to the two no-email cases on purpose (user, 2026-07-25 —
        # the carrier's office, and Pepe y Dalda above): an Amazon
        # locker/counter closes itself from the "Se ha recogido" email, and
        # the alt store stays admin-only.
        for kind in (PickupPoint.Kind.AMAZON_LOCKER,
                     PickupPoint.Kind.AMAZON_COUNTER,
                     PickupPoint.Kind.ALT_STORE):
            other = self._elsewhere(kind)
            url = reverse("confirm_pickup", args=[other.pk])
            html = self.client.get(
                reverse("package_detail", args=[other.pk])).content
            self.assertNotIn(url.encode(), html, kind)
            self.assertEqual(self.client.get(url).status_code, 404, kind)

    def test_terminal_package_offers_nothing_to_confirm(self):
        self.pkg.state = Package.State.PICKED_UP
        self.pkg.save()
        html = self.client.get(
            reverse("package_detail", args=[self.pkg.pk])).content
        self.assertNotIn(reverse("confirm_pickup", args=[self.pkg.pk]).encode(), html)
        # And the URL itself is closed, not just hidden.
        self.assertEqual(
            self.client.get(reverse("confirm_pickup", args=[self.pkg.pk])).status_code,
            404)

    def test_get_asks_for_the_day_without_changing_anything(self):
        html = self.client.get(
            reverse("confirm_pickup", args=[self.pkg.pk])).content
        self.assertIn(b'name="picked_up_on"', html)
        self.assertIn(f'value="{self.today.isoformat()}"'.encode(), html)
        # Bounded by the input too: never the future, never before it arrived.
        self.assertIn(f'max="{self.today.isoformat()}"'.encode(), html)
        self.assertIn(f'min="{self.pkg.actual_arrival.isoformat()}"'.encode(), html)
        self.pkg.refresh_from_db()
        self.assertEqual(self.pkg.state, Package.State.AWAITING_PICKUP)

    def test_confirming_a_past_day_files_the_pickup_on_that_day(self):
        # The whole point of the dialog: picked up yesterday, confirmed today.
        yesterday = self.today - timedelta(days=1)
        response = self.client.post(
            reverse("confirm_pickup", args=[self.pkg.pk]),
            {"picked_up_on": yesterday.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.pkg.refresh_from_db()
        self.assertEqual(self.pkg.state, Package.State.PICKED_UP)
        self.assertEqual(self.pkg.picked_up_on, yesterday)
        # The card comes back updated, and the stale chip behind it refreshes.
        self.assertIn(b"Recogido", response.content)
        self.assertEqual(response["HX-Trigger"], "package-updated")

        html = self.client.get(reverse("home"), HTTP_HX_REQUEST="true").content
        self.assertIn(b'is-picked"', html)
        self.assertNotIn(b'is-action_needed"', html)

    def test_a_future_day_is_refused(self):
        response = self.client.post(
            reverse("confirm_pickup", args=[self.pkg.pk]),
            {"picked_up_on": (self.today + timedelta(days=1)).isoformat()})
        self.assertIn("futuro".encode(), response.content)
        self.assertNotIn("HX-Trigger", response)
        self.pkg.refresh_from_db()
        self.assertEqual(self.pkg.state, Package.State.AWAITING_PICKUP)

    def test_a_day_before_it_arrived_is_refused(self):
        response = self.client.post(
            reverse("confirm_pickup", args=[self.pkg.pk]),
            {"picked_up_on": (self.pkg.actual_arrival - timedelta(days=1)).isoformat()})
        self.assertIn("todavía no estaba".encode(), response.content)
        self.pkg.refresh_from_db()
        self.assertEqual(self.pkg.state, Package.State.AWAITING_PICKUP)

    def test_confirming_starts_the_review_clock(self):
        # A Vine pickup owes a review 30 days later, whether the email or the
        # user reported it (see ingest.set_review_due).
        self.pkg.is_vine = True
        self.pkg.save()
        Review.objects.create(package=self.pkg, product_title=self.pkg.description)
        self.client.post(reverse("confirm_pickup", args=[self.pkg.pk]),
                         {"picked_up_on": self.today.isoformat()})
        review = Review.objects.get(package=self.pkg)
        self.assertEqual(review.due_on, self.today + timedelta(days=30))

    def test_confirming_one_package_never_sweeps_the_point(self):
        # Unlike the email pickup, which sweeps the whole point because the
        # email is unreliable about its own scope: a tap on one card is not.
        # It matters most here: a CARRIER point dedups by carrier name, so two
        # failed deliveries share one "UPS" row while sitting in two different
        # physical offices (see PickupPoint.Kind.CARRIER).
        other = Package.objects.create(
            pickup_point=self.carrier, state=Package.State.AWAITING_PICKUP,
            description="Otro intento fallido", carrier="UPS")
        self.client.post(reverse("confirm_pickup", args=[self.pkg.pk]),
                         {"picked_up_on": self.today.isoformat()})
        other.refresh_from_db()
        self.assertEqual(other.state, Package.State.AWAITING_PICKUP)
