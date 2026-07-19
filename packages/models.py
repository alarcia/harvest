from django.db import models


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
    def is_amazon(self):
        return self.kind != self.Kind.ALT_STORE

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

    pickup_point = models.ForeignKey(
        PickupPoint, on_delete=models.PROTECT, related_name="packages"
    )
    description = models.CharField(max_length=255, blank=True)
    pickup_code = models.CharField(max_length=20, blank=True)

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
    # a cost of €0.00; for now the flag is all the calendar needs.
    is_vine = models.BooleanField(default=False)
    cost = models.DecimalField(max_digits=6, decimal_places=2, default=0)

    state = models.CharField(
        max_length=20, choices=State.choices, default=State.IN_TRANSIT
    )

    # The three calendar dates. estimated -> dashed line, actual -> solid line,
    # deadline -> red border. The deadline is read from the email, never
    # calculated, and is null for the alt store (no deadline).
    estimated_arrival = models.DateField(null=True, blank=True)
    actual_arrival = models.DateField(null=True, blank=True)
    deadline = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-actual_arrival", "-estimated_arrival"]

    def __str__(self):
        return self.description or f"Package #{self.pk}"


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
