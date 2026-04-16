"""Plain-dataclass DTOs shared by the provider layer and search algorithm."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Leg:
    origin: str
    destination: str
    depart: datetime
    arrive: datetime
    price: float
    currency: str
    carrier: str | None = None
    stops: int | None = None


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
