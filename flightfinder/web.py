"""Read-only FastAPI web UI over the SQLite DB written by the CLI refresher."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .storage import Storage

load_dotenv()

_PKG_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))


def _db() -> Storage:
    return Storage(os.environ.get("FLIGHTFINDER_DB", "flightfinder.db"))


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


templates.env.filters["ts"] = _fmt_ts


app = FastAPI(title="Flight-finder")
app.mount("/static", StaticFiles(directory=str(_PKG_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    storage = _db()
    try:
        trips = storage.list_trips()
        cards = []
        for t in trips:
            cheapest = storage.cheapest_combo(t["name"])
            cards.append({"trip": t, "cheapest": cheapest})
        return templates.TemplateResponse(
            request, "index.html", {"cards": cards}
        )
    finally:
        storage.close()


@app.get("/trip/{name}", response_class=HTMLResponse)
def trip_detail(request: Request, name: str) -> HTMLResponse:
    storage = _db()
    try:
        trips = {t["name"]: t for t in storage.list_trips()}
        if name not in trips:
            raise HTTPException(status_code=404, detail="trip not found")
        combos = storage.list_combos(name, limit=50)
        history = storage.price_history(name, limit=500)
        chart = {
            "labels": [_fmt_ts(p["found_at"]) for p in history],
            "prices": [p["cheapest_total_price"] for p in history],
        }
        return templates.TemplateResponse(
            request,
            "trip.html",
            {
                "trip": trips[name],
                "combos": combos,
                "chart_json": json.dumps(chart),
            },
        )
    finally:
        storage.close()
