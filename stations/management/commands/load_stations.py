"""Load ``data/stations_geocoded.csv`` into the Station table (idempotent)."""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from stations.models import Station


class Command(BaseCommand):
    help = "Load the geocoded stations CSV into the database (update_or_create on station_id)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--input", type=Path, default=Path(settings.STATIONS_GEOCODED_CSV))

    def handle(self, *args, **options) -> None:
        path: Path = options["input"]
        if not path.exists():
            raise CommandError(f"{path} not found. Run `python manage.py build_station_geodata` first.")

        created = updated = 0
        with path.open(encoding="utf-8", newline="") as fh, transaction.atomic():
            for row in csv.DictReader(fh):
                _, was_created = Station.objects.update_or_create(
                    station_id=int(row["station_id"]),
                    defaults={
                        "name": row["name"],
                        "address": row["address"],
                        "city": row["city"],
                        "state": row["state"],
                        "price": Decimal(row["price"]),
                        "lat": float(row["lat"]),
                        "lon": float(row["lon"]),
                        "geocode_source": row["geocode_source"],
                    },
                )
                created += was_created
                updated += not was_created
        self.stdout.write(
            f"Stations loaded: {created} created, {updated} updated, {Station.objects.count()} total."
        )
