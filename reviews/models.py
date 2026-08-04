from datetime import timedelta

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Exists, OuterRef, Q
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from packages.models import Package


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
        a pending review with no order date is hidden there, and so is one
        whose package isn't in the user's hands yet, so neither may make a
        cycle navigable here — it would land you on an empty page, which is
        the very thing this method exists to prevent. (A *written* review of a
        package still in transit is reachable all the same: `written_in` files
        it by `_cycle_date`, which starts at the same `ordered_on`.)"""
        ordered_in = Review.objects.received().filter(
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

    def received(self):
        """Only reviews of products the user actually has in his hands.

        A review is owed for something he can try out: while the package is
        still on its way — or still sitting on a counter waiting for the trip —
        there is nothing to say about it, and its `due_on` (pickup + 30 days)
        doesn't even exist yet, so the row would sit in the backlog dateless
        and impossible to close.

        Review rows are created when an Amazon order enters the database, but
        older rows may have been created before that rule existed, and some
        packages may have regressed out of `picked_up` when a late
        "Entregado" was forwarded after a bulk pickup sweep. Filtering by the
        current package state belongs here rather than in a cleanup: the same
        row reappears, dated, as soon as the package is received.

        A package-less row never matches, which is right: those are the
        historical import and the ones the "Gracias por tu reseña" email
        creates on its own — written reviews, and `written()` is what the
        history is built from."""
        return self.filter(package__state__in=[Package.State.PICKED_UP,
                                                Package.State.DELIVERED])

    def written(self):
        """The ones that are *on Amazon* — the "Reseñas escritas" history.
        A `draft` has text too, but it's still a chore: it belongs with the
        backlog at the top of the page, not with the history at the bottom."""
        return self.filter(status__in=[Review.Status.APPROVED, Review.Status.PUBLISHED])

    def in_cycle(self, starts_on, ends_on):
        """Filed inside the given period, by `_cycle_date`. Takes plain dates
        or query expressions, so `VineCycle.navigable` can hand it OuterRefs
        and get the same rule the page renders."""
        return self.annotate(cycle_date=_cycle_date()).filter(
            cycle_date__gte=starts_on, cycle_date__lte=ends_on,
        )

    def vencidas(self, today=None, cycle=None):
        """Pending, in hand, overdue, and ordered inside the given (default:
        current) VineCycle — the only ones that nag. No current cycle
        configured ⇒ nothing is urgent.

        `received()` is redundant in practice (a `due_on` is only ever set on
        receipt) and kept anyway: the badge must never count a row the ⚠ list
        doesn't show, and the list filters the same way. One hand-typed
        `due_on` in the admin is all it would take.

        `draft` is deliberately *not* counted: writing the review is the work,
        and once it's written the red badge has done its job — the row moves
        to its own "Borradores" group, which sits directly under the urgent
        one and keeps saying how late it is. Counting drafts here would leave
        the badge naming a number the ⚠ list doesn't show."""
        today = today or timezone.localdate()
        cycle = cycle if cycle is not None else VineCycle.current(today)
        if cycle is None:
            return self.none()
        return self.received().filter(
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
        # Written in Harvest's own editor but not yet on Amazon: the whole
        # trio (title/rating/text) is filled in and re-editable. Not part of
        # `written()` — "Reseñas escritas" is what's actually been posted —
        # and not part of the corpus either, for the same reason.
        DRAFT = "draft", "Borrador"
        # The user has pasted it into Amazon and said so ("Ya la he
        # publicado"). Terminal as far as Harvest is concerned; the
        # confirmation email only upgrades it to PUBLISHED days later.
        APPROVED = "approved", "Aprobada"
        # Confirmed live by the "tu reseña está en directo" email.
        PUBLISHED = "published", "Publicada"

    # The statuses still being worked on: the editor and the suggestion panel
    # open these and nothing else. Everything past DRAFT is already on
    # Amazon — its text is a record of what was posted, not a working copy —
    # so it stays read-only here (the admin remains the safety net, as
    # always).
    EDITABLE = (Status.PENDING, Status.DRAFT)

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

    # The user's own impressions of the product, jotted down while using it:
    # the raw material a suggestion is built from, and the only field of the
    # working area he writes himself. Kept apart from `text` on purpose
    # (user, 2026-08-01) — notes are for him, `text` is for Amazon.
    notes = models.TextField(blank=True)

    # The current suggested draft, headline and body, overwritten each time a
    # new one is requested — never merged into the trio below on its own: the
    # user incorporates it explicitly, and rewrites it from there. Named
    # `suggestion`, not `draft`, because `Status.DRAFT` means something else
    # entirely (his own finished text, waiting to be posted) and the two side
    # by side read as the same thing.
    suggestion_title = models.CharField(max_length=255, blank=True)
    suggestion = models.TextField(blank=True)

    # The review as written by the user — headline, stars, body. Filled by
    # the editor while still a `draft`, then unchanged through
    # approved/published: what he saved is what he pastes into Amazon. This
    # trio is what the corpus is made of (from `approved` on; a draft is not
    # in it yet, it may still be rewritten).
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


class ReferenceReview(models.Model):
    """One review known to be good, kept as an example of how the user writes.

    Separate from `Review` on purpose, and not a query over it. A `Review` is
    a **chore** — something owed, written, posted — with a lifecycle, a
    package, a deadline. A row here is a **sample of a voice**, and the two
    populations only partly overlap: the corpus wants texts Harvest never saw
    (years of reviews written straight on Amazon, pasted in by hand), and it
    does *not* want most of what `Review` holds — the ~32 rows the
    confirmation email created carry a truncated excerpt, which as an example
    would teach the wrong thing (see `text_is_complete`).

    **Hand-curated, in full, and nothing else (user, 2026-08-02).** Every row
    goes into every request — there is no cap, no selection, no ranking and no
    automatic feeding. That is the whole design, and it followed from settling
    the question the module was built around: ten fixed examples beat feeding
    the corpus back on itself, because few-shot examples **saturate**. Their
    job is tone, length and format; eight or ten good ones do all of it, and
    the two-hundredth teaches nothing the tenth didn't while making every
    prompt longer — and worse, if it came from text the model itself drafted.

    So the table is exactly what the user puts in it: ten today, twelve the day
    two more come out well. At that size "all of them" is both the simplest
    rule and the right one, and it needs no field to express. An earlier
    version had `is_example`, `is_pinned`, `source_review` and a configurable
    slice size, all of which existed to manage a growth that isn't going to
    happen. Don't bring them back without the growth.
    """

    product_title = models.CharField(max_length=255, verbose_name="producto")
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        verbose_name="estrellas",
    )
    title = models.CharField(max_length=255, verbose_name="titular")
    text = models.TextField(verbose_name="texto")

    added_on = models.DateField(default=timezone.localdate, verbose_name="añadida el")

    class Meta:
        # Oldest first, which is the order they were typed in: with every row
        # going out on every request this decides nothing but readability.
        ordering = ["added_on", "pk"]
        verbose_name = "reseña de referencia"
        verbose_name_plural = "reseñas de referencia"

    def __str__(self):
        return f"{self.product_title} ({self.rating}★)"
