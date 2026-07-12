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
    search_fields = ("description", "pickup_code")
    date_hierarchy = "deadline"
    list_select_related = ("pickup_point",)


@admin.register(RawEmail)
class RawEmailAdmin(admin.ModelAdmin):
    list_display = ("__str__", "message_id", "received_at", "processed")
    list_filter = ("processed",)
    search_fields = ("subject", "message_id")
    readonly_fields = ("raw",)
