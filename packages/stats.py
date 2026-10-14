"""Delivery and shipping statistics for Harvest.

Computes transition velocities, order volumes, delivery punctuality,
and weekday habits from Package records and RawEmail timestamps.
"""

from collections import Counter
from datetime import timedelta
import statistics

from django.db.models import Count
from django.utils import timezone

from .models import Package, RawEmail


def _format_duration(hours):
    """Formats an hour duration nicely in Spanish."""
    if hours is None:
        return "—"
    if hours < 1:
        mins = max(1, int(round(hours * 60)))
        return f"{mins} min"
    if hours < 24:
        return f"{hours:.1f} h".replace(".", ",")
    days = hours / 24.0
    return f"{days:.1f} d".replace(".", ",")


def _calc_metric(values, is_days=False):
    """Calculates mean, median, formatted string, and count for a list of floats."""
    if not values:
        return {
            "mean": None,
            "median": None,
            "display": "—",
            "subtext": "Sin datos suficientes",
            "count": 0,
        }
    avg = statistics.mean(values)
    med = statistics.median(values)
    count = len(values)
    if is_days:
        display = f"{avg:.1f} días".replace(".", ",")
        med_str = f"{med:.1f} d".replace(".", ",")
        subtext = f"Mediana: {med_str} · {count} paquete{'s' if count != 1 else ''}"
    else:
        display = _format_duration(avg)
        med_str = _format_duration(med)
        if avg >= 24:
            subtext = f"~{int(round(avg))} h (mediana: {med_str}) · {count} envíos"
        else:
            subtext = f"Mediana: {med_str} · {count} envíos"

    return {
        "mean": avg,
        "median": med,
        "display": display,
        "subtext": subtext,
        "count": count,
    }


def get_delivery_stats():
    """Computes full delivery and package statistics."""
    today = timezone.localdate()
    first_of_month = today.replace(day=1)
    last_month_end = first_of_month - timedelta(days=1)
    first_of_last_month = last_month_end.replace(day=1)
    monday_this_week = today - timedelta(days=today.weekday())

    # 1. Volume metrics
    orders_week = Package.objects.filter(ordered_on__gte=monday_this_week).count()
    orders_month = Package.objects.filter(ordered_on__gte=first_of_month).count()
    orders_last_month = Package.objects.filter(
        ordered_on__gte=first_of_last_month, ordered_on__lt=first_of_month
    ).count()
    total_packages = Package.objects.count()

    # Active states
    in_transit = Package.objects.filter(state=Package.State.IN_TRANSIT).count()
    last_mile = Package.objects.filter(state=Package.State.LAST_MILE).count()
    awaiting_pickup = Package.objects.filter(state=Package.State.AWAITING_PICKUP).count()

    # 2. Transition velocities
    ord_to_shp, shp_to_arr, ofd_to_arr, arr_to_pck, cycle_days = [], [], [], [], []
    early_count, on_time_count, late_count = 0, 0, 0

    packages = (
        Package.objects.select_related("pickup_point")
        .prefetch_related("emails")
        .all()
    )

    arrivals_by_day = Counter()
    pickups_by_day = Counter()

    for p in packages:
        # Punctuality
        if p.actual_arrival and p.estimated_arrival:
            diff = (p.actual_arrival - p.estimated_arrival).days
            if diff < 0:
                early_count += 1
            elif diff == 0:
                on_time_count += 1
            else:
                late_count += 1

        # Weekday activity
        if p.actual_arrival:
            arrivals_by_day[p.actual_arrival.weekday()] += 1
        if p.picked_up_on:
            pickups_by_day[p.picked_up_on.weekday()] += 1

        # Timestamps from raw emails
        kinds = {}
        for e in p.emails.all():
            if e.received_at and (e.kind not in kinds or e.received_at < kinds[e.kind]):
                kinds[e.kind] = e.received_at

        # (a) Ordered to Shipped
        if "ordered" in kinds and "shipped" in kinds and kinds["shipped"] >= kinds["ordered"]:
            ord_to_shp.append((kinds["shipped"] - kinds["ordered"]).total_seconds() / 3600.0)
        elif p.ordered_on and p.shipped_on and p.shipped_on >= p.ordered_on:
            ord_to_shp.append((p.shipped_on - p.ordered_on).days * 24.0)

        # Arrival timestamp / date
        arrival_time = (
            kinds.get("ready_for_pickup")
            or kinds.get("delivered")
            or kinds.get("store_reception")
        )

        # (b) Shipped to Arrival
        if "shipped" in kinds and arrival_time and arrival_time >= kinds["shipped"]:
            shp_to_arr.append((arrival_time - kinds["shipped"]).total_seconds() / 3600.0)
        elif p.shipped_on and p.actual_arrival and p.actual_arrival >= p.shipped_on:
            shp_to_arr.append((p.actual_arrival - p.shipped_on).days * 24.0)

        # (c) Out for delivery (Last mile) to Arrival
        if "out_for_delivery" in kinds and arrival_time and arrival_time >= kinds["out_for_delivery"]:
            ofd_to_arr.append((arrival_time - kinds["out_for_delivery"]).total_seconds() / 3600.0)

        # (d) Waiting at pickup point to Picked up
        if arrival_time and kinds.get("picked_up") and kinds["picked_up"] >= arrival_time:
            arr_to_pck.append((kinds["picked_up"] - arrival_time).total_seconds() / 3600.0)
        elif p.actual_arrival and p.picked_up_on and p.picked_up_on >= p.actual_arrival:
            arr_to_pck.append((p.picked_up_on - p.actual_arrival).days * 24.0)

        # (e) Total end-to-end cycle (ordered to in hands)
        end_time = kinds.get("picked_up") or kinds.get("delivered")
        if "ordered" in kinds and end_time and end_time >= kinds["ordered"]:
            cycle_days.append((end_time - kinds["ordered"]).total_seconds() / 86400.0)
        elif p.ordered_on and p.received_on and p.received_on >= p.ordered_on:
            cycle_days.append((p.received_on - p.ordered_on).days * 1.0)

    # 3. Punctuality breakdown
    total_punc = early_count + on_time_count + late_count
    punctuality = {
        "total": total_punc,
        "on_time": on_time_count,
        "on_time_pct": round((on_time_count / total_punc) * 100) if total_punc else 0,
        "early": early_count,
        "early_pct": round((early_count / total_punc) * 100) if total_punc else 0,
        "late": late_count,
        "late_pct": round((late_count / total_punc) * 100) if total_punc else 0,
    }

    # 4. Top destinations
    top_destinations = []
    points_agg = (
        Package.objects.values(
            "pickup_point__name", "pickup_point__display_name", "pickup_point__kind"
        )
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    for pt in points_agg[:5]:
        label = (pt["pickup_point__display_name"] or "").strip() or pt["pickup_point__name"]
        pct = round((pt["count"] / total_packages) * 100) if total_packages else 0
        top_destinations.append({
            "name": label,
            "kind": pt["pickup_point__kind"],
            "count": pt["count"],
            "pct": pct,
        })

    # 5. Weekday activity
    day_names = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
    weekday_data = []
    max_act = max(
        max((arrivals_by_day[i] for i in range(7)), default=1),
        max((pickups_by_day[i] for i in range(7)), default=1),
        1,
    )
    for i in range(7):
        arr = arrivals_by_day[i]
        pck = pickups_by_day[i]
        weekday_data.append({
            "name": day_names[i],
            "arrivals": arr,
            "arrivals_pct": round((arr / max_act) * 100),
            "pickups": pck,
            "pickups_pct": round((pck / max_act) * 100),
        })

    return {
        "volume": {
            "week": orders_week,
            "month": orders_month,
            "last_month": orders_last_month,
            "total": total_packages,
            "in_transit": in_transit,
            "last_mile": last_mile,
            "awaiting_pickup": awaiting_pickup,
            "active_total": in_transit + last_mile + awaiting_pickup,
        },
        "velocity": {
            "ordered_to_shipped": _calc_metric(ord_to_shp),
            "shipped_to_arrival": _calc_metric(shp_to_arr),
            "last_mile_to_arrival": _calc_metric(ofd_to_arr),
            "waiting_to_pickup": _calc_metric(arr_to_pck),
            "cycle_total": _calc_metric(cycle_days, is_days=True),
        },
        "punctuality": punctuality,
        "destinations": top_destinations,
        "weekdays": weekday_data,
    }
