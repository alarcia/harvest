# Generated data migration to backfill due_on for received packages

from datetime import timedelta
from django.db import migrations


def backfill_due_on(apps, schema_editor):
    Review = apps.get_model("reviews", "Review")
    for review in Review.objects.filter(due_on__isnull=True, package__isnull=False).select_related("package"):
        pkg = review.package
        received_on = None
        if pkg.state == "picked_up":
            received_on = pkg.picked_up_on
        elif pkg.state == "delivered":
            received_on = pkg.actual_arrival or pkg.estimated_arrival
        if received_on:
            review.due_on = received_on + timedelta(days=30)
            review.save(update_fields=["due_on"])


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0008_alter_referencereview_options_and_more'),
        ('packages', '0016_pickuppoint_maps_url'),
    ]

    operations = [
        migrations.RunPython(backfill_due_on, migrations.RunPython.noop),
    ]
