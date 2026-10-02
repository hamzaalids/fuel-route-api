"""Cost-optimal fuel stops for a fixed-capacity tank ("gas station problem").

Pure Python, no Django/numpy imports. Inputs are plain numbers and small
dataclasses so this is trivially unit-testable.

Model
-----
Nodes are ``[origin] + stations (sorted by miles) + [destination]``. The tank
starts empty at the origin, which acts as a fill-up point priced at
``origin_price`` (estimated from nearby stations). The destination has price 0.
Consequently ``sum(gallons bought) == total_miles / mpg`` and the total cost is
the real cost of every gallon burned.

Algorithm (greedy look-ahead, provably optimal for this problem)
----------------------------------------------------------------
At node *i* with ``fuel`` in the tank:

* if some node in range is **cheaper** than *i*: buy just enough to reach the
  first such node, then drive there;
* otherwise **fill up** and drive to the cheapest node in range (ties → the
  farthest, i.e. fewer stops).

Because fuel bought at *i* is only ever bought when nothing cheaper is
reachable (fill-up) or in the exact amount needed to reach a cheaper price
(partial), no gallon could have been bought cheaper elsewhere.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import fmean, median

EPS = 1e-9


class NoFeasiblePlan(Exception):
    """Two consecutive reachable fuel points are farther apart than the range."""

    def __init__(self, message: str, gap_start_miles: float, gap_end_miles: float) -> None:
        super().__init__(message)
        self.gap_start_miles = gap_start_miles
        self.gap_end_miles = gap_end_miles


@dataclass(frozen=True, slots=True)
class Candidate:
    """A station projected onto the route."""

    station_id: int
    miles: float  # distance from route start
    price: float  # $/gallon


@dataclass(frozen=True, slots=True)
class Purchase:
    station_id: int | None  # None → origin
    miles: float
    price: float
    gallons: float
    tank_on_arrival: float

    @property
    def cost(self) -> float:
        return self.gallons * self.price


@dataclass(frozen=True, slots=True)
class FuelPlan:
    purchases: list[Purchase]  # in route order, only non-zero purchases
    total_gallons: float
    total_cost: float

    @property
    def origin_fill(self) -> Purchase | None:
        return next((p for p in self.purchases if p.station_id is None), None)

    @property
    def stops(self) -> list[Purchase]:
        return [p for p in self.purchases if p.station_id is not None]


@dataclass(frozen=True, slots=True)
class _Node:
    station_id: int | None
    miles: float
    price: float


def plan_fuel_stops(
    total_miles: float,
    stations: Sequence[Candidate],
    origin_price: float,
    max_range_miles: float = 500.0,
    mpg: float = 10.0,
) -> FuelPlan:
    """Return the minimum-cost purchase plan. Raises ``NoFeasiblePlan`` if the route can't be covered."""
    if total_miles < 0 or max_range_miles <= 0 or mpg <= 0 or origin_price <= 0:
        raise ValueError("total_miles must be >= 0; max_range_miles, mpg and origin_price must be > 0")

    capacity = max_range_miles / mpg
    nodes = [_Node(None, 0.0, float(origin_price))]
    nodes += [
        _Node(s.station_id, float(s.miles), float(s.price))
        for s in sorted(stations, key=lambda s: s.miles)
        if 0.0 <= s.miles <= total_miles + EPS
    ]
    nodes.append(_Node(None, float(total_miles), 0.0))
    last = len(nodes) - 1

    purchases: list[Purchase] = []
    fuel = 0.0
    i = 0
    while i != last:
        here = nodes[i]
        reachable_end = i
        while (
            reachable_end + 1 <= last and nodes[reachable_end + 1].miles - here.miles <= max_range_miles + EPS
        ):
            reachable_end += 1
        if reachable_end == i:
            nxt = nodes[i + 1]
            raise NoFeasiblePlan(
                f"No fuel station within {max_range_miles:g} miles after mile {here.miles:.1f}; "
                f"the next fuel point is at mile {nxt.miles:.1f} ({nxt.miles - here.miles:.1f} miles away).",
                gap_start_miles=here.miles,
                gap_end_miles=nxt.miles,
            )

        cheaper = next((j for j in range(i + 1, reachable_end + 1) if nodes[j].price < here.price), None)
        if cheaper is not None:
            target = cheaper
            need = (nodes[target].miles - here.miles) / mpg
            buy = max(0.0, need - fuel)
        else:
            # Nothing cheaper in range: fill up, drive to the cheapest reachable (farthest on ties).
            target = min(range(i + 1, reachable_end + 1), key=lambda j: (nodes[j].price, -nodes[j].miles))
            buy = capacity - fuel

        buy = min(buy, capacity - fuel)  # never overfill (guards float drift)
        if buy > EPS:
            purchases.append(Purchase(here.station_id, here.miles, here.price, buy, fuel))
            fuel += buy
        fuel -= (nodes[target].miles - here.miles) / mpg
        if fuel < -1e-6:
            raise AssertionError("fuel went negative; optimizer invariant broken")
        fuel = max(fuel, 0.0)  # absorb float noise only
        i = target

    total_gallons = sum(p.gallons for p in purchases)
    return FuelPlan(
        purchases=purchases, total_gallons=total_gallons, total_cost=sum(p.cost for p in purchases)
    )


def estimate_origin_price(
    nearby_prices: Sequence[float],
    state_prices: Sequence[float],
    global_prices: Sequence[float],
    cheapest_k: int = 5,
    min_nearby: int = 3,
) -> tuple[float, str]:
    """Price assumed at the start point (which is not itself a station in the dataset).

    * ≥ ``min_nearby`` stations within the search radius → mean of the ``cheapest_k`` of them;
    * otherwise the start state's average;
    * otherwise the global median.

    Returns ``(price, basis)`` where basis is ``"nearby"``, ``"state"`` or ``"global"``.
    """
    if len(nearby_prices) >= min_nearby:
        cheapest = sorted(nearby_prices)[:cheapest_k]
        return fmean(cheapest), "nearby"
    if state_prices:
        return fmean(state_prices), "state"
    if not global_prices:
        raise ValueError("global_prices must not be empty")
    return float(median(global_prices)), "global"
