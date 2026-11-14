"""Correos Express: the carrier's own emails, and Amazon's "lo intentarán de nuevo".

Synthesized from the real emails of 2026-11-14 (a Vine order out for delivery,
missed, sent to a pickup shop, collected) rather than stored as .eml: they
carry the user's address. The wording and line breaks are the real ones.
"""

from datetime import date
from email.message import EmailMessage

from django.test import TestCase
from django.urls import reverse

from .ingest import process_message
from .models import Package, PickupPoint, RawEmail
from .parser import EmailKind, parse_email

TRACKING = "9387101068762950"
ORDER = "405-8875146-7531562"


def _eml(subject, lines, when, token=""):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["Message-ID"] = f"<ce-{abs(hash((subject, when)))}@example.com>"
    msg["Date"] = when
    msg.set_content("".join(f"<div>{line}</div>" for line in lines) + token,
                    subtype="html")
    return msg.as_bytes()


def out_for_delivery(subject="Fwd: Correos Express: Tu paquete de AMAZON llegará en las próximas horas."):
    return _eml(subject, [
        "¡TU PAQUETE YA ESTÁ EN CAMINO!", "LLEGARÁ EN LAS PRÓXIMAS HORAS",
        "¡Hola!", "Te informamos que tu", f"envío con número {TRACKING} de AMAZON",
        "será entregado el día 14/11", "La dirección de entrega es",
        "CALLE EJEMPLO 2; 2° C, 28029, MADRID", "Gracias por confiar en Correos Express",
    ], "Sat, 14 Nov 2026 08:54:00 +0100")


MISSED = _eml("Fwd: Aviso de Paso", [
    "Correos Express: destinatario no contesta a las 11:37. Para concertar nueva entrega pulse aqui:",
    f"https://www.cexpr.es/c?n={TRACKING}",
], "Sat, 14 Nov 2026 12:06:00 +0100")

AT_POINT = _eml(f"Fwd: Comunicación Envío: {TRACKING}", [
    "Ya puedes recoger tu pedido :)", "Tienes 7 días para retirarlo", "¡Hola!",
    "Tu envío", TRACKING, "ya está disponible en el Punto:", "ENVIOS YIEN",
    "PLAZA FONSAGRADA, 5, LOCAL 1", ". Recuerda que dispones de",
    "7 días para su retirada", ", si no será devuelto al remitente.", "7966",
], "Sat, 14 Nov 2026 12:54:00 +0100")

PICKED_UP = _eml("Fwd: Correos Express: Tu paquete de BCN8 ha sido recogido", [
    "Tu pedido ha sido entregado :)", "¡Gracias!", "¡Hola!", "Tu envío", TRACKING,
    "ha sido recogido en el Punto:", "ENVIOS YIEN", "PLAZA FONSAGRADA, 5, LOCAL 1",
], "Sat, 14 Nov 2026 16:30:00 +0100")

RETRY = _eml('Intento de entrega realizado: "INIU Batería Externa..."', [
    "Se ha intentado realizar tu entrega",
    "Se ha intentado entregar hoy a las 11:37 a. m.",
    "Lamentablemente, Correos Express ha tenido un problema al intentar realizar la entrega.",
    "Lo intentarán de nuevo.", "Charo - Madrid, Madrid", "Pedido n.º", ORDER,
    "Seguimiento del envío", "INIU Batería Externa 10000mAh par...",
], "Sat, 14 Nov 2026 11:06:53 +0000", token="urn:rtn:msg:20261114110653abc")


class CorreosExpressTests(TestCase):
    def setUp(self):
        home = PickupPoint.objects.create(name="Charo - Madrid, Madrid",
                                          kind=PickupPoint.Kind.HOME)
        self.pkg = Package.objects.create(
            pickup_point=home, order_id=ORDER, state=Package.State.IN_TRANSIT,
            is_vine=True, ordered_on=date(2026, 11, 8), shipped_on=date(2026, 11, 9),
            estimated_arrival=date(2026, 11, 15),
        )

    def test_parses_each_template(self):
        self.assertEqual(parse_email(out_for_delivery()).kind, EmailKind.CARRIER_OUT_FOR_DELIVERY)
        self.assertEqual(parse_email(out_for_delivery()).estimated_arrival, date(2026, 11, 14))
        self.assertEqual(parse_email(MISSED).kind, EmailKind.CARRIER_MISSED)
        self.assertEqual(parse_email(PICKED_UP).kind, EmailKind.CARRIER_PICKED_UP)
        at_point = parse_email(AT_POINT)
        self.assertEqual(at_point.tracking_number, TRACKING)
        self.assertEqual(at_point.pickup_location, "ENVIOS YIEN")
        self.assertEqual(at_point.pickup_address, "PLAZA FONSAGRADA, 5, LOCAL 1")
        self.assertEqual(at_point.pickup_before, date(2026, 11, 21))
        retry = parse_email(RETRY)
        self.assertEqual((retry.kind, retry.carrier, retry.order_id),
                         (EmailKind.DELIVERY_RETRY, "Correos Express", ORDER))

    def test_whole_lifecycle(self):
        process_message(RETRY)
        self.pkg.refresh_from_db()
        self.assertEqual((self.pkg.state, self.pkg.carrier),
                         (Package.State.IN_TRANSIT, "Correos Express"))

        process_message(out_for_delivery())
        self.pkg.refresh_from_db()
        self.assertEqual(self.pkg.state, Package.State.LAST_MILE)
        self.assertEqual(self.pkg.carrier_tracking_number, TRACKING)
        self.assertEqual(self.pkg.estimated_arrival, date(2026, 11, 14))

        process_message(MISSED)
        self.pkg.refresh_from_db()
        self.assertEqual(self.pkg.state, Package.State.LAST_MILE)

        process_message(AT_POINT)
        self.pkg.refresh_from_db()
        self.assertEqual(self.pkg.state, Package.State.AWAITING_PICKUP)
        self.assertEqual(self.pkg.deadline, date(2026, 11, 21))
        point = self.pkg.pickup_point
        self.assertEqual((point.kind, point.name, point.label),
                         (PickupPoint.Kind.ALT_STORE, "ENVIOS YIEN", "ENVIOS YIEN"))
        self.assertIn("ENVIOS+YIEN", point.maps_url)

        process_message(PICKED_UP)
        self.pkg.refresh_from_db()
        self.assertEqual((self.pkg.state, self.pkg.picked_up_on),
                         (Package.State.PICKED_UP, date(2026, 11, 14)))
        self.assertEqual(RawEmail.objects.exclude(parse_error="").count(), 0)

    def test_second_parcel_reuses_the_shop(self):
        process_message(out_for_delivery())
        process_message(AT_POINT)
        other = Package.objects.create(
            pickup_point=PickupPoint.objects.get(kind=PickupPoint.Kind.HOME),
            state=Package.State.IN_TRANSIT, carrier_tracking_number="1111111111111111")
        process_message(_eml("Fwd: Comunicación Envío: 1111111111111111", [
            "Tu envío", "1111111111111111", "ya está disponible en el Punto:",
            "ENVIOS YIEN", "PLAZA FONSAGRADA, 5, LOCAL 1",
            "7 días para su retirada"], "Sat, 14 Nov 2026 18:00:00 +0100"))
        other.refresh_from_db()
        self.assertEqual(PickupPoint.objects.filter(name="ENVIOS YIEN").count(), 1)
        self.assertEqual(other.pickup_point.name, "ENVIOS YIEN")

    def test_ambiguous_claim_fails_loudly(self):
        Package.objects.create(pickup_point=self.pkg.pickup_point, order_id="404-0000000-0000000",
                               state=Package.State.IN_TRANSIT)
        record, _ = process_message(out_for_delivery())
        self.assertIn("único pedido", record.parse_error)
        self.pkg.refresh_from_db()
        self.assertEqual(self.pkg.carrier_tracking_number, "")

    def test_amazon_retry_narrows_an_ambiguous_claim(self):
        Package.objects.create(pickup_point=self.pkg.pickup_point, order_id="404-0000000-0000000",
                               state=Package.State.IN_TRANSIT)
        process_message(RETRY)
        record, _ = process_message(out_for_delivery())
        self.assertEqual(record.parse_error, "")
        self.pkg.refresh_from_db()
        self.assertEqual(self.pkg.carrier_tracking_number, TRACKING)

    def test_unknown_tracking_after_the_first_email_fails_loudly(self):
        record, _ = process_message(AT_POINT)
        self.assertIn("sin paquete asociado", record.parse_error)

    def test_non_amazon_parcel_is_not_claimed(self):
        record, _ = process_message(out_for_delivery("Fwd: Correos Express: Tu paquete de ZARA llegará en las próximas horas."))
        self.assertNotEqual(record.parse_error, "")

    def test_card_shows_order_date(self):
        html = self.client.get(reverse("package_detail", args=[self.pkg.pk])).content.decode()
        self.assertIn("<b>Pedido</b> domingo 8 de noviembre", html)
