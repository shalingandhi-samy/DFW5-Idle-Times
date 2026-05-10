"""
scraper_worker.py — Simple Playwright scraper for DRAX /associates/.

Finds everyone with Status=IN and Current Dept = Stationary Picking or Box Finishing.
Computes elapsed_secs server-side so the browser timer is timezone-agnostic.

Run as subprocess via scraper.py. Writes results.json then exits.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE_DIR  = Path(__file__).parent
AUTH_FILE = BASE_DIR / "auth_state.json"
OUT_FILE  = BASE_DIR / "results.json"

DRAX_BASE = "https://drax.walmart.com"
ASSOC_URL = f"{DRAX_BASE}/associates/"

TARGET_DEPTS = {"stationary picking", "box finishing"}

TARGET_SC_CODES: dict[str, str] = {
    "019209516": "Stationary Picking",
    "019034514": "Box Finishing",
}

# ── JavaScript extractor ──────────────────────────────────────────────────────
_EXTRACT_JS = """
() => {
  const tables = $('table').filter(function() {
    return $.fn.DataTable && $.fn.DataTable.isDataTable(this);
  });
  if (!tables.length) return { headers: [], rows: [] };

  const tbl  = tables.first();
  const dt   = tbl.DataTable();
  const node = tbl[0];

  const headers = Array.from(node.querySelectorAll('thead th'))
                       .map(th => th.innerText.trim().toLowerCase());

  const fieldMap = {
    'name':      'name',
    'associate': 'name',
    'status':    'status',
    'shift':     'shift',
    'sc code':   'sc_code',
    'win':       'win',
    'timestamp': 'timestamp',
  };

  const rows = [];
  dt.rows().every(function() {
    const cells = Array.from(this.node().querySelectorAll('td'));
    const raw   = cells.map(c => c.innerText.trim());
    const obj   = { _raw: raw };

    // Associate ID from any link in the row
    const link = this.node().querySelector('a[href*="/associates/"]');
    if (link) {
      const parts = link.href.split('/associates/');
      obj.associate_id = parts.length > 1 ? parts[1].split('/')[0] : null;
    } else {
      obj.associate_id = null;
    }

    headers.forEach((h, i) => {
      const v = raw[i] || '';
      if (fieldMap[h]) {
        obj[fieldMap[h]] = v;
      } else if (h.includes('current') && h.includes('dept')) {
        obj.current_dept = v;
      } else if (h.includes('assoc') && h.includes('#')) {
        obj.win = v;
      } else if (h.includes('idle') && h.includes('hour')) {
        obj.idle_hours = parseFloat(v) || 0;
      } else if (h.includes('hour')) {
        obj.total_hours = parseFloat(v) || 0;
      }
    });

    rows.push(obj);
  });

  return { headers, rows };
}
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_drax_ts(ts: str | None) -> datetime | None:
    """Parse DRAX 'MM-DD HH:MM:SS' using the server's local clock year."""
    if not ts:
        return None
    try:
        return datetime.strptime(
            f"{datetime.now().year}-{ts}", "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        return None


async def _launch(pw: Any, headless: bool, storage: dict | None = None):
    kw: dict[str, Any] = {"headless": headless}
    if not headless:
        kw["args"] = ["--start-maximized"]
    ctx_kw: dict[str, Any] = {"viewport": {"width": 1600, "height": 900}}
    if storage:
        ctx_kw["storage_state"] = storage
    try:
        b = await pw.chromium.launch(channel="msedge", **kw)
    except Exception:
        b = await pw.chromium.launch(**kw)
    return b, await b.new_context(**ctx_kw)


async def _wait_for_table(page: Any) -> bool:
    try:
        await page.wait_for_function(
            "() => typeof $ !== 'undefined' && "
            "$('table').filter(function(){"
            "  return $.fn.DataTable && $.fn.DataTable.isDataTable(this);"
            "}).length > 0",
            timeout=40_000,
        )
        await page.wait_for_function(
            "() => $('table').filter(function(){"
            "  return $.fn.DataTable && $.fn.DataTable.isDataTable(this) &&"
            "  $(this).DataTable().rows().count() > 0;"
            "}).length > 0",
            timeout=40_000,
        )
        return True
    except Exception:
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    storage = json.loads(AUTH_FILE.read_text()) if AUTH_FILE.exists() else None
    print(f"[INFO] Navigating to {ASSOC_URL}")

    async with async_playwright() as pw:
        browser, ctx = await _launch(pw, headless=bool(storage), storage=storage)
        page = await ctx.new_page()
        await page.goto(ASSOC_URL, wait_until="domcontentloaded", timeout=60_000)

        if not page.url.startswith(DRAX_BASE):
            print(f"[INFO] SSO redirect — waiting up to 3 min for login...")
            await page.wait_for_url(f"{DRAX_BASE}/**", timeout=180_000)

        if not await _wait_for_table(page):
            _write_error("Timed out waiting for associates table on /associates/")
            await browser.close()
            return

        payload = await page.evaluate(_EXTRACT_JS)
        await ctx.storage_state(path=str(AUTH_FILE))
        await browser.close()

    headers  = payload.get("headers", [])
    all_rows = payload.get("rows", [])
    print(f"[INFO] Got {len(all_rows)} rows — headers: {headers}")

    now     = datetime.now()
    matches = []

    for row in all_rows:
        status  = row.get("status", "").strip().upper()
        sc_code = row.get("sc_code", "").strip()
        dept    = row.get("current_dept", "").strip().lower()

        if status != "IN":
            continue

        if sc_code in TARGET_SC_CODES:
            row["resolved_sc_code"] = sc_code
        elif any(t in dept for t in TARGET_DEPTS):
            row["resolved_sc_code"] = "019209516" if "stationary" in dept else "019034514"
        else:
            continue

        # Server clock is Eastern, DRAX timestamps are Central — subtract 1 hr
        ts_dt = _parse_drax_ts(row.get("timestamp"))
        row["elapsed_secs"] = max(0, int((now - ts_dt).total_seconds()) - 3600) if ts_dt else None

        row["drax_url"] = (
            f"{DRAX_BASE}/associates/{row['associate_id']}/"
            if row.get("associate_id") else ASSOC_URL
        )
        matches.append(row)

    print(f"[INFO] Filtered to {len(matches)} associates (IN + target dept)")

    dept_totals: dict[str, dict] = {
        code: {"label": label, "total": 0, "flagged": 0}
        for code, label in TARGET_SC_CODES.items()
    }
    for row in matches:
        sc = row.get("resolved_sc_code", "")
        if sc in dept_totals:
            dept_totals[sc]["total"] += 1

    OUT_FILE.write_text(json.dumps({
        "associates":  matches,
        "dept_totals": dept_totals,
        "all_count":   len(all_rows),
        "scraped_at":  now.isoformat(timespec="seconds"),
        "error":       None,
    }))
    print(f"[INFO] Done — {len(matches)} associates written to results.json")


def _write_error(msg: str) -> None:
    OUT_FILE.write_text(json.dumps({
        "associates":  [],
        "dept_totals": {
            code: {"label": label, "total": 0, "flagged": 0}
            for code, label in TARGET_SC_CODES.items()
        },
        "all_count":   0,
        "scraped_at":  datetime.now().isoformat(timespec="seconds"),
        "error":       msg,
    }))
    print(f"[ERROR] {msg}")


if __name__ == "__main__":
    asyncio.run(main())
