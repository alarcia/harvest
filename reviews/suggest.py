"""Draft suggestions for a review.

Harvest never writes a review by itself and never posts anything to Amazon:
what this module produces is a **suggested draft** — a headline and a body —
that the user reads, rewrites and approves by hand before pasting it in. It is
built from what he already provided: the product's name, the stars he chose,
his own notes about using it, and the reference corpus of reviews he has
already had validated (`ReferenceReview`).

**The wording lives in the database, not here.** `Config.suggestion_prompt` is
the template and it ships empty; this file only substitutes four markers into
it and posts the result. That is the public-repo discretion the whole module
is written under, and it is also why the client below is a plain generic HTTP
call configured by `SUGGEST_*` environment variables rather than a vendor
library — nothing in git names a service, a model, or a header.

**Two walls stand between a loop in the app and a bill.** The outer one is at
the provider (prepaid balance, auto-reload off) and is the one that really
can't be climbed. The inner one is `Config.claim_suggestion()`, booked *after*
`is_configured()` and *before* the request: an install with the feature off
never touches the counter, and an attempt that fails still costs its slot,
because it may well have been billed anyway. There are deliberately **no
retries** — a silent retry is a silent charge.
"""

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

from packages.models import Config

from .models import ReferenceReview

logger = logging.getLogger(__name__)

# A ceiling, not a reservation: only what actually comes back is billed, so
# this is set well clear of any review the user would write rather than
# trimmed to save tenths of a cent. Too low is the expensive setting — a reply
# cut mid-sentence is a call paid for and thrown away.
_MAX_TOKENS = 2000

# One socket timeout for the whole exchange. The user is watching a modal
# spinner, and gunicorn has its own worker timeout behind this one.
_TIMEOUT_SECONDS = 45

# The output contract, and the *only* instruction that lives in code rather
# than in the editable template: the parser depends on it, so a template the
# user rewrites must not be able to break reading the answer. It says nothing
# about how to write — that is entirely `Config.suggestion_prompt`'s job.
_FORMAT = ('Responde únicamente con un objeto JSON con dos claves: "titulo" '
           '(el titular de la reseña) y "texto" (el cuerpo). Sin explicaciones '
           'ni texto fuera del objeto.')


class SuggestionUnavailable(RuntimeError):
    """Raised when no proposal can be produced right now — not configured,
    the endpoint failed, or it answered with nothing usable. Always carries a
    message meant to be shown to the user as-is."""


def is_configured():
    """Whether a proposal can be requested at all — the single switch the UI
    reads to decide what to render. Only the wiring is checked here; the
    template is a separate, actionable failure (see `suggest_draft`)."""
    return bool(getattr(settings, "SUGGEST_API_URL", "")
                and getattr(settings, "SUGGEST_API_KEY", "")
                and getattr(settings, "SUGGEST_MODEL", ""))


def _extra_headers():
    """The endpoint-specific headers, parsed from `SUGGEST_API_HEADERS`.

    One `Nombre: valor` per line. This exists so that no header a particular
    service happens to require has to be written down in a public repository:
    the deployment carries them, the code just forwards them.
    """
    raw = getattr(settings, "SUGGEST_API_HEADERS", "")
    headers = {}
    for line in raw.splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip():
            headers[name.strip()] = value.strip()
    return headers


def _examples_block(limit):
    """The corpus, rendered for the template's `{ejemplos}` marker.

    Formatting only — every word of instruction around it comes from the
    template. Empty string when there is nothing to show, so a fresh install
    reads as "no examples" rather than as a stray heading.
    """
    blocks = []
    for example in ReferenceReview.examples(limit):
        blocks.append(f"[{example.rating}/5] {example.title}\n{example.text}")
    return "\n\n".join(blocks)


def _prompt(review, config):
    """The template with its four markers filled in.

    `str.replace` rather than `str.format`: the template is typed into a
    textarea by hand, and a stray brace in it must never be able to raise.
    A marker the template doesn't mention simply isn't sent.
    """
    stars = review.rating or 0
    filled = config.suggestion_prompt
    for marker, value in (
        ("{producto}", review.product_title or ""),
        ("{estrellas}", f"{stars}/5"),
        ("{notas}", review.notes or ""),
        ("{ejemplos}", _examples_block(config.suggestion_examples)),
    ):
        filled = filled.replace(marker, value)
    return filled


def _post(payload):
    """POST `payload` and return the decoded JSON body.

    Every failure becomes a `SuggestionUnavailable` the panel can show. The
    status code is quoted because it's the one thing that tells the user
    whether to retry (429, 529) or to go and look at something (401, 402);
    the response *body* never is, since it's remote text of unknown shape.
    """
    request = urllib.request.Request(
        settings.SUGGEST_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": settings.SUGGEST_API_KEY,
            **_extra_headers(),
        },
        method="POST",
    )
    try:
        # No retry, on purpose: see the module docstring.
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Logged without the body and, above all, without the request: the key
        # is in those headers and this line goes to `docker logs`.
        logger.warning("Propuesta rechazada por el servicio: HTTP %s", exc.code)
        raise SuggestionUnavailable(
            f"El servicio de propuestas ha respondido con un error ({exc.code}). "
            f"Inténtalo de nuevo en un momento."
        ) from exc
    except Exception as exc:
        logger.warning("Propuesta fallida: %s", type(exc).__name__)
        raise SuggestionUnavailable(
            "No se ha podido contactar con el servicio de propuestas."
        ) from exc


def _parse(data):
    """Pull `(title, text)` out of the answer.

    The reply is asked for as a JSON object and *started* for it (see
    `suggest_draft`), which is what makes this reliable enough to parse
    strictly: anything else is a malformed answer, not a shape to guess at.
    """
    try:
        blocks = data["content"]
        raw = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        # `raw_decode` rather than `loads`: it reads the first complete object
        # and ignores whatever follows, so a stray closing line after the JSON
        # costs nothing.
        proposal, _ = json.JSONDecoder().raw_decode('{"titulo":' + raw)
        title = str(proposal["titulo"]).strip()
        text = str(proposal["texto"]).strip()
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Propuesta ilegible: %s", type(exc).__name__)
        raise SuggestionUnavailable(
            "La propuesta ha llegado en un formato que no se entiende. "
            "Vuelve a intentarlo."
        ) from exc
    if not title or not text:
        raise SuggestionUnavailable("La propuesta ha llegado vacía. Vuelve a intentarlo.")
    return title[:255], text


def suggest_draft(review):
    """Return `(title, text)` for `review`, or raise `SuggestionUnavailable`.

    Inputs, in the order they matter: the user's `notes` (his real
    impressions — the whole point, and what keeps the result his), the
    `rating` he already decided on, `product_title`, and the reference corpus.
    Never a title: that is the one input already known, and asking for it
    would be asking him to do the work twice.
    """
    if not is_configured():
        raise SuggestionUnavailable(
            "La sugerencia de borrador todavía no está disponible."
        )

    config = Config.load()
    if not config.suggestion_prompt.strip():
        raise SuggestionUnavailable(
            "Falta la plantilla de la propuesta en la configuración."
        )

    claimed, limit = Config.claim_suggestion()
    if not claimed:
        raise SuggestionUnavailable(
            "Las sugerencias de borrador están desactivadas." if limit == 0 else
            f"Se ha alcanzado el tope de {limit} sugerencias de este mes. "
            f"Puedes subirlo en la configuración."
        )

    data = _post({
        "model": settings.SUGGEST_MODEL,
        "max_tokens": _MAX_TOKENS,
        "system": _FORMAT,
        "messages": [
            {"role": "user", "content": _prompt(review, config)},
            # The answer is also *started* for it, so it can only carry on
            # from inside the object — belt and braces around the one thing
            # that has to hold for the reply to be readable at all.
            {"role": "assistant", "content": '{"titulo":'},
        ],
    })
    return _parse(data)
