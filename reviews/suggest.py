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

**The monthly cap is already in place (2026-08-02),** ahead of the call it
guards, because the failure it exists for is the app calling in a loop — and
that is not a thing to discover after the fact on a bill. `Config
.claim_suggestion()` is booked *after* `is_configured()` and *before* the
request, so an install with the feature off never touches the counter, and an
attempt that fails still costs its slot (it may have been billed all the
same). It is the inner wall only: the outer one is the provider's prepaid
balance with auto-reload off, which is what actually makes a stolen key
worthless.
"""

from django.conf import settings

from packages.models import Config


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

    claimed, limit = Config.claim_suggestion()
    if not claimed:
        raise SuggestionUnavailable(
            "Las sugerencias de borrador están desactivadas." if limit == 0 else
            f"Se ha alcanzado el tope de {limit} sugerencias de este mes. "
            f"Puedes subirlo en la configuración."
        )

    raise SuggestionUnavailable(
        "La sugerencia de borrador todavía no está disponible."
    )
