from django.contrib import admin

from .models import Station


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = ("station_id", "name", "city", "state", "price", "lat", "lon", "geocode_source")
    list_filter = ("state", "geocode_source")
    search_fields = ("name", "city", "address")
    ordering = ("price",)
