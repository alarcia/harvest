import re
import unicodedata
from decimal import Decimal

from django.db import models


def _fold(text):
    """Case-, accent- and apostrophe-insensitive form of a place name.

    Address lines are typed by three different hands — the user's into an
    Amazon checkout, Amazon's into an email template, the shop's into its own
    signature — and they disagree on exactly the characters Catalan street
    names are made of: "Regència" / "Regencia", "d'Urgell" / "d´Urgell" /
    "d’Urgell" (Amazon prints the acute accent, see the Counter venue line).
    Folding all of it away is what lets one marker match every spelling.
    """
    # Apostrophes first: NFKD decomposes a standalone acute accent (Amazon's
    # "D´URGELL") into a space plus a combining mark, which the next line
    # would then throw away — turning the word into "D URGELL" and quietly
    # failing to match anything.
    text = re.sub(r"[´`'’‘]", "'", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip().casefold()


class Config(models.Model):
    """Single-row table for the few numbers that must change without a deploy.

    Always read through `load()`, which creates the row on first use, so
    nothing has to seed it and a fresh database behaves like a configured one.
    """

    # Sellers outside the EU are charged a fixed EU import duty and, in
    # practice, all of them pass it on to the buyer — so since 2026 a *free*
    # Vine order can print this exact amount instead of 0.00€ (see
    # `means_vine`). The figure is legislation, not code: it will change, hence
    # this row rather than a constant.
    eu_import_surcharge = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("3.63"),
        # Named in Spanish like everything the user reads: the admin is the
        # only UI this table has, and "Eu import surcharge" is not it.
        verbose_name="recargo aduanero de la UE",
        help_text="Recargo aduanero de la UE que los vendedores de fuera de la "
                  "Unión repercuten al cliente. Un pedido Vine con recargo "
                  "cuesta exactamente esta cifra en el correo de envío en lugar "
                  "de 0,00€. Ponlo a 0 para desactivar la excepción.",
    )

    # An Amazon order can be sent to Pepe y Dalda's own street address: the
    # user types it into the checkout like any other delivery address, and the
    # parcel ends up on the shop's counter. Amazon's emails say nothing about
    # that — the destination line is the only tell — so these markers are what
    # turn "a home delivery" into "a Pepe y Dalda package" (see
    # ingest._pickup_point). One per line, matched as a fragment of the
    # destination line, so the street alone catches every way it gets written.
    # In the database rather than in code for the same reason as the surcharge
    # above: the exact wording Amazon prints isn't known until it arrives, and
    # correcting it must not need a deploy.
    pepe_addresses = models.TextField(
        blank=True,
        default="Regència d'Urgell\nPepe y Dalda",
        verbose_name="direcciones que en realidad son Pepe y Dalda",
        help_text="Direcciones que en realidad son Pepe y Dalda, una por "
                  "línea. Un pedido de Amazon enviado a una de ellas se "
                  "clasifica como Pepe y Dalda en lugar de como entrega a "
                  "domicilio. Basta un fragmento de la línea de destino del "
                  "correo (la calle, por ejemplo); no distingue mayúsculas, "
                  "acentos ni apóstrofos. Déjalo vacío para desactivarlo.",
    )

    class Meta:
        verbose_name = "configuración"
        verbose_name_plural = "configuración"

    def __str__(self):
        return "Configuración"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def means_vine(self, total):
        """Does this order total, as printed by Amazon, mean "free Vine item"?

        0.00€ is the classic signal (weak on the Pedido email, authoritative on
        the Enviado — see ingest._apply_cost). The second case is the EU import
        surcharge: a Vine order from a seller outside the Union costs the user
        exactly that surcharge and nothing else, so the total reads e.g. 3.63€
        on an item that is still free. Any *other* amount is a real purchase.

        A paid order that happens to cost exactly the surcharge would be
        misread — accepted: `is_vine` stays editable in the admin, and the
        alternative (missing every surcharged Vine review) is worse.
        """
        if total is None:
            return False
        return total == 0 or (self.eu_import_surcharge > 0
                              and total == self.eu_import_surcharge)

    def is_pepe_address(self, location):
        """Is this Amazon destination line really Pepe y Dalda's counter?

        The line is Amazon's own rendering of a saved address ("Rosa - Can
        Salgot (lliça D'amunt), Barcelona"), so nothing in it is guaranteed —
        which is why the match is a fragment, folded (see `_fold`), against a
        list the user owns. An empty list, or a marker that is only
        whitespace, matches nothing: this must never be the rule that
        swallows every home delivery.
        """
        if not location:
            return False
        haystack = _fold(location)
        markers = (marker.strip() for marker in self.pepe_addresses.splitlines())
        return any(_fold(marker) in haystack for marker in markers if marker)


class PickupPoint(models.Model):
    """Where a package ends up: an Amazon locker/counter, the alt store, or a
    home address (a relative's place). "Pickup point" is a slight misnomer for
    the home case — there's no trip — but it's the same "where does this land"
    slot, so the model stays one table."""

    class Kind(models.TextChoices):
        AMAZON_LOCKER = "amazon_locker", "Amazon Locker"
        AMAZON_COUNTER = "amazon_counter", "Amazon Counter"
        ALT_STORE = "alt_store", "Alternative store"
        # A home/relative address: Amazon delivers and that's the end of it,
        # no pickup trip. The name is the destination line from the email.
        HOME = "home", "Entrega a domicilio"
        # A failed home delivery diverted to a carrier's own office (e.g. UPS
        # after a missed handoff). The email never names the specific office,
        # only the carrier, so this dedups by carrier name, not a real address.
        CARRIER = "carrier", "Transportista"
        # The toy shop that doubles as a delivery address: the user types the
        # store's street address into any e-commerce checkout and the shop
        # emails once the parcel (or letter) is at the counter. Its own
        # category, not the ALT_STORE bucket (user, 2026-07-25): it has its
        # own email lifecycle, its own colour and enough volume to be worth
        # recognizing at a glance. Only ever one row — it's one shop — so
        # ingestion dedups it by kind alone, not by name.
        #
        # The checkout it gets typed into is often Amazon's (user,
        # 2026-07-31), and then the package is an ordinary Amazon shipment
        # that happens to land here: Amazon's own three emails drive it, and
        # only the destination line says where it went (see
        # Config.pepe_addresses). It is a Pepe y Dalda package all the same —
        # that's where the trip goes — so it gets this kind, this colour and
        # this shop's closing days, not a home address's.
        PEPE_Y_DALDA = "pepe_y_dalda", "Pepe y Dalda"

    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # Postal code read from the venue line, used to dedup Amazon Locker/Counter
    # points: Amazon spells the same venue differently across templates (the
    # "Pedido" line reads "Les Mesures, ..., LA SEU D´URGELL, 25700", the
    # "Entregado" line reads "Les Mesures ... LLEIDA , 25700" — same counter,
    # different punctuation and even city vs. province). The postal code is
    # the one token both templates agree on, so it's the dedup key instead of
    # the free-text name. Blank for HOME/ALT_STORE points, which dedup by name.
    location_key = models.CharField(max_length=5, blank=True, db_index=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def is_home(self):
        return self.kind == self.Kind.HOME


class Package(models.Model):
    """One physical package the user goes to pick up — one bar on the calendar.

    We model the package, never the order. Vine items are 1:1, but a regular
    Amazon order that ships as several boxes is several packages, one row each.
    """

    class State(models.TextChoices):
        IN_TRANSIT = "in_transit", "In transit"
        AWAITING_PICKUP = "awaiting_pickup", "Awaiting pickup"
        PICKED_UP = "picked_up", "Picked up"
        # Terminal state for home deliveries: no pickup trip, the "Entregado"
        # email (or its estimated day) is the end of the line.
        DELIVERED = "delivered", "Delivered"
        RETURNED = "returned", "Returned"

    class ItemKind(models.TextChoices):
        """What is actually waiting at the counter. Only Pepe y Dalda makes
        the distinction — their notice is either "Recepción paquete" or
        "Recepción carta" — and it changes what the user goes to fetch, so
        the card says which. Everything else is a parcel, hence the default."""

        PACKAGE = "package", "Paquete"
        LETTER = "letter", "Carta"

    pickup_point = models.ForeignKey(
        PickupPoint, on_delete=models.PROTECT, related_name="packages"
    )
    description = models.CharField(max_length=255, blank=True)
    pickup_code = models.CharField(max_length=20, blank=True)
    item_kind = models.CharField(
        max_length=10, choices=ItemKind.choices, default=ItemKind.PACKAGE
    )
    # Who the delivery is addressed to. Blank everywhere except Pepe y Dalda,
    # where it's the one thing the user has to say out loud at the counter to
    # be handed the right parcel — most are for his wife, some are his.
    recipient = models.CharField(max_length=60, blank=True)

    # Set when a home delivery is diverted to a carrier's office (see
    # PickupPoint.Kind.CARRIER). The delivery-attempt email never carries the
    # carrier's own tracking number (only Amazon's own order links), so it's
    # filled in by hand once looked up; carrier_tracking_url stays blank until
    # then.
    carrier = models.CharField(max_length=32, blank=True)
    carrier_tracking_number = models.CharField(max_length=64, blank=True)

    # Ingestion matching keys. The Amazon order number ("Pedido n.º") groups
    # every email of a lifecycle; the shipment id pins the box when an order
    # splits into several packages. Blank for alt-store (manual) packages.
    order_id = models.CharField(max_length=32, blank=True, db_index=True)
    shipment_id = models.CharField(max_length=32, blank=True, db_index=True)

    # Detail-view extras read from the emails. image_url is the product
    # thumbnail; barcode_url is the static image scanned at the counter.
    asin = models.CharField(max_length=16, blank=True)
    image_url = models.URLField(max_length=500, blank=True)
    barcode_url = models.URLField(max_length=500, blank=True)

    # Lifecycle event days, for painting the ○/●/✓ marks on their real dates.
    ordered_on = models.DateField(null=True, blank=True)
    shipped_on = models.DateField(null=True, blank=True)
    picked_up_on = models.DateField(null=True, blank=True)

    # Vine (free-in-exchange-for-a-review) items are flagged at ingestion from
    # the printed total (€0.00, or exactly the EU import surcharge — see
    # Config.means_vine); for now the flag is all the calendar needs.
    is_vine = models.BooleanField(default=False)
    cost = models.DecimalField(max_digits=6, decimal_places=2, default=0)

    state = models.CharField(
        max_length=20, choices=State.choices, default=State.IN_TRANSIT
    )

    # The three calendar dates. estimated -> dashed line, actual -> solid line,
    # deadline -> red border. The deadline is read from the email, never
    # calculated, and is null for the alt store (no deadline).
    estimated_arrival = models.DateField(null=True, blank=True)
    # Some "Pedido" emails give a delivery *window* ("Llegada entre el 24 de
    # julio y el 28 de julio") instead of a single day. estimated_arrival holds
    # the start of that window, this the end; null means a single-day estimate,
    # which is the common case. Only the wording ever reads it — the chip rides
    # on the start (and then on today, see views._effective_estimate). Cleared
    # by any later email carrying a firm single date, the "Enviado" above all.
    estimated_arrival_end = models.DateField(null=True, blank=True)
    actual_arrival = models.DateField(null=True, blank=True)
    deadline = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-actual_arrival", "-estimated_arrival"]

    def __str__(self):
        return self.description or f"Package #{self.pk}"

    @property
    def carrier_tracking_url(self):
        """Direct link to the carrier's own locator (office + hours) — the
        one thing the delivery-attempt email never provides. Only UPS is
        wired up; extend with an elif once another carrier's format is
        confirmed against a real email, per the CARRIER kind's docstring."""
        if self.carrier == "UPS" and self.carrier_tracking_number:
            return f"https://www.ups.com/track?loc=es_ES&tracknum={self.carrier_tracking_number}"
        return ""

    @property
    def amazon_tracking_url(self):
        """Direct link to Amazon's own page for this package — simplified from
        the "Seguimiento del envío" button in the email, which wraps this
        same destination in a one-time click-tracking redirect
        (gp/r.html?...&U=<this, url-encoded>&...) that isn't worth storing.

        Offered on every Amazon package whatever its state (2026-07-25): it
        started life as the carrier pickup's only way to check status, but the
        link is just as useful anywhere — it opens the Amazon app on the phone.
        Built from ids we already store, so nothing new has to be collected.
        The tracker needs the shipment id to pin the box; the emails that only
        carry an order number (a consolidated "Se ha recogido", a Pedido not
        yet shipped) fall back to the order page, which is one tap away from
        the same tracking.

        The order id is what makes a package Amazon's, not where it lands
        (2026-07-31): an Amazon order delivered to Pepe y Dalda's counter is
        still an Amazon order and still worth tracking. The two ways a row is
        born without one — the shop's own "Recepción…" notice and manual
        alt-store entry — are exactly the ones with nothing to link to."""
        if not self.order_id:
            return ""
        if self.shipment_id:
            return (
                "https://www.amazon.es/progress-tracker/package"
                f"?_encoding=UTF8&orderId={self.order_id}"
                f"&packageIndex=0&shipmentId={self.shipment_id}"
            )
        return (
            "https://www.amazon.es/gp/your-account/order-details"
            f"?_encoding=UTF8&orderID={self.order_id}"
        )


class RawEmail(models.Model):
    """The raw email as received, stored before parsing.

    Kept so the whole history can be reprocessed once the parser improves.
    Populated by the ingestion pipeline.
    """

    message_id = models.CharField(max_length=255, unique=True)
    subject = models.CharField(max_length=255, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    raw = models.TextField()
    processed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    # Outcome of the parse. A non-empty parse_error is what the calendar
    # surfaces as the red banner: never silently dropped. `kind` is the
    # parser's EmailKind value; `note` says what ingestion did with it.
    kind = models.CharField(max_length=32, blank=True)
    parse_error = models.TextField(blank=True)
    note = models.CharField(max_length=255, blank=True)
    package = models.ForeignKey(
        "Package", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="emails",
    )

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return self.subject or self.message_id
