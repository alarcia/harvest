<div align="center">

  <h1><img src="assets/logo.png" alt="Harvest logo" height="40" valign="middle"> Harvest</h1>

  <p>
    <a href="https://www.djangoproject.com/"><img src="https://img.shields.io/badge/Django-092E20?logo=django&logoColor=white" alt="Django" /></a>
    <a href="https://htmx.org/"><img src="https://img.shields.io/badge/HTMX-3D72D7?logo=htmx&logoColor=white" alt="HTMX" /></a>
    <a href="https://www.sqlite.org/"><img src="https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white" alt="SQLite" /></a>
    <a href="https://www.docker.com/"><img src="https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white" alt="Docker" /></a>
    <a href="https://github.com/features/actions"><img src="https://img.shields.io/badge/GitHub_Actions-2088FF?logo=githubactions&logoColor=white" alt="GitHub Actions" /></a>
    <a href="https://www.cloudflare.com/"><img src="https://img.shields.io/badge/Cloudflare-F38020?logo=cloudflare&logoColor=white" alt="Cloudflare" /></a>
  </p>

  <p align="center"><strong>A calendar for packages waiting at a pickup point, so a 30-minute drive there is never wasted</strong></p>
</div>

## About

My pickup point is a 30-40 minute drive away, so before making the trip the only question
that matters is: what's actually worth picking up today? Harvest answers it at a
glance — a calendar showing what's waiting, what's arriving, and what's about to expire,
straight from a phone.

Two package origins feed the calendar:

- **Amazon** (Lockers and Counters), many of them via the Vine program, arriving 4-5 a week.
  The "ready for pickup" email carries the exact deadline — read as-is, never estimated.
- **A local toy store** acting as a drop point for non-Amazon shipments — €1 a package, no
  deadline, but pricier the longer it sits. No email trail, so these get logged by hand.

## How it works

1. **Ingestion** — a dedicated Gmail inbox collects Amazon emails forwarded to it.
   It's polled over IMAP; every raw email is saved before parsing, so nothing is lost if the
   parser needs to improve later; then it's matched to the right physical package.
2. **Calendar** — a mobile-first view (month, fortnight or week) where each package shows up
   as a labelled chip on the days that matter: ordered, shipped, estimated arrival, ready for
   pickup, and its last day flagged in red. Amazon and store are color-coded.
3. **Manual entry** — covered for free by the Django admin, which also doubles as the safety
   net for fixing bad data by hand.

That's the order of importance, not the order of construction — and the two are deliberately
inverted. Manual entry lands first, because the Django admin hands it over for free. The
calendar comes next, built and judged against seed data. Ingestion goes last, because it's the
riskiest part of the project and it should be written with real emails already on the table.

## Stack

- **Backend** — Django + SQLite, server-rendered templates, HTMX for interactivity without a
  JS build step. Chosen for being opinionated: an unopinionated framework leaves too many
  ways to write the same thing, which matters when the code is written by agents across
  different sessions.
- **Ingestion** — IMAP with an App Password rather than the Gmail API, which in "Testing"
  mode expires refresh tokens every 7 days and would require security-audit verification to
  publish. `BeautifulSoup` + `dateparser` do the parsing.
- **Infra** — Docker on a Raspberry Pi, published through an existing Cloudflare Tunnel and
  gated by Cloudflare Access with an allowlist — no login code gets written.
  GitHub Actions builds and deploys.

## Status

The app is deployed on the Raspberry Pi and viewable on mobile. The calendar is built —
month, fortnight and week views, with the per-package chip vocabulary — currently running on
placeholder data while its look is dialed in. Manual entry works via the Django admin. Still
ahead, and deliberately last: the email parser and ingestion, the riskiest part of the project.

Real email is kept away from the app until the parser has earned it, in three stages. **Seed
data** first, from a repeatable command, so the calendar can be built and judged. Then a
handful of **`.eml` fixtures**, saved by hand out of an existing inbox, against which the
parser is written as a pure function that touches neither the database nor IMAP. Then **real
mail through the app**, forwarded one at a time and watched — and only once that behaves does
automatic forwarding get switched on. Pointing the app at a live mailbox any earlier is how a
database fills up with garbage that then has to be untangled by hand.

---

<div align="center">
  <p>Built with ❤️ by <a href="https://github.com/alarcia">alarcia</a></p>
</div>
