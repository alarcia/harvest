"""Split Tesla accessories order products into individual packages and reviews.

Separates items that were grouped together with '+' into individual packages
and reviews with their own ASIN, title, image, and estimated arrival.
"""

from datetime import date
from django.db import migrations


def split_tesla_packages(apps, schema_editor):
    Package = apps.get_model("packages", "Package")
    Review = apps.get_model("reviews", "Review")

    wigoo_clean = (
        "Wigoo Accesorios actualizados para Tesla Model 3 2025 "
        "[Mantente limpio, protege la pintura] Protectores contra salpicaduras "
        "para todo tipo de clima con kit de instalación, para Tesla Model 3 2024"
    )
    spigen_clean = "Spigen Tesla Organizador de Almacenamiento Oculto Debajo de la Pantalla"
    lantu_clean = (
        "LANTU 5 Pcs Accesorios para Tesla Model 3 2024-2026 Organizador de Consola Central, "
        "Bandeja de Silicona, Caja de Almacenamiento para Reposabrazos, Caja Oculta y Portavasos"
    )

    # 1. Clean Wigoo package and review
    pkg_wigoo = Package.objects.filter(asin="B0DSBZK22W").first()
    if pkg_wigoo:
        if "+ Enerfero" in pkg_wigoo.description or "+ " in pkg_wigoo.description:
            pkg_wigoo.description = wigoo_clean
            pkg_wigoo.save(update_fields=["description"])
        Review.objects.filter(asin="B0DSBZK22W").filter(product_title__contains="+").update(
            product_title=wigoo_clean
        )

    # 2. Clean Spigen package and review
    pkg_spigen = Package.objects.filter(asin="B0GYV8X592").first()
    if pkg_spigen:
        if "+ LANTU" in pkg_spigen.description or "+ " in pkg_spigen.description:
            pkg_spigen.description = spigen_clean
            pkg_spigen.save(update_fields=["description"])
        Review.objects.filter(asin="B0GYV8X592").filter(product_title__contains="+").update(
            product_title=spigen_clean
        )

    # 3. Create missing LANTU package and review if Spigen exists and LANTU does not
    if pkg_spigen and not Package.objects.filter(asin="B0CR2VVM6P").exists():
        lantu_pkg = Package.objects.create(
            order_id=pkg_spigen.order_id,
            shipment_id=pkg_spigen.shipment_id or "TcRdZ1MrY",
            asin="B0CR2VVM6P",
            description=lantu_clean,
            image_url="https://m.media-amazon.com/images/I/71iyu36+j6L._SS90_.jpg",
            item_kind=pkg_spigen.item_kind,
            pickup_point=pkg_spigen.pickup_point,
            ordered_on=pkg_spigen.ordered_on,
            shipped_on=pkg_spigen.shipped_on or date(2026, 10, 14),
            is_vine=False,
            cost=pkg_spigen.cost,
            state=pkg_spigen.state,
            estimated_arrival=pkg_spigen.estimated_arrival or date(2026, 10, 15),
            estimated_arrival_end=pkg_spigen.estimated_arrival_end,
        )
        Review.objects.get_or_create(
            asin="B0CR2VVM6P",
            defaults={
                "package": lantu_pkg,
                "product_title": lantu_clean,
                "status": "pending",
                "text_is_complete": True,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("packages", "0018_alter_package_state"),
        ("reviews", "0010_update_review_due_on_14_days"),
    ]

    operations = [
        migrations.RunPython(split_tesla_packages, migrations.RunPython.noop),
    ]
