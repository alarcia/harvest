"""Set ordered_on for packages that have an ordered email but were missing the date.

Specifically fixes packages 165 (Leikaendi Hub) and 166 (Enerfero Alfombrillas) from
the 5-item Tesla order (RawEmail 720, 2026-10-14), whose ordered_on remained None
due to an earlier multi-item ingestion bug, causing their reviews to be hidden
from the reviews list.
"""

from datetime import date
from django.db import migrations
from django.utils import timezone


def set_packages_ordered_on(apps, schema_editor):
    Package = apps.get_model("packages", "Package")
    RawEmail = apps.get_model("packages", "RawEmail")

    # 1. Explicitly set ordered_on for all 5 Tesla order products
    tesla_asins = ["B0DSBZK22W", "B0GT95VTJX", "B0CM3JJJGW", "B0GYV8X592", "B0CR2VVM6P"]
    Package.objects.filter(asin__in=tesla_asins).update(ordered_on=date(2026, 10, 14))

    # 2. General check: for any other package with ordered_on=None that has a matching
    # 'ordered' RawEmail, backfill ordered_on from the email's received_at.
    for pkg in Package.objects.filter(ordered_on__isnull=True).exclude(order_id=""):
        ordered_email = (
            RawEmail.objects.filter(kind="ordered", raw__icontains=pkg.order_id)
            .order_by("received_at")
            .first()
        )
        if ordered_email and ordered_email.received_at:
            pkg.ordered_on = timezone.localtime(ordered_email.received_at).date()
            pkg.save(update_fields=["ordered_on"])


class Migration(migrations.Migration):

    dependencies = [
        ("packages", "0021_package_cancelled_on_package_returned_on_and_more"),
    ]

    operations = [
        migrations.RunPython(set_packages_ordered_on, migrations.RunPython.noop),
    ]

