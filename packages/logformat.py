"""Logging formatter that timestamps in the app's TIME_ZONE.

So `docker logs` reads in the user's wall-clock time (Europe/Madrid) no
matter what timezone the container runs in — the whole point of the audit
trail is answering "when did this happen" at a glance.
"""

import datetime
import logging
from zoneinfo import ZoneInfo

from django.conf import settings


class LocalTimeFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.datetime.fromtimestamp(record.created, ZoneInfo(settings.TIME_ZONE))
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S %Z")
