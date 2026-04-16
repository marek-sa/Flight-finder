"""Tests for the fast-flights wrapper (offline, monkeypatched get_flights)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import pytest

from flightfinder import flights as flights_mod


@dataclass
class _FakeFlight:
    is_best: bool
    name: str
    departure: str
    arrival: str
    arrival_time_ahead: str
    duration: str
    stops: int
    delay: str | None
    price: str


@dataclass
class _FakeResult:
    current_price: str
    flights: list[_FakeFlight]


def _install_fake(monkeypatch, flights):
    def fake_get_flights(*, flight_data, **_):
        return _FakeResult(current_price="low", flights=flights)
    monkeypatch.setattr(flights_mod, "get_flights", fake_get_flights)


async def test_parses_basic_offer(monkeypatch):
    _install_fake(monkeypatch, [
        _FakeFlight(True, "Ryanair", "8:20 AM on Wed, Jun 25",
                    "11:40 AM on Wed, Jun 25", "", "3 hr 20 min",
                    0, None, "€123"),
    ])
    legs = await flights_mod.fetch_one_way_offers("TFS", "LIS", date(2026, 6, 25))
    assert len(legs) == 1
    leg = legs[0]
    assert leg.origin == "TFS"
    assert leg.destination == "LIS"
    assert leg.depart == datetime(2026, 6, 25, 8, 20)
    assert leg.arrive == datetime(2026, 6, 25, 11, 40)
    assert leg.price == 123.0
    assert leg.currency == "EUR"
    assert leg.carrier == "Ryanair"
    assert leg.stops == 0


async def test_handles_overnight_via_arrival_time_ahead(monkeypatch):
    _install_fake(monkeypatch, [
        _FakeFlight(False, "Emirates", "10:30 PM on Wed, Jun 25",
                    "6:15 AM on Fri, Jun 27", "+2",
                    "19 hr 45 min", 1, None, "$1,234.50"),
    ])
    legs = await flights_mod.fetch_one_way_offers("DXB", "KRK", date(2026, 6, 25))
    assert len(legs) == 1
    assert legs[0].depart == datetime(2026, 6, 25, 22, 30)
    assert legs[0].arrive == datetime(2026, 6, 27, 6, 15)
    assert legs[0].price == 1234.50
    assert legs[0].currency == "USD"


async def test_handles_overnight_via_time_inference(monkeypatch):
    # No +N suffix, but arrival time-of-day is before departure -> overnight.
    _install_fake(monkeypatch, [
        _FakeFlight(False, "LOT", "11:00 PM", "02:30 AM", "",
                    "3 hr 30 min", 0, None, "450 zł"),
    ])
    legs = await flights_mod.fetch_one_way_offers("WAW", "KRK", date(2026, 6, 25))
    assert legs[0].depart == datetime(2026, 6, 25, 23, 0)
    assert legs[0].arrive == datetime(2026, 6, 26, 2, 30)
    assert legs[0].currency == "PLN"


async def test_skips_unparseable_rows(monkeypatch):
    _install_fake(monkeypatch, [
        _FakeFlight(False, "??", "??", "??", "", "", 0, None, "Price unavailable"),
        _FakeFlight(False, "LOT", "8:00 AM", "10:00 AM", "", "2h", 0, None, "£99"),
    ])
    legs = await flights_mod.fetch_one_way_offers("LON", "KRK", date(2026, 6, 25))
    assert len(legs) == 1
    assert legs[0].carrier == "LOT"
    assert legs[0].currency == "GBP"


async def test_provider_error_returns_empty(monkeypatch):
    def boom(**_):
        raise RuntimeError("blocked by google")
    monkeypatch.setattr(flights_mod, "get_flights", boom)
    legs = await flights_mod.fetch_one_way_offers("LON", "KRK", date(2026, 6, 25))
    assert legs == []


def test_parse_price_known_symbols():
    assert flights_mod._parse_price("€250") == (250.0, "EUR")
    assert flights_mod._parse_price("£1,234") == (1234.0, "GBP")
    assert flights_mod._parse_price("US$99.99") == (99.99, "USD")
    assert flights_mod._parse_price("450 zł") == (450.0, "PLN")
    assert flights_mod._parse_price("Price unavailable") is None
    assert flights_mod._parse_price("") is None
