"""Load and validate the YAML trip configuration."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class Defaults(BaseModel):
    currency: str = "EUR"
    candidate_intermediate_cities_max: int = Field(default=25, ge=1, le=200)
    cache_ttl_hours: int = Field(default=6, ge=0)


class TripConfig(BaseModel):
    name: str
    origin: str
    destination: str
    depart_date_from: date
    depart_date_to: date
    layover_nights_min: int = Field(ge=0)
    layover_nights_max: int = Field(ge=0)
    max_total_price: float | None = None
    candidate_intermediate_cities: list[str] = Field(default_factory=list)
    adults: int = Field(default=1, ge=1, le=9)
    cabin: str = "ECONOMY"
    currency: str | None = None

    @field_validator("origin", "destination", "candidate_intermediate_cities")
    @classmethod
    def _upper(cls, v):
        if isinstance(v, str):
            return v.upper()
        return [x.upper() for x in v]

    @field_validator("cabin")
    @classmethod
    def _cabin_upper(cls, v: str) -> str:
        allowed = {"ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"}
        v = v.upper()
        if v not in allowed:
            raise ValueError(f"cabin must be one of {sorted(allowed)}")
        return v

    @model_validator(mode="after")
    def _validate_ranges(self) -> "TripConfig":
        if self.depart_date_to < self.depart_date_from:
            raise ValueError("depart_date_to must be on or after depart_date_from")
        if self.layover_nights_max < self.layover_nights_min:
            raise ValueError("layover_nights_max must be >= layover_nights_min")
        if self.origin == self.destination:
            raise ValueError("origin and destination must differ")
        return self


class Config(BaseModel):
    defaults: Defaults = Field(default_factory=Defaults)
    trips: list[TripConfig]

    @model_validator(mode="after")
    def _unique_trip_names(self) -> "Config":
        names = [t.name for t in self.trips]
        if len(set(names)) != len(names):
            raise ValueError("trip names must be unique")
        for trip in self.trips:
            if trip.currency is None:
                trip.currency = self.defaults.currency
        return self


def load_config(path: str | Path) -> Config:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text())
    return Config.model_validate(raw)
