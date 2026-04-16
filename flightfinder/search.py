"""Compose fast-flights queries into 'cheapest A -> X -> B with a break' trips.

The core pure function here is :func:`pair_legs`, which takes two lists of
priced one-way legs and returns every (leg1, leg2) combination that satisfies
the minimum/maximum layover constraint, ranked by total price. It has no I/O
so it is trivially unit-testable.

:func:`search_trip` is the glue: for every candidate intermediate city X, it
asks the provider (fast-flights) for leg A->X and leg X->B offers over the
trip's date windows (with SQLite-backed caching), then hands everything to
``pair_legs``.

Because ``fast-flights`` does not offer an "anywhere from origin" discovery
endpoint, we rely on the trip's ``candidate_intermediate_cities`` list. If
the user leaves it empty, we fall back to :data:`DEFAULT_HUBS`.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import date, timedelta
from typing import Any, Iterable

from .config import TripConfig
from .flights import fetch_one_way_offers
from .models import Combo, Leg
from .storage import Storage

log = logging.getLogger(__name__)


# Reasonable default list of intermediate-city candidates when the trip's
# whitelist is empty. Mostly major European/Middle-Eastern hubs with good
# onward connectivity. Users can override per-trip in config.
DEFAULT_HUBS: list[str] = [
    "LON", "PAR", "AMS", "FRA", "MAD", "BCN", "LIS", "ROM", "MIL",
    "IST", "DUB", "CPH", "ZRH", "VIE", "ATH", "WAW", "PRG", "DXB",
]


# -------------------------------------------------------------- core pairing
def pair_legs(
    leg1_options: Iterable[Leg],
    leg2_options: Iterable[Leg],
    *,
    min_nights: int,
    max_nights: int,
    top_k: int | None = None,
) -> list[Combo]:
    """Return combos where ``min_nights <= nights_in_X <= max_nights``, sorted cheapest first.

    "Nights in X" is measured as the calendar-day difference between
    ``leg1.arrive`` and ``leg2.depart``.
    """
    leg1s = list(leg1_options)
    leg2s = list(leg2_options)
    combos: list[Combo] = []
    for l1 in leg1s:
        for l2 in leg2s:
            if l1.destination != l2.origin:
                continue
            nights = (l2.depart.date() - l1.arrive.date()).days
            if nights < min_nights or nights > max_nights:
                continue
            if l2.depart <= l1.arrive:
                continue
            # Cross-currency totals would be meaningless; skip mismatches.
            if l1.currency != l2.currency:
                continue
            combos.append(
                Combo(
                    intermediate=l1.destination,
                    leg1=l1,
                    leg2=l2,
                    total_price=round(l1.price + l2.price, 2),
                    layover_nights=nights,
                )
            )
    combos.sort(key=lambda c: c.total_price)
    if top_k is not None:
        combos = combos[:top_k]
    return combos


# -------------------------------------------------------- date enumeration
def _daterange(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


# ---------------------------------------------------------- cached fetches
async def _fetch_offers_cached(
    storage: Storage,
    origin: str,
    destination: str,
    depart_date: date,
    *,
    adults: int,
    cabin: str,
    ttl_seconds: int,
) -> list[Leg]:
    key_date = depart_date.isoformat()
    cached = storage.get_cached_offers(origin, destination, key_date, ttl_seconds)
    if cached is not None:
        return [Leg(**_rehydrate(row)) for row in cached]

    legs = await fetch_one_way_offers(
        origin, destination, depart_date, adults=adults, cabin=cabin
    )
    storage.put_cached_offers(
        origin,
        destination,
        key_date,
        [_serialize_leg(leg) for leg in legs],
    )
    return legs


def _serialize_leg(leg: Leg) -> dict[str, Any]:
    d = asdict(leg)
    d["depart"] = leg.depart.isoformat()
    d["arrive"] = leg.arrive.isoformat()
    return d


def _rehydrate(row: dict[str, Any]) -> dict[str, Any]:
    from datetime import datetime
    return {
        "origin": row["origin"],
        "destination": row["destination"],
        "depart": datetime.fromisoformat(row["depart"]),
        "arrive": datetime.fromisoformat(row["arrive"]),
        "price": float(row["price"]),
        "currency": row["currency"],
        "carrier": row.get("carrier"),
        "stops": row.get("stops"),
    }


def _intermediates_for(trip: TripConfig, cap: int) -> list[str]:
    """Return the candidate intermediate city list, skipping the destination."""
    raw = trip.candidate_intermediate_cities or DEFAULT_HUBS
    out: list[str] = []
    for code in raw:
        if code == trip.origin or code == trip.destination:
            continue
        if code not in out:
            out.append(code)
        if len(out) >= cap:
            break
    return out


# -------------------------------------------------------------- public api
async def search_trip(
    storage: Storage,
    trip: TripConfig,
    *,
    candidate_cap: int,
    cache_ttl_seconds: int,
    top_k: int = 50,
) -> list[Combo]:
    """Run the full A->X->B search for one trip config."""
    intermediates = _intermediates_for(trip, candidate_cap)
    log.info("trip %s: %d candidate intermediates", trip.name, len(intermediates))
    if not intermediates:
        return []

    leg1_dates = _daterange(trip.depart_date_from, trip.depart_date_to)
    leg2_dates = _daterange(
        trip.depart_date_from + timedelta(days=trip.layover_nights_min),
        trip.depart_date_to + timedelta(days=trip.layover_nights_max),
    )

    async def gather_leg1(x: str) -> list[Leg]:
        results = await asyncio.gather(
            *(
                _fetch_offers_cached(
                    storage, trip.origin, x, d,
                    adults=trip.adults, cabin=trip.cabin,
                    ttl_seconds=cache_ttl_seconds,
                )
                for d in leg1_dates
            )
        )
        return [leg for batch in results for leg in batch]

    async def gather_leg2(x: str) -> list[Leg]:
        results = await asyncio.gather(
            *(
                _fetch_offers_cached(
                    storage, x, trip.destination, d,
                    adults=trip.adults, cabin=trip.cabin,
                    ttl_seconds=cache_ttl_seconds,
                )
                for d in leg2_dates
            )
        )
        return [leg for batch in results for leg in batch]

    all_combos: list[Combo] = []
    for x in intermediates:
        leg1s, leg2s = await asyncio.gather(gather_leg1(x), gather_leg2(x))
        if not leg1s or not leg2s:
            continue
        all_combos.extend(
            pair_legs(
                leg1s,
                leg2s,
                min_nights=trip.layover_nights_min,
                max_nights=trip.layover_nights_max,
            )
        )

    all_combos.sort(key=lambda c: c.total_price)
    return all_combos[:top_k]
