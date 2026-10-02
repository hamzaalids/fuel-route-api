import logging
import sys
import time

from django.apps import AppConfig

log = logging.getLogger(__name__)

# Management commands that don't serve requests skip the warm-up so e.g. `migrate`
# or `build_station_geodata` work before the data files exist.
_SERVING_COMMANDS = {"runserver", "runserver_plus"}


class RoutingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "routing"

    def ready(self) -> None:
        is_manage_py = len(sys.argv) > 1 and sys.argv[0].endswith("manage.py")
        if is_manage_py and sys.argv[1] not in _SERVING_COMMANDS:
            return
        if "pytest" in sys.modules:
            return
        self._warm_up()

    @staticmethod
    def _warm_up() -> None:
        """Load the station index and gazetteer once so the first request is fast."""
        from stations.gazetteer import get_gazetteer
        from stations.loader import get_station_index

        started = time.perf_counter()
        index = get_station_index()  # raises FileNotFoundError with the command to run if missing
        gazetteer = get_gazetteer()
        log.info(
            "Loaded %d stations and %d gazetteer places in %.0f ms",
            len(index),
            len(gazetteer),
            (time.perf_counter() - started) * 1000,
        )
