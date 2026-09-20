"""Calendar domain and projection engine.

Owns the visual grammar, timeline marks, chip calculation, urgency ranking,
and calendar grid construction. Agnestic of HTTP / HTMX request handling.

The visual grammar: one chip = one mark on one day, keyed by a rendering kind.
Several kinds share one model state — how a day relates to the deadline decides
the kind, never a new state in the database.
"""

from collections import defaultdict
from datetime import date, timedelta
import re

from django.urls import reverse
from django.utils.formats import date_format

from .models import Package, PickupPoint

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
URGENCY_RANK = {
    "deadline": 0,
    "leaves": 0,
    "action_needed": 0,
    "waiting": 1,
    "shipped": 2,
    "ordered": 2,
    "estimated": 3,
    "deadline_estimated": 3,
    "leaves_estimated": 3,
    "picked": 4,
    "delivered": 4,
}
_URGENCY = URGENCY_RANK

# Grace observed between the "Entregado" email (actual_arrival) and the
# "antes del" deadline it carries: 3 days at a Locker, 7 at a Counter,
# consistent across every real package so far (checked 2026-07-23 against
# production data and fixtures). Not something the parser ever reads or
# calculates — the real deadline always comes from the email — but stable
# enough to *forecast* it for a package still in_transit, before that email
# arrives. The alt store and home deliveries have no deadline at all, so
# they're absent here and get no preview.
PREVIEW_GRACE_DAYS = {
    PickupPoint.Kind.AMAZON_LOCKER: 3,
    PickupPoint.Kind.AMAZON_COUNTER: 7,
}
_PREVIEW_GRACE_DAYS = PREVIEW_GRACE_DAYS

# Pepe y Dalda's shutters, printed at the foot of every email they send
# ("Lunes cerrado. Martes a sábado de 10:30 a 13:30 y de 17 a 20 h. Domingo
# cerrado"). Only **Monday** raises a warning on the board (user,
# 2026-07-25): a shop shut on a Sunday surprises nobody, but a Monday chip
# still reading "Listo" is exactly how a wasted trip gets planned. Both days
# are named on the card, which has room to be complete.
PEPE_CLOSED_WEEKDAYS = "los domingos y los lunes"
_PEPE_CLOSED_WEEKDAYS = PEPE_CLOSED_WEEKDAYS
PEPE_WARN_WEEKDAY = 0  # Monday, per date.weekday()
_PEPE_WARN_WEEKDAY = PEPE_WARN_WEEKDAY

STATE_LABELS = {
    Package.State.IN_TRANSIT: "En camino",
    Package.State.AWAITING_PICKUP: "Listo para recoger",
    Package.State.PICKED_UP: "Recogido",
    Package.State.DELIVERED: "Entregado",
    Package.State.RETURNED: "Devuelto",
}
_STATE_LABELS = STATE_LABELS

# A description that names only a count, not a product: picked-up / delivered
# emails with no item links whose subject was just "N productos" or "Entregado:
# N producto". These name nothing, so the chip shows an honest placeholder
# rather than echoing the boilerplate (the state tag already says Recogido /
# Entregado, so repeating it would be the redundant "Entregado · Entregado…").
# Matches both fresh ingests (empty description) and legacy rows already stored.
COUNT_DESC_PATTERN = re.compile(
    r"^(?:entregado:?\s*)?\d+\s+productos?(?:\s*\|?\s*n\.?º de pedido.*)?$",
    re.IGNORECASE,
)
_COUNT_DESC = COUNT_DESC_PATTERN

# Which colour family a chip belongs to. Three sources, not two: Pepe y
# Dalda is its own category beside Amazon and the "Otros" bucket (user,
# 2026-07-25), so it gets its own hue rather than borrowing the alt store's.
SOURCE_FAMILIES = {
    PickupPoint.Kind.ALT_STORE: "store",
    PickupPoint.Kind.PEPE_Y_DALDA: "pepe",
}
_SOURCES = SOURCE_FAMILIES

AWAITING_PICKUP_KINDS = frozenset({"waiting", "deadline", "leaves", "action_needed"})
_AWAITING_PICKUP_KINDS = AWAITING_PICKUP_KINDS

# Points whose pickups no email will ever confirm, so the user closes them
# by hand from the card: a carrier's office (Amazon abandons that lifecycle
# the moment the delivery fails) and Pepe y Dalda — whether the shop's own
# notice put the package there (that email is the whole correspondence) or an
# Amazon order was addressed to its counter, which Amazon signs off as
# "entregado" and then goes quiet. The alt store stays out — it has no emails
# at all, so it's manual end to end and lives in the admin.
MANUAL_PICKUP_KINDS = frozenset({
    PickupPoint.Kind.CARRIER, PickupPoint.Kind.PEPE_Y_DALDA,
})
_MANUAL_PICKUP_KINDS = MANUAL_PICKUP_KINDS


def parse_anchor(value, fallback=None):
    """Parse an ISO date string, or return fallback if invalid/None."""
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback


_parse_anchor = parse_anchor


def monday(day):
    """Return the Monday of the week containing day."""
    return day - timedelta(days=day.weekday())


_monday = monday


def effective_estimate(pkg, today):
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


_effective_estimate = effective_estimate


def estimate_note(pkg, today):
    """The parenthetical on an "Estimado" chip that has moved onto today, so
    it can't be read as a promise that the package lands today — the mistake
    that would send the user on a wasted trip. Empty while the estimate still
    sits where the email put it."""
    if not pkg.estimated_arrival or today <= pkg.estimated_arrival:
        return ""
    end = pkg.estimated_arrival_end
    if end and today <= end:
        return f"hasta el {short_day(end)}"
    return "con retraso"


_estimate_note = estimate_note


def shop_closed_on(point, day):
    """Is this a day the point is shut *and* worth warning about?"""
    return (point.kind == PickupPoint.Kind.PEPE_Y_DALDA
            and day.weekday() == PEPE_WARN_WEEKDAY)


_shop_closed_on = shop_closed_on


def waiting_note(pkg, today):
    """"3 días" — how long a deadline-less package has been on the counter.

    Only for the points that never expire: their chip is redrawn on today
    every day, so without this it reads exactly the same on day one and on
    day nine. Empty on the day it arrives, when the chip's position already
    says everything.

    A closing day displaces the count: how long it's been waiting is a
    nudge, "you cannot fetch it today" is a fact, and only one of them fits
    on a chip."""
    if shop_closed_on(pkg.pickup_point, today):
        return "cerrado hoy"
    if not pkg.actual_arrival:
        return ""
    days = (today - pkg.actual_arrival).days
    if days < 1:
        return ""
    return "1 día" if days == 1 else f"{days} días"


_waiting_note = waiting_note


def preview_leaves_day(pkg, today):
    """The forecasted "antes del" day for an in-transit package at a pickup
    point with a known grace window, or None. A guess from the estimated
    arrival — never a substitute for the real deadline once it's read from
    the "Entregado" email.

    Hangs off the *effective* estimate, so it moves with it: a forecast built
    on an arrival day we no longer believe would put red dashed boxes in the
    past, which is the same incongruence one step further down the chain."""
    grace = PREVIEW_GRACE_DAYS.get(pkg.pickup_point.kind)
    arrival = effective_estimate(pkg, today)
    if not (arrival and grace):
        return None
    return arrival + timedelta(days=grace)


_preview_leaves_day = preview_leaves_day


def short_day(day):
    """"28 jul" — a chip-sized day. The month is never dropped: a window can
    cross into the next one, and "hasta el 2" for a 28-July-to-2-August window
    reads as a day already past."""
    return date_format(day, r"j b")


_short_day = short_day


def long_day(day):
    """"viernes 24 de julio" — the card's spelling, weekday included: the user
    plans a trip by day of the week. Month always spelled out, on both ends of
    a window, for the same reason _short_day keeps it."""
    return date_format(day, r"l j \d\e F")


_long_day = long_day


def estimate_line(pkg, today):
    """The card's "Llegada estimada" value, or "" when there's nothing to say.

    Four readings of the same two fields, so the sentence never claims more
    than the email did: a single named day, a window still ahead, a window
    already running ("entre hoy y el…"), and either of them overrun, where it
    switches to the past tense and admits the delay."""
    start, end = pkg.estimated_arrival, pkg.estimated_arrival_end
    if not start:
        return ""
    if end and today > end:
        return f"se esperaba entre el {long_day(start)} y el {long_day(end)} · con retraso"
    if end and today > start:
        return f"entre hoy y el {long_day(end)}"
    if end:
        return f"entre el {long_day(start)} y el {long_day(end)}"
    if today > start:
        return f"se esperaba el {long_day(start)} · con retraso"
    return long_day(start)


_estimate_line = estimate_line


def state_label(pkg):
    """The modal's "Estado" line. A carrier pickup reuses AWAITING_PICKUP but
    must not read as the calm "Listo para recoger" — same reasoning as the
    action_needed chip kind, so the card doesn't undercut the calendar's own
    red warning the moment the user taps in for the actionable detail."""
    if (pkg.state == Package.State.AWAITING_PICKUP
            and pkg.pickup_point.kind == PickupPoint.Kind.CARRIER):
        return "Recoger ya en el transportista"
    return STATE_LABELS.get(pkg.state, pkg.state)


_state_label = state_label


def package_label(pkg):
    """The product name to print on a chip, or a placeholder when unknown."""
    desc = (pkg.description or "").strip()
    return desc if desc and not COUNT_DESC_PATTERN.match(desc) else "Producto desconocido"


_label = package_label


def point_label(point):
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


_point_label = point_label


def source_family(point):
    """Which colour family a chip belongs to."""
    return SOURCE_FAMILIES.get(point.kind, "amazon")


_source = source_family


def package_marks(pkg, today):
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
        est_day = effective_estimate(pkg, today)
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
                marks.append((est_day, "estimated", estimate_note(pkg, today)))
        leaves_day = preview_leaves_day(pkg, today)
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
            return [(today, "waiting", waiting_note(pkg, today))]
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


_marks = package_marks


def get_chips(start, end, today):
    """Query active packages and project them into chip dictionaries within [start, end]."""
    chips = []
    packages = (Package.objects
                .exclude(state=Package.State.RETURNED)
                .select_related("pickup_point"))
    for pkg in packages:
        source = source_family(pkg.pickup_point)
        label = package_label(pkg)
        detail_url = reverse("package_detail", args=[pkg.pk])
        chips.extend(
            {"date": day, "kind": kind, "tag": STATE_TAGS[kind], "note": note,
             "label": label, "source": source, "detail_url": detail_url,
             "point_id": pkg.pickup_point_id,
             "point_kind": pkg.pickup_point.kind,
             "point_name": pkg.pickup_point.label,
             "point_maps_url": pkg.pickup_point.maps_url,
             # Drawn on a day the shop is shut: earns a ⚠ on the chip itself,
             # since the grid gets read without opening anything. Only marks
             # a chip you'd act on — a pickup already made needs no warning.
             "closed": (kind == "waiting"
                        and shop_closed_on(pkg.pickup_point, day))}
            for day, kind, note in package_marks(pkg, today) if start <= day <= end
        )
    return chips


_chips = get_chips


def day_chips(chips, day):
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
                    "point_kind": group[0].get("point_kind"),
                    "point_name": group[0].get("point_name"),
                    "detail_url": reverse("delivered_detail", args=[day.isoformat(), point_id]),
                    "closed": False,
                })
            else:
                collapsed.extend(group)
        todays = rest + collapsed

    return sorted(todays, key=lambda c: URGENCY_RANK[c["kind"]])


_day_chips = day_chips


def day_pickup_summary(chips):
    """Summarize points to visit for packages awaiting pickup on a day.

    Returns a list of dicts: [{'name': 'Morera', 'count': 4, 'source': 'amazon'}, ...]
    ordered by count descending, then point name.
    Ignores home deliveries (no trip needed) and packages not awaiting pickup.
    """
    counts_by_point = {}
    for c in chips:
        if c.get("kind") in AWAITING_PICKUP_KINDS and c.get("point_kind") != PickupPoint.Kind.HOME:
            pid = c["point_id"]
            if pid not in counts_by_point:
                counts_by_point[pid] = {
                    "name": c.get("point_name", ""),
                    "count": 0,
                    "source": c.get("source", "amazon"),
                    "maps_url": c.get("point_maps_url", ""),
                }
            counts_by_point[pid]["count"] += 1

    summary = list(counts_by_point.values())
    summary.sort(key=lambda item: (-item["count"], item["name"]))
    return summary


_day_pickup_summary = day_pickup_summary


def can_confirm_pickup(pkg):
    """Whether the card offers the manual "ya lo he recogido" (see
    confirm_pickup). Only for the points in MANUAL_PICKUP_KINDS: everything
    else keeps closing itself from email, and a manual button there would only
    invite closing a package the email would have closed correctly anyway."""
    return (pkg.state == Package.State.AWAITING_PICKUP
            and pkg.pickup_point.kind in MANUAL_PICKUP_KINDS)


_can_confirm_pickup = can_confirm_pickup


def build_calendar_weeks(start, n_weeks, today, month, chips):
    """Construct week/day matrices for rendering the calendar template."""
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
                "chips": day_chips(chips, day),
            })
        weeks.append({"number": days[0]["date"].isocalendar()[1], "days": days})
    return weeks
