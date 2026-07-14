# Test fixtures — local only, never committed

The `.eml` files in this folder are **real Amazon.es emails** carrying personal
data (names, home addresses, order numbers), so they are **gitignored** (see the
repo `.gitignore`) and never pushed to GitHub.

They are the archetypes the parser and ingestion tests run against
(`packages/tests.py`). Because they aren't committed:

- **Deployment doesn't need them.** The production app reads the live mailbox,
  not these files; only the test suite uses them.
- **A fresh clone can't run the parser/ingest tests** until the fixtures are
  regenerated locally. Do that with the read-only dump command against the
  dedicated inbox:

  ```
  uv run python manage.py imap_dump          # writes tests/fixtures/*.eml
  ```

  (or restore them from a local backup). The dedicated inbox self-cleans, so if
  a processed email is already in Trash, forward it back or pull it from there
  first.

Keep one fixture per Amazon template; add one whenever a new template shows up
(and update the tests to cover it).
