"""One-off: create Reviews for history that predates the R1 ingestion hooks.

Real data already had Vine packages and successfully-processed
`review_published` RawEmails ("Gracias por tu reseña") before the hooks that
turn those into `reviews.Review` rows existed — the old handler for that
email kind was a no-op, so nothing was ever created. `reprocess` doesn't
reach these (they have no `parse_error`; they parsed fine, they just did
nothing). This command reads the already-stored data and applies the new
hooks retroactively, once. Safe to run more than once — both passes are
idempotent, see `packages.ingest.backfill_reviews`.
"""

from django.core.management.base import BaseCommand

from ...ingest import backfill_reviews


class Command(BaseCommand):
    help = "Backfill reviews.Review from already-ingested Vine packages and review emails."

    def handle(self, *args, **options):
        result = backfill_reviews()
        self.stdout.write(self.style.SUCCESS(
            f"{result['packages']} reseña(s) pendiente(s) creada(s) para paquetes Vine, "
            f"{result['emails']} email(s) de reseña reproducido(s)."
        ))
