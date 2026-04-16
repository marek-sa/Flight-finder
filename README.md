# Flight-finder

Find the cheapest **A → X → B** flights where **X is any intermediate city**
and you spend a flexible, configurable number of nights there. It's a stop-over
holiday planner: instead of searching "London → Tokyo" directly, it asks
"What is the cheapest way to fly from London to Tokyo if I first stop anywhere
for 3 to 6 nights?"

Prices are pulled from the [Amadeus Self-Service](https://developers.amadeus.com/)
API (free tier), stored in SQLite, and served through a small FastAPI web UI
plus a CLI refresher you can trigger on a schedule.

## How the search works

For each trip defined in `config.yaml`:

1. **Discover intermediate cities X.** Either use the trip's whitelist, or
   call Amadeus *Flight Inspiration Search* from A over the departure window.
2. **Price every leg.** For each X, fetch *Flight Offers Search* results for
   A → X on every date in the depart window, and for X → B on every date in
   `[depart_from + min_nights, depart_to + max_nights]`. Results are cached
   in SQLite for a configurable TTL so repeated refreshes reuse them.
3. **Pair legs.** Combine any (leg1, leg2) where
   `min_nights ≤ leg2.depart − leg1.arrive ≤ max_nights`, sum the prices,
   rank the combos, and store them.
4. **Record history.** One row per refresh captures the cheapest total so the
   web UI can chart the trend.

## Setup

```bash
git clone https://github.com/marek-sa/flight-finder.git
cd flight-finder
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env          # fill in your Amadeus credentials
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

Get free API credentials at <https://developers.amadeus.com/> → create a
Self-Service app → copy the API key and secret into `.env`.

## Usage

```bash
# Refresh prices for every trip and print a ranked table per trip.
python -m flightfinder.cli refresh

# Refresh one trip only.
python -m flightfinder.cli refresh --trip lon-to-tyo-citybreak

# Show the current cheapest combos stored for a trip (no API calls).
python -m flightfinder.cli list lon-to-tyo-citybreak

# Run the web UI on http://127.0.0.1:8000/
python -m flightfinder.cli web
```

The CLI highlights combos in green when their total is at or below the
`max_total_price` threshold for that trip.

## Schedule with systemd

```bash
mkdir -p ~/.config/systemd/user
cp systemd/flightfinder.service ~/.config/systemd/user/
cp systemd/flightfinder.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now flightfinder.timer
journalctl --user -u flightfinder.service -f
```

The unit assumes the repo lives at `~/Flight-finder` and a `.venv` inside it.
Edit the paths in `flightfinder.service` if you put things elsewhere.

## Configuration reference

See `config.example.yaml`. Key fields per trip:

| Field | Meaning |
|---|---|
| `origin` / `destination` | IATA city or airport codes (e.g. `LON`, `TYO`). |
| `depart_date_from` / `depart_date_to` | Window for the first leg. |
| `layover_nights_min` / `layover_nights_max` | Allowed nights in the intermediate city. |
| `max_total_price` | Highlight / "alert" threshold for the cheapest combo. |
| `candidate_intermediate_cities` | Optional whitelist. If empty, Flight Inspiration discovers them. |
| `adults`, `cabin`, `currency` | Pax + cabin class + pricing currency. |

## Development

```bash
pytest -q
```

Unit tests cover config validation, storage round-trips, the leg-pairing
algorithm, and the Amadeus HTTP client (token cache + 429 backoff) via `respx`.

## Layout

```
flightfinder/
  amadeus.py      # async HTTP client + OAuth2 token cache
  search.py       # pair_legs() + search_trip() orchestration
  storage.py      # SQLite schema: trips, legs_cache, combos, price_history
  config.py       # Pydantic config models + YAML loader
  cli.py          # `refresh` / `list` / `web` subcommands
  web.py          # FastAPI app (index + /trip/{name})
  templates/      # Jinja2 views
  static/         # style.css
systemd/          # example user timer + service
tests/            # pytest suite
```
