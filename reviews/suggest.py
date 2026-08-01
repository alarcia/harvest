"""Draft suggestions for a review.

Harvest never writes a review by itself and never posts anything to Amazon:
what this module produces is a **suggested draft** — a headline and a body —
that the user reads, rewrites and approves by hand before pasting it in. It is
built from what he already provided: the product's name, the stars he chose,
and his own notes about using it.

**Not wired up yet (2026-08-01).** Everything around it is: the panel that
collects the notes and the stars, the request path, the "Incorporar al
borrador" step that moves an accepted proposal into the review's own fields.
Only the remote call is missing, so `is_configured()` is the single switch
that turns the feature on — the UI asks it what to render, and the view asks
it before trying. Configuring `SUGGEST_API_URL`/`SUGGEST_API_KEY` (see
`.env`, kept out of the repo like the IMAP password) and filling in
`_request()` is the whole of what's left.
"""

from django.conf import settings


class SuggestionUnavailable(RuntimeError):
    """Raised when no proposal can be produced right now — not configured,
    the endpoint failed, or it answered with nothing usable. Always carries a
    message meant to be shown to the user as-is."""


def is_configured():
    """Whether a proposal can be requested at all. False today, and the
    reason the button says so instead of failing when pressed."""
    return bool(getattr(settings, "SUGGEST_API_URL", "")
                and getattr(settings, "SUGGEST_API_KEY", ""))


def suggest_draft(review):
    """Return `(title, text)` for `review`, or raise `SuggestionUnavailable`.

    Inputs, in the order they matter: the user's `notes` (his real
    impressions — the whole point, and what keeps the result his), the
    `rating` he already decided on, and `product_title`. The style guide that
    shapes the wording lives in the database, never in this file: it's tuned
    without a deploy, and the repo is public.
    """
    if not is_configured():
        raise SuggestionUnavailable(
            "La sugerencia de borrador todavía no está disponible."
        )
    raise SuggestionUnavailable(
        "La sugerencia de borrador todavía no está disponible."
    )
