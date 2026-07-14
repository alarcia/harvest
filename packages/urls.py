from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("packages/new/", views.add_package, name="add_package"),
    path("packages/<int:pk>/", views.package_detail, name="package_detail"),
]
