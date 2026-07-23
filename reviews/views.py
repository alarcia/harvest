from datetime import date
from urllib.parse import urlencode

from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from packages.views import wants_fragment

from .models import Review, VineCycle

STATUS_LABELS = {
    Review.Status.PENDING: "Pendiente",
    Review.Status.APPROVED: "Aprobada",
    Review.Status.PUBLISHED: "Publicada",
}


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
    with written reviews at the bottom. A past cycle is reachable via
    ?cycle=<starts_on>, deliberately out of the way (this is rare, "hacer
    reseñas pasadas" territory): its backlog shows too, just never as
    urgent — urgency is a current-cycle concept. Full page normally, bare
    fragment for the Calendario/Reseñas nav-pill swap and for the cycle/
    toggle controls.

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

    # A pending review with no known order date can't be placed in any
    # cycle — it means the "Pedido" email was never ingested, only a later
    # one (e.g. straight from a review-published match). Rather than guess,
    # it stays off this list entirely; the review-published email closes it
    # into "Reseñas escritas" on its own whenever it arrives.
    pending = base.filter(status=Review.Status.PENDING, package__ordered_on__isnull=False)
    pending = (pending.filter(package__ordered_on__gte=cycle.starts_on,
                               package__ordered_on__lte=cycle.ends_on)
               if cycle else pending.none())

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

    confirmed = sorted(
        base.filter(status__in=[Review.Status.APPROVED, Review.Status.PUBLISHED]),
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
        "include_non_vine": include_non_vine,
        "vencidas": vencidas,
        "pendientes": pendientes,
        "confirmed": confirmed,
    }
    template = "reviews/_reviews.html" if wants_fragment(request) else "reviews/reviews.html"
    return render(request, template, context)


def review_detail(request, pk):
    """Read-only detail card for a review of any status — pending backlog
    rows and written (approved/published) rows alike open it — swapped into
    the shared #modal slot."""
    review = get_object_or_404(
        Review.objects.select_related("package", "package__pickup_point"), pk=pk
    )
    return render(request, "reviews/_review_detail.html", {
        "review": review,
        "status_label": STATUS_LABELS.get(review.status, review.status),
    })
