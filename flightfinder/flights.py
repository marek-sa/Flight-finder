"""Wrapper around `fast-flights` (Google Flights scraper).

The rest of the app only needs a single function: fetch the cheap one-way
offers for a route and a specific departure date, as a list of
:class:`~flightfinder.models.Leg`. Calls are run in a thread (fast-flights is
synchronous) and gated by a module-level semaphore to avoid being rate-limited
by Google.

Quirks of fast-flights output we defend against:

* ``Flight.departure`` / ``Flight.arrival`` are locale-formatted strings like
  ``"8:20 AM on Tue, Jun 25"``. We parse out the ``HH:MM`` portion and combine
  with the known query date. Overnight flights are handled via
  ``arrival_time_ahead`` (e.g. ``"+1"``), and defensively by comparing times.
* ``Flight.price`` is a locale-formatted string like ``"$1,234"``. We map the
  leading currency symbol to an ISO code; anything unknown is stored as-is.
* Some rows return ``"Price unavailable"``; those are skipped.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

from fast_flights import FlightData, Passengers, get_flights

from .models import Leg

log = logging.getLogger(__name__)


_CURRENCY_SYMBOLS: list[tuple[str, str]] = [
    # Order matters: multi-char symbols first.
    ("US$", "USD"),
    ("CA$", "CAD"),
    ("A$", "AUD"),
    ("NZ$", "NZD"),
    ("HK$", "HKD"),
    ("S$", "SGD"),
    ("R$", "BRL"),
    ("zł", "PLN"),
    ("Kč", "CZK"),
    ("Ft", "HUF"),
    ("kr", "SEK"),
    ("€", "EUR"),
    ("£", "GBP"),
    ("¥", "JPY"),
    ("₹", "INR"),
    ("₽", "RUB"),
    ("$", "USD"),
]

_NUMBER_RE = re.compile(r"[\d][\d,]*(?:\.\d+)?")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])?")
_AHEAD_RE = re.compile(r"([+-]?\d+)")


def _parse_price(raw: str) -> tuple[float, str] | None:
    if not raw:
        return None
    s = raw.strip()
    if "unavailable" in s.lower():
        return None
    currency = "USD"
    for sym, code in _CURRENCY_SYMBOLS:
        if sym in s:
            currency = code
            break
    m = _NUMBER_RE.search(s.replace("\xa0", " "))
    if not m:
        return None
    try:
        value = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return value, currency


def _parse_time(raw: str) -> dtime | None:
    if not raw:
        return None
    m = _TIME_RE.search(raw)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    meridiem = (m.group(3) or "").lower()
    if meridiem == "pm" and h != 12:
        h += 12
    elif meridiem == "am" and h == 12:
        h = 0
    if not (0 <= h < 24 and 0 <= mi < 60):
        return None
    return dtime(h, mi)


def _ahead_days(raw: str) -> int:
    if not raw:
        return 0
    m = _AHEAD_RE.search(raw)
    return int(m.group(1)) if m else 0


def _flight_to_leg(
    flight: Any,
    *,
    origin: str,
    destination: str,
    query_date: date,
) -> Leg | None:
    price = _parse_price(getattr(flight, "price", ""))
    if price is None:
        return None
    dep_t = _parse_time(getattr(flight, "departure", ""))
    arr_t = _parse_time(getattr(flight, "arrival", ""))
    if dep_t is None or arr_t is None:
        return None
    depart_dt = datetime.combine(query_date, dep_t)
    arrive_dt = datetime.combine(query_date, arr_t)
    ahead = _ahead_days(getattr(flight, "arrival_time_ahead", ""))
    if ahead > 0:
        arrive_dt += timedelta(days=ahead)
    elif arrive_dt <= depart_dt:
        # Overnight flight where fast-flights did not expose +N.
        arrive_dt += timedelta(days=1)
    stops = getattr(flight, "stops", None)
    try:
        stops_int = int(stops) if stops is not None else None
    except (TypeError, ValueError):
        stops_int = None
    return Leg(
        origin=origin,
        destination=destination,
        depart=depart_dt,
        arrive=arrive_dt,
        price=price[0],
        currency=price[1],
        carrier=(getattr(flight, "name", None) or None),
        stops=stops_int,
    )


def _default_concurrency() -> int:
    try:
        return max(1, int(os.environ.get("FLIGHTFINDER_CONCURRENCY", "3")))
    except ValueError:
        return 3


_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_default_concurrency())
    return _semaphore


def _sync_fetch(
    origin: str,
    destination: str,
    depart_date: date,
    *,
    adults: int,
    seat: str,
    max_stops: int | None,
    fetch_mode: str,
) -> list[Any]:
    result = get_flights(
        flight_data=[
            FlightData(
                date=depart_date.isoformat(),
                from_airport=origin,
                to_airport=destination,
                max_stops=max_stops,
            )
        ],
        trip="one-way",
        seat=seat,
        passengers=Passengers(adults=adults),
        fetch_mode=fetch_mode,
    )
    return list(result.flights or [])


async def fetch_one_way_offers(
    origin: str,
    destination: str,
    depart_date: date,
    *,
    adults: int = 1,
    cabin: str = "ECONOMY",
    max_stops: int | None = None,
    fetch_mode: str | None = None,
) -> list[Leg]:
    """Return parsed :class:`Leg` objects for a one-way route on a given date.

    Transient errors from fast-flights (network hiccups, Google blocking a
    single query) are logged and swallowed — the caller gets an empty list
    and continues with other dates/intermediates.
    """
    seat = cabin.lower().replace("_", "-")
    fetch_mode = fetch_mode or os.environ.get("FLIGHTFINDER_FETCH_MODE", "common")
    async with _get_semaphore():
        try:
            raw = await asyncio.to_thread(
                _sync_fetch,
                origin,
                destination,
                depart_date,
                adults=adults,
                seat=seat,
                max_stops=max_stops,
                fetch_mode=fetch_mode,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("fast-flights %s->%s %s failed: %s", origin, destination, depart_date, exc)
            return []
    legs: list[Leg] = []
    for flight in raw:
        leg = _flight_to_leg(flight, origin=origin, destination=destination, query_date=depart_date)
        if leg is not None:
            legs.append(leg)
    return legs
