from datetime import date, timedelta

from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .forms import PackageForm

# Weeks shown per view. Month is special-cased: its length depends on the anchor.
VIEW_WEEKS = {"week": 1, "fortnight": 2}


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
