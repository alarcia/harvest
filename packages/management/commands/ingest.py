"""Scan the Gmail inbox and ingest every email not yet in the database.

One-shot by default (safe to run by hand or from cron); `--loop` runs the
scan forever with an idle heartbeat between cycles, which is how the Pi's
`ingest` compose service runs it. Read-only against the mailbox unless
GMAIL_TRASH_PROCESSED is on, and idempotent by Message-ID either way.
"""

import logging
import os
import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ...ingest import scan_inbox

logger = logging.getLogger("packages.ingest")


def _summary(stats):
    return (f"{stats['messages']} en bandeja, {stats['new']} nuevos, "
            f"{stats['failed']} sin parsear, {stats['trashed']} a papelera")


class Command(BaseCommand):
    help = "Ingest new inbox emails into RawEmail/Package (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop", action="store_true",
            help="Scan forever, sleeping --interval seconds between cycles.",
        )
        parser.add_argument(
            "--interval", type=int,
            default=int(os.environ.get("INGEST_EVERY_SECONDS", 600)),
            help="Seconds between scans in --loop mode (default 600).",
        )

    def handle(self, *args, **options):
        if not options["loop"]:
            try:
                scan_inbox()
            except Exception as exc:
                raise CommandError(str(exc)) from exc
            return

        interval = max(60, options["interval"])
        logger.info(
            "Worker de ingesta arrancado; intervalo=%ds; papelera=%s; buzón=%s",
            interval, settings.GMAIL_TRASH_PROCESSED, settings.GMAIL_IMAP_USER or "(sin credenciales)",
        )
        while True:
            try:
                scan_inbox()
            except Exception:
                # Never let one bad cycle kill the worker; log and try again.
                logger.exception("Ciclo de ingesta con error; se reintenta")
            next_at = (timezone.localtime() + timedelta(seconds=interval)).strftime("%H:%M:%S")
            logger.info("En reposo %ds; próximo escaneo ~%s", interval, next_at)
            time.sleep(interval)
