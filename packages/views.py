import re
from datetime import date, timedelta

from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .forms import PackageForm
from .models import Package, PickupPoint, RawEmail

# Weeks shown per view. Month is special-cased: its length depends on the anchor.
VIEW_WEEKS = {"week": 1, "fortnight": 2}

# The visual grammar: one chip = one mark on one day, keyed by a rendering
# kind. Several kinds share one model state — how a day relates to the
# deadline decides the kind, never a new state in the database.
#   ordered   — order placed ("Pedido" email). Hollow dot, no box.
#   shipped   — shipping notice ("Enviado"). Filled dot, no box.
#   estimated — tentative arrival ("Llega el lunes"). Dashed box; gone once it lands.
#   waiting   — sitting at the pickup point, marked once on today. Filled box.
#   deadline  — last safe day ("antes del 14" ⇒ the 13th). Red filled box.
#   leaves    — the "antes del" day itself: may leave at any moment. Red dashed
#               box — dashed meaning uncertain, same grammar as "estimated".
#   picked    — confirmed picked up that day. Muted + check.
STATE_TAGS = {
    "ordered": "Pedido",
    "shipped": "Enviado",
    "estimated": "Estimado",
    "waiting": "Listo",
    "deadline": "Último día",
    "leaves": "Se va",
    "picked": "Recogido",
    "delivered": "Entregado",
}

# Within a day, red first, then actionable, then informational. The certain
# facts (ordered/shipped) sort before the "estimated" guess: when both share a
# day, "Enviado" reads before "Estimado" (a fact beats a promise).
_URGENCY = {"deadline": 0, "leaves": 0, "waiting": 1,
            "shipped": 2, "ordered": 2, "estimated": 3,
            "picked": 4, "delivered": 4}

_STATE_LABELS = {
    Package.State.IN_TRANSIT: "En camino",
    Package.State.AWAITING_PICKUP: "Listo para recoger",
    Package.State.PICKED_UP: "Recogido",
    Package.State.DELIVERED: "Entregado",
    Package.State.RETURNED: "Devuelto",
}

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
    kind of place it is."""
    if point.kind == PickupPoint.Kind.HOME:
        return f"Entrega a domicilio · {point.name}"
    if point.kind == PickupPoint.Kind.ALT_STORE:
        # "Otros" (not "Tienda"): the non-Amazon bucket is various stores and
        # drop-off spots, all handled the same, distinct from Amazon.
        return f"Otros · {point.name}"
    return point.name


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
        # Ship and estimated arrival on the *same* day ("Enviado hoy, llega
        # hoy", the rare same-day delivery): one chip that says both, so the
        # arrival still shows where the user looks for it instead of vanishing.
        if pkg.estimated_arrival and pkg.estimated_arrival == fact_day:
            note = "llega hoy" if fact_day == today else "llega el mismo día"
            return [(fact_day, fact_kind, note)]
        marks = []
        if fact_kind:
            marks.append((fact_day, fact_kind, ""))
        if pkg.estimated_arrival:
            marks.append((pkg.estimated_arrival, "estimated", ""))
        return marks

    if pkg.state == Package.State.AWAITING_PICKUP:
        if not pkg.deadline:  # alt store never expires: today's cell only
            return [(today, "waiting", "")]
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
        source = ("store" if pkg.pickup_point.kind == PickupPoint.Kind.ALT_STORE
                  else "amazon")
        label = _label(pkg)
        detail_url = reverse("package_detail", args=[pkg.pk])
        chips.extend(
            {"date": day, "kind": kind, "tag": STATE_TAGS[kind], "note": note,
             "label": label, "source": source, "detail_url": detail_url}
            for day, kind, note in _marks(pkg, today) if start <= day <= end
        )
    return chips


def _day_chips(chips, day):
    """One day's chips, sorted by urgency, with same-day pickups collapsed into
    a single recap chip. A pickup trip empties several points at once and the
    month view has no room for a chip each; one "N productos" chip stands in,
    and tapping it lists everything that came home that day (see picked_detail).
    Only pickups collapse — home deliveries stay their own 🏠 marks."""
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
            "source": "amazon" if any(c["source"] == "amazon" for c in picked) else "store",
            "detail_url": reverse("picked_detail", args=[day.isoformat()]),
        }]
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


def home(request):
    """The calendar. Full page normally, bare fragment for HTMX swaps."""
    today = timezone.localdate()
    view = request.GET.get("view", "month")
    if view not in ("month", "week", "fortnight"):
        view = "month"
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
        # Direction of travel decides the swap animation; no direction = fade.
        "anim": {"next": "slide-next", "prev": "slide-prev"}.get(direction, "fade"),
        "nav": {
            "prev": _nav(view, prev_anchor, "prev"),
            "next": _nav(view, next_anchor, "next"),
            "today": _nav(view, today),
            # Switching views recenters on today: the calendar is about the
            # coming weeks, not about wandering off into other periods.
            "views": [(v, label, _nav(v, today)) for v, label in
                      (("month", "Mes"), ("fortnight", "Quincena"), ("week", "Semana"))],
        },
    }
    template = "packages/_calendar.html" if request.headers.get("HX-Request") else "packages/calendar.html"
    return render(request, template, context)


def package_detail(request, pk):
    """Minimal product card for a tapped chip, swapped into the modal slot."""
    pkg = get_object_or_404(Package.objects.select_related("pickup_point"), pk=pk)
    point = pkg.pickup_point
    return render(request, "packages/_package_detail.html", {
        "package": pkg,
        "label": _label(pkg),
        "point_label": _point_label(point),
        "source": ("store" if point.kind == PickupPoint.Kind.ALT_STORE
                   else "amazon"),
        "state_label": _STATE_LABELS.get(pkg.state, pkg.state),
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
        "source": ("store" if pkg.pickup_point.kind == PickupPoint.Kind.ALT_STORE
                   else "amazon"),
    } for pkg in packages]
    return render(request, "packages/_picked_detail.html",
                  {"day": picked_day, "items": items})


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
