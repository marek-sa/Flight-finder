"""Unit tests for the pairing algorithm (no network)."""
from __future__ import annotations

from datetime import datetime

from flightfinder.models import Leg
from flightfinder.search import pair_legs


def _leg(origin, destination, depart, arrive, price, carrier="XX", currency="EUR"):
    return Leg(
        origin=origin,
        destination=destination,
        depart=datetime.fromisoformat(depart),
        arrive=datetime.fromisoformat(arrive),
        price=price,
        currency=currency,
        carrier=carrier,
    )


def test_pair_legs_respects_layover_range():
    leg1 = [_leg("LON", "LIS", "2026-06-01T08:00", "2026-06-01T11:00", 90)]
    leg2s = [
        _leg("LIS", "TYO", "2026-06-02T09:00", "2026-06-03T06:00", 500),  # 1 night
        _leg("LIS", "TYO", "2026-06-04T09:00", "2026-06-05T06:00", 480),  # 3 nights
        _leg("LIS", "TYO", "2026-06-08T09:00", "2026-06-09T06:00", 520),  # 7 nights
    ]
    out = pair_legs(leg1, leg2s, min_nights=3, max_nights=6)
    assert [c.layover_nights for c in out] == [3]
    assert out[0].total_price == 90 + 480


def test_pair_legs_skips_mismatched_intermediate():
    leg1 = [_leg("LON", "LIS", "2026-06-01T08:00", "2026-06-01T11:00", 90)]
    leg2 = [_leg("MAD", "TYO", "2026-06-04T09:00", "2026-06-05T06:00", 400)]
    assert pair_legs(leg1, leg2, min_nights=1, max_nights=10) == []


def test_pair_legs_sorts_and_top_k():
    leg1 = [
        _leg("LON", "LIS", "2026-06-01T08:00", "2026-06-01T11:00", 100),
        _leg("LON", "LIS", "2026-06-02T08:00", "2026-06-02T11:00", 80),
    ]
    leg2 = [
        _leg("LIS", "TYO", "2026-06-05T09:00", "2026-06-06T06:00", 500),
        _leg("LIS", "TYO", "2026-06-06T09:00", "2026-06-07T06:00", 450),
    ]
    out = pair_legs(leg1, leg2, min_nights=3, max_nights=6, top_k=2)
    assert len(out) == 2
    assert out[0].total_price <= out[1].total_price
    assert out[0].total_price == 80 + 450


def test_pair_legs_ignores_leg2_not_after_arrival():
    leg1 = [_leg("LON", "LIS", "2026-06-01T18:00", "2026-06-01T21:00", 90)]
    leg2 = [_leg("LIS", "TYO", "2026-06-01T19:00", "2026-06-02T10:00", 400)]
    assert pair_legs(leg1, leg2, min_nights=0, max_nights=3) == []


def test_pair_legs_skips_currency_mismatch():
    leg1 = [_leg("LON", "LIS", "2026-06-01T08:00", "2026-06-01T11:00", 100, currency="GBP")]
    leg2 = [_leg("LIS", "TYO", "2026-06-05T09:00", "2026-06-06T06:00", 400, currency="EUR")]
    # Different currencies would produce meaningless totals; combo is dropped.
    assert pair_legs(leg1, leg2, min_nights=3, max_nights=6) == []
