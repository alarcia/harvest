"""Parser regression net: every fixture is a real Amazon.es email.

The .eml files under tests/fixtures/ are the archetypes of every known
communication, dumped read-only from the dedicated inbox (`imap_dump`). The
day Amazon changes a template, these tests are what says which extraction
broke — keep one fixture per template, and add one whenever a new template
shows up.
"""

from datetime import date
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings

from .ingest import process_message, scan_inbox
from .models import Package, PickupPoint, RawEmail
from .parser import EmailKind, ParseError, parse_email

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_bytes()


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
        self.assertTrue(parsed.is_vine)
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
        self.assertTrue(parsed.is_vine)

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
        self.assertFalse(parsed.is_vine)
        self.assertEqual(parsed.shipment_id, "TgvslGX9H")

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
