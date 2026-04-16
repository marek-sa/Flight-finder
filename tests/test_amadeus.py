"""HTTP-level tests for :class:`AmadeusClient` using respx."""
from __future__ import annotations

import httpx
import pytest
import respx

from flightfinder.amadeus import AmadeusClient


def _mock_token(mock, expires_in=1799):
    mock.post("https://test.api.amadeus.com/v1/security/oauth2/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "tkn-abc", "expires_in": expires_in}
        )
    )


async def test_token_cached_between_calls():
    with respx.mock(assert_all_called=False) as mock:
        token_route = mock.post(
            "https://test.api.amadeus.com/v1/security/oauth2/token"
        ).mock(
            return_value=httpx.Response(200, json={"access_token": "tkn", "expires_in": 1799})
        )
        mock.get("https://test.api.amadeus.com/v1/shopping/flight-destinations").mock(
            return_value=httpx.Response(200, json={"data": [{"destination": "LIS"}]})
        )
        async with AmadeusClient(client_id="id", client_secret="secret") as client:
            await client.flight_destinations("LON")
            await client.flight_destinations("LON")
        assert token_route.call_count == 1


async def test_429_triggers_retry_then_succeeds():
    with respx.mock(assert_all_called=False) as mock:
        _mock_token(mock)
        route = mock.get("https://test.api.amadeus.com/v2/shopping/flight-offers")
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "0"}, json={"errors": [{"detail": "slow down"}]}),
            httpx.Response(200, json={"data": [{"id": "1"}]}),
        ]
        async with AmadeusClient(client_id="id", client_secret="secret") as client:
            offers = await client.flight_offers("LON", "TYO", "2026-06-01")
        assert offers == [{"id": "1"}]
        assert route.call_count == 2


async def test_auth_headers_and_params_forwarded():
    with respx.mock(assert_all_called=False) as mock:
        _mock_token(mock)
        offers_route = mock.get(
            "https://test.api.amadeus.com/v2/shopping/flight-offers"
        ).mock(return_value=httpx.Response(200, json={"data": []}))
        async with AmadeusClient(client_id="id", client_secret="secret") as client:
            await client.flight_offers(
                "LON", "TYO", "2026-06-01", adults=2, cabin="BUSINESS", currency="USD"
            )
        req = offers_route.calls[0].request
        assert req.headers["Authorization"] == "Bearer tkn-abc"
        assert req.url.params["originLocationCode"] == "LON"
        assert req.url.params["destinationLocationCode"] == "TYO"
        assert req.url.params["departureDate"] == "2026-06-01"
        assert req.url.params["adults"] == "2"
        assert req.url.params["travelClass"] == "BUSINESS"
        assert req.url.params["currencyCode"] == "USD"


async def test_non_200_non_retryable_raises():
    with respx.mock(assert_all_called=False) as mock:
        _mock_token(mock)
        mock.get("https://test.api.amadeus.com/v2/shopping/flight-offers").mock(
            return_value=httpx.Response(400, text="bad request")
        )
        async with AmadeusClient(client_id="id", client_secret="secret") as client:
            with pytest.raises(Exception):
                await client.flight_offers("LON", "TYO", "2026-06-01")
