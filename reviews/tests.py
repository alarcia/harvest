"""View-level tests for the reviews module's read-only landing page.

Ingestion-side coverage (Review creation/matching from real emails) lives in
packages/tests.py, next to the parser/ingest fixtures it's built from.
"""

import json
from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from packages.models import Config, Package, PickupPoint

from .models import ReferenceReview, Review, VineCycle
from .suggest import SuggestionUnavailable, suggest_draft


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

    def test_draft_gets_its_own_group_between_urgent_and_pending(self):
        pkg = _package(ordered_on=self._in_current())
        draft = Review.objects.create(
            package=pkg, product_title=pkg.description, status=Review.Status.DRAFT,
            title="Un titular", rating=4, text="El cuerpo de la reseña.",
        )
        response = self._get()
        self.assertIn(draft, response.context["borradores"])
        self.assertNotIn(draft, response.context["pendientes"])
        self.assertNotIn(draft, response.context["vencidas"])
        self.assertNotIn(draft, response.context["confirmed"])  # not history yet
        # The order the user asked for, read straight off the rendered page.
        body = response.content.decode()
        self.assertLess(body.index("Borradores"), body.index("Pendientes"))
        self.assertIn("Un titular", body)  # recognisable without opening it

    def test_overdue_draft_leaves_the_urgent_group_and_the_badge(self):
        # Writing the draft is the work the badge nags about, so it stops
        # counting — but the row keeps saying how late it is where it lands.
        pkg = _package(ordered_on=self._in_current(), picked_up_on=self._in_current())
        draft = Review.objects.create(
            package=pkg, product_title=pkg.description, status=Review.Status.DRAFT,
            title="Un titular", rating=4, text="Cuerpo.",
            due_on=self.today - timedelta(days=3),
        )
        response = self._get()
        self.assertIn(draft, response.context["borradores"])
        self.assertEqual(list(response.context["vencidas"]), [])
        self.assertEqual(response.context["vencidas_count"], 0)
        self.assertContains(response, "vencida desde el")

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

    def test_pending_card_offers_writing_the_draft(self):
        review = Review.objects.create(product_title="Algo", status=Review.Status.PENDING)
        response = self.client.get(reverse("review_detail", args=[review.pk]))
        self.assertContains(response, "Escribir borrador")
        self.assertContains(response, reverse("review_edit", args=[review.pk]))

    def test_draft_card_shows_its_text_and_offers_rewriting_it(self):
        # The whole point of the status: a draft reads like a written review
        # in the card, without having to reopen the editor.
        review = Review.objects.create(
            product_title="Algo", status=Review.Status.DRAFT,
            title="Mi titular", rating=4, text="Mi texto completo.",
        )
        response = self.client.get(reverse("review_detail", args=[review.pk]))
        self.assertContains(response, "Mi titular")
        self.assertContains(response, "Mi texto completo.")
        self.assertContains(response, "4★")
        self.assertContains(response, "Editar borrador")

    def test_published_card_has_no_editor(self):
        review = Review.objects.create(product_title="Algo", status=Review.Status.PUBLISHED)
        response = self.client.get(reverse("review_detail", args=[review.pk]))
        self.assertNotContains(response, "borrador")


class ReviewEditorTests(TestCase):
    """The draft editor: the module's first write path."""

    def setUp(self):
        self.review = Review.objects.create(
            package=_package(ordered_on=timezone.localdate()),
            product_title="Funda con teclado", status=Review.Status.PENDING,
        )
        self.url = reverse("review_edit", args=[self.review.pk])

    def _post(self, **fields):
        data = {"title": "Un titular", "rating": "4", "text": "El cuerpo."}
        data.update(fields)
        return self.client.post(self.url, data)

    def test_editor_prefills_what_is_already_there(self):
        self.review.title, self.review.rating, self.review.text = "Titular", 3, "Cuerpo"
        self.review.status = Review.Status.DRAFT
        self.review.save()
        response = self.client.get(self.url)
        self.assertContains(response, 'value="Titular"')
        self.assertContains(response, "Cuerpo")
        # The chosen star comes back checked, not reset to nothing.
        self.assertInHTML(
            '<input type="radio" name="rating" id="rev-star-3" value="3" checked>',
            response.content.decode(),
        )

    def test_saving_turns_a_pending_review_into_a_draft(self):
        response = self._post(title="Funda completa", rating="5", text="Muy bien.")
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, Review.Status.DRAFT)
        self.assertEqual(self.review.title, "Funda completa")
        self.assertEqual(self.review.rating, 5)
        self.assertEqual(self.review.text, "Muy bien.")
        # Written by hand, so it belongs in the corpus.
        self.assertTrue(self.review.text_is_complete)
        # Lands back on the card, and tells the list behind it to refetch.
        self.assertContains(response, "Editar borrador")
        self.assertEqual(response["HX-Trigger"], "package-updated")

    def test_a_draft_can_be_rewritten_and_stays_a_draft(self):
        self._post(title="Primera versión", rating="3", text="Primer texto.")
        self._post(title="Segunda versión", rating="5", text="Segundo texto.")
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, Review.Status.DRAFT)
        self.assertEqual(self.review.title, "Segunda versión")
        self.assertEqual(self.review.text, "Segundo texto.")

    def test_every_field_is_required(self):
        for missing, message in [("title", "título"), ("rating", "puntuación"),
                                  ("text", "texto")]:
            with self.subTest(missing=missing):
                response = self._post(**{missing: ""})
                self.review.refresh_from_db()
                self.assertEqual(self.review.status, Review.Status.PENDING)
                self.assertContains(response, message)

    def test_a_rejected_save_gives_back_what_was_typed(self):
        # A thousand characters of writing must never be lost to a missing
        # star — the form comes back filled in, not blank.
        response = self._post(rating="", text="Un texto que costó escribir.")
        self.assertContains(response, "Un texto que costó escribir.")
        self.assertContains(response, 'value="Un titular"')

    def test_a_bogus_rating_is_refused(self):
        for bogus in ["0", "6", "cuatro", "4.5"]:
            with self.subTest(rating=bogus):
                self._post(rating=bogus)
                self.review.refresh_from_db()
                self.assertEqual(self.review.status, Review.Status.PENDING)

    def test_a_published_review_cannot_be_edited(self):
        self.review.status = Review.Status.PUBLISHED
        self.review.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self._post().status_code, 404)


class ReviewApproveTests(TestCase):
    """"Ya la he publicado en Amazon" — the step no email can observe until
    Amazon's own confirmation turns up days later."""

    def setUp(self):
        self.review = Review.objects.create(
            package=_package(ordered_on=timezone.localdate()),
            product_title="Funda con teclado", status=Review.Status.DRAFT,
            title="Un titular", rating=4, text="El cuerpo.",
        )
        self.url = reverse("review_approve", args=[self.review.pk])

    def test_it_asks_before_doing_it(self):
        response = self.client.get(self.url)
        self.assertContains(response, "¿Ya la has publicado?")
        self.assertContains(response, "Cancelar")
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, Review.Status.DRAFT)  # GET changes nothing

    def test_confirming_approves_it_dated_today(self):
        response = self.client.post(self.url)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, Review.Status.APPROVED)
        self.assertEqual(self.review.approved_on, timezone.localdate())
        self.assertEqual(response["HX-Trigger"], "package-updated")
        # The text is untouched — approving says where it is, not what it says.
        self.assertEqual(self.review.text, "El cuerpo.")

    def test_an_approved_review_leaves_the_backlog_for_the_written_ones(self):
        self.client.post(self.url)
        response = self.client.get(reverse("reviews_list"), HTTP_HX_REQUEST="true")
        self.review.refresh_from_db()
        self.assertIn(self.review, response.context["confirmed"])
        self.assertNotIn(self.review, response.context["borradores"])
        self.assertNotIn(self.review, response.context["pendientes"])

    def test_only_a_draft_can_be_approved(self):
        for status in [Review.Status.PENDING, Review.Status.PUBLISHED,
                        Review.Status.APPROVED]:
            with self.subTest(status=status):
                self.review.status = status
                self.review.save()
                self.assertEqual(self.client.get(self.url).status_code, 404)
                self.assertEqual(self.client.post(self.url).status_code, 404)


class ReviewSuggestTests(TestCase):
    """The notes-and-suggestion panel. The remote call isn't wired up yet, so
    what's under test is everything around it — which is the part that has to
    already work when it is."""

    def setUp(self):
        self.review = Review.objects.create(
            package=_package(ordered_on=timezone.localdate()),
            product_title="Funda con teclado", status=Review.Status.PENDING,
        )
        self.url = reverse("review_suggest", args=[self.review.pk])

    def _open(self, title="", text="", rating=""):
        """Arrive from the editor, the only way in — carrying whatever is
        typed there at that moment."""
        return self.client.post(self.url, {
            "action": "open", "title": title, "text": text, "rating": rating,
        })

    def test_panel_shows_notes_and_stars_but_never_asks_for_a_title(self):
        self.review.notes = "El hueco de la cámara queda grande"
        self.review.rating = 3
        self.review.save()
        response = self.client.get(self.url)
        self.assertContains(response, "El hueco de la cámara queda grande")
        self.assertInHTML(
            '<input type="radio" name="rating" id="rev-star-3" value="3" checked>',
            response.content.decode(),
        )
        # The product's name is the title input, and it's already known — so
        # there's a hidden field carrying the editor's headline, but nothing
        # to type one into.
        self.assertContains(response, "Funda con teclado")
        self.assertNotContains(response, 'for="rev-title"')

    def test_the_editor_is_reached_from_here_not_the_card(self):
        # The panel lives inside the editor (user, 2026-08-01), so the card
        # only ever offers the editor, and every exit here lands back in it.
        card = self.client.get(reverse("review_detail", args=[self.review.pk]))
        self.assertNotContains(card, "Notas y sugerencia")
        editor = self.client.get(reverse("review_edit", args=[self.review.pk]))
        self.assertContains(editor, "Notas y sugerencia")
        self.assertContains(editor, self.url)

    def test_unsaved_work_survives_the_detour(self):
        # The whole reason the editor's fields ride along as hidden inputs:
        # opening the panel swaps the modal, and a half-written review left
        # in the DOM would simply be gone.
        typed = "Un párrafo a medias\ncon salto de línea."
        panel = self._open(title="Titular a medias", text=typed, rating="4")
        self.assertContains(panel, "Titular a medias")
        self.assertContains(panel, "con salto de línea.")

        back = self.client.post(self.url, {
            "action": "back", "title": "Titular a medias", "text": typed,
            "rating": "4", "notes": "",
        })
        self.assertContains(back, "Guardar borrador")  # the editor, not the card
        self.assertContains(back, "Titular a medias")
        self.assertContains(back, "con salto de línea.")
        # And nothing was written on the way through.
        self.review.refresh_from_db()
        self.assertEqual(self.review.title, "")
        self.assertEqual(self.review.status, Review.Status.PENDING)

    def test_saving_notes_keeps_them_without_touching_the_review(self):
        response = self.client.post(self.url, {
            "action": "save", "notes": "Se enreda un poco con el uso",
            "rating": "4", "title": "Titular a medias", "text": "A medias.",
        })
        self.review.refresh_from_db()
        self.assertEqual(self.review.notes, "Se enreda un poco con el uso")
        self.assertEqual(self.review.rating, 4)
        # Notes are not a review: this is still a pending chore, and the text
        # in the editor is still unsaved.
        self.assertEqual(self.review.status, Review.Status.PENDING)
        self.assertEqual(self.review.text, "")
        # Back in the editor with the work still there.
        self.assertContains(response, "Guardar borrador")
        self.assertContains(response, "Titular a medias")

    def test_suggesting_needs_the_stars_first(self):
        response = self.client.post(self.url, {
            "action": "suggest", "notes": "Algo", "rating": "",
        })
        self.assertContains(response, "puntuación")

    def test_suggesting_with_no_notes_at_all_is_allowed(self):
        # The five-reviews-in-one-evening case (user, 2026-08-02): there are
        # products he has nothing to say about, and the proposal has to be
        # reachable anyway. It must get *past* the form and fail on the wiring
        # instead — anything else and the button is unusable without notes.
        response = self.client.post(self.url, {
            "action": "suggest", "notes": "", "rating": "4",
        })
        self.assertNotContains(response, "nota sobre el producto.")
        self.assertContains(response, "todavía no está disponible")

    def test_suggesting_says_it_is_not_connected_yet_and_keeps_the_notes(self):
        response = self.client.post(self.url, {
            "action": "suggest", "notes": "Muy cómoda", "rating": "5",
        })
        self.assertContains(response, "todavía no está")
        self.review.refresh_from_db()
        self.assertEqual(self.review.notes, "Muy cómoda")  # never lost to a failed request
        self.assertEqual(self.review.status, Review.Status.PENDING)
        self.assertEqual(self.review.title, "")

    def _with_proposal(self):
        """Stand in for what the suggestion step will store once it's wired up."""
        self.review.suggestion_title = "Funda completa y versátil"
        self.review.suggestion = "El cuerpo propuesto, para reescribir."
        self.review.save()

    def test_incorporating_hands_the_proposal_to_the_editor_unsaved(self):
        self._with_proposal()
        response = self.client.post(self.url, {
            "action": "incorporate", "notes": "Mis notas", "rating": "4",
            "title": "", "text": "",
        })
        # In the editor, filled in, ready to rewrite — but nothing stored: the
        # editor's own "Guardar borrador" is still the only writer, so a
        # mistaken tap costs one Cancelar, not a review.
        self.assertContains(response, "Funda completa y versátil")
        self.assertContains(response, "El cuerpo propuesto, para reescribir.")
        self.assertContains(response, "Guardar borrador")
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, Review.Status.PENDING)
        self.assertEqual(self.review.title, "")
        self.assertEqual(self.review.text, "")
        # The proposal survives — comparing against it is the user's business.
        self.assertEqual(self.review.suggestion, "El cuerpo propuesto, para reescribir.")

    def test_the_panel_warns_before_replacing_work_already_typed(self):
        self._with_proposal()
        blank = self._open()
        self.assertNotContains(blank, "Sustituirá")
        started = self._open(title="Lo mío", text="Mi texto")
        self.assertContains(started, "Sustituirá")

    def test_panel_is_closed_once_the_review_is_on_amazon(self):
        self.review.status = Review.Status.PUBLISHED
        self.review.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)


@override_settings(SUGGEST_API_URL="https://example.invalid/v1",
                   SUGGEST_API_KEY="test-key", SUGGEST_MODEL="test-model")
class SuggestionBudgetTests(TestCase):
    """The monthly cap on suggestions — the one guard Harvest itself can put
    between a loop in the app and a bill at the provider.

    The remote call is still missing, so `suggest_draft` always ends in
    "todavía no está disponible"; what these check is *which* refusal comes
    out, because that is what says whether a request would have been made."""

    def setUp(self):
        # A template has to be in place: `suggest_draft` checks for one before
        # it books anything, so without this every test here would fail on the
        # wrong step.
        Config.objects.update_or_create(
            pk=1, defaults={"suggestion_prompt": "Redacta sobre {producto}"})
        self.review = Review.objects.create(
            package=_package(ordered_on=timezone.localdate()),
            product_title="Funda con teclado", status=Review.Status.PENDING,
            notes="Cierra bien y pesa poco", rating=4,
        )

    def test_the_month_runs_out_and_says_so(self):
        Config.objects.filter(pk=1).update(suggestions_per_month=2)
        for _ in range(2):
            claimed, limit = Config.claim_suggestion()
            self.assertTrue(claimed)
            self.assertEqual(limit, 2)
        self.assertEqual(Config.claim_suggestion(), (False, 2))

        with self.assertRaises(SuggestionUnavailable) as spent:
            suggest_draft(self.review)
        self.assertIn("tope de 2 sugerencias", str(spent.exception))

    def test_a_new_month_starts_over_without_anything_having_to_run(self):
        Config.objects.filter(pk=1).update(suggestions_per_month=1)
        july = date(2026, 7, 20)
        self.assertEqual(Config.claim_suggestion(july)[0], True)
        self.assertEqual(Config.claim_suggestion(date(2026, 7, 31))[0], False)
        # No job fires on the 1st: the first caller of a new month finds a
        # stale stamp and resets it on the way past.
        self.assertEqual(Config.claim_suggestion(date(2026, 8, 1))[0], True)
        config = Config.load()
        self.assertEqual(config.suggestions_month, date(2026, 8, 1))
        self.assertEqual(config.suggestions_used, 1)

    def test_zero_switches_the_feature_off_rather_than_meaning_unlimited(self):
        Config.objects.filter(pk=1).update(suggestions_per_month=0)
        self.assertEqual(Config.claim_suggestion(), (False, 0))
        with self.assertRaises(SuggestionUnavailable) as off:
            suggest_draft(self.review)
        self.assertIn("desactivadas", str(off.exception))
        self.assertEqual(Config.load().suggestions_used, 0)

    @override_settings(SUGGEST_API_URL="", SUGGEST_API_KEY="")
    def test_an_unconfigured_install_never_spends_a_slot(self):
        # The order matters: nothing can be billed while the feature is off, so
        # nothing may be counted either — otherwise the cap would drain itself
        # on an install that can't make a single request.
        with self.assertRaises(SuggestionUnavailable):
            suggest_draft(self.review)
        self.assertEqual(Config.load().suggestions_used, 0)

    def test_the_panel_shows_the_refusal_the_user_can_act_on(self):
        Config.objects.filter(pk=1).update(suggestions_per_month=0)
        response = self.client.post(
            reverse("review_suggest", args=[self.review.pk]),
            {"action": "suggest", "notes": "Cierra bien", "rating": "4"},
        )
        self.assertContains(response, "desactivadas")
        # The notes still saved: asking for help and being told no is not a
        # reason to lose what was typed.
        self.assertEqual(Review.objects.get(pk=self.review.pk).notes, "Cierra bien")


def _answer(titulo="Cumple lo que promete", texto="Un cuerpo de reseña."):
    """What the endpoint replies, minus the opening `{"titulo":` the request
    already put in the assistant's mouth."""
    body = json.dumps({"titulo": titulo, "texto": texto}, ensure_ascii=False)
    return {"content": [{"type": "text", "text": body.split(":", 1)[1]}]}


@override_settings(SUGGEST_API_URL="https://example.invalid/v1",
                   SUGGEST_API_KEY="test-key", SUGGEST_MODEL="test-model",
                   SUGGEST_API_HEADERS="x-version: 2026-01-01")
class SuggestDraftTests(TestCase):
    """The proposal itself: what gets sent, what comes back, and what happens
    when it doesn't. The endpoint is always mocked — the tests must never make
    a real request, least of all a billable one."""

    def setUp(self):
        Config.objects.update_or_create(pk=1, defaults={
            "suggestion_prompt": "Producto: {producto}\nEstrellas: {estrellas}\n"
                                 "Notas: {notas}\nEjemplos:\n{ejemplos}",
            "suggestion_examples": 2,
        })
        self.review = Review.objects.create(
            package=_package(ordered_on=timezone.localdate()),
            product_title="Funda con teclado", status=Review.Status.PENDING,
            notes="Cierra bien y pesa poco", rating=4,
        )

    def test_the_request_carries_the_notes_the_stars_and_the_corpus(self):
        ReferenceReview.objects.create(product_title="Cargador", rating=5,
                                        title="Rápido", text="Carga en una hora.")
        with patch("reviews.suggest._post", return_value=_answer()) as post:
            suggest_draft(self.review)
        payload = post.call_args[0][0]
        sent = payload["messages"][0]["content"]
        self.assertIn("Funda con teclado", sent)
        self.assertIn("4/5", sent)
        self.assertIn("Cierra bien y pesa poco", sent)
        self.assertIn("[5/5] Rápido", sent)
        self.assertIn("Carga en una hora.", sent)
        # The model comes from the environment, never from the code.
        self.assertEqual(payload["model"], "test-model")

    def test_a_proposal_can_be_asked_for_with_no_notes(self):
        # The whole request still goes out; `{notas}` simply resolves to
        # nothing and the template is written to carry on from the product and
        # the stars. Nothing here may depend on the notes existing.
        self.review.notes = ""
        self.review.save()
        with patch("reviews.suggest._post", return_value=_answer()) as post:
            title, text = suggest_draft(self.review)
        sent = post.call_args[0][0]["messages"][0]["content"]
        self.assertIn("Funda con teclado", sent)
        self.assertIn("4/5", sent)
        self.assertNotIn("{notas}", sent)
        self.assertTrue(title and text)

    def test_only_the_configured_number_of_examples_travels(self):
        for n in range(5):
            ReferenceReview.objects.create(product_title=f"Cosa {n}", rating=4,
                                            title=f"Titular {n}", text=f"Texto {n}")
        with patch("reviews.suggest._post", return_value=_answer()) as post:
            suggest_draft(self.review)
        sent = post.call_args[0][0]["messages"][0]["content"]
        # Two most recent, by `-added_on, -pk`.
        self.assertIn("Titular 4", sent)
        self.assertIn("Titular 3", sent)
        self.assertNotIn("Titular 2", sent)

    def test_retired_examples_stay_out(self):
        ReferenceReview.objects.create(product_title="Vieja", rating=1,
                                        title="No me representa", text="...",
                                        is_example=False)
        with patch("reviews.suggest._post", return_value=_answer()) as post:
            suggest_draft(self.review)
        self.assertNotIn("No me representa",
                          post.call_args[0][0]["messages"][0]["content"])

    def test_a_stray_brace_in_the_template_cannot_break_it(self):
        # The template is typed into a textarea; `str.format` would raise on
        # this and take the whole feature down until someone edited the DB.
        Config.objects.filter(pk=1).update(
            suggestion_prompt="Usa un {tono} coloquial sobre {producto}")
        with patch("reviews.suggest._post", return_value=_answer()) as post:
            suggest_draft(self.review)
        sent = post.call_args[0][0]["messages"][0]["content"]
        self.assertIn("{tono}", sent)          # untouched, not an error
        self.assertIn("Funda con teclado", sent)

    def test_the_answer_is_read_back_as_headline_and_body(self):
        with patch("reviews.suggest._post",
                    return_value=_answer("Ligera y resistente", "Llevo un mes.")):
            title, text = suggest_draft(self.review)
        self.assertEqual(title, "Ligera y resistente")
        self.assertEqual(text, "Llevo un mes.")

    def test_trailing_prose_after_the_json_is_ignored(self):
        answer = _answer()
        answer["content"][0]["text"] += "\n\nEspero que te sirva."
        with patch("reviews.suggest._post", return_value=answer):
            title, _ = suggest_draft(self.review)
        self.assertEqual(title, "Cumple lo que promete")

    def test_an_unreadable_answer_says_so_instead_of_crashing(self):
        with patch("reviews.suggest._post", return_value={"content": []}):
            with self.assertRaises(SuggestionUnavailable) as broken:
                suggest_draft(self.review)
        self.assertIn("no se entiende", str(broken.exception))

    def test_without_a_template_it_asks_for_one_and_spends_nothing(self):
        Config.objects.filter(pk=1).update(suggestion_prompt="")
        with patch("reviews.suggest._post") as post:
            with self.assertRaises(SuggestionUnavailable) as missing:
                suggest_draft(self.review)
        self.assertIn("plantilla", str(missing.exception))
        post.assert_not_called()
        self.assertEqual(Config.load().suggestions_used, 0)

    def test_the_panel_shows_the_proposal_once_it_arrives(self):
        with patch("reviews.suggest._post",
                    return_value=_answer("Ligera y resistente", "Llevo un mes.")):
            response = self.client.post(
                reverse("review_suggest", args=[self.review.pk]),
                {"action": "suggest", "notes": "Cierra bien", "rating": "4"},
            )
        self.assertContains(response, "Ligera y resistente")
        saved = Review.objects.get(pk=self.review.pk)
        self.assertEqual(saved.suggestion_title, "Ligera y resistente")
        # Still nothing written into the review itself: only "Incorporar"
        # moves a proposal across, and only the editor saves it.
        self.assertEqual(saved.title, "")
        self.assertEqual(saved.status, Review.Status.PENDING)


class ReferenceCorpusTests(TestCase):
    """How the corpus fills itself. The bar is "validated by Amazon *and*
    written here", because the rows the confirmation email creates on its own
    hold a 250-character excerpt — as an example that would teach the model to
    stop mid-sentence."""

    def _review(self, **kwargs):
        fields = dict(product_title="Funda", title="Buen titular", rating=4,
                       text="Un texto completo y propio.", text_is_complete=True,
                       status=Review.Status.PUBLISHED)
        fields.update(kwargs)
        return Review.objects.create(**fields)

    def test_a_review_written_here_joins_the_corpus(self):
        review = self._review()
        remembered = ReferenceReview.remember(review)
        self.assertIsNotNone(remembered)
        self.assertEqual(remembered.title, "Buen titular")
        self.assertEqual(remembered.rating, 4)
        self.assertEqual(remembered.source_review, review)

    def test_a_truncated_excerpt_never_does(self):
        self.assertIsNone(ReferenceReview.remember(
            self._review(text="Empieza y se corta a los 250…",
                          text_is_complete=False)))
        self.assertEqual(ReferenceReview.objects.count(), 0)

    def test_a_review_with_no_headline_never_does(self):
        # A proposal has to produce a headline, so an example without one
        # teaches nothing about the half that's missing.
        self.assertIsNone(ReferenceReview.remember(self._review(title="")))

    def test_remembering_twice_keeps_one_row(self):
        review = self._review()
        first = ReferenceReview.remember(review)
        second = ReferenceReview.remember(review)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ReferenceReview.objects.count(), 1)

    def test_the_corpus_survives_its_review_being_deleted(self):
        review = self._review()
        ReferenceReview.remember(review)
        review.delete()
        surviving = ReferenceReview.objects.get()
        self.assertIsNone(surviving.source_review)
        self.assertEqual(surviving.text, "Un texto completo y propio.")

    def test_a_proposal_pasted_almost_as_it_came_is_kept_but_switched_off(self):
        # The drift guard. From here on most reviews start as a proposal built
        # from this very corpus; feeding those back in unfiltered would have
        # the suggestions imitating their own output until the user's voice
        # was gone from it entirely.
        proposal = ("Este carro de la compra destaca por su buena calidad y "
                    "diseño práctico. La estructura metálica es firme, las "
                    "ruedas son sólidas y permiten desplazarlo sin esfuerzo, y "
                    "el conjunto no resulta pesado. El interior está aislado.")
        review = self._review(suggestion=proposal,
                               text=proposal.replace("firme", "sólida"))
        remembered = ReferenceReview.remember(review)
        self.assertIsNotNone(remembered)          # kept — he may want it later
        self.assertFalse(remembered.is_example)   # but never teaching on its own

    def test_keeping_the_whole_proposal_and_adding_to_it_is_still_not_his(self):
        # The realistic near-miss, and the one `autojunk` used to wave through:
        # every word of the proposal survives, with a paragraph of his own
        # stuck on the end. That is the proposal's voice with a postscript.
        proposal = ("Este carro de la compra destaca por su buena calidad y "
                    "diseño práctico. La estructura metálica es firme, las "
                    "ruedas son sólidas y permiten desplazarlo sin esfuerzo, y "
                    "el conjunto no resulta pesado. El interior está aislado.")
        review = self._review(
            suggestion=proposal,
            text=proposal + " Lo uso a diario para la compra del mercado.")
        self.assertFalse(ReferenceReview.remember(review).is_example)

    def test_a_proposal_he_actually_rewrote_counts_as_his(self):
        review = self._review(
            suggestion=("Este carro de la compra destaca por su buena calidad "
                        "y diseño práctico. La estructura metálica es firme."),
            text=("Cogí el gris. Pesa poco y las ruedas van finas por el "
                  "adoquinado, que es donde otros se atascan. El forro de "
                  "dentro se me ha enganchado ya una vez con una lata."),
        )
        self.assertTrue(ReferenceReview.remember(review).is_example)

    def test_a_review_written_with_no_proposal_at_all_is_his_by_definition(self):
        self.assertTrue(ReferenceReview.remember(self._review(suggestion="")).is_example)

    def test_pinned_examples_are_never_pushed_out_by_recent_ones(self):
        core = ReferenceReview.objects.create(
            product_title="La buena", rating=4, title="Mi mejor titular",
            text="El tono que quiero que imite.", is_pinned=True)
        for n in range(5):
            ReferenceReview.objects.create(product_title=f"Nueva {n}", rating=4,
                                            title=f"Reciente {n}", text=f"Texto {n}")
        picked = list(ReferenceReview.examples(2))
        self.assertEqual(picked[0], core)
        self.assertEqual(picked[1].title, "Reciente 4")

    def test_switched_off_examples_stay_out_even_when_pinned(self):
        ReferenceReview.objects.create(
            product_title="Retirada", rating=4, title="No", text="...",
            is_pinned=True, is_example=False)
        self.assertEqual(list(ReferenceReview.examples(5)), [])


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
