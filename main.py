"""
main.py — FastAPI dashboard for DFW5 Idle Times.

Monitors Stationary Pick & Box Finishing associates pulled from
DRAX building_overview, enriched with per-associate idle data.

Design:
- /refresh  → fires scrape in background, returns IMMEDIATELY
- Background loop re-scrapes every SCRAPE_EVERY_SECS
- HTMX polls /data every POLL_SECS for live UI updates
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
from fastapi.templating import Jinja2Templates

from scraper import (
    IDLE_THRESHOLD_HRS,
    TARGET_SC_CODES,
    scrape_idle_data_sync,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

TEMPLATES           = Jinja2Templates(directory=Path(__file__).parent / "templates")
SCRAPE_EVERY_SECS   = 300   # re-scrape every 5 min
POLL_SECS           = 10    # browser polls /data every 10 s
SCRAPE_TIMEOUT_SECS = 180
RESULTS_FILE        = Path(__file__).parent / "results.json"

# ── Shared state ──────────────────────────────────────────────────────────────

_state: dict = {
    "associates":   [],
    "dept_totals":  {
        code: {"label": label, "total": 0, "flagged": 0}
        for code, label in TARGET_SC_CODES.items()
    },
    "all_count":    0,
    "scraped_at":   None,
    "error":        None,
    "refreshing":   False,
}
_lock = asyncio.Lock()


# ── Scrape helpers ────────────────────────────────────────────────────────────

async def _run_scrape() -> None:
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(scrape_idle_data_sync),
            timeout=SCRAPE_TIMEOUT_SECS,
        )
        async with _lock:
            _state.update(result)
    except asyncio.TimeoutError:
        logger.error("Scrape timed out after %ds", SCRAPE_TIMEOUT_SECS)
        async with _lock:
            _state["error"]     = f"Scrape timed out after {SCRAPE_TIMEOUT_SECS}s — DRAX may be slow. Will retry."
            _state["scraped_at"] = datetime.now().isoformat(timespec="seconds")
    except Exception:
        logger.exception("Scrape failed")
        async with _lock:
            _state["error"]     = "Unexpected scrape error — check terminal logs."
            _state["scraped_at"] = datetime.now().isoformat(timespec="seconds")
    finally:
        async with _lock:
            _state["refreshing"] = False


async def _trigger_scrape() -> None:
    """Start a background scrape if one isn't already running."""
    async with _lock:
        if _state["refreshing"]:
            logger.info("Scrape already in progress — skipping")
            return
        _state["refreshing"] = True
        _state["error"]      = None
    asyncio.create_task(_run_scrape())


async def _background_loop() -> None:
    while True:
        await _trigger_scrape()
        await asyncio.sleep(SCRAPE_EVERY_SECS)


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load last cached results so first page visit isn't blank
    if RESULTS_FILE.exists():
        try:
            cached = json.loads(RESULTS_FILE.read_text())
            _state.update(cached)
            logger.info(
                "Loaded cached results (%d associates)", len(_state["associates"])
            )
        except Exception as exc:
            logger.warning("Could not load results.json: %s", exc)

    task = asyncio.create_task(_background_loop())
    yield
    task.cancel()


app = FastAPI(title="DFW5 Idle Times", lifespan=lifespan)


# ── Utilities ─────────────────────────────────────────────────────────────────

def _fmt_threshold(hrs: float) -> str:
    total_mins = int(hrs * 60)
    h, m = divmod(total_mins, 60)
    if h and m:
        return f"{h} hr {m} min"
    return f"{h} hr" if h else f"{m} min"


def _fmt_shift(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso) if "T" not in iso else datetime.fromisoformat(iso)
        return dt.strftime("%I:%M %p")
    except Exception:
        return iso


def _ctx() -> dict:
    return {
        "associates":      _state["associates"],
        "dept_totals":     _state["dept_totals"],
        "all_count":       _state["all_count"],
        "scraped_at":      _state["scraped_at"],
        "error":           _state["error"],
        "refreshing":      _state["refreshing"],
        "threshold":       IDLE_THRESHOLD_HRS,
        "threshold_label": _fmt_threshold(IDLE_THRESHOLD_HRS),
        "refresh_secs":    SCRAPE_EVERY_SECS,
        "poll_secs":       POLL_SECS,
        "target_codes":    TARGET_SC_CODES,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return TEMPLATES.TemplateResponse(request, "index.html", _ctx())


@app.get("/data", response_class=HTMLResponse)
async def data(request: Request):
    """HTMX polls this endpoint every POLL_SECS for fresh content."""
    return TEMPLATES.TemplateResponse(request, "partials/dashboard.html", _ctx())


@app.post("/refresh", response_class=HTMLResponse)
async def manual_refresh(request: Request):
    """Kick off a background scrape and return immediately."""
    await _trigger_scrape()
    return TEMPLATES.TemplateResponse(request, "partials/dashboard.html", _ctx())


if __name__ == "__main__":
    import webbrowser
    import uvicorn
    webbrowser.open("http://127.0.0.1:8001")
    uvicorn.run("main:app", host="127.0.0.1", port=8001, reload=False)
