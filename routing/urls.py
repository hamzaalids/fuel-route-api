from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.HealthView.as_view(), name="health"),
    path("route/", views.RouteView.as_view(), name="route"),
    path("route/map/", views.route_map, name="route-map"),
]
