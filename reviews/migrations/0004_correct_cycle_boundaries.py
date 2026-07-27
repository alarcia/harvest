"""Correct the Vine cycle boundaries against Amazon's own data.

Migration 0002 seeded a decade of cycles on the belief that the boundary sits
forever on the 27th of January and July. It doesn't: it drifts a day earlier
each period (27 Jan 2026 → 26 Jul 2026 → 25 Jan 2027). The JSON behind the
Vine page — which the VineHelper extension surfaces — carries the real
instants, and they are exact midnights UTC: this period opened 2026-07-26
00:00 UTC (02:00 in Madrid) and is re-evaluated 2027-01-25 00:00 UTC (01:00 in
Madrid), which is what Amazon prints as "26 jul 2026 - 25 ene 2027".

Two consequences, applied here:

* **The boundary day belongs to the incoming cycle.** The cut falls at
  01:00/02:00 local, so the day is the new period's for every practical
  purpose — both orders placed on 2026-07-26 (09:04 and 09:07) were already
  seven hours into it. Applying the same rule at both ends is what makes the
  range coherent, and it is why this cycle ends on the 24th and not on the
  re-evaluation day itself.
* **The seeded tail is fiction and goes.** Everything past the next boundary
  was computed from a constant we now know to be wrong, so it is deleted
  rather than shifted: `_ensure_through` regenerates rows contiguously on
  demand, and each real boundary gets corrected once Amazon publishes it.
  Shifting only the two known cycles and leaving the rest would open a
  two-day hole (25-26 July 2027) where `current()` returns None and nothing
  is urgent.
"""

from datetime import date

from django.db import migrations

# The last boundary Amazon has actually published. The end paired with it is
# a placeholder on the old six-month step, same as anything `_ensure_through`
# would write — correct it when the real re-evaluation date shows up.
NEXT_KNOWN_START = date(2027, 1, 25)


def correct_boundaries(apps, schema_editor):
    VineCycle = apps.get_model("reviews", "VineCycle")
    VineCycle.objects.filter(starts_on=date(2026, 1, 27)).update(ends_on=date(2026, 7, 25))
    VineCycle.objects.filter(starts_on=date(2026, 7, 27)).update(
        starts_on=date(2026, 7, 26), ends_on=date(2027, 1, 24),
    )
    VineCycle.objects.filter(starts_on=date(2027, 1, 27)).update(
        starts_on=NEXT_KNOWN_START, ends_on=date(2027, 7, 24),
    )
    VineCycle.objects.filter(starts_on__gt=NEXT_KNOWN_START).delete()


def restore_seeded_boundaries(apps, schema_editor):
    """Put migration 0002's 27th-of-the-month rows back, tail included."""
    VineCycle = apps.get_model("reviews", "VineCycle")
    VineCycle.objects.filter(starts_on__in=[date(2026, 7, 26), NEXT_KNOWN_START]).delete()
    VineCycle.objects.bulk_create(
        [
            VineCycle(starts_on=date(year, month, 27), ends_on=ends_on)
            for year in range(2026, 2031)
            for month, ends_on in ((1, date(year, 7, 26)), (7, date(year + 1, 1, 26)))
        ],
        ignore_conflicts=True,
    )
    VineCycle.objects.filter(starts_on=date(2026, 1, 27)).update(ends_on=date(2026, 7, 26))


class Migration(migrations.Migration):

    dependencies = [
        ("reviews", "0003_review_text_is_complete"),
    ]

    operations = [
        migrations.RunPython(correct_boundaries, restore_seeded_boundaries),
    ]
