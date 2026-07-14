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
#   waiting   — sitting at the pickup point, one mark per remaining day. Filled box.
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

# Within a day, red first, then actionable, then informational.
_URGENCY = {"deadline": 0, "leaves": 0, "waiting": 1, "estimated": 2,
            "shipped": 3, "ordered": 3, "picked": 4, "delivered": 4}

_STATE_LABELS = {
    Package.State.IN_TRANSIT: "En camino",
    Package.State.AWAITING_PICKUP: "Listo para recoger",
    Package.State.PICKED_UP: "Recogido",
    Package.State.DELIVERED: "Entregado",
    Package.State.RETURNED: "Devuelto",
}


def _marks(pkg, today):
    """(day, kind) pairs for one package — the board shows the present and
    the future, not history. Superseded states are purged: the order mark
    upgrades to the shipping mark, "estimated" dies when the package lands,
    "waiting" paints only the remaining window (today → deadline), and a
    picked-up package leaves nothing but the check on its day."""
    if pkg.state == Package.State.IN_TRANSIT:
        marks = []
        if pkg.shipped_on:
            marks.append((pkg.shipped_on, "shipped"))
        elif pkg.ordered_on:
            marks.append((pkg.ordered_on, "ordered"))
        if pkg.estimated_arrival:
            marks.append((pkg.estimated_arrival, "estimated"))
        return marks

    if pkg.state == Package.State.AWAITING_PICKUP:
        if not pkg.deadline:  # alt store never expires: today's cell only
            return [(today, "waiting")]
        last_safe = pkg.deadline - timedelta(days=1)
        if today > pkg.deadline:
            # Past the deadline, not confirmed picked: per the misleading
            # "no longer available" email, it usually is still there.
            return [(today, "leaves")]
        marks = []
        day = today
        while day < last_safe:
            marks.append((day, "waiting"))
            day += timedelta(days=1)
        if today <= last_safe:
            marks.append((last_safe, "deadline"))
        marks.append((pkg.deadline, "leaves"))
        return marks

    if pkg.state == Package.State.PICKED_UP:
        day = pkg.picked_up_on or pkg.actual_arrival
        return [(day, "picked")] if day else []

    if pkg.state == Package.State.DELIVERED:
        # Home delivery: a single mark on the day it landed. No trip, no
        # deadline — just a record that it arrived.
        day = pkg.actual_arrival or pkg.estimated_arrival
        return [(day, "delivered")] if day else []

    return []  # returned: gone from the board


def _chips(start, end, today):
    chips = []
    packages = (Package.objects
                .exclude(state=Package.State.RETURNED)
                .select_related("pickup_point"))
    for pkg in packages:
        source = ("store" if pkg.pickup_point.kind == PickupPoint.Kind.ALT_STORE
                  else "amazon")
        label = pkg.description or f"Paquete {pkg.pk}"
        chips.extend(
            {"date": day, "kind": kind, "tag": STATE_TAGS[kind],
             "label": label, "source": source, "package_id": pkg.pk}
            for day, kind in _marks(pkg, today) if start <= day <= end
        )
    return chips


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
                "chips": sorted((c for c in chips if c["date"] == day),
                                key=lambda c: _URGENCY[c["kind"]]),
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
    # Amazon pickup names already read "Amazon Locker/Counter - …"; the home
    # and alt-store cases need a word to say what kind of place it is.
    if point.kind == PickupPoint.Kind.HOME:
        point_label = f"Entrega a domicilio · {point.name}"
    elif point.kind == PickupPoint.Kind.ALT_STORE:
        # "Otros" (not "Tienda"): the non-Amazon bucket is various stores and
        # drop-off spots, all handled the same, distinct from Amazon.
        point_label = f"Otros · {point.name}"
    else:
        point_label = point.name
    return render(request, "packages/_package_detail.html", {
        "package": pkg,
        "point_label": point_label,
        "source": ("store" if point.kind == PickupPoint.Kind.ALT_STORE
                   else "amazon"),
        "state_label": _STATE_LABELS.get(pkg.state, pkg.state),
    })


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
