from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class VineCycle(models.Model):
    """One Vine evaluation period (~6 months, e.g. 27 Jan → 26 Jul).

    Reviews only count toward the cycle their *order* falls in: when a new
    cycle starts, the previous backlog stops being urgent (clean slate) but
    stays workable — an old product can still be reviewed and its
    confirmation email still closes it, just outside the current cycle.
    Rows are entered by hand in the admin, one per cycle.
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
        return cls.objects.filter(starts_on__lte=today, ends_on__gte=today).first()


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

    # Amazon's "R…" review id, read from the live-confirmation email.
    review_id = models.CharField(max_length=20, blank=True)

    # The hard-reminder day: pickup + 30 days by default, editable per review.
    due_on = models.DateField(null=True, blank=True)
    approved_on = models.DateField(null=True, blank=True)
    published_on = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.product_title
