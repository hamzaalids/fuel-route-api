"""Request validation and response schema (the latter is for API docs only)."""

from rest_framework import serializers

from .services.geocoding import normalize_query


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200, allow_blank=False, trim_whitespace=True)
    finish = serializers.CharField(max_length=200, allow_blank=False, trim_whitespace=True)

    def validate(self, attrs: dict) -> dict:
        if normalize_query(attrs["start"]) == normalize_query(attrs["finish"]):
            raise serializers.ValidationError("Start and finish must be different locations.")
        return attrs


class PointSerializer(serializers.Serializer):
    query = serializers.CharField()
    lat = serializers.FloatField()
    lon = serializers.FloatField()


class GeometrySerializer(serializers.Serializer):
    type = serializers.CharField()
    coordinates = serializers.ListField(child=serializers.ListField(child=serializers.FloatField()))


class RouteInfoSerializer(serializers.Serializer):
    distance_miles = serializers.FloatField()
    duration_minutes = serializers.IntegerField()
    geometry = GeometrySerializer()


class VehicleSerializer(serializers.Serializer):
    max_range_miles = serializers.FloatField()
    mpg = serializers.FloatField()
    tank_capacity_gallons = serializers.FloatField()


class FuelStopSerializer(serializers.Serializer):
    order = serializers.IntegerField()
    station_id = serializers.IntegerField()
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    miles_from_start = serializers.FloatField()
    offset_from_route_miles = serializers.FloatField()
    price_per_gallon = serializers.FloatField()
    gallons_purchased = serializers.FloatField()
    cost = serializers.FloatField()
    tank_gallons_on_arrival = serializers.FloatField()


class OriginFillSerializer(serializers.Serializer):
    price_per_gallon = serializers.FloatField()
    gallons = serializers.FloatField()
    cost = serializers.FloatField()
    price_basis = serializers.ChoiceField(choices=["nearby", "state", "global"])
    note = serializers.CharField()


class SummarySerializer(serializers.Serializer):
    total_gallons = serializers.FloatField()
    total_fuel_cost = serializers.FloatField()
    average_price_per_gallon = serializers.FloatField()
    number_of_stops = serializers.IntegerField()
    origin_fill = OriginFillSerializer()


class MapSerializer(serializers.Serializer):
    url = serializers.CharField()
    geojson = serializers.DictField()


class TimingsSerializer(serializers.Serializer):
    geocode = serializers.IntegerField(required=False)
    routing = serializers.IntegerField(required=False)
    projection = serializers.IntegerField(required=False)
    optimize = serializers.IntegerField(required=False)
    build = serializers.IntegerField(required=False)
    total = serializers.IntegerField()


class MetaSerializer(serializers.Serializer):
    external_api_calls = serializers.IntegerField()
    cache_hit = serializers.BooleanField()
    candidate_stations_in_corridor = serializers.IntegerField()
    geocode_sources = serializers.DictField(child=serializers.CharField())
    timings_ms = TimingsSerializer()


class RouteResponseSerializer(serializers.Serializer):
    start = PointSerializer()
    finish = PointSerializer()
    route = RouteInfoSerializer()
    vehicle = VehicleSerializer()
    fuel_stops = FuelStopSerializer(many=True)
    summary = SummarySerializer()
    map = MapSerializer()
    meta = MetaSerializer()


class ErrorDetailSerializer(serializers.Serializer):
    code = serializers.CharField()
    message = serializers.CharField()


class ErrorResponseSerializer(serializers.Serializer):
    error = ErrorDetailSerializer()
