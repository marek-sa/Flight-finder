"""Config loader tests."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from flightfinder.config import load_config


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_load_minimal(tmp_path):
    cfg = load_config(_write(tmp_path, """
        trips:
          - name: t1
            origin: lon
            destination: tyo
            depart_date_from: 2026-06-01
            depart_date_to:   2026-06-10
            layover_nights_min: 2
            layover_nights_max: 5
    """))
    assert cfg.trips[0].origin == "LON"
    assert cfg.trips[0].destination == "TYO"
    assert cfg.trips[0].currency == "EUR"  # default


def test_invalid_layover_range_rejected(tmp_path):
    with pytest.raises(Exception):
        load_config(_write(tmp_path, """
            trips:
              - name: bad
                origin: LON
                destination: TYO
                depart_date_from: 2026-06-01
                depart_date_to:   2026-06-10
                layover_nights_min: 5
                layover_nights_max: 2
        """))


def test_duplicate_trip_names_rejected(tmp_path):
    with pytest.raises(Exception):
        load_config(_write(tmp_path, """
            trips:
              - name: dup
                origin: LON
                destination: TYO
                depart_date_from: 2026-06-01
                depart_date_to:   2026-06-10
                layover_nights_min: 1
                layover_nights_max: 3
              - name: dup
                origin: BER
                destination: NYC
                depart_date_from: 2026-06-01
                depart_date_to:   2026-06-10
                layover_nights_min: 1
                layover_nights_max: 3
        """))
