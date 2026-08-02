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
    """The corpus. It fills itself as reviews are validated, but the admin is
    how the *good* old ones get in — years of reviews written straight on
    Amazon, typed or pasted in by hand, which is the whole reason this is a
    table of its own and not a query over `Review`."""

    list_display = ("product_title", "rating", "title", "is_example",
                    "is_pinned", "added_on")
    # Both judgements are made while reading the list of examples — "this one
    # isn't me" and "this one always goes" — so both are editable right there.
    list_editable = ("is_example", "is_pinned")
    list_filter = ("is_example", "is_pinned", "rating")
    search_fields = ("product_title", "title", "text")
    raw_id_fields = ("source_review",)
