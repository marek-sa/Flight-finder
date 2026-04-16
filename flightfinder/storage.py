"""SQLite persistence layer.

One database holds three concerns:

* ``legs_cache`` — raw Amadeus Flight Offers responses keyed by
  ``(origin, destination, depart_date)`` so repeated refreshes within the TTL
  do not re-hit the API.
* ``combos`` — paired A->X + X->B itineraries discovered for each trip.
* ``price_history`` — one row per refresh with the cheapest total for a trip,
  so the web UI can draw a trend line.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    name        TEXT PRIMARY KEY,
    config_json TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS legs_cache (
    origin       TEXT NOT NULL,
    destination  TEXT NOT NULL,
    depart_date  TEXT NOT NULL,
    fetched_at   REAL NOT NULL,
    offers_json  TEXT NOT NULL,
    PRIMARY KEY (origin, destination, depart_date)
);

CREATE TABLE IF NOT EXISTS combos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_name        TEXT NOT NULL,
    intermediate     TEXT NOT NULL,
    leg1_depart      TEXT NOT NULL,
    leg1_arrive      TEXT NOT NULL,
    leg2_depart      TEXT NOT NULL,
    leg2_arrive      TEXT NOT NULL,
    layover_nights   INTEGER NOT NULL,
    leg1_price       REAL NOT NULL,
    leg2_price       REAL NOT NULL,
    total_price      REAL NOT NULL,
    currency         TEXT NOT NULL,
    leg1_carrier     TEXT,
    leg2_carrier     TEXT,
    found_at         REAL NOT NULL,
    FOREIGN KEY (trip_name) REFERENCES trips(name)
);

CREATE INDEX IF NOT EXISTS combos_trip_total
    ON combos (trip_name, total_price);

CREATE TABLE IF NOT EXISTS price_history (
    trip_name              TEXT NOT NULL,
    found_at               REAL NOT NULL,
    cheapest_total_price   REAL,
    cheapest_combo_id      INTEGER,
    PRIMARY KEY (trip_name, found_at),
    FOREIGN KEY (trip_name) REFERENCES trips(name)
);
"""


@dataclass
class ComboRow:
    id: int
    trip_name: str
    intermediate: str
    leg1_depart: str
    leg1_arrive: str
    leg2_depart: str
    leg2_arrive: str
    layover_nights: int
    leg1_price: float
    leg2_price: float
    total_price: float
    currency: str
    leg1_carrier: str | None
    leg2_carrier: str | None
    found_at: float


class Storage:
    def __init__(self, path: str | Path = "flightfinder.db") -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --------------------------------------------------------------- trips
    def upsert_trip(self, name: str, config_json: str) -> None:
        self._conn.execute(
            """
            INSERT INTO trips (name, config_json, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET config_json = excluded.config_json
            """,
            (name, config_json, time.time()),
        )
        self._conn.commit()

    def list_trips(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT name, config_json, created_at FROM trips ORDER BY name"
        ).fetchall()
        return [
            {
                "name": r["name"],
                "config": json.loads(r["config_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ---------------------------------------------------------- legs cache
    def get_cached_offers(
        self, origin: str, destination: str, depart_date: str, ttl_seconds: int
    ) -> list[dict[str, Any]] | None:
        row = self._conn.execute(
            "SELECT fetched_at, offers_json FROM legs_cache "
            "WHERE origin=? AND destination=? AND depart_date=?",
            (origin, destination, depart_date),
        ).fetchone()
        if row is None:
            return None
        if ttl_seconds > 0 and (time.time() - row["fetched_at"]) > ttl_seconds:
            return None
        return json.loads(row["offers_json"])

    def put_cached_offers(
        self,
        origin: str,
        destination: str,
        depart_date: str,
        offers: list[dict[str, Any]],
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO legs_cache (origin, destination, depart_date, fetched_at, offers_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(origin, destination, depart_date) DO UPDATE SET
                fetched_at = excluded.fetched_at,
                offers_json = excluded.offers_json
            """,
            (origin, destination, depart_date, time.time(), json.dumps(offers)),
        )
        self._conn.commit()

    # ------------------------------------------------------------- combos
    @contextmanager
    def replace_combos(self, trip_name: str) -> Iterator[sqlite3.Connection]:
        """Transactionally wipe and repopulate combos for one trip."""
        self._conn.execute("DELETE FROM combos WHERE trip_name = ?", (trip_name,))
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def insert_combo(self, trip_name: str, combo: dict[str, Any]) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO combos (
                trip_name, intermediate,
                leg1_depart, leg1_arrive, leg2_depart, leg2_arrive,
                layover_nights, leg1_price, leg2_price, total_price, currency,
                leg1_carrier, leg2_carrier, found_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trip_name,
                combo["intermediate"],
                combo["leg1_depart"],
                combo["leg1_arrive"],
                combo["leg2_depart"],
                combo["leg2_arrive"],
                combo["layover_nights"],
                combo["leg1_price"],
                combo["leg2_price"],
                combo["total_price"],
                combo["currency"],
                combo.get("leg1_carrier"),
                combo.get("leg2_carrier"),
                time.time(),
            ),
        )
        return int(cur.lastrowid)

    def list_combos(self, trip_name: str, limit: int = 50) -> list[ComboRow]:
        rows = self._conn.execute(
            "SELECT * FROM combos WHERE trip_name = ? "
            "ORDER BY total_price ASC LIMIT ?",
            (trip_name, limit),
        ).fetchall()
        return [ComboRow(**dict(r)) for r in rows]

    def cheapest_combo(self, trip_name: str) -> ComboRow | None:
        row = self._conn.execute(
            "SELECT * FROM combos WHERE trip_name = ? "
            "ORDER BY total_price ASC LIMIT 1",
            (trip_name,),
        ).fetchone()
        return ComboRow(**dict(row)) if row else None

    # ------------------------------------------------------ price history
    def record_price_point(
        self,
        trip_name: str,
        cheapest_total_price: float | None,
        cheapest_combo_id: int | None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO price_history (trip_name, found_at, cheapest_total_price, cheapest_combo_id)
            VALUES (?, ?, ?, ?)
            """,
            (trip_name, time.time(), cheapest_total_price, cheapest_combo_id),
        )
        self._conn.commit()

    def price_history(self, trip_name: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT found_at, cheapest_total_price FROM price_history "
            "WHERE trip_name = ? ORDER BY found_at ASC LIMIT ?",
            (trip_name, limit),
        ).fetchall()
        return [dict(r) for r in rows]
