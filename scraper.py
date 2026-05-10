"""
scraper.py — Thin subprocess wrapper for scraper_worker.py.

Passes building_id + tz_offset so the worker knows which FC to select.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from config import BUILDINGS, IDLE_THRESHOLD_HRS, TARGET_SC_CODES

BASE_DIR = Path(__file__).parent
PYTHON   = sys.executable


def results_file(building_id: str) -> Path:
    return BASE_DIR / f"results_{building_id}.json"


def scrape_idle_data_sync(building_id: str) -> dict:
    """
    Run scraper_worker.py as a subprocess for the given building.
    Called via asyncio.to_thread() from main.py.
    """
    building = BUILDINGS[building_id]
    worker   = BASE_DIR / "scraper_worker.py"

    result = subprocess.run(
        [
            PYTHON, str(worker),
            building_id,
            str(building["tz_offset_secs"]),
            building["fc_search"],
        ],
        capture_output=False,
        timeout=180,
    )

    if result.returncode != 0:
        return _error(building_id, f"Worker exited with code {result.returncode}")

    rfile = results_file(building_id)
    if not rfile.exists():
        return _error(building_id, "Worker finished but results file not found")

    with open(rfile) as f:
        return json.load(f)


def _error(building_id: str, msg: str) -> dict:
    return {
        "associates":  [],
        "dept_totals": {
            code: {"label": label, "total": 0, "flagged": 0}
            for code, label in TARGET_SC_CODES.items()
        },
        "all_count":   0,
        "scraped_at":  datetime.now().isoformat(timespec="seconds"),
        "error":       msg,
    }
