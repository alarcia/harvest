"""Re-apply the Vine rule to packages ingested before the EU surcharge
exception existed (see models.Config.means_vine).

Those were unmarked as Vine by their "Enviado" email for costing money, when
the money was only the import duty a non-EU seller passed on. Ingestion is
idempotent by Message-ID and `reprocess` only touches parse *failures*, so
nothing else would ever revisit them — hence this one-shot pass.

Only Amazon-ingested rows (an order id) whose cost is *exactly* the configured
surcharge are touched. They owe a review each: run `manage.py backfill_reviews`
afterwards (idempotent) to create the missing `pending` rows.
"""

from django.db import migrations


def reflag_surcharged_vine(apps, schema_editor):
    Config = apps.get_model("packages", "Config")
    Package = apps.get_model("packages", "Package")

    config, _ = Config.objects.get_or_create(pk=1)
    if not config.eu_import_surcharge:
        return
    Package.objects.filter(
        is_vine=False, cost=config.eu_import_surcharge
    ).exclude(order_id="").update(is_vine=True)


class Migration(migrations.Migration):

    dependencies = [
        ("packages", "0008_config"),
    ]

    # Irreversible on purpose: the flag it sets is indistinguishable from one
    # the user ticked by hand, so unwinding would be guesswork.
    operations = [
        migrations.RunPython(reflag_surcharged_vine, migrations.RunPython.noop),
    ]
