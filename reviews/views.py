from datetime import date, timedelta
import math
from urllib.parse import urlencode

from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from packages.models import Package
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


def _ordered_on(review, fallback=date.max):
    return review.package.ordered_on if review.package and review.package.ordered_on else fallback


def _due_on(review, fallback=date.max):
    due = review.effective_due_on or fallback
    return (due, _ordered_on(review, fallback))


def _confirmed_on(review):
    return review.approved_on or review.published_on or review.created_at.date()


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


def _gamification_status(today_reviews, today_target):
    """Computes the golf-themed daily gamification status, CSS class, and title.

    Scale (relative to today_target >= 1):
    - 0 to < mitad: 😴 (Hay que espabilar)
    - mitad to today_target - 1: ✍️ (En progreso)
    - today_target: ¡Par!
    - today_target + 1: 🐦 ¡Birdie!
    - today_target + 2: 🦅 ¡Eagle!
    - today_target + 3: 🪶 ¡Albatros!
    - today_target + 4+: 🔥 ¡On fire!
    """
    diff = today_reviews - today_target
    half = today_target / 2

    if diff < 0:
        if today_reviews < half:
            return "😴", "status-asleep", "Hay que espabilar"
        return "✍️", "status-progress", "¡En progreso!"
    elif diff == 0:
        return "¡Par!", "status-par", "¡Objetivo diario cumplido!"
    elif diff == 1:
        return "🐦 ¡Birdie!", "status-birdie", "¡1 por encima del objetivo!"
    elif diff == 2:
        return "🦅 ¡Eagle!", "status-eagle", "¡2 por encima del objetivo!"
    elif diff == 3:
        return "🪶 ¡Albatros!", "status-albatross", "¡3 por encima del objetivo!"
    else:
        return "🔥 ¡On fire!", "status-fire", "¡En racha!"


def _cycle_stats(cycle, today):
    """Computes Amazon Vine evaluation metrics for the given cycle.

    Always strictly Vine-scoped (ignores the non-vine toggle):
    - orders_count: Vine packages ordered inside the cycle, plus historical
      package-less reviews in the cycle (guaranteed >= written_reviews).
    - reviews_count: Written (approved or published) Vine reviews in the cycle.
    - pct: Percentage of reviewed orders (0-100%).
    - pct_class: 'danger' (<30%), 'warning' (<60%), 'success' (>=90%), or 'neutral'.
    - days_remaining: Days left until cycle.ends_on (if current cycle).
    - gold_achieved: True if reviews_count >= 80 and pct >= 90.
    - gold_needed_reviews: max(0, 80 - reviews_count).
    - needed_for_90: Extra reviews needed to reach 90% of current orders.
    - needed_for_60: Extra reviews needed to reach 60% of current orders.
    - is_risk_zone: True if pct < 60 and orders_count > 0 and is_current.
    - today_reviews: Vine reviews written today in this cycle.
    - today_target: Daily review goal (immutable during the day, min 1).
    - today_status: Golf gamification emoji / status.
    """
    if not cycle:
        return None

    vine_packages = Package.objects.filter(
        is_vine=True,
        ordered_on__gte=cycle.starts_on,
        ordered_on__lte=cycle.ends_on,
    ).count()

    package_less = Review.objects.filter(
        package__isnull=True,
    ).written().in_cycle(cycle.starts_on, cycle.ends_on).count()

    written_reviews = (
        Review.objects.vine(include_non_vine=False)
        .written()
        .in_cycle(cycle.starts_on, cycle.ends_on)
        .count()
    )

    orders_count = max(vine_packages + package_less, written_reviews)
    pct = round((written_reviews / orders_count) * 100) if orders_count > 0 else 0

    if orders_count == 0:
        pct_class = "neutral"
    elif pct < 30:
        pct_class = "danger"
    elif pct < 60:
        pct_class = "warning"
    elif pct >= 90:
        pct_class = "success"
    else:
        pct_class = "neutral"

    is_current = cycle.starts_on <= today <= cycle.ends_on
    is_past = today > cycle.ends_on
    is_future = today < cycle.starts_on

    if is_current:
        days_remaining = max(0, (cycle.ends_on - today).days)
    elif is_future:
        days_remaining = (cycle.starts_on - today).days
    else:
        days_remaining = 0

    target_90 = math.ceil(0.90 * orders_count)
    needed_for_90 = max(0, target_90 - written_reviews)

    target_60 = math.ceil(0.60 * orders_count)
    needed_for_60 = max(0, target_60 - written_reviews)

    gold_achieved = (written_reviews >= 80 and pct >= 90)
    gold_needed_reviews = max(0, 80 - written_reviews)
    is_risk_zone = (pct < 60 and orders_count > 0 and is_current)

    today_reviews = (
        Review.objects.vine(include_non_vine=False)
        .written()
        .in_cycle(cycle.starts_on, cycle.ends_on)
        .filter(
            Q(approved_on=today)
            | Q(approved_on__isnull=True, published_on=today)
            | Q(approved_on__isnull=True, published_on__isnull=True, created_at__date=today)
        )
        .count()
    ) if is_current else 0

    # Base target is immutable during the day: reviews already done today
    # are subtracted so that completing reviews today does not lower the bar
    # mid-day.
    written_at_start = max(0, written_reviews - today_reviews)

    # Order pace (last 15 days) and review target to reach/keep 90%
    recent_days = 15
    window_start = max(cycle.starts_on, today - timedelta(days=recent_days))
    recent_orders = Package.objects.filter(
        is_vine=True,
        ordered_on__gte=window_start,
        ordered_on__lte=today,
    ).count()
    window_length = max(1, (today - window_start).days + 1)
    orders_per_day = recent_orders / window_length
    orders_per_week = round(orders_per_day * 7, 1)

    horizon_days = min(30, max(7, days_remaining)) if is_current else 0
    if is_current and horizon_days > 0:
        projected_new_orders = round(orders_per_day * horizon_days)
        projected_total_orders = orders_count + projected_new_orders
        target_reviews = math.ceil(0.90 * projected_total_orders)
        needed_at_start = max(0, target_reviews - written_at_start)
        target_reviews_per_day = math.ceil(needed_at_start / horizon_days) if needed_at_start > 0 else 0
        target_reviews_per_week = target_reviews_per_day * 7
        today_target = max(1, target_reviews_per_day)
    else:
        horizon_days = 0
        target_reviews_per_day = 0
        target_reviews_per_week = 0
        today_target = 1

    today_status, today_class, today_title = _gamification_status(today_reviews, today_target)

    return {
        "orders_count": orders_count,
        "reviews_count": written_reviews,
        "pct": pct,
        "pct_bar_width": min(pct, 100),
        "pct_class": pct_class,
        "is_current": is_current,
        "is_past": is_past,
        "is_future": is_future,
        "days_remaining": days_remaining,
        "gold_achieved": gold_achieved,
        "gold_needed_reviews": gold_needed_reviews,
        "needed_for_90": needed_for_90,
        "needed_for_60": needed_for_60,
        "is_risk_zone": is_risk_zone,
        "orders_per_day": round(orders_per_day, 1),
        "orders_per_week": orders_per_week,
        "horizon_days": horizon_days,
        "target_reviews_per_day": target_reviews_per_day,
        "target_reviews_per_week": target_reviews_per_week,
        "today_reviews": today_reviews,
        "today_target": today_target,
        "today_status": today_status,
        "today_gamification_class": today_class,
        "today_title": today_title,
    }


def reviews_list(request):
    """The reviews module's landing page: the *current* Vine cycle's backlog
    by default — drafts first (to edit and finish them), then urgent, then the
    plain backlog (earliest due date first) — with the reviews written *in that
    same cycle* at the bottom. The backlog is only ever products he already has:
    a package still on its way, or still waiting at a counter, owes nothing yet.

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
    non_vine_only = request.GET.get("non_vine") == "1"
    if raw_cycle:
        cycle = _find_cycle(raw_cycle)
        if cycle is None or not navigable.filter(pk=cycle.pk).exists():
            return redirect(_reviews_url(None, non_vine_only))
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

    base = Review.objects.select_related("package", "package__pickup_point")

    # The chore, at both its stages: still to write (`pending`) and written
    # but not yet on Amazon (`draft`). Same cycle rule for both, since a
    # draft is the very same row a day later.
    open_reviews = base.filter(status__in=Review.EDITABLE,
                                package__ordered_on__isnull=False).received()
    open_reviews = list(
        open_reviews.filter(package__ordered_on__gte=cycle.starts_on,
                             package__ordered_on__lte=cycle.ends_on)
        if cycle else open_reviews.none()
    )

    confirmed_reviews = list(
        base.written().in_cycle(cycle.starts_on, cycle.ends_on) if cycle else base.none()
    )

    # Partition reviews by Vine vs Non-Vine (both are rendered into DOM for instant toggle)
    vine_open = [r for r in open_reviews if r.is_vine]
    vine_pending = [r for r in vine_open if not r.is_draft]
    vine_borradores = sorted((r for r in vine_open if r.is_draft),
                             key=lambda r: _ordered_on(r, date.max))
    if is_current:
        vine_vencidas = sorted((r for r in vine_pending if r.due_on and r.due_on <= today),
                               key=_due_on)
        vencidas_ids = {r.pk for r in vine_vencidas}
        vine_pendientes = sorted((r for r in vine_pending if r.pk not in vencidas_ids),
                                 key=_due_on)
    else:
        vine_vencidas = []
        vine_pendientes = sorted(vine_pending, key=_due_on)
    vine_confirmed = sorted((r for r in confirmed_reviews if r.is_vine),
                            key=_confirmed_on, reverse=True)

    non_vine_open = [r for r in open_reviews if not r.is_vine]
    non_vine_borradores = sorted((r for r in non_vine_open if r.is_draft),
                                 key=lambda r: _ordered_on(r, date.max))
    non_vine_pendientes = sorted((r for r in non_vine_open if not r.is_draft),
                                 key=_due_on)
    non_vine_vencidas = []  # non-Vine purchases are voluntary; never urgent
    non_vine_confirmed = sorted((r for r in confirmed_reviews if not r.is_vine),
                                key=_confirmed_on, reverse=True)

    if non_vine_only:
        borradores = non_vine_borradores
        vencidas = non_vine_vencidas
        pendientes = non_vine_pendientes
        confirmed = non_vine_confirmed
        inactive_borradores = vine_borradores
        inactive_vencidas = vine_vencidas
        inactive_pendientes = vine_pendientes
        inactive_confirmed = vine_confirmed
    else:
        borradores = vine_borradores
        vencidas = vine_vencidas
        pendientes = vine_pendientes
        confirmed = vine_confirmed
        inactive_borradores = non_vine_borradores
        inactive_vencidas = non_vine_vencidas
        inactive_pendientes = non_vine_pendientes
        inactive_confirmed = non_vine_confirmed

    # Reviews from other cycles, available in the DOM for instant global search in the client.
    current_ids = {r.pk for r in open_reviews} | {r.pk for r in confirmed_reviews}
    other_open = list(
        base.filter(status__in=Review.EDITABLE, package__ordered_on__isnull=False)
        .received()
        .exclude(pk__in=current_ids)
    )
    other_borradores = sorted(
        (r for r in other_open if r.is_draft),
        key=lambda r: _ordered_on(r, date.max)
    )
    other_pendientes = sorted(
        (r for r in other_open if not r.is_draft),
        key=_due_on
    )
    other_confirmed = sorted(
        base.written().exclude(pk__in=current_ids),
        key=_confirmed_on,
        reverse=True
    )

    context = {
        "active_nav": "reviews",
        "vencidas_count": Review.objects.vencidas(today).count(),  # global, unfiltered by the toggle
        "cycle": cycle,
        "stats": _cycle_stats(cycle, today),
        "is_current_cycle": is_current,
        "prev_cycle_url": _reviews_url(prev_cycle, non_vine_only) if prev_cycle else None,
        "next_cycle_url": _reviews_url(next_cycle, non_vine_only) if next_cycle else None,
        "current_cycle_url": _reviews_url(None, non_vine_only) if not is_current else None,
        "toggle_url": _reviews_url(None if is_current else cycle, not non_vine_only),
        "self_url": _reviews_url(None if is_current else cycle, non_vine_only),
        "include_non_vine": non_vine_only,
        "non_vine_only": non_vine_only,
        "vencidas": vencidas,
        "borradores": borradores,
        "pendientes": pendientes,
        "confirmed": confirmed,
        "inactive_borradores": inactive_borradores,
        "inactive_vencidas": inactive_vencidas,
        "inactive_pendientes": inactive_pendientes,
        "inactive_confirmed": inactive_confirmed,
        "other_borradores": other_borradores,
        "other_pendientes": other_pendientes,
        "other_confirmed": other_confirmed,
        "today": today,
    }
    template = "reviews/_reviews.html" if wants_fragment(request) else "reviews/reviews.html"
    return render(request, template, context)


def _review_card(
    request, review, active_tab="review", error=None, notes=None, rating=None,
    draft_title=None, draft_text=None,
):
    """Renders the review card. When editable (pending or draft), it provides
    both the Reseña reading view and the Notas y sugerencia panel behind
    segmented tabs."""
    is_draft = review.is_draft
    can_edit = review.status in Review.EDITABLE
    current_title = (draft_title if draft_title is not None else review.title).strip()
    current_text = (draft_text if draft_text is not None else review.text).strip()
    sug_title = review.suggestion_title.strip()
    sug_text = review.suggestion.strip()
    is_incorporated = bool(
        sug_text
        and current_title == sug_title
        and current_text == sug_text
    )
    would_replace = bool(
        not is_incorporated
        and (current_title or current_text)
    )
    has_written_draft = bool(review.title.strip() or review.text.strip())
    status_label = "Borrador" if is_draft else STATUS_LABELS.get(review.status, review.status)
    actual_notes = notes if notes is not None else review.notes
    return render(request, "reviews/_review_detail.html", {
        "review": review,
        "status_label": status_label,
        # Pending ⇒ nothing written yet, draft ⇒ rewrite what's there. Past
        # that the text is a record of what's on Amazon, not a working copy.
        "can_edit": can_edit,
        "has_draft": has_written_draft,
        # Only a draft with written text can be closed by hand: there has to be something
        # written before "ya la he publicado" can mean anything.
        "can_approve": has_written_draft and is_draft,
        "active_tab": active_tab,
        "stars": STAR_VALUES,
        "rating": rating if rating is not None else review.rating,
        "notes": actual_notes,
        "has_notes": bool(actual_notes and actual_notes.strip()),
        "draft_title": draft_title if draft_title is not None else review.title,
        "draft_text": draft_text if draft_text is not None else review.text,
        "suggestions_enabled": is_configured(),
        "error": error,
        "is_incorporated": is_incorporated,
        "would_replace": would_replace,
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
        "has_notes": bool(review.notes and review.notes.strip()),
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
    tab = request.GET.get("tab", "review")
    return _review_card(request, review, active_tab=tab)


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
    if review.status != Review.Status.DRAFT or not (review.title.strip() or review.text.strip()):
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

    if action in ("back", "incorporate"):
        return _editor(request, review, draft_title, rating, draft_text)

    return _review_card(
        request, review, active_tab="suggest", error=error, notes=notes, rating=rating,
        draft_title=draft_title, draft_text=draft_text,
    )

