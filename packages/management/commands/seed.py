from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction

from packages.models import Package, PickupPoint


class Command(BaseCommand):
    help = "Create placeholder packages for building and judging the calendar."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing packages and pickup points before seeding.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if options["reset"]:
            Package.objects.all().delete()
            PickupPoint.objects.all().delete()
            self.stdout.write("Cleared existing packages and pickup points.")

        locker, _ = PickupPoint.objects.get_or_create(
            name="Locker Carrefour", kind=PickupPoint.Kind.AMAZON_LOCKER
        )
        counter, _ = PickupPoint.objects.get_or_create(
            name="Counter Estanco Centro", kind=PickupPoint.Kind.AMAZON_COUNTER
        )
        store, _ = PickupPoint.objects.get_or_create(
            name="Punto Pack La Tienda", kind=PickupPoint.Kind.ALT_STORE
        )

        today = date.today()
        S = Package.State
        rows = [
            # Vine, still coming: only an estimated arrival.
            dict(
                pickup_point=locker,
                description="Vine — USB-C cable",
                is_vine=True,
                cost=0,
                state=S.IN_TRANSIT,
                estimated_arrival=today + timedelta(days=3),
            ),
            # Vine, waiting at the locker with a real deadline.
            dict(
                pickup_point=locker,
                description="Vine — LED desk lamp",
                is_vine=True,
                cost=0,
                state=S.AWAITING_PICKUP,
                estimated_arrival=today - timedelta(days=2),
                actual_arrival=today - timedelta(days=1),
                deadline=today + timedelta(days=3),
            ),
            # Regular Amazon purchase, waiting at a counter.
            dict(
                pickup_point=counter,
                description="Coffee grinder",
                is_vine=False,
                cost=39.90,
                state=S.AWAITING_PICKUP,
                estimated_arrival=today - timedelta(days=1),
                actual_arrival=today,
                deadline=today + timedelta(days=2),
            ),
            # Alt store (non-Amazon shop): no deadline.
            dict(
                pickup_point=store,
                description="Toy store — building blocks",
                is_vine=False,
                cost=1,
                state=S.AWAITING_PICKUP,
                actual_arrival=today,
            ),
            # Vine, already collected.
            dict(
                pickup_point=locker,
                description="Vine — Bluetooth speaker",
                is_vine=True,
                cost=0,
                state=S.PICKED_UP,
                estimated_arrival=today - timedelta(days=8),
                actual_arrival=today - timedelta(days=6),
                deadline=today - timedelta(days=2),
            ),
            # Returned (illustrative state; none returned in real life yet).
            dict(
                pickup_point=counter,
                description="Wrong-size headphones",
                is_vine=False,
                cost=59.00,
                state=S.RETURNED,
                estimated_arrival=today - timedelta(days=12),
                actual_arrival=today - timedelta(days=10),
                deadline=today - timedelta(days=6),
            ),
        ]

        for data in rows:
            Package.objects.create(**data)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(rows)} packages across {PickupPoint.objects.count()} pickup points."
            )
        )
