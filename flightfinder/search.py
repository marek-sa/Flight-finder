"""Compose Amadeus endpoints into 'cheapest A -> X -> B with a break' queries.

The core pure function here is :func:`pair_legs`, which takes two lists of
priced one-way legs and returns every (leg1, leg2) combination that satisfies
the minimum/maximum layover constraint, ranked by total price. It has no I/O
so it is trivially unit-testable.

:func:`search_trip` is the glue: it discovers candidate intermediate cities
(either from the user's whitelist or from Flight Inspiration Search), fetches
leg offers for each side of the trip (with SQLite-backed caching), and hands
everything to ``pair_legs``.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from .amadeus import AmadeusClient
from .config import TripConfig
from .storage import Storage

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Leg:
    origin: str
    destination: str
    depart: datetime
    arrive: datetime
    price: float
    currency: str
    carrier: str | None = None


@dataclass(frozen=True)
class Combo:
    intermediate: str
    leg1: Leg
    leg2: Leg
    total_price: float
    layover_nights: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "intermediate": self.intermediate,
            "leg1_depart": self.leg1.depart.isoformat(),
            "leg1_arrive": self.leg1.arrive.isoformat(),
            "leg2_depart": self.leg2.depart.isoformat(),
            "leg2_arrive": self.leg2.arrive.isoformat(),
            "layover_nights": self.layover_nights,
            "leg1_price": self.leg1.price,
            "leg2_price": self.leg2.price,
            "total_price": self.total_price,
            "currency": self.leg1.currency,
            "leg1_carrier": self.leg1.carrier,
            "leg2_carrier": self.leg2.carrier,
        }


# ---------------------------------------------------------------- parsing
def parse_offer(offer: dict[str, Any]) -> Leg | None:
    """Extract a :class:`Leg` from an Amadeus Flight Offers ``data`` entry.

    Amadeus responses are deeply nested; we take the first itinerary and look
    at its first and last segment to get origin/destination + times. Returns
    ``None`` if the offer is malformed.
    """
    try:
        itin = offer["itineraries"][0]
        segs = itin["segments"]
        first, last = segs[0], segs[-1]
        depart = datetime.fromisoformat(first["departure"]["at"])
        arrive = datetime.fromisoformat(last["arrival"]["at"])
        price = float(offer["price"]["grandTotal"])
        currency = offer["price"]["currency"]
        carrier = first.get("carrierCode")
        return Leg(
            origin=first["departure"]["iataCode"],
            destination=last["arrival"]["iataCode"],
            depart=depart,
            arrive=arrive,
            price=price,
            currency=currency,
            carrier=carrier,
        )
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        log.debug("skip malformed offer: %s", exc)
        return None


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
    client: AmadeusClient,
    storage: Storage,
    origin: str,
    destination: str,
    depart_date: date,
    *,
    adults: int,
    cabin: str,
    currency: str,
    ttl_seconds: int,
) -> list[Leg]:
    key_date = depart_date.isoformat()
    cached = storage.get_cached_offers(origin, destination, key_date, ttl_seconds)
    if cached is None:
        try:
            cached = await client.flight_offers(
                origin,
                destination,
                depart_date,
                adults=adults,
                cabin=cabin,
                currency=currency,
            )
        except Exception as exc:  # noqa: BLE001 — log and treat as no offers.
            log.warning("flight_offers %s->%s %s failed: %s", origin, destination, key_date, exc)
            return []
        storage.put_cached_offers(origin, destination, key_date, cached)
    legs: list[Leg] = []
    for offer in cached:
        leg = parse_offer(offer)
        if leg is not None:
            legs.append(leg)
    return legs


async def _discover_intermediates(
    client: AmadeusClient,
    trip: TripConfig,
    cap: int,
) -> list[str]:
    """Return candidate intermediate city codes.

    Uses the trip's whitelist when non-empty; otherwise calls Flight Inspiration
    Search for the origin over the date window.
    """
    if trip.candidate_intermediate_cities:
        return trip.candidate_intermediate_cities[:cap]
    date_range = f"{trip.depart_date_from.isoformat()},{trip.depart_date_to.isoformat()}"
    try:
        results = await client.flight_destinations(
            trip.origin,
            departure_date=date_range,
            one_way=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "flight_destinations for %s failed (%s); "
            "add candidate_intermediate_cities to the trip to proceed",
            trip.origin,
            exc,
        )
        return []
    seen: list[str] = []
    for row in results:
        dest = row.get("destination")
        if dest and dest != trip.destination and dest not in seen:
            seen.append(dest)
        if len(seen) >= cap:
            break
    return seen


# -------------------------------------------------------------- public api
async def search_trip(
    client: AmadeusClient,
    storage: Storage,
    trip: TripConfig,
    *,
    candidate_cap: int,
    cache_ttl_seconds: int,
    top_k: int = 50,
) -> list[Combo]:
    """Run the full A->X->B search for one trip config."""
    intermediates = await _discover_intermediates(client, trip, candidate_cap)
    log.info("trip %s: %d candidate intermediates", trip.name, len(intermediates))
    if not intermediates:
        return []

    currency = trip.currency or "EUR"
    leg1_dates = _daterange(trip.depart_date_from, trip.depart_date_to)
    # The second-leg date window is [from + min_nights, to + max_nights].
    leg2_dates = _daterange(
        trip.depart_date_from + timedelta(days=trip.layover_nights_min),
        trip.depart_date_to + timedelta(days=trip.layover_nights_max),
    )

    async def gather_leg1(x: str) -> list[Leg]:
        results = await asyncio.gather(
            *(
                _fetch_offers_cached(
                    client, storage, trip.origin, x, d,
                    adults=trip.adults, cabin=trip.cabin, currency=currency,
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
                    client, storage, x, trip.destination, d,
                    adults=trip.adults, cabin=trip.cabin, currency=currency,
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
