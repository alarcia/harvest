import logging
import re
from collections import defaultdict
from datetime import date, timedelta

from django.conf import settings
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.views.decorators.http import require_POST

from reviews.models import Review

from .forms import PackageForm
from .ingest import _sync_review_for_vine, scan_now, set_review_due
from .models import Package, PickupPoint, RawEmail

# Same logger the worker writes its audit trail with, so a manual sweep reads
# identically — just in the web container's log (`docker logs harvest-web`).
logger = logging.getLogger("packages.ingest")

# Weeks shown per view. Month is special-cased: its length depends on the anchor.
VIEW_WEEKS = {"week": 1, "fortnight": 2}

# The visual grammar: one chip = one mark on one day, keyed by a rendering
# kind. Several kinds share one model state — how a day relates to the
# deadline decides the kind, never a new state in the database.
#   ordered            — order placed ("Pedido" email). Hollow dot, no box.
#   shipped             — shipping notice ("Enviado"). Filled dot, no box.
#   estimated           — tentative arrival ("Llega el lunes"). Dashed box; gone once it lands.
#                         Never sits in the past: an estimate Amazon missed
#                         rides on today until the package really arrives
#                         (see _effective_estimate), with a note saying why.
#   deadline_estimated,
#   leaves_estimated    — forecast of the last-safe/"antes del" days, from the
#                         estimated arrival plus the pickup point's observed
#                         grace window (see _PREVIEW_GRACE_DAYS). Same red
#                         dashed box as "leaves": a guess, superseded the
#                         moment the real "Entregado" email sets the real
#                         deadline and the package leaves in_transit.
#   waiting             — sitting at the pickup point, marked once on today. Filled box.
#   deadline            — last safe day ("antes del 14" ⇒ the 13th). Red filled box.
#   leaves              — the "antes del" day itself: may leave at any moment. Red dashed
#                         box — dashed meaning uncertain, same grammar as "estimated".
#   action_needed       — awaiting pickup at a carrier's office (see
#                         PickupPoint.Kind.CARRIER), marked once on today like
#                         "waiting". No known deadline, but *more* urgent, not
#                         less — a failed delivery needs an active trip, not a
#                         routine one — so it borrows "deadline"'s solid red
#                         instead of "waiting"'s passive source color, plus a
#                         ⚠ mark (user, 2026-07-24: must read as distinct from
#                         the passive "Listo"/"Entregado").
#   picked              — confirmed picked up that day. Muted + check.
STATE_TAGS = {
    "ordered": "Pedido",
    "shipped": "Enviado",
    "estimated": "Estimado",
    "deadline_estimated": "Último día",
    "leaves_estimated": "Se va",
    "waiting": "Listo",
    "deadline": "Último día",
    "leaves": "Se va",
    "action_needed": "Recoger ya",
    "picked": "Recogido",
    "delivered": "Entregado",
}

# Within a day, red first, then actionable, then informational. The certain
# facts (ordered/shipped) sort before the "estimated" guess: when both share a
# day, "Enviado" reads before "Estimado" (a fact beats a promise). The
# deadline/leaves forecasts are guesses too, so they sort with "estimated".
# action_needed sits with deadline/leaves: no date attached, but the most
# urgent thing on the board regardless.
_URGENCY = {"deadline": 0, "leaves": 0, "action_needed": 0, "waiting": 1,
            "shipped": 2, "ordered": 2,
            "estimated": 3, "deadline_estimated": 3, "leaves_estimated": 3,
            "picked": 4, "delivered": 4}

# Grace observed between the "Entregado" email (actual_arrival) and the
# "antes del" deadline it carries: 3 days at a Locker, 7 at a Counter,
# consistent across every real package so far (checked 2026-07-23 against
# production data and fixtures). Not something the parser ever reads or
# calculates — the real deadline always comes from the email — but stable
# enough to *forecast* it for a package still in_transit, before that email
# arrives. The alt store and home deliveries have no deadline at all, so
# they're absent here and get no preview.
_PREVIEW_GRACE_DAYS = {
    PickupPoint.Kind.AMAZON_LOCKER: 3,
    PickupPoint.Kind.AMAZON_COUNTER: 7,
}


def _effective_estimate(pkg, today):
    """The day an in-transit package's arrival is *currently* expected: the
    estimate the email gave, or today once that day has come and gone.

    An estimate is a promise, not a fact, and it slips — Amazon missed the
    single day it named, or the "Pedido" gave a window ("Llegada entre el 24
    de julio y el 28 de julio", fixture 023) whose first day passed with
    nothing at the point. The package is still on its way either way, so the
    mark rides on today instead of sitting in the past: a stale estimate on a
    past day is both a claim we know to be false and effectively invisible,
    since the board is read forwards. Same grammar as "leaves", which already
    rides on today once the deadline passes unconfirmed.

    Only in_transit packages get this — an arrival that actually happened is a
    fact with its own date."""
    if not pkg.estimated_arrival:
        return None
    return max(pkg.estimated_arrival, today)


def _estimate_note(pkg, today):
    """The parenthetical on an "Estimado" chip that has moved onto today, so
    it can't be read as a promise that the package lands today — the mistake
    that would send the user on a wasted trip. Empty while the estimate still
    sits where the email put it."""
    if not pkg.estimated_arrival or today <= pkg.estimated_arrival:
        return ""
    end = pkg.estimated_arrival_end
    if end and today <= end:
        return f"hasta el {_short_day(end)}"
    return "con retraso"


# Pepe y Dalda's shutters, printed at the foot of every email they send
# ("Lunes cerrado. Martes a sábado de 10:30 a 13:30 y de 17 a 20 h. Domingo
# cerrado"). Only **Monday** raises a warning on the board (user,
# 2026-07-25): a shop shut on a Sunday surprises nobody, but a Monday chip
# still reading "Listo" is exactly how a wasted trip gets planned. Both days
# are named on the card, which has room to be complete.
_PEPE_CLOSED_WEEKDAYS = "los domingos y los lunes"
_PEPE_WARN_WEEKDAY = 0  # Monday, per date.weekday()


def _shop_closed_on(point, day):
    """Is this a day the point is shut *and* worth warning about?"""
    return (point.kind == PickupPoint.Kind.PEPE_Y_DALDA
            and day.weekday() == _PEPE_WARN_WEEKDAY)


def _waiting_note(pkg, today):
    """"3 días" — how long a deadline-less package has been on the counter.

    Only for the points that never expire: their chip is redrawn on today
    every day, so without this it reads exactly the same on day one and on
    day nine. Empty on the day it arrives, when the chip's position already
    says everything.

    A closing day displaces the count: how long it's been waiting is a
    nudge, "you cannot fetch it today" is a fact, and only one of them fits
    on a chip."""
    if _shop_closed_on(pkg.pickup_point, today):
        return "cerrado hoy"
    if not pkg.actual_arrival:
        return ""
    days = (today - pkg.actual_arrival).days
    if days < 1:
        return ""
    return "1 día" if days == 1 else f"{days} días"


def _preview_leaves_day(pkg, today):
    """The forecasted "antes del" day for an in-transit package at a pickup
    point with a known grace window, or None. A guess from the estimated
    arrival — never a substitute for the real deadline once it's read from
    the "Entregado" email.

    Hangs off the *effective* estimate, so it moves with it: a forecast built
    on an arrival day we no longer believe would put red dashed boxes in the
    past, which is the same incongruence one step further down the chain."""
    grace = _PREVIEW_GRACE_DAYS.get(pkg.pickup_point.kind)
    arrival = _effective_estimate(pkg, today)
    if not (arrival and grace):
        return None
    return arrival + timedelta(days=grace)


def _short_day(day):
    """"28 jul" — a chip-sized day. The month is never dropped: a window can
    cross into the next one, and "hasta el 2" for a 28-July-to-2-August window
    reads as a day already past."""
    return date_format(day, r"j b")


def _long_day(day):
    """"viernes 24 de julio" — the card's spelling, weekday included: the user
    plans a trip by day of the week. Month always spelled out, on both ends of
    a window, for the same reason _short_day keeps it."""
    return date_format(day, r"l j \d\e F")


def _estimate_line(pkg, today):
    """The card's "Llegada estimada" value, or "" when there's nothing to say.

    Four readings of the same two fields, so the sentence never claims more
    than the email did: a single named day, a window still ahead, a window
    already running ("entre hoy y el…"), and either of them overrun, where it
    switches to the past tense and admits the delay."""
    start, end = pkg.estimated_arrival, pkg.estimated_arrival_end
    if not start:
        return ""
    if end and today > end:
        return f"se esperaba entre el {_long_day(start)} y el {_long_day(end)} · con retraso"
    if end and today > start:
        return f"entre hoy y el {_long_day(end)}"
    if end:
        return f"entre el {_long_day(start)} y el {_long_day(end)}"
    if today > start:
        return f"se esperaba el {_long_day(start)} · con retraso"
    return _long_day(start)

_STATE_LABELS = {
    Package.State.IN_TRANSIT: "En camino",
    Package.State.AWAITING_PICKUP: "Listo para recoger",
    Package.State.PICKED_UP: "Recogido",
    Package.State.DELIVERED: "Entregado",
    Package.State.RETURNED: "Devuelto",
}


def _state_label(pkg):
    """The modal's "Estado" line. A carrier pickup reuses AWAITING_PICKUP but
    must not read as the calm "Listo para recoger" — same reasoning as the
    action_needed chip kind, so the card doesn't undercut the calendar's own
    red warning the moment the user taps in for the actionable detail."""
    if (pkg.state == Package.State.AWAITING_PICKUP
            and pkg.pickup_point.kind == PickupPoint.Kind.CARRIER):
        return "Recoger ya en el transportista"
    return _STATE_LABELS.get(pkg.state, pkg.state)

# A description that names only a count, not a product: picked-up / delivered
# emails with no item links whose subject was just "N productos" or "Entregado:
# N producto". These name nothing, so the chip shows an honest placeholder
# rather than echoing the boilerplate (the state tag already says Recogido /
# Entregado, so repeating it would be the redundant "Entregado · Entregado…").
# Matches both fresh ingests (empty description) and legacy rows already stored.
_COUNT_DESC = re.compile(
    r"^(?:entregado:?\s*)?\d+\s+productos?(?:\s*\|?\s*n\.?º de pedido.*)?$",
    re.IGNORECASE,
)


def _label(pkg):
    """The product name to print on a chip, or a placeholder when unknown."""
    desc = (pkg.description or "").strip()
    return desc if desc and not _COUNT_DESC.match(desc) else "Producto desconocido"


def _point_label(point):
    """Human name for a pickup point. Amazon venues already read
    "Amazon Locker/Counter - …"; home and alt-store need a word to say what
    kind of place it is.

    Always `point.label`, never `point.name`: the stored name is the email's
    own wording, kept verbatim so ingestion can match venues by it, and the
    user overrides it in the admin with the short name he actually says out
    loud (PickupPoint.display_name)."""
    if point.kind == PickupPoint.Kind.HOME:
        return f"Entrega a domicilio · {point.label}"
    if point.kind == PickupPoint.Kind.ALT_STORE:
        # "Otros" (not "Tienda"): the non-Amazon bucket is various stores and
        # drop-off spots, all handled the same, distinct from Amazon.
        return f"Otros · {point.label}"
    if point.kind == PickupPoint.Kind.CARRIER:
        return f"Recogida en transportista · {point.label}"
    # Pepe y Dalda names itself, address included (the signature its emails
    # sign off with), so it needs no prefix — same as an Amazon venue.
    return point.label


# Which colour family a chip belongs to. Three sources, not two: Pepe y
# Dalda is its own category beside Amazon and the "Otros" bucket (user,
# 2026-07-25), so it gets its own hue rather than borrowing the alt store's.
_SOURCES = {
    PickupPoint.Kind.ALT_STORE: "store",
    PickupPoint.Kind.PEPE_Y_DALDA: "pepe",
}


def _source(point):
    return _SOURCES.get(point.kind, "amazon")


def _marks(pkg, today):
    """(day, kind, note) triples for one package — the board shows the present
    and the future, not history. Superseded states are purged: the order mark
    upgrades to the shipping mark, "estimated" dies when the package lands,
    "waiting" paints only today (not every day of the remaining window), and a
    picked-up package leaves nothing but the check on its day. `note` is a small
    qualifier shown in parentheses, empty for most marks."""
    if pkg.state == Package.State.IN_TRANSIT:
        fact_day, fact_kind = None, None
        if pkg.shipped_on:
            fact_day, fact_kind = pkg.shipped_on, "shipped"
        elif pkg.ordered_on:
            fact_day, fact_kind = pkg.ordered_on, "ordered"
        marks = []
        est_day = _effective_estimate(pkg, today)
        # Ship and estimated arrival on the *same* day ("Enviado hoy, llega
        # hoy", the rare same-day delivery): one chip that says both, so the
        # arrival still shows where the user looks for it instead of vanishing.
        # Only while the estimate still sits where the email put it
        # (`est_day == pkg.estimated_arrival`): one that slipped onto today is
        # a different, weaker statement and keeps its own chip to say so.
        if est_day and est_day == pkg.estimated_arrival == fact_day:
            note = "llega hoy" if fact_day == today else "llega el mismo día"
            marks.append((fact_day, fact_kind, note))
        else:
            if fact_kind:
                marks.append((fact_day, fact_kind, ""))
            if est_day:
                marks.append((est_day, "estimated", _estimate_note(pkg, today)))
        leaves_day = _preview_leaves_day(pkg, today)
        if leaves_day:
            marks.append((leaves_day - timedelta(days=1), "deadline_estimated", "estimado"))
            marks.append((leaves_day, "leaves_estimated", "estimado"))
        return marks

    if pkg.state == Package.State.AWAITING_PICKUP:
        if pkg.pickup_point.kind == PickupPoint.Kind.CARRIER:
            # UPS never gives a deadline either, but unlike the alt store this
            # isn't mild — a failed delivery needs an active trip today, so it
            # gets its own louder mark instead of falling into "waiting" below.
            return [(today, "action_needed", "")]
        if not pkg.deadline:
            # Nothing ever expires here (the alt store and Pepe y Dalda both
            # just hold it), so the mark rides on today, walking one cell
            # forward every day it isn't collected — that walk *is* the
            # urgency, in the absence of a deadline to go red about. The note
            # says how long it's been sitting there, since a chip that keeps
            # moving otherwise erases the one fact that makes it pressing.
            return [(today, "waiting", _waiting_note(pkg, today))]
        last_safe = pkg.deadline - timedelta(days=1)
        if today > pkg.deadline:
            # Past the deadline, not confirmed picked: per the misleading
            # "no longer available" email, it usually is still there.
            return [(today, "leaves", "")]
        marks = []
        if today < last_safe:
            marks.append((today, "waiting", ""))
        if today <= last_safe:
            marks.append((last_safe, "deadline", ""))
        marks.append((pkg.deadline, "leaves", ""))
        return marks

    if pkg.state == Package.State.PICKED_UP:
        day = pkg.picked_up_on or pkg.actual_arrival
        return [(day, "picked", "")] if day else []

    if pkg.state == Package.State.DELIVERED:
        # Home delivery: a single mark on the day it landed. No trip, no
        # deadline — just a record that it arrived.
        day = pkg.actual_arrival or pkg.estimated_arrival
        return [(day, "delivered", "")] if day else []

    return []  # returned: gone from the board


def _chips(start, end, today):
    chips = []
    packages = (Package.objects
                .exclude(state=Package.State.RETURNED)
                .select_related("pickup_point"))
    for pkg in packages:
        source = _source(pkg.pickup_point)
        label = _label(pkg)
        detail_url = reverse("package_detail", args=[pkg.pk])
        chips.extend(
            {"date": day, "kind": kind, "tag": STATE_TAGS[kind], "note": note,
             "label": label, "source": source, "detail_url": detail_url,
             "point_id": pkg.pickup_point_id,
             # Drawn on a day the shop is shut: earns a ⚠ on the chip itself,
             # since the grid gets read without opening anything. Only marks
             # a chip you'd act on — a pickup already made needs no warning.
             "closed": (kind == "waiting"
                        and _shop_closed_on(pkg.pickup_point, day))}
            for day, kind, note in _marks(pkg, today) if start <= day <= end
        )
    return chips


def _day_chips(chips, day):
    """One day's chips, sorted by urgency, with same-day pickups and same-day,
    same-address deliveries each collapsed into a recap chip. A pickup trip
    empties several points at once, so *every* pickup that day folds into one
    "N productos" chip (see picked_detail). Deliveries instead fold per
    address: two homes getting packages the same day is rare, and each is a
    different person to tell "this is what arrived (or should arrive)", so
    they stay separate chips — only deliveries to the *same* home collapse
    (see delivered_detail)."""
    todays = [c for c in chips if c["date"] == day]
    picked = [c for c in todays if c["kind"] == "picked"]
    if len(picked) > 1:
        rest = [c for c in todays if c["kind"] != "picked"]
        todays = rest + [{
            "date": day,
            "kind": "picked",
            "tag": STATE_TAGS["picked"],
            "note": "",
            "label": f"{len(picked)} productos",
            # Amazon wins a mixed trip (it's the bulk of any haul); a day of
            # pickups from one other source keeps that source's own colour
            # rather than falling back to the "Otros" grape.
            "source": ("amazon" if any(c["source"] == "amazon" for c in picked)
                       else picked[0]["source"]),
            "detail_url": reverse("picked_detail", args=[day.isoformat()]),
            "closed": False,  # a pickup already made: nothing to warn about
        }]

    delivered_by_point = defaultdict(list)
    for c in todays:
        if c["kind"] == "delivered":
            delivered_by_point[c["point_id"]].append(c)
    if any(len(group) > 1 for group in delivered_by_point.values()):
        rest = [c for c in todays if c["kind"] != "delivered"]
        collapsed = []
        for point_id, group in delivered_by_point.items():
            if len(group) > 1:
                collapsed.append({
                    "date": day,
                    "kind": "delivered",
                    "tag": STATE_TAGS["delivered"],
                    "note": "",
                    "label": f"{len(group)} productos",
                    "source": group[0]["source"],
                    "point_id": point_id,
                    "detail_url": reverse("delivered_detail", args=[day.isoformat(), point_id]),
                    "closed": False,
                })
            else:
                collapsed.extend(group)
        todays = rest + collapsed

    return sorted(todays, key=lambda c: _URGENCY[c["kind"]])


def _monday(day):
    return day - timedelta(days=day.weekday())


def _parse_anchor(value, fallback):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback


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
    anchor = _parse_anchor(request.GET.get("anchor"), today)
    direction = request.GET.get("dir")

    if view == "month":
        first = anchor.replace(day=1)
        next_first = (first + timedelta(days=31)).replace(day=1)
        start = _monday(first)
        n_weeks = ((next_first - timedelta(days=1) - start).days // 7) + 1
        prev_anchor, next_anchor = (first - timedelta(days=1)).replace(day=1), next_first
        month = first
    else:
        start = _monday(anchor)
        n_weeks = VIEW_WEEKS[view]
        prev_anchor, next_anchor = start - timedelta(weeks=n_weeks), start + timedelta(weeks=n_weeks)
        month = None

    end = start + timedelta(weeks=n_weeks, days=-1)
    chips = _chips(start, end, today)

    weeks = []
    for w in range(n_weeks):
        days = []
        for i in range(7):
            day = start + timedelta(weeks=w, days=i)
            days.append({
                "date": day,
                "is_today": day == today,
                "is_past": day < today,
                "in_month": month is None or day.month == month.month,
                "chips": _day_chips(chips, day),
            })
        weeks.append({"number": days[0]["date"].isocalendar()[1], "days": days})

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
    the_day = _parse_anchor(day, None)
    if the_day is None:
        raise Http404("Bad date")
    today = timezone.localdate()
    return render(request, "packages/_day_detail.html", {
        "day": the_day,
        "chips": _day_chips(_chips(the_day, the_day, today), the_day),
    })


# Points whose pickups no email will ever confirm, so the user closes them
# by hand from the card: a carrier's office (Amazon abandons that lifecycle
# the moment the delivery fails) and Pepe y Dalda — whether the shop's own
# notice put the package there (that email is the whole correspondence) or an
# Amazon order was addressed to its counter, which Amazon signs off as
# "entregado" and then goes quiet. The alt store stays out — it has no emails
# at all, so it's manual end to end and lives in the admin.
_MANUAL_PICKUP_KINDS = frozenset({
    PickupPoint.Kind.CARRIER, PickupPoint.Kind.PEPE_Y_DALDA,
})


def _can_confirm_pickup(pkg):
    """Whether the card offers the manual "ya lo he recogido" (see
    confirm_pickup). Only for the points above: everything else keeps closing
    itself from email, and a manual button there would only invite closing a
    package the email would have closed correctly anyway."""
    return (pkg.state == Package.State.AWAITING_PICKUP
            and pkg.pickup_point.kind in _MANUAL_PICKUP_KINDS)


def _package_card(request, pkg, back_day):
    """Renders the package card. Shared by the tapped chip and by the manual
    pickup confirmation, which lands back on the very same card."""
    point = pkg.pickup_point
    today = timezone.localdate()
    in_transit = pkg.state == Package.State.IN_TRANSIT
    return render(request, "packages/_package_detail.html", {
        "package": pkg,
        "label": _label(pkg),
        "point_label": _point_label(point),
        "source": _source(point),
        "state_label": _state_label(pkg),
        # The card is where the delivery window gets spelled out in full: the
        # chip only has room to say "Estimado", so this is the one place the
        # user can see what Amazon actually promised.
        "estimate_line": _estimate_line(pkg, today) if in_transit else "",
        # Only meaningful while in_transit: once the real "Entregado" email
        # sets pkg.deadline, that's what the card shows instead.
        "preview_leaves_day": (_preview_leaves_day(pkg, today)
                                if in_transit else None),
        "can_confirm_pickup": _can_confirm_pickup(pkg),
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
        "closed_days": (_PEPE_CLOSED_WEEKDAYS
                        if (point.kind == PickupPoint.Kind.PEPE_Y_DALDA
                            and pkg.state == Package.State.AWAITING_PICKUP)
                        else ""),
        "closed_today": _shop_closed_on(point, today),
        # Set when the card was opened from a day modal: draws the ‹ control
        # that swaps that day back in.
        "back_day": back_day,
    })


def package_detail(request, pk):
    """Minimal product card for a tapped chip, swapped into the modal slot."""
    pkg = get_object_or_404(Package.objects.select_related("pickup_point"), pk=pk)
    return _package_card(request, pkg, _parse_anchor(request.GET.get("from_day"), None))


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
    if not _can_confirm_pickup(pkg):
        raise Http404("Not a carrier pickup awaiting confirmation")

    today = timezone.localdate()
    back_day = _parse_anchor(request.GET.get("from_day")
                              or request.POST.get("from_day"), None)
    error, day = None, today

    if request.method == "POST":
        day = _parse_anchor(request.POST.get("picked_up_on"), None)
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
            _sync_review_for_vine(pkg)
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
        "label": _label(pkg),
        "point_label": _point_label(pkg.pickup_point),
        "source": _source(pkg.pickup_point),
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
    picked_day = _parse_anchor(day, None)
    packages = (Package.objects
                .filter(state=Package.State.PICKED_UP, picked_up_on=picked_day)
                .select_related("pickup_point")
                .order_by("pickup_point__name", "pk")) if picked_day else []
    items = [{
        "package": pkg,
        "label": _label(pkg),
        "point_label": _point_label(pkg.pickup_point),
        "source": _source(pkg.pickup_point),
    } for pkg in packages]
    return render(request, "packages/_picked_detail.html", {
        "day": picked_day,
        "items": items,
        "back_day": _parse_anchor(request.GET.get("from_day"), None),
    })


def delivered_detail(request, day, point_id):
    """The consolidated per-address delivery chip's card: every item
    delivered to one home on one day.

    Unlike a pickup, a delivery only ever concerns the one address it landed
    at, so this stays scoped to `point_id` — two homes on the same day are
    two separate chips, each opening its own card."""
    the_day = _parse_anchor(day, None)
    packages = [
        pkg for pkg in (Package.objects
                         .filter(state=Package.State.DELIVERED, pickup_point_id=point_id)
                         .select_related("pickup_point")
                         .order_by("pk"))
        if the_day and (pkg.actual_arrival or pkg.estimated_arrival) == the_day
    ] if the_day else []
    items = [{"package": pkg, "label": _label(pkg)} for pkg in packages]
    return render(request, "packages/_delivered_detail.html", {
        "day": the_day,
        "point_label": _point_label(packages[0].pickup_point) if packages else "",
        "items": items,
        "back_day": _parse_anchor(request.GET.get("from_day"), None),
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
