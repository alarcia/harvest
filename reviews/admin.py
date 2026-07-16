from django.contrib import admin

from .models import Review, VineCycle


@admin.register(VineCycle)
class VineCycleAdmin(admin.ModelAdmin):
    list_display = ("starts_on", "ends_on")


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = (
        "product_title", "status", "due_on", "rating",
        "approved_on", "published_on",
    )
    list_filter = ("status",)
    search_fields = ("product_title", "asin", "review_id")
    date_hierarchy = "due_on"
    raw_id_fields = ("package",)
