from django.contrib import admin

from .models import Config, Package, PickupPoint, RawEmail


@admin.register(Config)
class ConfigAdmin(admin.ModelAdmin):
    """One row, always pk=1 (see Config.load). Adding a second or deleting the
    only one would leave ingestion reading a different row than the user edits,
    so both are off."""

    list_display = ("__str__", "eu_import_surcharge")

    def has_add_permission(self, request):
        return not Config.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PickupPoint)
class PickupPointAdmin(admin.ModelAdmin):
    # `display_name` is editable from the list: it's filled in once per venue,
    # reading the full name in the column beside it, which is exactly the
    # comparison the user is making when he shortens it.
    list_display = ("name", "display_name", "kind")
    list_editable = ("display_name",)
    list_filter = ("kind",)
    search_fields = ("name", "display_name")


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "pickup_point",
        "state",
        "is_vine",
        "cost",
        "estimated_arrival",
        "actual_arrival",
        "deadline",
    )
    list_filter = ("state", "is_vine", "pickup_point", "item_kind")
    search_fields = ("description", "pickup_code", "order_id", "shipment_id",
                     "carrier_tracking_number", "recipient")
    date_hierarchy = "deadline"
    list_select_related = ("pickup_point",)


@admin.register(RawEmail)
class RawEmailAdmin(admin.ModelAdmin):
    list_display = ("__str__", "kind", "received_at", "processed",
                    "parse_error", "note")
    list_filter = ("processed", "kind")
    search_fields = ("subject", "message_id", "parse_error")
    readonly_fields = ("raw",)
