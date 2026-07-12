from datetime import date, timedelta

from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .forms import PackageForm

# Weeks shown per view. Month is special-cased: its length depends on the anchor.
VIEW_WEEKS = {"week": 1, "fortnight": 2}

# TEMPORARY: hardcoded chips to design the day-cell rendering, local only.
# Delete once the calendar reads packages from the database.
#
# One entry = one mark on one day. Kinds map 1:1 to the visual grammar:
#   ordered   — order placed ("Pedido" email). Hollow dot, no box.
#   shipped   — shipping notice ("Enviado" email). Filled dot, no box.
#   estimated — tentative arrival ("Llega el lunes"). Dashed box; gone once it lands.
#   waiting   — sitting at the pickup point, one mark per remaining day. Filled box.
#   deadline  — last safe day ("antes del 14" ⇒ the 13th). Red filled box.
#   leaves    — the "antes del" day itself: may leave at any moment. Red dashed
#               box — dashed meaning uncertain, same grammar as "estimated".
#   picked    — confirmed picked up that day. Muted + check.
#
# The board shows the present and the future, not history. Purge rules:
#   - One mark for the latest *certain* state; a superseded one disappears
#     ("Pedido" dies when "Enviado" arrives). "Estimado" rides along while in
#     transit because it's the uncertain one, and dies when the package lands.
#   - "waiting" paints only the remaining window, today → deadline, never the
#     days it has already been sitting there. No deadline (alt store): today only.
#   - "picked" leaves a single check on its day and clears everything else.
STATE_TAGS = {
    "ordered": "Pedido",
    "shipped": "Enviado",
    "estimated": "Estimado",
    "waiting": "Listo",
    "deadline": "Último día",
    "leaves": "Se va",
    "picked": "Recogido",
}


def _c(day, label, source, kind):
    return {"date": date(2026, 7, day), "label": label, "source": source, "kind": kind}


# The samples assume "today" is Sun 2026-07-12.
SAMPLE_CHIPS = [
    # Ordered Sat 11, not shipped yet: the order mark plus the soft promise.
    _c(11, "Cargador USB-C 65W", "amazon", "ordered"),
    _c(13, "Cargador USB-C 65W", "amazon", "estimated"),
    # Shipped Fri 10 ("Pedido" of Thu 9 purged), promised for Tue 14.
    _c(10, "Funda Kindle", "amazon", "shipped"),
    _c(14, "Funda Kindle", "amazon", "estimated"),
    # At the locker since Fri 10, "antes del 14": the remaining window is
    # today (plain), the last safe day (red), and the leave day (red dashed).
    _c(12, "Auriculares JBL", "amazon", "waiting"),
    _c(13, "Auriculares JBL", "amazon", "deadline"),
    _c(14, "Auriculares JBL", "amazon", "leaves"),
    # Alt store, no deadline: it just rides on today's cell.
    _c(12, "Puzzle 1000 piezas", "store", "waiting"),
    # Picked up Fri 10: nothing left but the check on its day.
    _c(10, "Bombillas E27", "amazon", "picked"),
]


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
                "chips": [{**c, "tag": STATE_TAGS[c["kind"]]}
                          for c in SAMPLE_CHIPS if c["date"] == day],
            })
        weeks.append({"number": days[0]["date"].isocalendar()[1], "days": days})

    context = {
        "view": view,
        "month": month,
        "range_start": start,
        "range_end": start + timedelta(weeks=n_weeks, days=-1),
        "weeks": weeks,
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
