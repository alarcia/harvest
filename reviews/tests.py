"""View-level tests for the reviews module's read-only landing page.

Ingestion-side coverage (Review creation/matching from real emails) lives in
packages/tests.py, next to the parser/ingest fixtures it's built from.
"""

from datetime import date, timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from packages.models import Package, PickupPoint

from .models import Review, VineCycle


def _package(ordered_on=None, picked_up_on=None, is_vine=True, description="Producto de prueba"):
    point = PickupPoint.objects.create(
        name=f"Amazon Locker - Test {PickupPoint.objects.count()}",
        kind=PickupPoint.Kind.AMAZON_LOCKER,
    )
    return Package.objects.create(
        pickup_point=point, description=description, is_vine=is_vine,
        ordered_on=ordered_on, picked_up_on=picked_up_on,
        state=Package.State.PICKED_UP if picked_up_on else Package.State.IN_TRANSIT,
    )


def _review_in_cycle(cycle, status=Review.Status.PENDING, **kwargs):
    """A pending review anchored to `cycle` via its package's ordered_on —
    which is what makes that cycle a real, navigable destination (an empty
    seeded cycle is not)."""
    pkg = _package(ordered_on=cycle.starts_on + timedelta(days=3))
    return Review.objects.create(package=pkg, product_title=pkg.description,
                                 status=status, **kwargs)


def _published(product_title, ordered_on=None, **kwargs):
    """A written review, with or without a package behind it — the two ways
    it gets filed into a cycle."""
    return Review.objects.create(
        package=_package(ordered_on=ordered_on) if ordered_on else None,
        product_title=product_title, status=Review.Status.PUBLISHED, **kwargs,
    )


class ReviewsListViewTests(TestCase):
    def setUp(self):
        # Anchored to whatever cycle is running *now*, never to a literal
        # date: the view reads the real `timezone.localdate()`, so a
        # hardcoded "today" quietly rots into a different cycle the moment a
        # boundary passes — which is exactly what happened on 2026-07-26,
        # taking six of these tests down with it. Everything below places
        # its dates relative to the cycle instead.
        self.today = timezone.localdate()
        self.current_cycle = VineCycle.current(self.today)
        self.assertIsNotNone(self.current_cycle)

    def _in_current(self, offset=0):
        """A date inside the running cycle, `offset` days after its start."""
        return self.current_cycle.starts_on + timedelta(days=offset)

    def _get(self, url=None, **params):
        # HX-Request avoids the full page (which pulls in the topbar's
        # {% static %} logo — needs a collectstatic manifest this dev
        # environment doesn't have); the calendar's own tests do the same.
        return self.client.get(url or reverse("reviews_list"), params, HTTP_HX_REQUEST="true")

    def test_defaults_to_current_cycle(self):
        response = self._get()
        self.assertEqual(response.context["cycle"], self.current_cycle)
        self.assertTrue(response.context["is_current_cycle"])

    def test_pending_review_without_order_date_is_hidden(self):
        # No package at all, or a package that was never ORDERED — either
        # way there's no order date, so its cycle is unknowable. Must not
        # show as pending anywhere: it surfaces on its own once the
        # "Gracias por tu reseña" email closes it into "Reseñas escritas".
        Review.objects.create(product_title="Sin paquete conocido", status=Review.Status.PENDING)
        pkg = _package(ordered_on=None)
        Review.objects.create(package=pkg, product_title=pkg.description,
                               status=Review.Status.PENDING)
        response = self._get()
        self.assertEqual(list(response.context["pendientes"]), [])
        self.assertEqual(list(response.context["vencidas"]), [])
        self.assertNotContains(response, "Sin paquete conocido")

    def test_pending_review_with_order_date_in_current_cycle_shows(self):
        pkg = _package(ordered_on=self._in_current())
        review = Review.objects.create(package=pkg, product_title=pkg.description,
                                        status=Review.Status.PENDING)
        response = self._get()
        self.assertIn(review, response.context["pendientes"])

    def test_overdue_review_is_urgent_only_on_current_cycle(self):
        pkg = _package(ordered_on=self._in_current(), picked_up_on=self._in_current())
        review = Review.objects.create(
            package=pkg, product_title=pkg.description, status=Review.Status.PENDING,
            due_on=self.today,  # the day it comes due is already overdue
        )
        response = self._get()
        self.assertIn(review, response.context["vencidas"])
        self.assertNotIn(review, response.context["pendientes"])

    def test_past_cycle_backlog_shown_but_never_urgent(self):
        # The bug this guards: an item ordered *inside* the current cycle but
        # with a due_on that reads like it's "overdue" must never be dumped
        # into a past cycle just because it looks late — cycle membership is
        # decided by ordered_on alone.
        prev_cycle = (VineCycle.objects.filter(starts_on__lt=self.current_cycle.starts_on)
                      .order_by("-starts_on").first())
        pkg = _package(ordered_on=prev_cycle.starts_on + timedelta(days=5),
                        picked_up_on=prev_cycle.starts_on + timedelta(days=10))
        review = Review.objects.create(
            package=pkg, product_title=pkg.description, status=Review.Status.PENDING,
            due_on=prev_cycle.starts_on + timedelta(days=40),
        )
        # Not visible on the current cycle's page at all.
        current_response = self._get()
        self.assertNotIn(review, current_response.context["pendientes"])
        self.assertNotIn(review, current_response.context["vencidas"])

        # Visible on its own cycle's page, as a plain (never urgent) pending item.
        past_response = self._get(cycle=prev_cycle.starts_on.isoformat())
        self.assertFalse(past_response.context["is_current_cycle"])
        self.assertEqual(list(past_response.context["vencidas"]), [])
        self.assertIn(review, past_response.context["pendientes"])

    def test_prev_disabled_when_no_past_cycle_has_reviews(self):
        # The core of the reported bug: the 2020-2031 seed fills the table
        # with empty boundary rows, but with reviews only in the current
        # cycle (the user's real situation) there is nowhere to page back
        # to — prev must be disabled, not walk through a decade of empty
        # placeholders.
        _review_in_cycle(self.current_cycle)
        response = self._get()
        self.assertIsNone(response.context["next_cycle_url"])
        self.assertIsNone(response.context["prev_cycle_url"])
        self.assertIsNone(response.context["current_cycle_url"])

    def test_prev_reaches_a_past_cycle_with_reviews_skipping_empty_ones(self):
        cycles = list(VineCycle.objects.filter(starts_on__lt=self.current_cycle.starts_on)
                      .order_by("-starts_on")[:3])
        immediate_prev, _, two_back = cycles  # two_back has data, the one between is empty
        review = _review_in_cycle(two_back)

        response = self._get()
        # Prev skips the empty immediately-previous cycle and lands on the
        # nearest one that actually has a review.
        self.assertIsNotNone(response.context["prev_cycle_url"])
        self.assertIn(f"cycle={two_back.starts_on.isoformat()}", response.context["prev_cycle_url"])
        self.assertNotIn(immediate_prev.starts_on.isoformat(), response.context["prev_cycle_url"])

        # And that destination renders (not a redirect), showing its review,
        # with a way back to the current cycle.
        prev_response = self._get(response.context["prev_cycle_url"])
        self.assertEqual(prev_response.status_code, 200)
        self.assertFalse(prev_response.context["is_current_cycle"])
        self.assertIn(review, prev_response.context["pendientes"])
        self.assertIsNotNone(prev_response.context["next_cycle_url"])
        self.assertIsNotNone(prev_response.context["current_cycle_url"])

    def test_empty_seeded_past_cycle_url_redirects_to_current(self):
        # A boundary row that exists (the seed made it) but holds no reviews
        # and isn't today's must redirect, exactly like a nonexistent one —
        # you can't land on an empty cycle by hand-typing its URL either.
        empty_prev = (VineCycle.objects.filter(starts_on__lt=self.current_cycle.starts_on)
                      .order_by("-starts_on").first())
        response = self._get(cycle=empty_prev.starts_on.isoformat())
        self.assertRedirects(response, reverse("reviews_list"), fetch_redirect_response=False)

    def test_malformed_cycle_param_redirects_to_current(self):
        response = self._get(cycle="not-a-date")
        self.assertRedirects(response, reverse("reviews_list"), fetch_redirect_response=False)

    def test_wellformed_but_nonexistent_cycle_param_redirects_to_current(self):
        # A real ISO date that simply has no VineCycle row (well before the
        # 2020 seed, or any non-boundary date) must behave the same as a
        # malformed one: redirect, never render a mismatched cycle under
        # its URL.
        response = self._get(cycle="1999-01-27")
        self.assertRedirects(response, reverse("reviews_list"), fetch_redirect_response=False)

    def test_unknown_cycle_redirect_preserves_the_toggle(self):
        response = self._get(cycle="not-a-date", non_vine="1")
        self.assertRedirects(response, reverse("reviews_list") + "?non_vine=1",
                              fetch_redirect_response=False)

    def test_toggle_preserves_the_viewed_cycle(self):
        prev_cycle = (VineCycle.objects.filter(starts_on__lt=self.current_cycle.starts_on)
                      .order_by("-starts_on").first())
        _review_in_cycle(prev_cycle)  # make it navigable, else the URL redirects
        response = self._get(cycle=prev_cycle.starts_on.isoformat())
        self.assertIn(f"cycle={prev_cycle.starts_on.isoformat()}", response.context["toggle_url"])
        self.assertIn("non_vine=1", response.context["toggle_url"])

    @override_settings(STORAGES={
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })  # the full-page branch renders the topbar's {% static %} logo, which
        # needs a collectstatic manifest this dev/test environment doesn't have
    def test_history_restore_request_gets_the_full_page_not_a_fragment(self):
        # Same htmx gotcha as the calendar: a post-cache-miss browser-back
        # request carries HX-Request too, but htmx replaces the whole
        # document with the response, so it must get the full page.
        fragment = self.client.get(reverse("reviews_list"), HTTP_HX_REQUEST="true").content
        self.assertNotIn(b"<!doctype html>", fragment)

        restored = self.client.get(
            reverse("reviews_list"), HTTP_HX_REQUEST="true",
            HTTP_HX_HISTORY_RESTORE_REQUEST="true").content
        self.assertIn(b"<!doctype html>", restored)
        self.assertIn(b"app-topbar", restored)

    def test_confirmed_review_shows_product_title_not_review_headline(self):
        # The bug this guards: the card must lead with what the product
        # *is*, not the review's own headline ("Cumple con su función" reads
        # like nonsense without knowing what it's reviewing).
        pkg = _package(ordered_on=self._in_current())
        Review.objects.create(
            package=pkg, product_title="Nombre real del producto",
            status=Review.Status.PUBLISHED, title="Cumple con su función", rating=4,
        )
        response = self._get()
        self.assertContains(response, "Nombre real del producto")
        self.assertContains(response, "Cumple con su función")  # still shown, just secondary

    def test_written_reviews_are_filed_in_their_own_cycle(self):
        # The history section is "what I wrote for this period", not an
        # ever-growing pile repeated identically on every cycle's page.
        prev_cycle = (VineCycle.objects.filter(starts_on__lt=self.current_cycle.starts_on)
                      .order_by("-starts_on").first())
        _published("Del ciclo pasado", ordered_on=prev_cycle.starts_on + timedelta(days=5))
        _published("De este ciclo", ordered_on=self._in_current())

        response = self._get()
        self.assertEqual([r.product_title for r in response.context["confirmed"]],
                          ["De este ciclo"])

        # And a written review is enough to make its cycle a real
        # destination, with nothing pending in it.
        past = self._get(cycle=prev_cycle.starts_on.isoformat())
        self.assertEqual(past.status_code, 200)
        self.assertEqual([r.product_title for r in past.context["confirmed"]],
                          ["Del ciclo pasado"])

    def test_written_review_without_a_package_is_filed_by_when_it_was_written(self):
        # The pre-Harvest import and the rows "Gracias por tu reseña" creates
        # on its own carry no order date at all. Shelving them by the only
        # date we have beats hiding the whole corpus from every cycle.
        _published("Escrita en este ciclo", published_on=self._in_current())
        _published("Escrita hace años", published_on=date(2025, 3, 1))

        response = self._get()
        self.assertEqual([r.product_title for r in response.context["confirmed"]],
                          ["Escrita en este ciclo"])


class ReviewDetailViewTests(TestCase):
    def test_modal_heading_is_product_title(self):
        pkg = _package(ordered_on=date(2026, 2, 1))
        review = Review.objects.create(
            package=pkg, product_title="Nombre real del producto",
            status=Review.Status.PUBLISHED, title="Titular de la reseña",
        )
        response = self.client.get(reverse("review_detail", args=[review.pk]))
        self.assertContains(response, "<h2>Nombre real del producto</h2>", html=True)
        self.assertContains(response, "Titular de la reseña")


class VineCycleBoundaryTests(TestCase):
    """The boundaries Amazon actually published, as corrected by migration
    0004. It drifts a day earlier each period, so these are facts read off
    the Vine page's own data — not a rule anything can recompute."""

    def test_boundary_day_belongs_to_the_incoming_cycle(self):
        # The cut falls at 01:00/02:00 Madrid time, so the whole day is the
        # new period's — the two orders of 2026-07-26 were seven hours into
        # it. The rule holds at both ends of the same cycle, which is why it
        # closes on the 24th and not on the re-evaluation day.
        self.assertEqual(VineCycle.current(date(2026, 7, 25)).starts_on, date(2026, 1, 27))

        cycle = VineCycle.current(date(2026, 7, 26))
        self.assertEqual((cycle.starts_on, cycle.ends_on), (date(2026, 7, 26), date(2027, 1, 24)))

        self.assertEqual(VineCycle.current(date(2027, 1, 25)).starts_on, date(2027, 1, 25))

    def test_seeded_tail_past_the_last_known_boundary_is_gone(self):
        # Every row 0002 wrote beyond it came from the wrong constant, so it
        # was deleted rather than shifted — `_ensure_through` regenerates.
        self.assertFalse(VineCycle.objects.filter(starts_on__gt=date(2027, 1, 25)).exists())

    def test_no_date_falls_outside_a_cycle(self):
        # The hole this guards: shifting the known cycles while leaving the
        # seeded tail in place left 25-26 July 2027 in no cycle at all, where
        # `current()` returns None and nothing is ever urgent.
        for day in [date(2026, 7, 25), date(2026, 7, 26), date(2027, 1, 24),
                     date(2027, 1, 25), date(2027, 7, 24), date(2027, 7, 25),
                     date(2027, 7, 26)]:
            self.assertIsNotNone(VineCycle.current(day), day)


class VineCycleAutoCreationTests(TestCase):
    """`VineCycle.current()` self-heals forward instead of depending forever
    on the rows a migration wrote: the next cycle is created the first time
    something asks for a date past the latest one on record. What it creates
    is a placeholder on a six-month step — the real boundary drifts, so it
    gets corrected once Amazon publishes it."""

    def test_current_creates_the_missing_cycle_on_demand(self):
        latest_before = VineCycle.objects.order_by("-starts_on").first()
        self.assertEqual(latest_before.starts_on, date(2027, 1, 25))

        beyond_known = date(2027, 9, 1)  # past the last known row (ends 2027-07-24)
        cycle = VineCycle.current(beyond_known)

        self.assertIsNotNone(cycle)
        self.assertEqual((cycle.starts_on, cycle.ends_on), (date(2027, 7, 25), date(2028, 1, 24)))

    def test_current_backfills_every_skipped_cycle_not_just_the_last(self):
        # Simulate the app having been off (or the DB copy being stale) across
        # more than one boundary: every intermediate cycle must still exist,
        # not just the one covering `today` — history stays contiguous.
        count_before = VineCycle.objects.count()
        far_future = date(2028, 9, 1)  # three cycles past the last known row
        VineCycle.current(far_future)

        self.assertEqual(VineCycle.objects.count(), count_before + 3)
        for starts_on, ends_on in [
            (date(2027, 7, 25), date(2028, 1, 24)),
            (date(2028, 1, 25), date(2028, 7, 24)),
            (date(2028, 7, 25), date(2029, 1, 24)),
        ]:
            self.assertTrue(VineCycle.objects.filter(starts_on=starts_on, ends_on=ends_on).exists())

    def test_current_is_idempotent(self):
        beyond_known = date(2027, 9, 1)
        VineCycle.current(beyond_known)
        count_after_first = VineCycle.objects.count()
        VineCycle.current(beyond_known)
        self.assertEqual(VineCycle.objects.count(), count_after_first)

    def test_empty_table_does_not_crash(self):
        VineCycle.objects.all().delete()
        self.assertIsNone(VineCycle.current(date(2026, 7, 23)))
        self.assertEqual(VineCycle.objects.count(), 0)
