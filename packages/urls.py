from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("packages/new/", views.add_package, name="add_package"),
    path("packages/<int:pk>/", views.package_detail, name="package_detail"),
    path("picked/<str:day>/", views.picked_detail, name="picked_detail"),
    path("delivered/<str:day>/<int:point_id>/", views.delivered_detail, name="delivered_detail"),
    path("day/<str:day>/", views.day_detail, name="day_detail"),
]
