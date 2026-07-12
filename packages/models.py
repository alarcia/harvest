from django.db import models


class PickupPoint(models.Model):
    """Where a package is collected: an Amazon locker/counter or the alt store."""

    class Kind(models.TextChoices):
        AMAZON_LOCKER = "amazon_locker", "Amazon Locker"
        AMAZON_COUNTER = "amazon_counter", "Amazon Counter"
        ALT_STORE = "alt_store", "Alternative store"

    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=20, choices=Kind.choices)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def is_amazon(self):
        return self.kind in {self.Kind.AMAZON_LOCKER, self.Kind.AMAZON_COUNTER}


class Package(models.Model):
    """One physical package the user goes to pick up — one bar on the calendar.

    We model the package, never the order. Vine items are 1:1, but a regular
    Amazon order that ships as several boxes is several packages, one row each.
    """

    class State(models.TextChoices):
        IN_TRANSIT = "in_transit", "In transit"
        AWAITING_PICKUP = "awaiting_pickup", "Awaiting pickup"
        PICKED_UP = "picked_up", "Picked up"
        RETURNED = "returned", "Returned"

    pickup_point = models.ForeignKey(
        PickupPoint, on_delete=models.PROTECT, related_name="packages"
    )
    description = models.CharField(max_length=255, blank=True)
    pickup_code = models.CharField(max_length=20, blank=True)

    # Vine is flagged at ingestion from a cost of €0.00. It feeds the (deferred)
    # reviews module later; for now the flag is all the calendar needs.
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
    Only populated by the ingestion pipeline (Task 5); unused for now.
    """

    message_id = models.CharField(max_length=255, unique=True)
    subject = models.CharField(max_length=255, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    raw = models.TextField()
    processed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return self.subject or self.message_id
