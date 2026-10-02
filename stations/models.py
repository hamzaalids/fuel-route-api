from django.db import models


class Station(models.Model):
    """A truck stop with a fuel price and a (city-centroid accurate) position.

    The table exists for admin/inspection and as the canonical persisted copy.
    Runtime route searches use the in-memory ``StationIndex`` in ``loader.py``.
    """

    station_id = models.IntegerField(primary_key=True, help_text="OPIS Truckstop ID")
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    price = models.DecimalField(max_digits=6, decimal_places=3, help_text="Retail price per gallon")
    lat = models.FloatField()
    lon = models.FloatField()
    geocode_source = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["station_id"]
        indexes = [models.Index(fields=["lat", "lon"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.price}"
