from datetime import timedelta

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Exists, OuterRef, Q
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone


def _six_months_later(d):
    month = d.month - 1 + 6
    year = d.year + month // 12
    return d.replace(year=year, month=month % 12 + 1)


def _cycle_date():
    """The date a review is filed under when browsing by cycle.

    Normally the package's `ordered_on`: a Vine cycle evaluates the items
    *received* in it, so the order date is what binds a review to a period.
    Written reviews with no package at all fall back to when they were
    written — the pre-Harvest historical import, and the rows the "Gracias
    por tu reseña" email creates on its own, carry no order date, and
    shelving those by the only date we do have beats making the whole corpus
    unreachable from every cycle. Pending reviews get no such fallback (see
    `reviews_list`): there the cycle drives nagging, and a guess would nag in
    the wrong period.
    """
    return Coalesce(
        "package__ordered_on", "published_on", "approved_on", TruncDate("created_at"),
    )


class VineCycle(models.Model):
    """One Vine evaluation period (~6 months, e.g. 26 Jul → 24 Jan).

    Reviews only count toward the cycle their *order* falls in: when a new
    cycle starts, the previous backlog stops being urgent (clean slate) but
    stays workable — an old product can still be reviewed and its
    confirmation email still closes it, just outside the current cycle.

    **The boundary is not fixed and cannot be computed.** Migration 0002
    seeded a decade of rows believing it sat forever on the 27th of January
    and July; migration 0004 corrected that against Amazon's own data, which
    drifts a day earlier each period. Only the periods Amazon has actually
    published are trustworthy: the JSON behind the Vine page carries them as
    exact midnights UTC, so a cut lands at 01:00/02:00 Madrid time and the
    boundary *day* belongs to the incoming cycle. `_ensure_through` still
    tops the table up on demand so no date ever falls outside a cycle, but
    what it writes is a **guess** at a six-month step — correct it in the
    admin (or in a migration) once the real date shows up.

    Membership is by date, not by instant: an order placed on a boundary day
    before ~02:00 belongs to the outgoing cycle and Harvest will file it in
    the incoming one. Two hours, twice a year, in a window nobody shops in.
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
        to backfill from here.

        What it writes is a **placeholder**, not a fact — the real boundary
        drifts (see the class docstring), so a generated row exists only to
        keep every date inside *some* cycle until Amazon publishes the
        actual period."""
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
        cycle that has something to show — a pending review placed by its
        package's `ordered_on`, or a written one placed by `_cycle_date` —
        plus the current cycle even when empty.

        The point: the table holds half-year boundaries whether or not
        anything ever happened in them (migration 0002 seeded a decade of
        them, `_ensure_through` keeps adding), so a naive prev/next lets you
        page back through empty placeholder rows — which reads to the user
        as "travelling to cycles that don't exist". Only cycles with
        something in them (and today's) are real destinations; everything
        else is invisible to navigation and redirects to the current cycle
        if reached by a hand-typed URL.

        The two halves mirror the two lists `reviews_list` renders, exactly:
        a pending review with no order date is hidden there, so it must not
        make a cycle navigable here either — it would land you on an empty
        page, which is the very thing this method exists to prevent."""
        ordered_in = Review.objects.filter(
            package__ordered_on__gte=OuterRef("starts_on"),
            package__ordered_on__lte=OuterRef("ends_on"),
        )
        written_in = Review.objects.written().in_cycle(
            OuterRef("starts_on"), OuterRef("ends_on"),
        )
        condition = Exists(ordered_in) | Exists(written_in)
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

    def written(self):
        """The ones that exist as text — the "Reseñas escritas" history."""
        return self.filter(status__in=[Review.Status.APPROVED, Review.Status.PUBLISHED])

    def in_cycle(self, starts_on, ends_on):
        """Filed inside the given period, by `_cycle_date`. Takes plain dates
        or query expressions, so `VineCycle.navigable` can hand it OuterRefs
        and get the same rule the page renders."""
        return self.annotate(cycle_date=_cycle_date()).filter(
            cycle_date__gte=starts_on, cycle_date__lte=ends_on,
        )

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
