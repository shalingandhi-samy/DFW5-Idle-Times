"""
scraper.py — Thin subprocess wrapper for scraper_worker.py.

Keeps Playwright in its own process — zero asyncio/greenlet conflicts
with uvicorn's event loop.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR      = Path(__file__).parent
RESULTS_FILE  = BASE_DIR / "results.json"
PYTHON        = sys.executable

IDLE_THRESHOLD_HRS = 0.25

TARGET_SC_CODES: dict[str, str] = {
    "019209516": "Stationary Picking",
    "019034514": "Box Finishing",
    "019034295": "Bagging - Manual",
}


def scrape_idle_data_sync() -> dict:
    """
    Run scraper_worker.py as a subprocess, wait for completion,
    then read and return results.json.
    Called via asyncio.to_thread() from main.py.
    """
    worker = BASE_DIR / "scraper_worker.py"
    result = subprocess.run(
        [PYTHON, str(worker)],
        capture_output=False,   # stream output to terminal for visibility
        timeout=180,
    )
    if result.returncode != 0:
        return _error(f"Worker exited with code {result.returncode}")

    if not RESULTS_FILE.exists():
        return _error("Worker finished but results.json not found")

    with open(RESULTS_FILE) as f:
        return json.load(f)


def _error(msg: str) -> dict:
    return {
        "associates":   [],
        "dept_totals":  {
            code: {"label": label, "total": 0, "flagged": 0}
            for code, label in TARGET_SC_CODES.items()
        },
        "all_count":    0,
        "scraped_at":   datetime.now().isoformat(timespec="seconds"),
        "error":        msg,
    }
