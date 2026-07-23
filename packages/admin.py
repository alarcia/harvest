from django.contrib import admin

from .models import Package, PickupPoint, RawEmail


@admin.register(PickupPoint)
class PickupPointAdmin(admin.ModelAdmin):
    list_display = ("name", "kind")
    list_filter = ("kind",)
    search_fields = ("name",)


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
    list_filter = ("state", "is_vine", "pickup_point")
    search_fields = ("description", "pickup_code", "order_id", "shipment_id",
                     "carrier_tracking_number")
    date_hierarchy = "deadline"
    list_select_related = ("pickup_point",)


@admin.register(RawEmail)
class RawEmailAdmin(admin.ModelAdmin):
    list_display = ("__str__", "kind", "received_at", "processed",
                    "parse_error", "note")
    list_filter = ("processed", "kind")
    search_fields = ("subject", "message_id", "parse_error")
    readonly_fields = ("raw",)
