import logging
from datetime import timedelta

from django.conf import settings
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from reviews.models import Review

from . import calendar
from .forms import PackageForm
from .ingest import _sync_review_for_package, scan_now, set_review_due
from .models import Package, PickupPoint, RawEmail

# Same logger the worker writes its audit trail with, so a manual sweep reads
# identically — just in the web container's log (`docker logs harvest-web`).
logger = logging.getLogger("packages.ingest")

# Re-export calendar constants and helpers for backwards compatibility
VIEW_WEEKS = calendar.VIEW_WEEKS
STATE_TAGS = calendar.STATE_TAGS
_URGENCY = calendar.URGENCY_RANK
_PREVIEW_GRACE_DAYS = calendar.PREVIEW_GRACE_DAYS
_PEPE_CLOSED_WEEKDAYS = calendar.PEPE_CLOSED_WEEKDAYS
_PEPE_WARN_WEEKDAY = calendar.PEPE_WARN_WEEKDAY
_STATE_LABELS = calendar.STATE_LABELS
_COUNT_DESC = calendar.COUNT_DESC_PATTERN
_SOURCES = calendar.SOURCE_FAMILIES
_AWAITING_PICKUP_KINDS = calendar.AWAITING_PICKUP_KINDS
_MANUAL_PICKUP_KINDS = calendar.MANUAL_PICKUP_KINDS

_parse_anchor = calendar.parse_anchor
_monday = calendar.monday
_effective_estimate = calendar.effective_estimate
_estimate_note = calendar.estimate_note
_shop_closed_on = calendar.shop_closed_on
_waiting_note = calendar.waiting_note
_preview_leaves_day = calendar.preview_leaves_day
_short_day = calendar.short_day
_long_day = calendar.long_day
_estimate_line = calendar.estimate_line
_state_label = calendar.state_label
_label = calendar.package_label
_point_label = calendar.point_label
_source = calendar.source_family
_marks = calendar.package_marks
_chips = calendar.get_chips
_day_chips = calendar.day_chips
_day_pickup_summary = calendar.day_pickup_summary
_can_confirm_pickup = calendar.can_confirm_pickup


def _nav(view, anchor, direction=None):
    """URL pair for a nav control: `get` carries the animation direction,
    `push` is the clean URL that ends up in the address bar."""
    url = f"{reverse('home')}?view={view}&anchor={anchor.isoformat()}"
    return {"get": f"{url}&dir={direction}" if direction else url, "push": url}


def wants_fragment(request):
    """True for a genuine htmx swap, false for a full page load *and* for a
    history-restore request. htmx tags every request it makes with
    HX-Request, including the one it fires after a browser-back cache miss
    (sessionStorage is per-tab and iOS Safari purges it freely) — but that
    request replaces the *whole document*, so serving it a bare fragment
    renders as raw, chromeless HTML instead of the page it was on."""
    return (request.headers.get("HX-Request") == "true"
            and request.headers.get("HX-History-Restore-Request") != "true")


def home(request):
    """The calendar. Full page normally, bare fragment for HTMX swaps."""
    today = timezone.localdate()
    # Default to the fortnight agenda — this week's trip and the next
    # one's — with the month grid one tap away as the overview.
    fallback = "fortnight"
    view = request.GET.get("view", fallback)
    if view not in ("month", "week", "fortnight"):
        view = fallback
    anchor = calendar.parse_anchor(request.GET.get("anchor"), today)
    direction = request.GET.get("dir")

    if view == "month":
        first = anchor.replace(day=1)
        next_first = (first + timedelta(days=31)).replace(day=1)
        start = calendar.monday(first)
        n_weeks = ((next_first - timedelta(days=1) - start).days // 7) + 1
        prev_anchor, next_anchor = (first - timedelta(days=1)).replace(day=1), next_first
        month = first
    else:
        start = calendar.monday(anchor)
        n_weeks = calendar.VIEW_WEEKS[view]
        prev_anchor, next_anchor = start - timedelta(weeks=n_weeks), start + timedelta(weeks=n_weeks)
        month = None

    end = start + timedelta(weeks=n_weeks, days=-1)
    chips = calendar.get_chips(start, end, today)
    weeks = calendar.build_calendar_weeks(start, n_weeks, today, month, chips)

    context = {
        "view": view,
        "month": month,
        "range_start": start,
        "range_end": end,
        "weeks": weeks,
        # Emails the parser choked on: never silently dropped, so they get a
        # red banner until someone (an agent, probably) sorts them out.
        "parse_failures": RawEmail.objects.exclude(parse_error="")
                                          .order_by("-received_at", "-created_at")[:3],
        # The reviews nav pill's nag badge — same query on both pages.
        "vencidas_count": Review.objects.vencidas(today).count(),
        # Direction of travel decides the swap animation; no direction = fade.
        "anim": {"next": "slide-next", "prev": "slide-prev"}.get(direction, "fade"),
        "nav": {
            "prev": _nav(view, prev_anchor, "prev"),
            "next": _nav(view, next_anchor, "next"),
            "today": _nav(view, today),
            # Where the calendar is right now: what it refetches when a
            # package changes under it (the manual pickup confirmation).
            "current": _nav(view, anchor),
            # Switching views recenters on today: the calendar is about the
            # coming weeks, not about wandering off into other periods.
            "views": [(v, label, _nav(v, today)) for v, label in
                      (("month", "Mes"), ("fortnight", "Quincena"), ("week", "Semana"))],
        },
    }
    template = "packages/_calendar.html" if wants_fragment(request) else "packages/calendar.html"
    return render(request, template, context)


def day_detail(request, day):
    """One day blown up into the modal slot: the same chips the cell shows,
    but big enough to read and tap. The whole day cell opens this — on a phone
    the in-cell chips are dots or slivers — and each row leads on to the
    package card, which draws a ‹ back to here via ?from_day."""
    the_day = calendar.parse_anchor(day, None)
    if the_day is None:
        raise Http404("Bad date")
    today = timezone.localdate()
    raw_chips = calendar.get_chips(the_day, the_day, today)
    return render(request, "packages/_day_detail.html", {
        "day": the_day,
        "chips": calendar.day_chips(raw_chips, the_day),
        "pickup_summary": calendar.day_pickup_summary(raw_chips),
    })


def _package_card(request, pkg, back_day):
    """Renders the package card. Shared by the tapped chip and by the manual
    pickup confirmation, which lands back on the very same card."""
    point = pkg.pickup_point
    today = timezone.localdate()
    in_transit = pkg.state == Package.State.IN_TRANSIT
    return render(request, "packages/_package_detail.html", {
        "package": pkg,
        "label": calendar.package_label(pkg),
        "point_label": calendar.point_label(point),
        "point_maps_url": point.maps_url,
        "source": calendar.source_family(point),
        "state_label": calendar.state_label(pkg),
        # The card is where the delivery window gets spelled out in full: the
        # chip only has room to say "Estimado", so this is the one place the
        # user can see what Amazon actually promised.
        "estimate_line": calendar.estimate_line(pkg, today) if in_transit else "",
        # Only meaningful while in_transit: once the real "Entregado" email
        # sets pkg.deadline, that's what the card shows instead.
        "preview_leaves_day": (calendar.preview_leaves_day(pkg, today)
                               if in_transit else None),
        "can_confirm_pickup": calendar.can_confirm_pickup(pkg),
        # Parcel-or-letter is only ever a real question in a Pepe y Dalda
        # notice, which is the one email that says which; everywhere else the
        # row would just say "Paquete" on every card. Including on an Amazon
        # order delivered to that same counter — it has the shop's kind but
        # an Amazon order number, and Amazon never sends letters.
        "show_item_kind": (point.kind == PickupPoint.Kind.PEPE_Y_DALDA
                           and not pkg.order_id),
        # The shop's closing days, but only while there's still a trip to
        # plan: on a package already collected it's trivia. Loud on a
        # Monday, when the "Listo para recoger" line above would otherwise
        # send the user out to a shuttered door; a quiet reminder otherwise.
        "closed_days": (calendar.PEPE_CLOSED_WEEKDAYS
                        if (point.kind == PickupPoint.Kind.PEPE_Y_DALDA
                            and pkg.state == Package.State.AWAITING_PICKUP)
                        else ""),
        "closed_today": calendar.shop_closed_on(point, today),
        # Set when the card was opened from a day modal: draws the ‹ control
        # that swaps that day back in.
        "back_day": back_day,
    })


def package_detail(request, pk):
    """Minimal product card for a tapped chip, swapped into the modal slot."""
    pkg = get_object_or_404(Package.objects.select_related("pickup_point"), pk=pk)
    return _package_card(request, pkg, calendar.parse_anchor(request.GET.get("from_day"), None))


def confirm_pickup(request, pk):
    """Manual "ya lo he recogido", for the pickups no email ever confirms.

    Amazon pickups close themselves — the "Se ha recogido" email is final
    truth (see CLAUDE.md). Two cases never get that email (see
    _MANUAL_PICKUP_KINDS): a package diverted to a carrier's office after a
    failed home delivery, which leaves Amazon's lifecycle for good (their
    status reads "Entregado" and nothing else ever arrives), and anything at
    Pepe y Dalda, whose single "Recepción…" notice is the whole
    correspondence. Without this both would sit `awaiting_pickup` on the
    board forever.

    GET renders a confirmation step rather than acting on the tap: the day is
    the whole point of the dialog. Marking it "today" when the trip was
    yesterday would file the pickup on the wrong calendar day *and* start the
    review clock a day late, so the date is asked for, defaulted to today,
    and validated — never in the future, never before the package was at the
    point.

    Unlike an email pickup, this confirms **only this package**: the sweep of
    the whole point exists because the email is unreliable about its own
    scope, while a tap on one card is not.
    """
    pkg = get_object_or_404(Package.objects.select_related("pickup_point"), pk=pk)
    if not calendar.can_confirm_pickup(pkg):
        raise Http404("Not a carrier pickup awaiting confirmation")

    today = timezone.localdate()
    back_day = calendar.parse_anchor(request.GET.get("from_day")
                                     or request.POST.get("from_day"), None)
    error, day = None, today

    if request.method == "POST":
        day = calendar.parse_anchor(request.POST.get("picked_up_on"), None)
        if day is None:
            error = "Fecha no válida."
        elif day > today:
            error = "No puedes recoger un paquete en el futuro."
        elif pkg.actual_arrival and day < pkg.actual_arrival:
            error = "Ese día el paquete todavía no estaba en el punto."
        if error is None:
            pkg.state = Package.State.PICKED_UP
            pkg.picked_up_on = day
            pkg.save(update_fields=["state", "picked_up_on", "updated_at"])
            # A pickup is a pickup: the review clock starts the same way it
            # would have from the email.
            _sync_review_for_package(pkg)
            set_review_due(pkg, day)
            response = _package_card(request, pkg, back_day)
            # The chip behind the modal is now stale (it still says "Listo" on
            # the wrong day), so the calendar refetches itself — see the
            # hx-trigger on #app-view.
            response["HX-Trigger"] = "package-updated"
            return response
        day = day or today

    return render(request, "packages/_confirm_pickup.html", {
        "package": pkg,
        "label": calendar.package_label(pkg),
        "point_label": calendar.point_label(pkg.pickup_point),
        "point_maps_url": pkg.pickup_point.maps_url,
        "source": calendar.source_family(pkg.pickup_point),
        "day": day,
        "today": today,
        "min_day": pkg.actual_arrival,
        "error": error,
        "back_day": back_day,
    })


def picked_detail(request, day):
    """The consolidated pickup chip's card: every item picked up on one day.

    A single trip can empty several counters and lockers, so this lists them
    all — whatever point each sat in — the way tapping one chip should reveal
    the whole day's haul."""
    picked_day = calendar.parse_anchor(day, None)
    packages = (Package.objects
                .filter(state=Package.State.PICKED_UP, picked_up_on=picked_day)
                .select_related("pickup_point")
                .order_by("pickup_point__name", "pk")) if picked_day else []
    items = [{
        "package": pkg,
        "label": calendar.package_label(pkg),
        "point_label": calendar.point_label(pkg.pickup_point),
        "point_maps_url": pkg.pickup_point.maps_url,
        "source": calendar.source_family(pkg.pickup_point),
    } for pkg in packages]
    return render(request, "packages/_picked_detail.html", {
        "day": picked_day,
        "items": items,
        "back_day": calendar.parse_anchor(request.GET.get("from_day"), None),
    })


def delivered_detail(request, day, point_id):
    """The consolidated per-address delivery chip's card: every item
    delivered to one home on one day.

    Unlike a pickup, a delivery only ever concerns the one address it landed
    at, so this stays scoped to `point_id` — two homes on the same day are
    two separate chips, each opening its own card."""
    the_day = calendar.parse_anchor(day, None)
    packages = [
        pkg for pkg in (Package.objects
                         .filter(state=Package.State.DELIVERED, pickup_point_id=point_id)
                         .select_related("pickup_point")
                         .order_by("pk"))
        if the_day and (pkg.actual_arrival or pkg.estimated_arrival) == the_day
    ] if the_day else []
    items = [{"package": pkg, "label": calendar.package_label(pkg)} for pkg in packages]
    return render(request, "packages/_delivered_detail.html", {
        "day": the_day,
        "point_label": calendar.point_label(packages[0].pickup_point) if packages else "",
        "point_maps_url": packages[0].pickup_point.maps_url if packages else "",
        "items": items,
        "back_day": calendar.parse_anchor(request.GET.get("from_day"), None),
    })


def _ingest_pill(request, message, *, error=False):
    """The one-line answer a manual sweep leaves under the topbar button."""
    return render(request, "packages/_ingest_result.html",
                  {"message": message, "error": error})


@require_POST
def ingest_now(request):
    """The topbar's ⟳: sweep the inbox right now instead of waiting for the
    worker's next cycle.

    The `ingest` worker polls every 10 minutes and remains the audit trail
    (see CLAUDE.md); this covers the minutes in between, when an email has
    just landed and the user wants it on the board *before* planning the trip.
    It's the same `scan_inbox`, idempotent by Message-ID, so pressing it twice
    — or pressing it while the worker is mid-sweep — costs an IMAP login and
    nothing else. It does pick up stored *failures* though (see
    `process_message`): pressing ⟳ after deploying a parser fix is enough to
    clear the red banner, which is what the user reaches for it to do.

    Synchronous on purpose: the inbox self-cleans (processed mail goes to
    Trash), so a sweep is a handful of messages and a second or two, and
    answering "2 correos nuevos" outright beats a background job the page
    would then have to poll. A mailbox that's down is reported on the pill and
    logged, never raised: the calendar stays exactly as it was.
    """
    if not (settings.GMAIL_IMAP_USER and settings.GMAIL_IMAP_APP_PASSWORD):
        return _ingest_pill(request, "Buzón sin configurar", error=True)
    try:
        stats = scan_now()
    except Exception as exc:
        logger.warning("Escaneo manual fallido: %s: %s", type(exc).__name__, exc)
        return _ingest_pill(request, "No se pudo leer el buzón", error=True)

    parts = []
    if stats["new"]:
        parts.append("1 correo nuevo" if stats["new"] == 1
                     else f"{stats['new']} correos nuevos")
    if stats["fixed"]:
        # Old mail that had been stuck behind the red banner and parses now
        # that the parser learned its template. Said out loud because pressing
        # ⟳ right after a deploy is exactly how the user reaches for it, and
        # "sin correos nuevos" would read as "nothing happened".
        parts.append("1 correo reprocesado" if stats["fixed"] == 1
                     else f"{stats['fixed']} correos reprocesados")
    if stats["failed"]:
        # The red banner spells these out on the refresh below; the pill only
        # says there are some, so the user knows to look down.
        parts.append("1 sin procesar" if stats["failed"] == 1
                     else f"{stats['failed']} sin procesar")
    response = _ingest_pill(request, " · ".join(parts) or "Sin correos nuevos",
                            error=bool(stats["failed"]))
    if stats["new"] or stats["fixed"] or stats["failed"]:
        # Something changed under the view (new chips, or a new red banner):
        # reuse the trigger the manual pickup confirmation already fires, so
        # the section refetches itself in place — same view, same anchor, no
        # URL change. The topbar, pill included, sits outside #app-view, so
        # the refresh never wipes the answer.
        response["HX-Trigger"] = "package-updated"
    return response


def add_package(request):
    """Manual entry, open to anyone Cloudflare Access already let through.

    No login of our own: the app never distinguishes between the two
    allowlisted users. This is the only way alt-store packages get in at
    all, since that store generates no email.
    """
    if request.method == "POST":
        form = PackageForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("home")
    else:
        form = PackageForm()
    return render(request, "packages/package_form.html", {"form": form})
