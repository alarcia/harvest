from datetime import date
from urllib.parse import urlencode

from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from packages.views import wants_fragment

from .models import Review, VineCycle
from .suggest import SuggestionUnavailable, is_configured, suggest_draft

STATUS_LABELS = {
    Review.Status.PENDING: "Pendiente",
    Review.Status.DRAFT: "Borrador",
    Review.Status.APPROVED: "Aprobada",
    Review.Status.PUBLISHED: "Publicada",
}

# Rendered left-to-right by the star widget's own `row-reverse`, so the DOM
# order has to be 5→1 for the pure-CSS "fill everything after the checked
# one" rule to light up 1…N. See `.rev-stars` in _reviews.html.
STAR_VALUES = [5, 4, 3, 2, 1]


def _ordered_on(review, fallback):
    return review.package.ordered_on if review.package and review.package.ordered_on else fallback


def _confirmed_on(review):
    return review.published_on or review.approved_on or review.created_at.date()


def _reviews_url(cycle=None, non_vine=False):
    params = {}
    if cycle is not None:
        params["cycle"] = cycle.starts_on.isoformat()
    if non_vine:
        params["non_vine"] = "1"
    query = urlencode(params)
    base = reverse("reviews_list")
    return f"{base}?{query}" if query else base


def _find_cycle(raw):
    """The VineCycle a raw ?cycle= value names, or None for anything that
    isn't a real row — malformed, or a well-formed date that simply has no
    cycle (pre-2020, mid-cycle, or any other date no row starts on)."""
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return None
    return VineCycle.objects.filter(starts_on=parsed).first()


def reviews_list(request):
    """The reviews module's landing page: the *current* Vine cycle's backlog
    by default — urgent first, then the plain backlog (oldest order first) —
    with the reviews written *in that same cycle* at the bottom. The backlog
    is only ever products he already has: a package still on its way, or still
    waiting at a counter, owes nothing yet.

    A past cycle is reachable via ?cycle=<starts_on>, deliberately out of the
    way (this is rare, "hacer reseñas pasadas" territory): its backlog shows
    too, just never as urgent — urgency is a current-cycle concept. Full page
    normally, bare fragment for the Calendario/Reseñas nav-pill swap and for
    the cycle/toggle controls.

    A ?cycle= that isn't a *navigable* cycle — one that either doesn't exist
    as a row (hand-typed, a stale bookmark, a boundary predating the seed) or
    exists but is one of the empty placeholder rows the 2020-2031 seed
    created (no reviews in it, and not today's) — redirects to the canonical
    current-cycle URL rather than silently rendering the current cycle's data
    under a URL that names a different one. Every link the page itself
    generates already only points at navigable cycles, so this only fires on
    a URL that didn't come from clicking through the app."""
    today = timezone.localdate()
    current_cycle = VineCycle.current(today)
    navigable = VineCycle.navigable(current_cycle)

    raw_cycle = request.GET.get("cycle")
    include_non_vine = request.GET.get("non_vine") == "1"
    if raw_cycle:
        cycle = _find_cycle(raw_cycle)
        if cycle is None or not navigable.filter(pk=cycle.pk).exists():
            return redirect(_reviews_url(None, include_non_vine))
    else:
        cycle = current_cycle
    is_current = cycle is not None and current_cycle is not None and cycle.pk == current_cycle.pk

    # Prev/next step only through navigable cycles, so the paginator never
    # offers an empty placeholder row as a destination. `next` is naturally
    # bounded by today's cycle (nothing later ever has reviews), but the
    # guard keeps it explicit.
    prev_cycle = (navigable.filter(starts_on__lt=cycle.starts_on)
                  .order_by("-starts_on").first()) if cycle else None
    next_cycle = None
    if cycle and not is_current:
        next_cycle = (navigable.filter(starts_on__gt=cycle.starts_on)
                      .order_by("starts_on").first())

    base = (Review.objects
            .select_related("package", "package__pickup_point")
            .vine(include_non_vine))

    # The chore, at both its stages: still to write (`pending`) and written
    # but not yet on Amazon (`draft`). Same cycle rule for both, since a
    # draft is the very same row a day later.
    #
    # A review with no known order date can't be placed in any cycle — it
    # means the "Pedido" email was never ingested, only a later one (e.g.
    # straight from a review-published match). Rather than guess, it stays
    # off this list entirely; the review-published email closes it into
    # "Reseñas escritas" on its own whenever it arrives.
    #
    # The Review row exists from the order onward, but the chore is only shown
    # once the product is in his hands: `received()` keeps out everything
    # still travelling or still waiting on a counter. This is state-based, so
    # a manual state change is reflected without needing another email.
    open_reviews = base.filter(status__in=Review.EDITABLE,
                                package__ordered_on__isnull=False).received()
    open_reviews = list(
        open_reviews.filter(package__ordered_on__gte=cycle.starts_on,
                             package__ordered_on__lte=cycle.ends_on)
        if cycle else open_reviews.none()
    )
    pending = [r for r in open_reviews if r.status == Review.Status.PENDING]
    # Drafts never join the urgent group even when overdue: the badge and the
    # ⚠ list have to name the same rows (see `ReviewQuerySet.vencidas`), and
    # with a backlog that's mostly overdue already, writing a draft has to
    # visibly *move* the row somewhere. It still says how late it is on its
    # own row.
    borradores = sorted((r for r in open_reviews if r.status == Review.Status.DRAFT),
                         key=lambda r: _ordered_on(r, date.max))

    if is_current:
        vencidas = sorted((r for r in pending if r.due_on and r.due_on <= today),
                           key=lambda r: r.due_on)
        vencidas_ids = {r.pk for r in vencidas}
        pendientes = sorted((r for r in pending if r.pk not in vencidas_ids),
                             key=lambda r: _ordered_on(r, date.max))
    else:
        # Browsing history: nothing is "urgent" outside the current cycle,
        # per the cycle's whole point (last cycle's backlog is demoted, not
        # deleted) — just the plain backlog for that period.
        vencidas = []
        pendientes = sorted(pending, key=lambda r: _ordered_on(r, date.max))

    # Written reviews belong to a cycle too — the history section is "what I
    # wrote for this period", not an ever-growing pile repeated on every
    # page. Filed by order date like the backlog, except that a package-less
    # row (historical import, or one the "Gracias por tu reseña" email
    # created on its own) falls back to when it was written; see
    # `_cycle_date`. No fallback for pending above, on purpose: there the
    # cycle drives nagging.
    confirmed = sorted(
        base.written().in_cycle(cycle.starts_on, cycle.ends_on) if cycle else base.none(),
        key=_confirmed_on, reverse=True,
    )

    context = {
        "active_nav": "reviews",
        "vencidas_count": Review.objects.vencidas(today).count(),  # global, unfiltered by the toggle
        "cycle": cycle,
        "is_current_cycle": is_current,
        "prev_cycle_url": _reviews_url(prev_cycle, include_non_vine) if prev_cycle else None,
        "next_cycle_url": _reviews_url(next_cycle, include_non_vine) if next_cycle else None,
        "current_cycle_url": _reviews_url(None, include_non_vine) if not is_current else None,
        "toggle_url": _reviews_url(None if is_current else cycle, not include_non_vine),
        # This very page: what the section refetches when an ingest sweep
        # changes something under it (the topbar's "procesar ahora" button,
        # which can close a review or create a pending one).
        "self_url": _reviews_url(None if is_current else cycle, include_non_vine),
        "include_non_vine": include_non_vine,
        "vencidas": vencidas,
        "borradores": borradores,
        "pendientes": pendientes,
        "confirmed": confirmed,
        "today": today,
    }
    template = "reviews/_reviews.html" if wants_fragment(request) else "reviews/reviews.html"
    return render(request, template, context)


def _review_card(request, review):
    """Renders the review card. Shared by the tapped row and by every action
    that lands back on it after doing its bit — the same idiom the calendar's
    manual pickup confirmation follows."""
    is_draft = review.status == Review.Status.DRAFT
    return render(request, "reviews/_review_detail.html", {
        "review": review,
        "status_label": STATUS_LABELS.get(review.status, review.status),
        # Pending ⇒ nothing written yet, draft ⇒ rewrite what's there. Past
        # that the text is a record of what's on Amazon, not a working copy.
        "can_edit": review.status in Review.EDITABLE,
        "has_draft": is_draft,
        # Only a draft can be closed by hand: there has to be something
        # written before "ya la he publicado" can mean anything.
        "can_approve": is_draft,
    })


def _editable_review(pk):
    """The review behind an action that writes to it, or 404. Everything past
    `draft` is on Amazon already and answers 404 to the editor, the
    suggestion panel and the approve step alike — one rule, one place."""
    review = get_object_or_404(
        Review.objects.select_related("package", "package__pickup_point"), pk=pk
    )
    if review.status not in Review.EDITABLE:
        raise Http404("Not an editable review")
    return review


def _editor(request, review, title, rating, text, error=None):
    """The draft form itself, rendered from explicit values rather than from
    the row — a rejected save has to give back what was *typed*, not what is
    stored."""
    return render(request, "reviews/_review_edit.html", {
        "review": review,
        "title": title,
        "rating": rating,
        "text": text,
        "stars": STAR_VALUES,
        "error": error,
    })


def review_detail(request, pk):
    """Detail card for a review of any status — pending backlog rows,
    drafts and written (approved/published) rows alike open it — swapped
    into the shared #modal slot. Read-only except for the actions it offers:
    the draft editor, the suggestion panel, and closing the draft once it's
    on Amazon."""
    review = get_object_or_404(
        Review.objects.select_related("package", "package__pickup_point"), pk=pk
    )
    return _review_card(request, review)


def review_edit(request, pk):
    """The draft editor: the review's own three fields — headline, stars and
    body, exactly what Amazon asks for — written by hand in the same #modal
    slot the card lives in.

    Saving is what turns a `pending` chore into a `draft`: the text exists,
    it just isn't on Amazon yet. It stays a draft (freely rewritable) until
    the "Gracias por tu reseña" email closes it as `published` — which fills
    nothing in, because the text is already there and is the user's own, so
    the truncated email excerpt never overwrites it and the corpus gets a
    complete text instead of a cut-off one.

    All three fields are required: a review missing any of them isn't one.
    The rating is validated server-side rather than with a `required` radio —
    the stars are hidden inputs behind their labels, and a browser can't
    focus a hidden control to complain about it, so it would silently refuse
    to submit instead. GET, and any POST that doesn't validate, re-render the
    form with whatever was typed; nothing is ever lost to a slip.
    """
    review = _editable_review(pk)

    error = None
    title, rating, text = review.title, review.rating, review.text

    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        text = request.POST.get("text", "").strip()
        try:
            rating = int(request.POST.get("rating", ""))
        except ValueError:
            rating = None
        if not title:
            error = "Ponle un título a la reseña."
        elif rating not in range(1, 6):
            error = "Elige una puntuación de 1 a 5 estrellas."
        elif not text:
            error = "Escribe el texto de la reseña."
        if error is None:
            review.title = title[:255]
            review.rating = rating
            review.text = text
            # Written by hand, so it's whole — unlike the excerpt the
            # published-confirmation email carries, this one belongs in the
            # corpus (see `Review.text_is_complete`).
            review.text_is_complete = True
            review.status = Review.Status.DRAFT
            review.save(update_fields=["title", "rating", "text",
                                        "text_is_complete", "status", "updated_at"])
            response = _review_card(request, review)
            # The row behind the modal has just changed group (Pendientes →
            # Borradores) and headline, so the section refetches itself —
            # same trigger the ingest sweep fires.
            response["HX-Trigger"] = "package-updated"
            return response

    return _editor(request, review, title, rating, text, error)


def review_approve(request, pk):
    """"Ya la he publicado en Amazon": the one step Harvest can't observe.

    Amazon confirms a review days later with the "Gracias por tu reseña"
    email, which is the real end of the chapter — but until it lands, a draft
    the user has already pasted in would keep sitting in the backlog asking
    to be written. This closes it in the meantime: `approved`, dated today,
    filed straight into "Reseñas escritas". The email still arrives later and
    upgrades it to `published` without touching a word of the text.

    A confirmation step rather than acting on the tap, like the calendar's
    manual pickup — it moves the row out of the working lists, and nothing in
    the UI undoes it (the admin does, as always). Unlike that one it doesn't
    ask for a day: pasting a review is something done right now, in front of
    the app, so today is right by construction.

    Only ever reachable from a `draft`: approving a `pending` review would
    file an empty row into the history.
    """
    review = get_object_or_404(
        Review.objects.select_related("package", "package__pickup_point"), pk=pk
    )
    if review.status != Review.Status.DRAFT:
        raise Http404("Not a draft awaiting approval")

    if request.method == "POST":
        review.status = Review.Status.APPROVED
        review.approved_on = timezone.localdate()
        review.save(update_fields=["status", "approved_on", "updated_at"])
        # The review is done — close every modal instead of landing back on
        # the card.  The inline script triggers the same closing animation
        # the × button uses; the empty swap that follows clears the slot.
        response = HttpResponse(
            '<script>'
            'const b=document.querySelector(".modal-slot .modal-back");'
            'if(b){closeModal(b)}'
            '</script>'
        )
        response["HX-Trigger"] = "package-updated"
        return response

    return render(request, "reviews/_review_approve.html", {"review": review})


def review_suggest(request, pk):
    """The suggestion panel — a detour *inside* the editor, not a sibling of
    it (user, 2026-08-01: "el usuario entra a la sección de escribir borrador
    y, si lo desea, tiene una sección de notas y sugerencia").

    So the editor stays the only thing that ever writes the review itself:
    this view touches `notes`, `rating` and the proposal, and nothing else.
    What it renders is either the panel or the editor — every way out of the
    panel leads back into the editor, which is where the user came from.

    **The editor's unsaved work rides along as hidden fields.** Opening the
    panel swaps the modal, and a half-written review left in the DOM would
    simply be gone; instead the headline and body travel with every request
    and are handed straight back. `rating` needs no such trick: the stars are
    the same field on both screens, on purpose, so choosing them here is
    choosing them there. Missing values fall back to what's stored, which is
    what a bare GET (a hand-typed URL, a test) gets.

    The actions:

    - **open** — arrive from the editor. Writes nothing: asking for help is
      not a decision about the review.
    - **Guardar notas** — persists them and returns. Notes are worth keeping
      on their own: they get written while the product is being used, days
      before the review does.
    - **Sugerir borrador** — requires the stars, and *only* the stars: notes
      are the normal input and the better one, but a proposal has to be
      reachable without them (see the check itself).
    - **Incorporar al borrador** — hands the proposal to the editor as its new
      headline and body. Still saves nothing: he reads it, rewrites it, and
      presses "Guardar borrador" like any other draft. Which also means
      incorporating by mistake costs one Cancelar, not a stored review.
    - **Cancelar / ‹** — back to the editor exactly as he left it.
    """
    review = _editable_review(pk)
    post = request.POST if request.method == "POST" else {}
    action = post.get("action", "open")

    # The editor's state, carried through untouched (see docstring).
    draft_title = post.get("title", review.title)
    draft_text = post.get("text", review.text)
    notes = post.get("notes", review.notes).strip()
    try:
        rating = int(post.get("rating", ""))
    except ValueError:
        rating = None if request.method == "POST" else review.rating

    error = None

    if action in ("save", "suggest", "incorporate"):
        review.notes = notes
        review.rating = rating
        review.save(update_fields=["notes", "rating", "updated_at"])

    if action == "suggest":
        # The stars are required and the notes are **not** (user, 2026-08-02).
        # Notes are the normal way in and by far the better one, but there are
        # products he has nothing to say about, and days when five reviews have
        # to be closed at once — on those, a proposal built from the product's
        # name and the rating alone is the difference between a review written
        # and a review skipped. He reads and rewrites every proposal anyway, so
        # the floor on quality is his, not the template's.
        if rating not in range(1, 6):
            error = "Elige la puntuación antes de pedir el borrador."
        else:
            try:
                review.suggestion_title, review.suggestion = suggest_draft(review)
                review.save(update_fields=["suggestion_title", "suggestion", "updated_at"])
            except SuggestionUnavailable as exc:
                error = str(exc)
    elif action == "incorporate" and review.suggestion:
        draft_title, draft_text = review.suggestion_title, review.suggestion

    if action in ("back", "save", "incorporate"):
        response = _editor(request, review, draft_title, rating, draft_text)
        # `rating` may have moved, and it shows on the draft's row.
        response["HX-Trigger"] = "package-updated"
        return response

    return render(request, "reviews/_review_suggest.html", {
        "review": review,
        "notes": notes,
        "rating": rating,
        "stars": STAR_VALUES,
        "suggestions_enabled": is_configured(),
        "error": error,
        # Handed back out unchanged on every exit.
        "draft_title": draft_title,
        "draft_text": draft_text,
        # Incorporating would overwrite something already written, so the
        # panel says so before he presses it rather than after.
        "would_replace": bool(draft_title or draft_text),
    })
