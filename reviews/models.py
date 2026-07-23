from datetime import timedelta

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone


def _six_months_later(d):
    month = d.month - 1 + 6
    year = d.year + month // 12
    return d.replace(year=year, month=month % 12 + 1)


class VineCycle(models.Model):
    """One Vine evaluation period (~6 months, e.g. 27 Jan → 26 Jul).

    Reviews only count toward the cycle their *order* falls in: when a new
    cycle starts, the previous backlog stops being urgent (clean slate) but
    stays workable — an old product can still be reviewed and its
    confirmation email still closes it, just outside the current cycle.

    History back to 2020 and forward to 2031 was bulk-seeded once by
    migration 0002. Beyond that range, `current()` creates whatever cycle is
    missing on demand (see `_ensure_through`) — the 27th boundary is fixed
    forever, so there's nothing to decide, only the right moment to do it:
    the first time anything asks what "today" belongs to and finds a gap.
    Still editable in the admin if a boundary ever turns out wrong.
    """

    starts_on = models.DateField(unique=True)
    ends_on = models.DateField()

    class Meta:
        ordering = ["-starts_on"]

    def __str__(self):
        return f"{self.starts_on} – {self.ends_on}"

    @classmethod
    def current(cls, today=None):
        today = today or timezone.localdate()
        cls._ensure_through(today)
        return cls.objects.filter(starts_on__lte=today, ends_on__gte=today).first()

    @classmethod
    def _ensure_through(cls, today):
        """Top up the table so it always covers `today`, one 6-month step at
        a time from whatever the latest known cycle is. A no-op the
        overwhelming majority of the time (one indexed SELECT, no write) —
        it only ever creates rows the first time `today` outruns the last
        one on record, which in practice is twice a year. Does nothing on a
        table with no rows at all: that's an unmigrated/empty DB, not a gap
        to backfill from here."""
        latest = cls.objects.order_by("-starts_on").first()
        if latest is None:
            return
        while latest.ends_on < today:
            starts_on = latest.ends_on + timedelta(days=1)
            latest, _ = cls.objects.get_or_create(
                starts_on=starts_on,
                defaults={"ends_on": _six_months_later(starts_on) - timedelta(days=1)},
            )

    @classmethod
    def navigable(cls, current=None):
        """The cycles the reviews paginator is allowed to land on: every
        cycle that actually contains a review (placed by its package's
        `ordered_on`), plus the current cycle even when empty.

        The point: migration 0002 seeds all 22 half-year boundaries from
        2020 to 2031 whether or not anything ever happened in them, so a
        naive prev/next lets you page back through a decade of empty
        placeholder rows — which reads to the user as "travelling to cycles
        that don't exist". Only cycles with something in them (and today's)
        are real destinations; everything else is invisible to navigation
        and redirects to the current cycle if reached by a hand-typed URL."""
        reviewed = Review.objects.filter(
            package__ordered_on__gte=OuterRef("starts_on"),
            package__ordered_on__lte=OuterRef("ends_on"),
        )
        condition = Exists(reviewed)
        if current is not None:
            condition |= Q(pk=current.pk)
        return cls.objects.filter(condition)


class ReviewQuerySet(models.QuerySet):
    def vine(self, include_non_vine=False):
        """Vine items plus package-less rows (historical imports, always
        Vine in practice) by default; the reviews page's "No vine" toggle
        opts into everything."""
        if include_non_vine:
            return self
        return self.filter(Q(package__isnull=True) | Q(package__is_vine=True))

    def vencidas(self, today=None, cycle=None):
        """Pending, overdue, and ordered inside the given (default: current)
        VineCycle — the only ones that nag. No current cycle configured ⇒
        nothing is urgent."""
        today = today or timezone.localdate()
        cycle = cycle if cycle is not None else VineCycle.current(today)
        if cycle is None:
            return self.none()
        return self.filter(
            status=Review.Status.PENDING, due_on__isnull=False, due_on__lte=today,
            package__ordered_on__gte=cycle.starts_on, package__ordered_on__lte=cycle.ends_on,
        )


class Review(models.Model):
    """One product review, from pending chore to published text.

    The approved text is the point: it joins the corpus of the user's past
    reviews, which seeds future draft suggestions. Historical reviews
    (pre-Harvest) will be imported with no package row, so `package` is
    nullable and the product identity (title, ASIN) is denormalized here —
    the corpus must also survive a package ever being deleted.

    There is no "overdue" status: urgency is derived — a `pending` review
    whose `due_on` has passed and whose package was ordered inside the
    current VineCycle is *vencida* (feeds the red badge).
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente"
        # Approved in Harvest and pasted into Amazon by the user.
        APPROVED = "approved", "Aprobada"
        # Confirmed live by the "tu reseña está en directo" email.
        PUBLISHED = "published", "Publicada"

    package = models.OneToOneField(
        "packages.Package", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="review",
    )
    product_title = models.CharField(max_length=255)
    # The matching key for the REVIEW_PUBLISHED email, which carries no order
    # number — only ASIN and review id.
    asin = models.CharField(max_length=16, blank=True, db_index=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )

    # The working area: the current suggested draft (overwritten each time a
    # new suggestion is requested) and the user's own impressions of the
    # product, folded into the next suggestion.
    draft = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    # The approved review as pasted into Amazon — headline, stars, body.
    # This trio is what the corpus is made of.
    title = models.CharField(max_length=255, blank=True)
    rating = models.PositiveSmallIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    text = models.TextField(blank=True)
    # False when `text` is the truncated excerpt the REVIEW_PUBLISHED email
    # carries for a review closed without ever going through the Harvest
    # editor — shown so the user isn't staring at a blank card, but excluded
    # from the corpus that seeds future draft suggestions (a cut-off
    # sentence would otherwise leak into a generated draft). True for
    # anything approved in Harvest or brought in by the historical import.
    text_is_complete = models.BooleanField(default=True)

    # Amazon's "R…" review id, read from the live-confirmation email.
    review_id = models.CharField(max_length=20, blank=True)

    # The hard-reminder day: pickup + 30 days by default, editable per review.
    due_on = models.DateField(null=True, blank=True)
    approved_on = models.DateField(null=True, blank=True)
    published_on = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ReviewQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.product_title

    @property
    def is_vine(self):
        """A package-less row is always a historical import — Vine in
        practice, same assumption the "No vine" toggle's default rests on."""
        return self.package_id is None or self.package.is_vine
