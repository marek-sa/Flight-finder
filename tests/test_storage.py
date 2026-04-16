"""Storage round-trip tests against an in-memory SQLite database."""
from __future__ import annotations

from flightfinder.storage import Storage


def test_leg_cache_roundtrip_and_ttl(tmp_path):
    s = Storage(tmp_path / "db.sqlite")
    s.put_cached_offers("LON", "LIS", "2026-06-01", [{"x": 1}])
    # Fresh read within TTL returns the payload.
    assert s.get_cached_offers("LON", "LIS", "2026-06-01", ttl_seconds=3600) == [{"x": 1}]
    # TTL=0 means "always refetch" — payload should be treated as stale for
    # any positive elapsed time, but since we just wrote it, use negative
    # equivalent: any very small TTL still returns (we pass 0 which disables check).
    # Use a negative-ish behavior by passing 1 (1s): still fresh. Separate case:
    # simulate stale by monkey-patching fetched_at.
    s._conn.execute(
        "UPDATE legs_cache SET fetched_at = 0 WHERE origin='LON'"
    )
    s._conn.commit()
    assert s.get_cached_offers("LON", "LIS", "2026-06-01", ttl_seconds=3600) is None


def test_combos_replace_and_list(tmp_path):
    s = Storage(tmp_path / "db.sqlite")
    s.upsert_trip("t1", '{"origin":"LON"}')
    with s.replace_combos("t1"):
        s.insert_combo("t1", _combo_dict(total=500))
        s.insert_combo("t1", _combo_dict(total=400))
    rows = s.list_combos("t1")
    assert [r.total_price for r in rows] == [400.0, 500.0]
    assert s.cheapest_combo("t1").total_price == 400.0

    # Replacing wipes previous rows.
    with s.replace_combos("t1"):
        s.insert_combo("t1", _combo_dict(total=999))
    rows = s.list_combos("t1")
    assert [r.total_price for r in rows] == [999.0]


def test_price_history_append(tmp_path):
    s = Storage(tmp_path / "db.sqlite")
    s.upsert_trip("t1", "{}")
    s.record_price_point("t1", 500.0, None)
    s.record_price_point("t1", 450.0, None)
    hist = s.price_history("t1")
    assert [p["cheapest_total_price"] for p in hist] == [500.0, 450.0]


def _combo_dict(total=500.0):
    return {
        "intermediate": "LIS",
        "leg1_depart": "2026-06-01T08:00",
        "leg1_arrive": "2026-06-01T11:00",
        "leg2_depart": "2026-06-04T09:00",
        "leg2_arrive": "2026-06-05T06:00",
        "layover_nights": 3,
        "leg1_price": 100.0,
        "leg2_price": total - 100.0,
        "total_price": total,
        "currency": "EUR",
        "leg1_carrier": "BA",
        "leg2_carrier": "TP",
    }
