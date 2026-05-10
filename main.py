"""
main.py — Multi-building FastAPI dashboard for Idle Times.

Routes:
    /                         → Home page (building cards)
    /building/<id>            → Building dashboard
    /data/<id>                → HTMX polling endpoint
    /refresh/<id>             → Trigger manual scrape
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import BUILDINGS, IDLE_THRESHOLD_HRS, TARGET_SC_CODES
from scraper import results_file, scrape_idle_data_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

TEMPLATES         = Jinja2Templates(directory=Path(__file__).parent / "templates")
SCRAPE_EVERY_SECS = 300
POLL_SECS         = 10
SCRAPE_TIMEOUT    = 180


# ── Per-building state + locks ────────────────────────────────────────────────

def _empty_state() -> dict:
    return {
        "associates":  [],
        "dept_totals": {
            code: {"label": label, "total": 0, "flagged": 0}
            for code, label in TARGET_SC_CODES.items()
        },
        "all_count":   0,
        "scraped_at":  None,
        "error":       None,
        "refreshing":  False,
    }


_states: dict[str, dict]          = {bid: _empty_state() for bid in BUILDINGS}
_locks:  dict[str, asyncio.Lock]  = {bid: asyncio.Lock() for bid in BUILDINGS}


# ── Scrape helpers ────────────────────────────────────────────────────────────

async def _run_scrape(building_id: str) -> None:
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(scrape_idle_data_sync, building_id),
            timeout=SCRAPE_TIMEOUT,
        )
        async with _locks[building_id]:
            _states[building_id].update(result)
    except asyncio.TimeoutError:
        logger.error("[%s] Scrape timed out", building_id)
        async with _locks[building_id]:
            _states[building_id]["error"] = f"Scrape timed out after {SCRAPE_TIMEOUT}s"
            _states[building_id]["scraped_at"] = datetime.now().isoformat(timespec="seconds")
    except Exception:
        logger.exception("[%s] Scrape failed", building_id)
        async with _locks[building_id]:
            _states[building_id]["error"] = "Unexpected scrape error — check terminal logs."
            _states[building_id]["scraped_at"] = datetime.now().isoformat(timespec="seconds")
    finally:
        async with _locks[building_id]:
            _states[building_id]["refreshing"] = False


async def _trigger_scrape(building_id: str) -> None:
    async with _locks[building_id]:
        if _states[building_id]["refreshing"]:
            logger.info("[%s] Scrape already in progress — skipping", building_id)
            return
        _states[building_id]["refreshing"] = True
        _states[building_id]["error"]      = None
    asyncio.create_task(_run_scrape(building_id))


async def _background_loop(building_id: str) -> None:
    while True:
        await _trigger_scrape(building_id)
        await asyncio.sleep(SCRAPE_EVERY_SECS)


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load cached results per building
    for bid in BUILDINGS:
        rfile = results_file(bid)
        if rfile.exists():
            try:
                cached = json.loads(rfile.read_text())
                _states[bid].update(cached)
                logger.info("[%s] Loaded cached results (%d associates)",
                            bid, len(_states[bid]["associates"]))
            except Exception as exc:
                logger.warning("[%s] Could not load cache: %s", bid, exc)

    # Start a background scrape loop per building
    tasks = [asyncio.create_task(_background_loop(bid)) for bid in BUILDINGS]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="FC Idle Times", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


# ── Utilities ─────────────────────────────────────────────────────────────────

def _building_ctx(building_id: str) -> dict:
    s = _states[building_id]
    b = BUILDINGS[building_id]
    return {
        **s,
        "building":        b,
        "target_codes":    TARGET_SC_CODES,
        "refresh_secs":    SCRAPE_EVERY_SECS,
        "poll_secs":       POLL_SECS,
        "threshold_label": "30 min",
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    buildings_ctx = []
    for bid, b in BUILDINGS.items():
        s = _states[bid]
        associates = s["associates"]
        red = sum(
            1 for a in associates
            if (a.get("elapsed_secs") or 0) >= 1800
        )
        buildings_ctx.append({
            **b,
            "total":       len(associates),
            "alerts":      red,
            "scraped_at":  s["scraped_at"],
            "refreshing":  s["refreshing"],
            "error":       s["error"],
        })
    return TEMPLATES.TemplateResponse(request, "home.html", {
        "buildings":    buildings_ctx,
        "refresh_secs": SCRAPE_EVERY_SECS,
    })


@app.get("/building/{building_id}", response_class=HTMLResponse)
async def building_dashboard(request: Request, building_id: str):
    if building_id not in BUILDINGS:
        return HTMLResponse("Building not found", status_code=404)
    return TEMPLATES.TemplateResponse(
        request, "index.html", _building_ctx(building_id)
    )


@app.get("/data/{building_id}", response_class=HTMLResponse)
async def data(request: Request, building_id: str):
    if building_id not in BUILDINGS:
        return HTMLResponse("Building not found", status_code=404)
    return TEMPLATES.TemplateResponse(
        request, "partials/dashboard.html", _building_ctx(building_id)
    )


@app.post("/refresh/{building_id}", response_class=HTMLResponse)
async def manual_refresh(request: Request, building_id: str):
    if building_id not in BUILDINGS:
        return HTMLResponse("Building not found", status_code=404)
    await _trigger_scrape(building_id)
    return TEMPLATES.TemplateResponse(
        request, "partials/dashboard.html", _building_ctx(building_id)
    )


if __name__ == "__main__":
    import webbrowser
    import uvicorn
    webbrowser.open("http://127.0.0.1:8001")
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False)
