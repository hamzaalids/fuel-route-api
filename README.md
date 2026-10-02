# Fuel-Route API

Django REST API that takes a start and a finish inside the USA and returns the driving route,
the **cost-optimal fuel stops** for a vehicle with a 500-mile range and 10 MPG, the **total fuel
spend**, and a Leaflet map. A normal request makes **exactly one external call** (OSRM); repeated
requests are served from cache in milliseconds.

```text
POST /api/v1/route/            {"start": "New York, NY", "finish": "Chicago, IL"}
GET  /api/v1/route/?start=New%20York%2C%20NY&finish=Chicago%2C%20IL      same, browser-friendly
GET  /api/v1/route/map/?start=New%20York%2C%20NY&finish=Chicago%2C%20IL
GET  /api/v1/health/
GET  /api/docs/                Swagger UI (drf-spectacular)
GET  /                         landing page: start/finish form, example links
```

- Django 6.1.1 · Django REST Framework 3.18.1 · Python 3.12+ · numpy · requests · SQLite
- Routing: public [OSRM](http://project-osrm.org/) demo server (configurable)
- Station data: `data/fuel-prices-for-be-assessment.csv`, geocoded **offline once** to city level and committed

---

## Quick start

```bash
git clone <this repo> && cd fuel-route-api
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                # defaults work out of the box
python manage.py migrate
python manage.py load_stations                      # geocoded CSV -> SQLite (for admin/inspection)
python manage.py runserver
```

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/route/ \
     -H "Content-Type: application/json" \
     -d '{"start": "New York, NY", "finish": "Chicago, IL"}' | python -m json.tool
```

Then open the `map.url` from the response, e.g.
`http://127.0.0.1:8000/api/v1/route/map/?start=New+York%2C+NY&finish=Chicago%2C+IL`.

No API keys are needed. The runtime reads the committed `data/stations_geocoded.csv` and
`data/us_places_gazetteer.csv`; the database is only used for the admin and `load_stations`.

Run the tests and linter:

```bash
pytest          # 183 tests, all external HTTP mocked
ruff check . && ruff format --check .
```

Postman: import `postman/fuel-route.postman_collection.json` (happy path, long trip, short trip,
cache-hit demo, coordinates input, and every error case; each request has assertions).

---

## API reference

### `POST /api/v1/route/`

Request body (JSON). Both fields required, non-empty, ≤ 200 characters. Accepted forms:

| Form | Example | Resolved by | External calls |
| --- | --- | --- | --- |
| `City, ST` / `City, State` | `Chicago, IL`, `Chicago, Illinois` | local gazetteer | 0 |
| `lat,lon` | `40.7128,-74.0060` | parsed | 0 |
| anything else | `Times Square, Manhattan` | Nominatim (cached 30 days) | 1 |

Both points must fall inside the contiguous-US bounding box (lat 24–50, lon −125 to −66.5).

Success `200` (geometry and GeoJSON truncated):

```json
{
  "start":  {"query": "New York, NY", "lat": 40.66271, "lon": -73.93868},
  "finish": {"query": "Chicago, IL",  "lat": 41.83705, "lon": -87.68494},
  "route": {
    "distance_miles": 795.77,
    "duration_minutes": 906,
    "geometry": {"type": "LineString", "coordinates": [[-73.93868, 40.66271], "..."]}
  },
  "vehicle": {"max_range_miles": 500.0, "mpg": 10.0, "tank_capacity_gallons": 50.0},
  "fuel_stops": [
    {
      "order": 1, "station_id": 62790, "name": "7-ELEVEN #40084", "address": "US-46/US-1/US-9",
      "city": "Palisades Park", "state": "NJ", "lat": 40.84702, "lon": -73.99706,
      "miles_from_start": 10.0, "offset_from_route_miles": 8.2,
      "price_per_gallon": 3.099, "gallons_purchased": 6.006, "cost": 18.61,
      "tank_gallons_on_arrival": 0.0
    },
    {"order": 2, "name": "DELAWARE TRUCK STOP", "city": "Delaware", "state": "NJ", "miles_from_start": 70.1,
     "price_per_gallon": 3.079, "gallons_purchased": 32.632, "cost": 100.47, "...": "..."},
    {"order": 3, "name": "SHEETZ #639", "city": "Youngstown", "state": "OH", "miles_from_start": 396.4,
     "price_per_gallon": 3.059, "gallons_purchased": 16.516, "cost": 50.52, "...": "..."},
    {"order": 4, "name": "S&G #88", "city": "Toledo", "state": "OH", "miles_from_start": 561.5,
     "price_per_gallon": 3.009, "gallons_purchased": 23.423, "cost": 70.48, "...": "..."}
  ],
  "summary": {
    "total_gallons": 79.577,
    "total_fuel_cost": 243.2,
    "average_price_per_gallon": 3.056,
    "number_of_stops": 4,
    "origin_fill": {
      "price_per_gallon": 3.112, "gallons": 1.001, "cost": 3.12, "price_basis": "nearby",
      "note": "The start point is not a station in the dataset, so it is priced from nearby stations (average of the 5 cheapest within 50 miles). The tank starts empty; this is the fuel bought before departure."
    }
  },
  "map": {
    "url": "/api/v1/route/map/?start=New+York%2C+NY&finish=Chicago%2C+IL",
    "geojson": {"type": "FeatureCollection", "features": ["LineString route", "Point start", "Point finish", "Point per stop"]}
  },
  "meta": {
    "external_api_calls": 1,
    "cache_hit": false,
    "candidate_stations_in_corridor": 206,
    "geocode_sources": {"start": "gazetteer", "finish": "gazetteer"},
    "timings_ms": {"geocode": 0, "routing": 969, "projection": 22, "optimize": 1, "build": 12, "total": 1054}
  }
}
```

Guarantees on every success response:

- `summary.total_gallons == route.distance_miles / 10` (the origin fill is included).
- `sum(fuel_stops[].cost) + summary.origin_fill.cost == summary.total_fuel_cost` **exactly** (money is
  `Decimal`, rounded to cents once per purchase).
- No leg between consecutive fuel points (origin, stops, destination) exceeds 500 miles.
- `route.geometry` is down-sampled to ≈0.25-mile spacing with exact endpoints.

Errors (`{"error": {"code": "...", "message": "..."}}`):

| Situation | HTTP | `code` |
| --- | --- | --- |
| Missing/blank/too-long field, same start & finish, bad JSON | 400 | `invalid_request` |
| Location can't be geocoded, or a bogus state code such as `Nowhere, ZZ` (rejected locally) | 404 | `location_not_found` |
| Location outside the contiguous USA (incl. `Toronto, ON`, `Tijuana, Mexico` — rejected locally, no geocoder call) | 400 | `outside_usa` |
| OSRM finds no route | 422 | `no_route` |
| No feasible fuel plan (gap > 500 mi between reachable fuel points; message says where) | 422 | `no_feasible_fuel_plan` |
| OSRM 5xx / connection error after one retry | 502 | `routing_unavailable` |
| OSRM timeout after one retry | 504 | `routing_unavailable` |
| Nominatim fallback failed (e.g. rejected User-Agent) | 502 | `geocoding_unavailable` |

### `GET /api/v1/route/?start=…&finish=…`

Identical response to the POST, taking the two locations as query parameters so it can be opened
straight from a browser or linked to. Same validation, same errors, same cache.

### `GET /api/v1/route/map/?start=…&finish=…`

HTML page (Leaflet + OpenStreetMap tiles via CDN, no build step): blue route polyline, green start
marker, red finish marker, numbered fuel-stop markers with popups (name, city/state, price, gallons,
cost) and a summary panel. It calls the same planner, so right after the POST it is a cache hit.

### `GET /`

Landing page with a start/finish form ("Show map" / "JSON" buttons), example trips and links to the
Swagger docs, so a reviewer can try the service without any tooling.

---

## Architecture

```text
OFFLINE (run once, result committed to repo)
  CSV ──clean/dedupe/US-only──▶ unique (city,state) ──▶ local gazetteer (+ Nominatim only for misses)
      ──▶ data/stations_geocoded.csv   (committed, so reviewers need zero setup)

RUNTIME  POST /api/v1/route/                                              routing/services/planner.py
  1. validate input                                                        serializers.py
  2. cache lookup (key = sha1(normalized start|finish))   ── hit → return immediately
  3. resolve start/finish → lat/lon   (local gazetteer; Nominatim fallback, cached)   geocoding.py
  4. ONE OSRM call → distance, duration, full geometry                     osrm.py
  5. resample route to ~1 mile spacing, cumulative miles  (numpy, local)   geometry.py
  6. stations within corridor of route + miles-along-route (numpy, local)  geometry.py
  7. fuel optimizer (pure Python, local)                                   optimizer.py
  8. build response + GeoJSON, store in cache, return                      planner.py
```

Why this shape:

- **Fuel data depends on the route**, so the flow is strictly sequential, not "geocode | route | fuel"
  in parallel.
- **The CSV has no coordinates.** Stations are geocoded once, offline, to city level (3,808 unique
  city/state pairs) and the result is committed. At request time station matching is pure numpy —
  no Overpass/Google/fuzzy name matching, no per-station calls.
- **Start/finish resolve locally.** "City, ST" is looked up in a committed gazetteer; Nominatim is only
  a fallback for free text. So the normal request budget is one external call (OSRM).
- The ~6k stations live in in-memory numpy arrays loaded once per process (`stations/loader.py`,
  warmed in `RoutingConfig.ready()` for `runserver`/WSGI, lazy otherwise). No DB or CSV read per request.

Code tour (≈90 s): `routing/services/planner.py` (flow) → `routing/services/optimizer.py`
(algorithm) → `tests/test_optimizer.py` (hand-checked cases + brute-force cross-check).

---

## How the optimizer works

This is the classic **gas-station problem**: a fixed tank (500 mi / 10 MPG = 50 gal), fuel can be
bought in any amount at any station, minimize total cost. A greedy look-ahead is provably optimal.

Model: nodes are `[origin] + stations sorted by miles-along-route + [destination]`. The tank starts
**empty** at the origin, which acts as a fill-up point priced at `origin_price` (see below). The
destination has price 0.

```text
fuel = 0; i = origin; plan = []
while i != destination:
    reachable = [j > i  with  pos[j] - pos[i] <= 500 (+1e-9)]
    if not reachable: raise NoFeasiblePlan(gap after pos[i])
    cheaper = first j in reachable (route order) with price[j] < price[i]
    if cheaper exists:
        buy = max(0, (pos[cheaper] - pos[i]) / mpg - fuel)   # only what is needed to reach it
        go to cheaper
    else:                                                    # nothing cheaper in range
        buy = capacity - fuel                                # fill up here …
        go to the cheapest node in reachable                 # … tie → the farthest (fewer stops)
    record purchase (i, buy, price[i]); fuel += buy; fuel -= dist / mpg
```

Why it is optimal: fuel is bought at a node only when (a) nothing cheaper is reachable — then every
gallon bought here would otherwise have to be bought at a higher price later — or (b) in exactly the
amount needed to reach a cheaper node, where the remaining demand is bought more cheaply. No gallon
in the plan could have been purchased at a lower price given the range constraint. The destination's
price 0 makes "buy just enough to finish" fall out of case (a). Stations with zero purchase are not
reported as stops. `tests/test_optimizer.py` cross-checks 40 random instances (≤ 8 stations, fuel
discretised to 0.5 gal) against an exact dynamic-programming solution.

**Origin price.** The start point is not a station in the dataset, so it is priced from nearby
stations: the average of the 5 cheapest stations within 50 miles; if fewer than 3 stations are
within 50 miles, the average of the start state (state of the nearest station); otherwise the global
median. `summary.origin_fill.price_basis` says which rule applied. Because the tank starts empty,
`total_gallons` always equals `distance / 10` and `total_fuel_cost` is the real cost of every gallon
burned (not just the intermediate stops).

---

## Assumptions

- **Vehicle**: 500-mile range, 10 MPG, hence a 50-gallon usable tank. It starts empty and buys fuel
  at the origin, priced from nearby stations (above).
- **Dataset**: the 620 Canadian rows are removed (route is USA-only). One price per station: rows
  sharing an *OPIS Truckstop ID* are the same station under name variants and are collapsed to the
  **lowest** price (`STATION_PRICE_POLICY=min`, also `max`/`first`), keeping the first non-empty name.
  *Rack ID* is an OPIS pricing-rack code, not a ZIP/location, and is ignored.
- **Station locations are city-centroid accurate** (a few miles), so a generous **10-mile corridor**
  (`CORRIDOR_MILES`) is used and stations in the same town share a position. The detour to reach a
  station off the route is not modelled; it is reported as `offset_from_route_miles`.
- **Prices** are a single grade per station as given in the file, quantised to 1/10 cent, and
  treated as current.
- **Routing** is OSRM's driving profile; traffic is not considered. Along-route positions are scaled
  so that the polyline length equals OSRM's reported road distance.

---

## Data pipeline & geocoding

`python manage.py build_station_geodata` (offline, already run; output committed):

| Step | Result on the shipped CSV |
| --- | --- |
| rows in CSV | 8,151 |
| US rows (50 states + DC) | 7,531 (dropped 620: ON 217, AB 180, BC 121, MB 42, SK 36, YT 8, QC 6, NS 6, NB 4) |
| dedupe by OPIS Truckstop ID (lowest price) | 6,626 |
| dedupe by (normalised address, city, state), cheapest wins | 6,169 |
| unique (city, state) pairs | 3,808 |
| geocoded via local gazetteer / via Nominatim / failed | 3,794 / 14 / 0 |
| stations written to `data/stations_geocoded.csv` | 6,169 |

City names are normalised before matching: trim/collapse whitespace, casefold, `St.`→`Saint`,
`Ft.`→`Fort`, `Mt.`→`Mount`, hyphens→spaces, and `Mc Lean`/`La Fayette`/`Du Bois`/`De Forest` joined
to match `McLean`/`LaFayette`/`DuBois`/`DeForest`. Every station is checked against the contiguous-US
box and a rough per-state bounding box derived from Census places; the command prints a summary and
any outliers. Nominatim misses are queried at ≤ 1 request/second with a resumable cache
(`data/nominatim_cache.json`, git-ignored), structured search first then free text, rejecting hits
outside the state's box (this caught "La Place, LA" being resolved into Alabama).

`data/us_places_gazetteer.csv` (34,898 places, 1.4 MB) is built by `python manage.py build_gazetteer`
from two free downloads placed in `data/raw/` (git-ignored):

- US Census Bureau Gazetteer, Places, 2024 — primary, authoritative centroids:
  `https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_place_national.zip`
- GeoNames US dump — fills New England towns, PA/NJ townships and consolidated cities (Boise,
  Nashville, Athens GA…) that the Census *places* file lacks:
  `https://download.geonames.org/export/dump/US.zip`

Census rows win; GeoNames populated places are appended only when the key is missing and the place is
populated, an administrative seat, or needed by a station — which keeps the committed file small.

`python manage.py load_stations` upserts the geocoded CSV into the `Station` table (idempotent).

---

## Performance & external-call budget

| Trip | Distance | Stops | Uncached total | of which OSRM | projection | cached |
| --- | --- | --- | --- | --- | --- | --- |
| New York → Chicago | 796 mi | 4 | ≈1.0 s | ≈0.95 s | ≈20 ms | ≈2 ms |
| Los Angeles → Dallas | 1,452 mi | 6 | ≈0.25–1.0 s | ≈0.2–1.0 s | ≈12 ms | ≈3 ms |
| Miami → Seattle | 3,304 mi | 22 | ≈0.6–1.4 s | ≈0.4–1.3 s | ≈23 ms | ≈6 ms |
| Denver → Colorado Springs | 74 mi | 0 | ≈0.17 s | ≈0.16 s | ≈3 ms | <1 ms |

(Measured against the public OSRM demo, whose latency varies; all other steps are local.)

- `meta.external_api_calls` is **1** for "City, ST" or "lat,lon" inputs, **2** if one endpoint needed
  Nominatim, **0** on a cache hit. An OSRM retry (timeout/5xx only, once) is counted honestly.
- Station projection is a two-stage numpy search: per-chunk bounding boxes hug the route (a single
  box around a diagonal route would admit most of the country), a coarse pass against every 10th
  route point via a BLAS matmul, then exact haversine around the best coarse points. ≈13 ms for a
  3,300-mile route against all 6,169 stations (`tests/test_geometry.py` asserts an upper bound).
- Payloads: ≈130 KB for NY→Chicago, ≈560 KB for Miami→Seattle (the 0.25-mile geometry appears both in
  `route.geometry` and `map.geojson`).

---

## Caching

- Full plan responses are cached for `ROUTE_CACHE_TTL` seconds (default 24 h) under
  `route:<sha1("normalized_start|normalized_finish")>`; normalisation casefolds, collapses whitespace,
  canonicalises the state ("Chicago, Illinois" ≡ "chicago, il") and strips punctuation noise.
- Nominatim results are cached separately for 30 days (negative results for 1 hour).
- Backend is Django's cache framework: `LocMemCache` by default, swap via `CACHE_BACKEND` /
  `CACHE_LOCATION` (e.g. `django.core.cache.backends.redis.RedisCache` + `redis://…`).
- Errors are never cached. One log line per request records cache hit/miss, external calls and
  timings; secrets are never logged.

---

## Configuration

All settings come from the environment / `.env` (see `.env.example` for documentation and defaults):
`SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `OSRM_BASE_URL`, `OSRM_TIMEOUT_SECONDS`, `NOMINATIM_BASE_URL`,
`NOMINATIM_USER_AGENT`, `MAX_RANGE_MILES`, `MPG`, `CORRIDOR_MILES`, `ROUTE_CACHE_TTL`, `CACHE_BACKEND`,
`CACHE_LOCATION`, `STATION_PRICE_POLICY`.

**Routing provider.** `OSRM_BASE_URL` defaults to the public demo `https://router.project-osrm.org`,
which is fine for demos but rate-limited and without SLA. Point it at a self-hosted OSRM
(`docker run … osrm/osrm-backend`) or any OSRM-compatible endpoint. Another provider can implement
the two-method `RoutingProvider` protocol in `routing/services/osrm.py`.

---

## Testing

```bash
pytest                       # unit + API tests, all HTTP mocked with requests-mock
pytest -q tests/test_optimizer.py
ruff check . && ruff format --check .
```

- `tests/test_optimizer.py`: hand-checked table cases (cheaper station halfway, don't over-buy at an
  expensive station, gap of exactly 500 vs 500.01, all stations pricier than origin, short trip,
  ties, same-position stations), invariants (gallons = miles/10, tank within [0, 50], no leg > 500),
  and a brute-force DP cross-check on random instances.
- `tests/test_geometry.py`: haversine, resampling, projection accuracy on straight and winding routes,
  and the coast-to-coast timing bound.
- `tests/test_osrm.py`, `tests/test_geocoding.py`: provider behaviour with mocked HTTP (retry rules,
  error mapping, gazetteer vs Nominatim, caching).
- `tests/test_api.py`, `tests/test_caching.py`: every row of the error table, cache hit → zero
  external calls, `external_api_calls == 1` for "City, ST", exact money invariants, map page.
- `tests/test_pipeline.py`, `tests/test_stations_data.py`: cleaning/dedupe rules and the numbers of
  the shipped dataset; committed data files are validated.

---

## Limitations & what I'd do with more time

- The public OSRM demo is not for production; self-host OSRM (or another provider) and add a circuit
  breaker.
- Station positions are city centroids, hence the 10-mile corridor and no detour cost. With real
  station coordinates I'd tighten the corridor and add the detour miles (and their fuel) to the
  optimisation.
- The optimiser minimises cost only; with no per-stop penalty it can emit small top-ups (e.g. 1–2 gal)
  when prices rise gradually along a route. A minimum-purchase rule or a fixed stop cost would trade a
  few cents for fewer stops.
- A single fuel grade/price per station; prices are treated as current.
- Contiguous US only (no Alaska/Hawaii); no traffic, tolls or time windows.
- `LocMemCache` is per-process; use Redis for multi-worker deployments.

---

## Decisions log

- **Django 6.1.1 / DRF 3.18.1** — latest stable on PyPI at build time; DRF 3.18.1 lists
  `Framework :: Django :: 6.1`. drf-spectacular 0.30.0 only lists up to Django 6.0 but works
  (schema + Swagger UI are tested), so `/api/docs/` is included.
- **Gazetteer source** — Census *Places* alone resolved 93 % of station cities; New England towns,
  townships and consolidated city-counties are not "places", so GeoNames fills the gap. Combined
  coverage is 99.6 % locally; the remaining 14 pairs came from Nominatim during the offline build.
- **Dedupe by address after dedupe by ID** — stations at the same exit with an identical address
  string collapse to the cheapest (6,626 → 6,169). Since they also share a city centroid, the
  optimiser would pick the cheapest anyway; this just keeps the corridor list smaller.
- **Prices quantised to 1/10 cent** — the CSV contains 3-way averages like `3.00733333`; fuel is
  quoted to a tenth of a cent, and this keeps `gallons × price` reproducible from the response.
- **Along-route miles scaled to OSRM's distance** — the polyline chord length is slightly shorter
  than OSRM's road distance; scaling makes `distance_miles`, leg lengths and `total_gallons` agree.
- **Origin fill reported explicitly** — a cheaper station may sit exactly at the start centroid; then
  the origin buys nothing and the estimated origin price is still shown with `gallons: 0`.
- **Cache key hashed** — the spec's `route:{start}|{finish}` contains spaces/punctuation that some
  backends (memcached) reject, so the normalised key is SHA-1 hashed.
- **Nominatim User-Agent** — Nominatim returns HTTP 403 for placeholder UAs containing `example.com`;
  the default in `.env.example` avoids that and the error is surfaced as `geocoding_unavailable`.
- **Station index warm-up** in `AppConfig.ready()` only for serving commands, so `migrate` and the
  data commands work before the data files exist; a missing `stations_geocoded.csv` fails fast with
  the command to run.
