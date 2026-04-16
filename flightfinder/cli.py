"""Command-line interface.

Run it via ``python -m flightfinder.cli`` or the ``flightfinder`` script
entry point installed by ``pip install -e .``.

Subcommands:
    refresh   run the search for every (or one) trip and store results
    list      print the cheapest current combos for a trip
    web       launch the FastAPI UI

The ``refresh`` command is what cron/systemd should call.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .config import Config, TripConfig, load_config
from .models import Combo
from .search import search_trip
from .storage import ComboRow, Storage

log = logging.getLogger("flightfinder")


def _db_path() -> str:
    return os.environ.get("FLIGHTFINDER_DB", "flightfinder.db")


def _print_table(
    console: Console,
    trip: TripConfig,
    combos: list,
    threshold: float | None,
) -> None:
    title = f"{trip.name}  ({trip.origin} -> X -> {trip.destination})"
    table = Table(title=title, show_lines=False)
    table.add_column("X", style="cyan")
    table.add_column("Leg 1 depart", style="magenta")
    table.add_column("Leg 2 depart", style="magenta")
    table.add_column("Nights", justify="right")
    table.add_column("Leg 1", justify="right")
    table.add_column("Leg 2", justify="right")
    table.add_column("Total", justify="right", style="bold")
    table.add_column("Currency")
    for c in combos[:15]:
        row_style = (
            "green" if threshold is not None and c.total_price <= threshold else None
        )
        is_row = isinstance(c, ComboRow)
        table.add_row(
            c.intermediate,
            c.leg1_depart if is_row else _fmt_dt(c.leg1.depart),
            c.leg2_depart if is_row else _fmt_dt(c.leg2.depart),
            str(c.layover_nights),
            f"{(c.leg1_price if is_row else c.leg1.price):.2f}",
            f"{(c.leg2_price if is_row else c.leg2.price):.2f}",
            f"{c.total_price:.2f}",
            c.currency if is_row else c.leg1.currency,
            style=row_style,
        )
    console.print(table)


def _fmt_dt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if hasattr(value, "strftime") else str(value)


async def _refresh(cfg: Config, only_trip: str | None, console: Console) -> int:
    storage = Storage(_db_path())
    exit_code = 0
    for trip in cfg.trips:
        if only_trip and trip.name != only_trip:
            continue
        console.print(f"[bold]Refreshing[/bold] {trip.name} ...")
        try:
            combos = await search_trip(
                storage,
                trip,
                candidate_cap=cfg.defaults.candidate_intermediate_cities_max,
                cache_ttl_seconds=cfg.defaults.cache_ttl_hours * 3600,
            )
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]ERROR[/red] {trip.name}: {exc}")
            log.exception("refresh failed for %s", trip.name)
            exit_code = 1
            continue

        storage.upsert_trip(trip.name, trip.model_dump_json())
        with storage.replace_combos(trip.name):
            cheapest_id: int | None = None
            cheapest_price: float | None = None
            for c in combos:
                new_id = storage.insert_combo(trip.name, c.as_dict())
                if cheapest_price is None or c.total_price < cheapest_price:
                    cheapest_price = c.total_price
                    cheapest_id = new_id
        storage.record_price_point(trip.name, cheapest_price, cheapest_id)

        if not combos:
            console.print(f"[yellow]No combos found for {trip.name}.[/yellow]")
            continue

        _print_table(console, trip, combos, trip.max_total_price)
        if trip.max_total_price is not None and combos[0].total_price <= trip.max_total_price:
            console.print(
                f"[bold green]ALERT[/bold green] {trip.name}: cheapest "
                f"{combos[0].total_price:.2f} {combos[0].leg1.currency} "
                f"is at/under threshold {trip.max_total_price:.2f}."
            )
    storage.close()
    return exit_code


def _list(cfg: Config, trip_name: str, console: Console) -> int:
    storage = Storage(_db_path())
    try:
        trip = next((t for t in cfg.trips if t.name == trip_name), None)
        if trip is None:
            console.print(f"[red]Unknown trip[/red]: {trip_name}")
            return 1
        combos = storage.list_combos(trip_name, limit=15)
        if not combos:
            console.print(f"No stored results for {trip_name}. Run `refresh` first.")
            return 0
        _print_table(console, trip, combos, trip.max_total_price)
        return 0
    finally:
        storage.close()


def _web(host: str, port: int) -> int:
    import uvicorn

    uvicorn.run("flightfinder.web:app", host=host, port=port, reload=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(
        level=os.environ.get("FLIGHTFINDER_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="flightfinder")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_refresh = sub.add_parser("refresh", help="Fetch prices and update the DB")
    p_refresh.add_argument("--trip", help="Only refresh this trip")

    p_list = sub.add_parser("list", help="Print current combos for a trip")
    p_list.add_argument("trip", help="Trip name")

    p_web = sub.add_parser("web", help="Run the FastAPI UI")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", default=8000, type=int)

    args = parser.parse_args(argv)
    console = Console()

    if args.cmd == "web":
        return _web(args.host, args.port)

    config_path = Path(args.config)
    if not config_path.exists():
        console.print(f"[red]Config not found[/red]: {config_path}")
        return 2
    cfg = load_config(config_path)

    if args.cmd == "refresh":
        return asyncio.run(_refresh(cfg, args.trip, console))
    if args.cmd == "list":
        return _list(cfg, args.trip, console)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
