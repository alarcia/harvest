import re

from django.db import migrations

_POSTAL_CODE = re.compile(r"\b(\d{5})\b")


def merge_duplicate_pickup_points(apps, schema_editor):
    """Backfill location_key on every Amazon Locker/Counter point, then merge
    rows that turn out to share one (kind, postal code) — the same physical
    venue, split into several rows because Amazon spells it differently across
    email templates (see PickupPoint.location_key, packages/ingest.py
    _pickup_point). Reassign each merged point's packages to the survivor
    before deleting it: PickupPoint.packages is on_delete=PROTECT, so a bare
    delete would refuse."""
    PickupPoint = apps.get_model("packages", "PickupPoint")
    Package = apps.get_model("packages", "Package")

    groups = {}
    for point in PickupPoint.objects.filter(
        kind__in=("amazon_locker", "amazon_counter")
    ):
        match = _POSTAL_CODE.search(point.name)
        if not match:
            continue
        point.location_key = match.group(1)
        point.save(update_fields=["location_key"])
        groups.setdefault((point.kind, point.location_key), []).append(point)

    for points in groups.values():
        if len(points) < 2:
            continue
        # Keep whichever point already carries the most packages (fewest FK
        # rewrites); ties broken by lowest id for determinism.
        points.sort(
            key=lambda p: (-Package.objects.filter(pickup_point=p).count(), p.id)
        )
        keeper, *dupes = points
        for dupe in dupes:
            Package.objects.filter(pickup_point=dupe).update(pickup_point=keeper)
            dupe.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("packages", "0004_pickuppoint_location_key"),
    ]

    operations = [
        migrations.RunPython(merge_duplicate_pickup_points, migrations.RunPython.noop),
    ]
