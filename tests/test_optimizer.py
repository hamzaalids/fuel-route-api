"""Table-driven, hand-checked optimizer tests plus a brute-force cross-check."""

from __future__ import annotations

import itertools
import random

import pytest

from routing.services.optimizer import (
    Candidate,
    FuelPlan,
    NoFeasiblePlan,
    estimate_origin_price,
    plan_fuel_stops,
)

RANGE = 500.0
MPG = 10.0
CAP = RANGE / MPG


def assert_invariants(plan: FuelPlan, total_miles: float, stations: list[Candidate], mpg=MPG, cap=CAP):
    """Fuel accounting must be exact and the tank must stay within [0, capacity]."""
    assert plan.total_gallons == pytest.approx(total_miles / mpg, abs=1e-6)
    assert plan.total_cost == pytest.approx(sum(p.cost for p in plan.purchases), abs=1e-9)
    assert all(p.gallons > 0 for p in plan.purchases)

    # Replay the trip: every purchase point + destination, in order.
    points = sorted({0.0, total_miles, *(p.miles for p in plan.purchases)})
    by_pos = {p.miles: p for p in plan.purchases}
    fuel = 0.0
    for prev, nxt in itertools.pairwise(points):
        purchase = by_pos.get(prev)
        if purchase:
            assert purchase.tank_on_arrival == pytest.approx(fuel, abs=1e-6)
            fuel += purchase.gallons
        assert fuel <= cap + 1e-6, "tank over capacity"
        assert nxt - prev <= RANGE + 1e-6, "leg longer than range"
        fuel -= (nxt - prev) / mpg
        assert fuel >= -1e-6, "fuel went negative"


def test_single_cheaper_station_halfway():
    stations = [Candidate(1, 200.0, 3.0)]
    plan = plan_fuel_stops(400.0, stations, origin_price=4.0)
    assert [(p.station_id, round(p.gallons, 6), p.price) for p in plan.purchases] == [
        (None, 20.0, 4.0),  # just enough to reach the cheaper station
        (1, 20.0, 3.0),  # rest of the trip from there
    ]
    assert plan.total_cost == pytest.approx(20 * 4.0 + 20 * 3.0)
    assert_invariants(plan, 400.0, stations)


def test_does_not_overbuy_at_expensive_station_when_cheaper_one_follows():
    stations = [Candidate(1, 300.0, 4.0), Candidate(2, 600.0, 3.0)]
    plan = plan_fuel_stops(900.0, stations, origin_price=3.5)
    assert [(p.station_id, round(p.gallons, 6)) for p in plan.purchases] == [
        (None, 50.0),  # nothing cheaper in range -> fill up at origin
        (1, 10.0),  # only enough to bridge to the cheap station
        (2, 30.0),
    ]
    assert plan.total_cost == pytest.approx(50 * 3.5 + 10 * 4.0 + 30 * 3.0)
    assert_invariants(plan, 900.0, stations)


def test_gap_exactly_range_is_feasible():
    stations = [Candidate(1, 500.0, 3.0)]
    plan = plan_fuel_stops(1000.0, stations, origin_price=3.0)
    assert [(p.station_id, round(p.gallons, 6)) for p in plan.purchases] == [(None, 50.0), (1, 50.0)]
    assert_invariants(plan, 1000.0, stations)


def test_gap_just_over_range_is_infeasible_and_reports_where():
    with pytest.raises(NoFeasiblePlan) as info:
        plan_fuel_stops(1000.0, [Candidate(1, 500.01, 3.0)], origin_price=3.0)
    assert info.value.gap_start_miles == 0.0
    assert info.value.gap_end_miles == pytest.approx(500.01)
    assert "mile 0.0" in str(info.value) and "500.0" in str(info.value)


def test_gap_in_the_middle_reports_the_last_reachable_point():
    stations = [Candidate(1, 400.0, 3.0), Candidate(2, 950.0, 3.0)]
    with pytest.raises(NoFeasiblePlan) as info:
        plan_fuel_stops(1200.0, stations, origin_price=3.0)
    assert info.value.gap_start_miles == 400.0
    assert info.value.gap_end_miles == 950.0


def test_all_stations_pricier_than_origin_fills_up_and_goes_to_cheapest_in_range():
    stations = [Candidate(1, 200.0, 3.5), Candidate(2, 400.0, 3.2)]
    plan = plan_fuel_stops(700.0, stations, origin_price=3.0)
    assert [(p.station_id, round(p.gallons, 6)) for p in plan.purchases] == [(None, 50.0), (2, 20.0)]
    assert plan.total_cost == pytest.approx(50 * 3.0 + 20 * 3.2)
    assert_invariants(plan, 700.0, stations)


def test_tie_on_price_prefers_the_farthest_station():
    stations = [Candidate(1, 200.0, 3.2), Candidate(2, 450.0, 3.2)]
    plan = plan_fuel_stops(800.0, stations, origin_price=3.0)
    assert [p.station_id for p in plan.stops] == [2]


def test_short_trip_needs_no_intermediate_stop():
    stations = [Candidate(1, 100.0, 3.9), Candidate(2, 250.0, 3.7)]
    plan = plan_fuel_stops(300.0, stations, origin_price=3.5)
    assert plan.stops == []
    assert plan.origin_fill is not None and plan.origin_fill.gallons == pytest.approx(30.0)
    assert_invariants(plan, 300.0, stations)


def test_short_trip_with_cheaper_station_still_uses_it():
    stations = [Candidate(1, 100.0, 2.9)]
    plan = plan_fuel_stops(300.0, stations, origin_price=3.5)
    assert [(p.station_id, round(p.gallons, 6)) for p in plan.purchases] == [(None, 10.0), (1, 20.0)]


def test_no_stations_short_trip():
    plan = plan_fuel_stops(120.0, [], origin_price=3.0)
    assert [(p.station_id, round(p.gallons, 6)) for p in plan.purchases] == [(None, 12.0)]


def test_no_stations_long_trip_is_infeasible():
    with pytest.raises(NoFeasiblePlan):
        plan_fuel_stops(600.0, [], origin_price=3.0)


def test_zero_length_trip_buys_nothing():
    plan = plan_fuel_stops(0.0, [], origin_price=3.0)
    assert plan.purchases == [] and plan.total_cost == 0.0


def test_two_stations_at_same_position_cheaper_wins():
    stations = [Candidate(1, 300.0, 3.4), Candidate(2, 300.0, 3.1), Candidate(3, 300.0, 3.6)]
    plan = plan_fuel_stops(600.0, stations, origin_price=3.5)
    assert [p.station_id for p in plan.stops] == [2]
    assert_invariants(plan, 600.0, stations)


def test_stations_beyond_destination_are_ignored():
    stations = [Candidate(1, 350.0, 3.0), Candidate(2, 420.0, 1.0)]
    plan = plan_fuel_stops(400.0, stations, origin_price=3.5)
    assert [p.station_id for p in plan.stops] == [1]


def test_invalid_inputs():
    with pytest.raises(ValueError):
        plan_fuel_stops(-1.0, [], origin_price=3.0)
    with pytest.raises(ValueError):
        plan_fuel_stops(10.0, [], origin_price=0.0)


# --- brute-force cross-check --------------------------------------------------


def brute_force_min_cost(
    total_miles: float, stations: list[Candidate], origin_price: float, cap: float, mpg: float
):
    """Exact DP over (node, fuel level) with fuel discretised to 0.5 gal.

    Positions are multiples of 5 miles so every leg needs a whole number of
    half-gallons and the discretisation loses nothing.
    """
    unit = 0.5
    nodes = [
        (0.0, origin_price),
        *[(s.miles, s.price) for s in sorted(stations, key=lambda s: s.miles)],
        (total_miles, 0.0),
    ]
    n = len(nodes)
    levels = round(cap / unit) + 1
    inf = float("inf")
    best = [[inf] * levels for _ in range(n)]
    best[0][0] = 0.0
    for i in range(n - 1):
        pos, price = nodes[i]
        for f in range(levels):
            if best[i][f] == inf:
                continue
            for buy in range(levels - f):
                cost = best[i][f] + buy * unit * price
                after = f + buy
                for j in range(i + 1, n):
                    need = round((nodes[j][0] - pos) / mpg / unit)
                    if need > after:
                        continue
                    if cost < best[j][after - need]:
                        best[j][after - need] = cost
    return min(best[n - 1])


@pytest.mark.parametrize("seed", range(40))
def test_greedy_matches_brute_force_on_random_instances(seed):
    rng = random.Random(seed)
    max_range, mpg = 100.0, 10.0  # capacity 10 gal -> 21 fuel levels; keeps the DP small
    cap = max_range / mpg
    n_stations = rng.randint(0, 8)
    total = rng.choice([50.0, 100.0, 150.0, 200.0, 250.0, 300.0])
    stations = [
        Candidate(
            k + 1, rng.randrange(5, int(total), 5) if total > 5 else 5.0, round(rng.uniform(2.5, 5.0), 2)
        )
        for k in range(n_stations)
    ]
    origin_price = round(rng.uniform(2.5, 5.0), 2)

    try:
        plan = plan_fuel_stops(total, stations, origin_price, max_range, mpg)
    except NoFeasiblePlan:
        assert brute_force_min_cost(total, stations, origin_price, cap, mpg) == float("inf")
        return

    assert_invariants(plan, total, stations, mpg=mpg, cap=cap)
    expected = brute_force_min_cost(total, stations, origin_price, cap, mpg)
    assert plan.total_cost == pytest.approx(expected, abs=1e-6)


# --- origin price -------------------------------------------------------------


def test_origin_price_uses_five_cheapest_nearby():
    price, basis = estimate_origin_price([3.9, 3.1, 3.5, 3.2, 3.3, 3.4, 4.5], [9.0], [9.0])
    assert basis == "nearby"
    assert price == pytest.approx((3.1 + 3.2 + 3.3 + 3.4 + 3.5) / 5)


def test_origin_price_falls_back_to_state_average_when_too_few_nearby():
    price, basis = estimate_origin_price([3.0, 3.1], [3.5, 3.7], [9.0])
    assert (price, basis) == (pytest.approx(3.6), "state")


def test_origin_price_falls_back_to_global_median():
    price, basis = estimate_origin_price([], [], [2.7, 3.4, 6.4])
    assert (price, basis) == (3.4, "global")
    with pytest.raises(ValueError):
        estimate_origin_price([], [], [])
