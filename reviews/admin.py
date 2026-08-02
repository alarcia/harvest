from django.contrib import admin

from .models import ReferenceReview, Review, VineCycle


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


@admin.register(ReferenceReview)
class ReferenceReviewAdmin(admin.ModelAdmin):
    """The corpus, and the admin is the only way into it — every row is typed
    or pasted by hand, which is the whole reason this is a table of its own and
    not a query over `Review`. All of them go out with every proposal, so this
    list *is* what the suggestions imitate: what's here is the whole input."""

    list_display = ("product_title", "rating", "title", "added_on")
    list_filter = ("rating",)
    search_fields = ("product_title", "title", "text")
