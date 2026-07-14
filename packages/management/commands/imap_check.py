"""Verify the Gmail IMAP credentials, without reading any mail.

Proves the plumbing (App Password, IMAP access) end to end so that when
ingestion lands, credentials are already a solved problem. Read-only: it
never fetches message content and never changes the mailbox.
"""

import imaplib

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Log in to the Gmail inbox over IMAP and report the message count.'

    def handle(self, *args, **options):
        if not (settings.GMAIL_IMAP_USER and settings.GMAIL_IMAP_APP_PASSWORD):
            raise CommandError(
                'GMAIL_IMAP_USER / GMAIL_IMAP_APP_PASSWORD are not set. '
                'Put them in .env (see .env.example).'
            )
        try:
            with imaplib.IMAP4_SSL(settings.GMAIL_IMAP_HOST) as conn:
                conn.login(settings.GMAIL_IMAP_USER, settings.GMAIL_IMAP_APP_PASSWORD)
                status, data = conn.select('INBOX', readonly=True)
                if status != 'OK':
                    raise CommandError(f'Login worked but INBOX did not open: {data}')
                count = int(data[0])
        except imaplib.IMAP4.error as exc:
            raise CommandError(
                f'IMAP error: {exc}\n'
                'Usual suspects: the App Password was mistyped (it must be the '
                '16 characters, spaces optional), 2-Step Verification is not '
                'enabled, or GMAIL_IMAP_USER is not the full address.'
            ) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f'OK — logged in as {settings.GMAIL_IMAP_USER}; '
                f'INBOX holds {count} message(s).'
            )
        )
