from django.urls import path

from . import views

urlpatterns = [
    path("", views.reviews_list, name="reviews_list"),
    path("<int:pk>/", views.review_detail, name="review_detail"),
    path("<int:pk>/borrador/", views.review_edit, name="review_edit"),
    path("<int:pk>/propuesta/", views.review_suggest, name="review_suggest"),
    path("<int:pk>/publicada/", views.review_approve, name="review_approve"),
]
