"""Thin async client for the Amadeus Self-Service APIs.

Only the two endpoints this project needs are implemented:

* Flight Inspiration Search  (cheapest destinations reachable from an origin)
* Flight Offers Search       (concrete priced itineraries for a route/date)

The client caches its OAuth2 token in-memory and applies a simple exponential
backoff when the API returns HTTP 429.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

log = logging.getLogger(__name__)

_TEST_BASE = "https://test.api.amadeus.com"
_PROD_BASE = "https://api.amadeus.com"


@dataclass
class _Token:
    value: str
    expires_at: float  # epoch seconds


class AmadeusError(Exception):
    pass


class AmadeusClient:
    """Minimal async Amadeus client.

    Credentials are taken from the ``AMADEUS_CLIENT_ID`` /
    ``AMADEUS_CLIENT_SECRET`` env vars unless passed explicitly. The
    ``AMADEUS_ENV`` env var picks between ``test`` (default) and ``production``.
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        base_url: str | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        max_retries: int = 4,
    ) -> None:
        self._client_id = client_id or os.environ.get("AMADEUS_CLIENT_ID", "")
        self._client_secret = client_secret or os.environ.get("AMADEUS_CLIENT_SECRET", "")
        env = os.environ.get("AMADEUS_ENV", "test").lower()
        self._base_url = base_url or (_PROD_BASE if env == "production" else _TEST_BASE)
        self._http = http or httpx.AsyncClient(base_url=self._base_url, timeout=30.0)
        self._owns_http = http is None
        self._token: _Token | None = None
        self._token_lock = asyncio.Lock()
        self._max_retries = max_retries

    async def __aenter__(self) -> "AmadeusClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------------ auth
    async def _get_token(self) -> str:
        async with self._token_lock:
            now = time.time()
            if self._token and self._token.expires_at - 30 > now:
                return self._token.value
            if not self._client_id or not self._client_secret:
                raise AmadeusError(
                    "AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET must be set"
                )
            resp = await self._http.post(
                "/v1/security/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                raise AmadeusError(f"token request failed: {resp.status_code} {resp.text}")
            body = resp.json()
            self._token = _Token(
                value=body["access_token"],
                expires_at=now + float(body.get("expires_in", 1799)),
            )
            return self._token.value

    # ---------------------------------------------------------------- request
    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        # Strip None values — Amadeus rejects empty params.
        params = {k: v for k, v in params.items() if v is not None}
        backoff = 1.0
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            token = await self._get_token()
            resp = await self._http.get(
                path,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                last_exc = AmadeusError(
                    f"{path} -> {resp.status_code}: {resp.text[:200]}"
                )
                if attempt == self._max_retries:
                    break
                retry_after = float(resp.headers.get("Retry-After", backoff))
                log.warning(
                    "amadeus %s returned %s; retrying in %.1fs (attempt %d)",
                    path,
                    resp.status_code,
                    retry_after,
                    attempt + 1,
                )
                await asyncio.sleep(retry_after)
                backoff *= 2
                continue
            if resp.status_code == 401 and attempt < self._max_retries:
                # Token may have expired despite our cache — force refresh.
                self._token = None
                continue
            if resp.status_code != 200:
                raise AmadeusError(
                    f"{path} -> {resp.status_code}: {resp.text[:500]}"
                )
            return resp.json()
        assert last_exc is not None
        raise last_exc

    # ----------------------------------------------------------- endpoints
    async def flight_destinations(
        self,
        origin: str,
        *,
        departure_date: str | None = None,
        one_way: bool = True,
        max_price: float | None = None,
    ) -> list[dict[str, Any]]:
        """Flight Inspiration Search: cheapest destinations from *origin*.

        ``departure_date`` may be a single ISO date (``2026-06-01``) or a range
        (``2026-06-01,2026-06-15``).
        """
        params: dict[str, Any] = {
            "origin": origin,
            "oneWay": "true" if one_way else "false",
        }
        if departure_date:
            params["departureDate"] = departure_date
        if max_price is not None:
            params["maxPrice"] = int(max_price)
        body = await self._get("/v1/shopping/flight-destinations", params)
        return body.get("data", [])

    async def flight_offers(
        self,
        origin: str,
        destination: str,
        departure_date: date | str,
        *,
        adults: int = 1,
        cabin: str = "ECONOMY",
        currency: str = "EUR",
        max_results: int = 5,
        non_stop: bool = False,
    ) -> list[dict[str, Any]]:
        """Flight Offers Search: concrete priced one-way offers."""
        if isinstance(departure_date, date):
            departure_date = departure_date.isoformat()
        params: dict[str, Any] = {
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": departure_date,
            "adults": adults,
            "travelClass": cabin,
            "currencyCode": currency,
            "max": max_results,
            "nonStop": "true" if non_stop else "false",
        }
        body = await self._get("/v2/shopping/flight-offers", params)
        return body.get("data", [])
