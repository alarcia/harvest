# Harvest

Calendar for packages at pickup points. The goal is to see at a glance what's waiting, what's arriving, and what's expiring, in order to plan the trip.

The app tracks **any package that makes the user drive to a pickup point**, whatever its
origin. They arrive through two channels:

- **Amazon** (Lockers and Counters). ~4-5 per week, **mostly Vine** (see below) but also regular
  paid purchases; both follow the same email lifecycle. The delivery notice ("Entregado")
  carries the exact deadline: it's the authoritative source, never calculated. A later email
  misleadingly implies it's already gone — see **Lifecycle**.
- **Alternative store** — a pickup point that receives **everything non-Amazon** (other shops: a
  toy store, etc.). Charges €1 per package, has no deadline, but starts charging more after a
  certain day. No emails: manual entry only.

## Lifecycle

The tracked unit is **one delivery the user goes to pick up** — one row, one bar on the
calendar. We model the **package**, never the order: a regular Amazon order can split into
several boxes arriving at different lockers on different days, and that is **several packages**
(several trips), each its own row. **Vine** — the bulk of the volume — is 1:1 (order = shipment
= package = item), so there the question never even arises. There is **no order→shipment→item
hierarchy to model**. Alternative-store items (other shops) are the same kind of row, entered by
hand (they generate no email).

States, and what drives each transition:

1. **`in_transit`** — the **"Pedido"** email (order placed) arrives. Sets the *estimated*
   arrival.
2. **`awaiting_pickup`** — the **"Entregado"** email (arrived at the locker/counter) arrives.
   Sets the *actual* arrival and the **deadline read from that email** (typically ~4 days of
   grace). Read, never calculated.
3. A later **"No longer available for pickup"** email is **misleading — do not trust it.** The
   package is still there; it only means the carrier will take it back *whenever they next
   pass*, possibly several more days later. Never auto-expire and never mark `returned` on this
   email.
4. **`picked_up`** or **`returned`** — the **user** confirms which. Never derived from an email.
   (Nothing has been returned so far.)

**Vine** packages are identified at ingestion by a **cost of €0.00** in the email — that single
flag is all the calendar needs now. After pickup a Vine package becomes *pending review* and
feeds the reviews module (deferred; see below). Flagging is not building the module.

## Priorities

In this order, set by the user:

1. **Automatic ingestion.** Read a dedicated Gmail inbox (Amazon emails are forwarded to it) and populate the database without intervention.
2. **Calendar view** usable from mobile, showing for each package the expected arrival, actual arrival, and expiration. Amazon and non-Amazon visually distinguished.
3. **Manual entry** of packages.
4. **Optimal-day algorithm.** Secondary. May not be implemented until late.

This is a calendar, not an optimization problem. Don't turn it into one.

**Vine is the bulk of what flows through the calendar**, but the calendar itself is
source-agnostic — it tracks every package equally. A **Vine reviews module** is planned for
later, once the calendar is finished, and is **100% Vine-only**. The Vine packages already flow
in from Gmail (identified by the €0.00 cost — see **Lifecycle**); after pickup they sit as
*pending review*. The module — reusing the existing ingestion, not a new calendar view — helps
write those reviews, with reminders for what's still pending and suggestions to make each one
easier. It's out of current scope: **flag the packages now, but don't build the module early**
and don't let it grow the data model beyond that flag before its time.

## Decisions made

Don't reopen these unless the user asks.

- **Django + HTMX**, SQLite, server-side templates. Django was chosen precisely for being
  opinionated: the code is written by agents across different sessions. A framework with fewer degrees of freedom produces predictable,
  reviewable code. Django admin covers manual entry and serves as a safety net for fixing
  data by hand.
- **Mailbox access via IMAP with an App Password**, not the Gmail API. In "Testing" mode
  Google expires the refresh token every 7 days, and publishing the app would require
  security-audit verification because `gmail.readonly` is a *restricted scope*. The App
  Password is only acceptable because the mailbox is dedicated and holds nothing else.
- **Deployment on a Raspberry Pi** via Cloudflare Tunnel (already set up by the user), with
  Docker and GitHub Actions. Authentication via **Cloudflare Access** with an
  allowlist: no login code gets written.

## Data rollout

Real email reaches the app in three stages, and never earlier:

1. **Seed data** — placeholder packages from a repeatable seed command, for building the
   calendar.
2. **Real emails as files** — a handful of `.eml` fixtures, exported from the user's *main*
   Gmail, used to write and test the parser as a pure function. It touches neither the
   database nor IMAP at this stage.
3. **Real emails through the app** — forwarded by hand first, one at a time; automatic
   forwarding only once ingestion is proven.

The dedicated inbox is the parser's **trigger, not an archive**, and it stays clean. Never ask
the user to bulk-forward their history into it; the samples come from an existing main Gmail,
which already holds years of them. And never point the app at a live mailbox before the parser is
proven.

### Placeholder calendar data — hardcoded, and it must be deleted on ingestion

Ahead of even stage 1, the calendar currently renders from a **hardcoded `SAMPLE_CHIPS` list
in `packages/views.py`** (labels via `STATE_TAGS`), *not* from the database — a deliberate
shortcut to design and validate how packages are drawn before any real data exists. It assumes
"today" is a fixed date. **The agent that wires the calendar to real data / builds ingestion
must delete this placeholder.**

It's a gift to that work, not dead weight: it's the finished spec for *how each state is
painted*, so the job becomes mapping real lifecycle events onto a vocabulary that already
exists. Each entry is one mark on one day, keyed by a **rendering `kind`** — and several kinds
collapse onto the **same model state** (don't let this vocabulary grow the data model):

| chip `kind`        | rendered as                     | lifecycle state (see above) | driven by              |
| ------------------ | ------------------------------- | --------------------------- | ---------------------- |
| `ordered`          | ○ hollow dot, no box            | `in_transit`                | "Pedido" email         |
| `shipped`          | ● filled dot, no box            | `in_transit` (unchanged)    | shipping notice        |
| `estimated`        | dashed box (uncertain)          | `in_transit`                | estimated arrival date |
| `waiting`          | filled box                      | `awaiting_pickup`           | "Entregado" email      |
| `deadline`         | red box (last safe day)         | `awaiting_pickup`           | day vs. read deadline  |
| `leaves`           | red dashed box (the "antes del" day) | `awaiting_pickup`      | day vs. read deadline  |
| `picked`           | muted ✓                         | `picked_up`                 | user confirmation      |

So `ordered`/`shipped`/`estimated` are three renderings of one `in_transit` package;
`waiting`/`deadline`/`leaves` are one `awaiting_pickup` package drawn differently depending on
how each day relates to the deadline. Use the table as the bridge between real email states and
how the calendar already knows to draw them.

## Roadmap

[ROADMAP.md](ROADMAP.md) is the detailed, task-by-task plan and the working checklist. It's
**local only** — gitignored, never committed. Treat it as internal.

- When you finish a task, tick its checkboxes in ROADMAP.md as part of the same work, so it
  always reflects reality.
- If you spot a better way to scope, split, or reorder the tasks still ahead, propose the
  change and update the roadmap — don't follow it blindly.

## How to work with the user

- **Never use the `AskUserQuestion` tool.** It takes over the whole chat, pops up abruptly
  while he's reading the response, and can't be minimized to keep reading calmly. Ask
  questions in natural language within the response; he'll answer the same way.
- **Language.** Talk to the user in Spanish. The **app's interface is in Spanish** too
  (`LANGUAGE_CODE = 'es'`; UI strings hardcoded in Spanish in the templates). Everything
  else is written in English: code, comments, commit messages, documentation.
- **Never leave a dev server running in the background** (`runserver`, `run_in_background`,
  a detached `&`). It squats the port in his own terminal and he has no easy way to find or
  kill it. Start one in the foreground to check something (screenshot, curl, click through a
  flow), then kill it yourself before ending the turn — same as you'd do with any other
  temporary state you create.
- **Never `git commit` or `git push`.** Not even when a task is clearly finished. Leave
  changes staged/unstaged for the user to commit himself, always — this app deploys via a
  self-hosted GitHub Actions runner, so a push to `main` triggers a real deploy to the
  Raspberry Pi.
