"""Parser regression net: every fixture is a real Amazon.es email.

The .eml files under tests/fixtures/ are the archetypes of every known
communication, dumped read-only from the dedicated inbox (`imap_dump`). The
day Amazon changes a template, these tests are what says which extraction
broke — keep one fixture per template, and add one whenever a new template
shows up.
"""

from datetime import date, timedelta
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .ingest import process_message, reprocess_failures, scan_inbox
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

    def test_pickup_reminder(self):
        # "Recordatorio: Paquete en espera de recogida" — a nag that a package
        # is still waiting. Recognized as its own kind so it never trips the
        # unknown-email alarm, and ingestion drives no transition from it.
        parsed = parse_email(
            fixture("022-recordatorio-paquete-en-espera-de-recogida.eml")
        )
        self.assertEqual(parsed.kind, EmailKind.PICKUP_REMINDER)
        self.assertEqual(parsed.order_id, "407-2753653-0825928")

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

    def test_phone_defaults_to_fortnight_desktop_to_month(self):
        # No explicit view: a phone UA ("Mobi", per MDN) opens the fortnight
        # agenda — this week's trip and the next one's — while anything else
        # keeps the month overview. An explicit choice beats the sniff.
        phone = self.client.get(
            reverse("home"), HTTP_HX_REQUEST="true",
            HTTP_USER_AGENT="Mozilla/5.0 (Linux; Android 16; SM-S936B) Mobile Safari")
        self.assertIn(b"view-fortnight", phone.content)
        desktop = self.client.get(reverse("home"), HTTP_HX_REQUEST="true")
        self.assertIn(b"view-month", desktop.content)
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
