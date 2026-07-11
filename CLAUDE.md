# Harvest

Calendar for packages at pickup points. The goal is to see at a glance what's waiting, what's arriving, and what's expiring, in order to plan the trip.

Two package sources:

- **Amazon** (Lockers and Counters). Estimated 4-5 per week.
  The "ready for pickup" email carries the exact deadline: it's the authoritative source, never calculated.
- **Alternative store** that acts as a pickup point for non-Amazon shipments. Charges €1 per package and has no deadline, but starts charging more after a certain day.

## Priorities

In this order, set by the user:

1. **Automatic ingestion.** Read a dedicated Gmail inbox (Amazon emails are forwarded to it) and populate the database without intervention.
2. **Calendar view** usable from mobile, showing for each package the expected arrival, actual arrival, and expiration. Amazon and non-Amazon visually distinguished.
3. **Manual entry** of packages.
4. **Optimal-day algorithm.** Secondary. May not be implemented until late.

This is a calendar, not an optimization problem. Don't turn it into one.

A **Vine reviews module** is planned for later, once the calendar is finished. Packages
ingested from Gmail and flagged as Vine feed a separate module — reusing the existing
ingestion, not a new calendar view — that helps write the reviews, with reminders for what's
still pending and suggestions to make each one easier. It's out of current scope: don't build
it early and don't let it shape the data model before its time.

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
- **Language.** Talk to the user in Spanish. Everything else is written in English: code,
  comments, commit messages, documentation, and the app's own interface.
