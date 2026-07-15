"""Re-parse stored emails whose parse previously failed (after a parser fix).

The inbox scan is idempotent by Message-ID: an email that failed to parse once
is never retried on later sweeps, even after the parser learns its template. So
a parser improvement leaves old failures stuck behind the red banner until they
are re-parsed from the stored raw bytes — which is what this does, without IMAP
and without re-forwarding anything.

Only failures (a parse_error on record) are touched, and those applied no state,
so re-parsing them is safe. Anything that now parses becomes `processed` and its
banner clears immediately; the email itself is swept from the inbox by the next
normal `ingest` run (its RawEmail is now `processed`). Prefer this over deleting
a RawEmail by hand — deleting only helps on the next IMAP sweep, this is instant.
"""

import logging

from django.core.management.base import BaseCommand

from ...ingest import reprocess_failures

logger = logging.getLogger("packages.ingest")


class Command(BaseCommand):
    help = "Re-parse stored RawEmails whose parse failed, after a parser fix."

    def handle(self, *args, **options):
        total, fixed = reprocess_failures()
        if total == 0:
            self.stdout.write("No hay correos sin procesar que reprocesar.")
            return
        self.stdout.write(self.style.SUCCESS(
            f"{total} fallo(s) reprocesado(s), {fixed} resuelto(s), "
            f"{total - fixed} aún sin parsear."
        ))
