# Data migration to update review due_on from 30 days to 14 days after receipt

from datetime import timedelta
from django.db import migrations


def update_due_on_to_14_days(apps, schema_editor):
    Review = apps.get_model("reviews", "Review")
    for review in Review.objects.filter(status__in=["pending", "draft"], package__isnull=False).select_related("package"):
        pkg = review.package
        received_on = None
        if pkg.state == "picked_up":
            received_on = pkg.picked_up_on
        elif pkg.state == "delivered":
            received_on = pkg.actual_arrival or pkg.estimated_arrival
        if received_on and review.due_on == received_on + timedelta(days=30):
            review.due_on = received_on + timedelta(days=14)
            review.save(update_fields=["due_on"])


def reverse_due_on_to_30_days(apps, schema_editor):
    Review = apps.get_model("reviews", "Review")
    for review in Review.objects.filter(status__in=["pending", "draft"], package__isnull=False).select_related("package"):
        pkg = review.package
        received_on = None
        if pkg.state == "picked_up":
            received_on = pkg.picked_up_on
        elif pkg.state == "delivered":
            received_on = pkg.actual_arrival or pkg.estimated_arrival
        if received_on and review.due_on == received_on + timedelta(days=14):
            review.due_on = received_on + timedelta(days=30)
            review.save(update_fields=["due_on"])


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0009_backfill_review_due_on'),
    ]

    operations = [
        migrations.RunPython(update_due_on_to_14_days, reverse_due_on_to_30_days),
    ]
