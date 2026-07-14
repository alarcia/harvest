"""Save every message in the Gmail inbox as a raw .eml file (read-only).

Stage 2 of the data rollout: the parser gets written and tested against these
files as a pure function, never against the live mailbox. Re-runnable: files
are keyed by IMAP UID, so an existing dump is overwritten, never duplicated.
"""

import email
import email.policy
import imaplib
import re
import unicodedata
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def _slug(subject):
    """Filename-safe ASCII slug of a subject line."""
    folded = unicodedata.normalize('NFKD', subject).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', '-', folded.lower()).strip('-')[:60] or 'no-subject'


class Command(BaseCommand):
    help = 'Dump every INBOX message to .eml fixture files (read-only).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--out',
            default='tests/fixtures',
            help='Directory for the .eml files (default: tests/fixtures).',
        )

    def handle(self, *args, **options):
        if not (settings.GMAIL_IMAP_USER and settings.GMAIL_IMAP_APP_PASSWORD):
            raise CommandError(
                'GMAIL_IMAP_USER / GMAIL_IMAP_APP_PASSWORD are not set. '
                'Put them in .env (see .env.example).'
            )
        out = Path(options['out'])
        out.mkdir(parents=True, exist_ok=True)

        try:
            with imaplib.IMAP4_SSL(settings.GMAIL_IMAP_HOST) as conn:
                conn.login(settings.GMAIL_IMAP_USER, settings.GMAIL_IMAP_APP_PASSWORD)
                status, data = conn.select('INBOX', readonly=True)
                if status != 'OK':
                    raise CommandError(f'Could not open INBOX: {data}')
                status, data = conn.uid('search', None, 'ALL')
                if status != 'OK':
                    raise CommandError(f'UID SEARCH failed: {data}')
                uids = data[0].split()

                for uid in uids:
                    status, msg_data = conn.uid('fetch', uid, '(RFC822)')
                    if status != 'OK' or not msg_data or msg_data[0] is None:
                        raise CommandError(f'FETCH failed for UID {uid.decode()}')
                    raw = msg_data[0][1]
                    msg = email.message_from_bytes(raw, policy=email.policy.default)
                    subject = msg.get('Subject', '')
                    path = out / f'{int(uid):03d}-{_slug(subject)}.eml'
                    path.write_bytes(raw)
                    self.stdout.write(
                        f'{path}\n'
                        f'    From:    {msg.get("From", "?")}\n'
                        f'    Date:    {msg.get("Date", "?")}\n'
                        f'    Subject: {subject}'
                    )
        except imaplib.IMAP4.error as exc:
            raise CommandError(f'IMAP error: {exc}') from exc

        self.stdout.write(self.style.SUCCESS(f'{len(uids)} message(s) saved to {out}/.'))
