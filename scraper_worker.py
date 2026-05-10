"""
scraper_worker.py — Playwright scraper for DRAX /associates/.

Usage:
    python scraper_worker.py <building_id> <tz_offset_secs>

Switches the active FC in DRAX to the target building, scrapes all
associates, filters to target SC codes, and writes results_<building>.json.
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

# ── Args ──────────────────────────────────────────────────────────────────────
BUILDING_ID     = sys.argv[1] if len(sys.argv) > 1 else "dfw5"
TZ_OFFSET_SECS  = int(sys.argv[2]) if len(sys.argv) > 2 else -3600
FC_SEARCH       = sys.argv[3] if len(sys.argv) > 3 else "DFW5"

BASE_DIR  = Path(__file__).parent
AUTH_FILE = BASE_DIR / "auth_state.json"
OUT_FILE  = BASE_DIR / f"results_{BUILDING_ID}.json"

DRAX_BASE = "https://drax.walmart.com"
ASSOC_URL = f"{DRAX_BASE}/associates/"

# ── SC codes / dept matching ──────────────────────────────────────────────────
TARGET_SC_CODES: dict[str, str] = {
    "019209516": "Stationary Picking",
    "019034514": "Box Finishing",
    "019034295": "Bagging - Manual",
    "019357098": "CPS Packing",
    "002172268": "Special Picking",
}

TARGET_DEPTS: set[str] = {
    "stationary picking",
    "box finishing",
    "bagging - manual", "bagging manual", "bagging",
    "central problem solve", "cps packing", "cps",
    "special picking",
}

# ── JavaScript DataTable extractor ────────────────────────────────────────────
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


async def _switch_fc(page: Any, fc_name: str) -> bool:
    """Click the FC switcher, search for fc_name, and select it."""
    try:
        print(f"[INFO] Switching FC to {fc_name}…")

        # Click the FC button in the navbar (contains "FC:" text)
        fc_btn = page.locator("a.nav-link:has-text('FC:'), button:has-text('FC:')")
        await fc_btn.first.click(timeout=10_000)

        # Wait for the FC modal to appear
        modal = page.locator("#fc-modal-dialog, .modal.show")
        await modal.first.wait_for(state="visible", timeout=10_000)

        # Type building name into the search box inside the modal
        search = page.locator("#fc-modal-dialog input[type='search'], .modal.show input[type='search']")
        await search.first.fill(fc_name, timeout=5_000)
        await page.wait_for_timeout(600)

        # Click the first matching FC link in the modal table
        fc_link = page.locator(f"#fc-modal-dialog a:has-text('{fc_name}'), .modal.show a:has-text('{fc_name}')")
        await fc_link.first.click(timeout=8_000)

        print(f"[INFO] FC switched to {fc_name} — waiting for table reload…")
        await page.wait_for_timeout(2_000)
        return True
    except Exception as exc:
        print(f"[WARN] FC switch failed: {exc} — scraping whatever FC is active")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    storage = json.loads(AUTH_FILE.read_text()) if AUTH_FILE.exists() else None
    print(f"[INFO] Building={BUILDING_ID} | FC={FC_SEARCH} | tz_offset={TZ_OFFSET_SECS}s")
    print(f"[INFO] Navigating to {ASSOC_URL}")

    async with async_playwright() as pw:
        browser, ctx = await _launch(pw, headless=bool(storage), storage=storage)
        page = await ctx.new_page()
        await page.goto(ASSOC_URL, wait_until="domcontentloaded", timeout=60_000)

        # SSO redirect
        if not page.url.startswith(DRAX_BASE):
            print(f"[INFO] SSO redirect — waiting up to 3 min for login…")
            await page.wait_for_url(f"{DRAX_BASE}/**", timeout=180_000)

        # Wait for initial table
        if not await _wait_for_table(page):
            _write_error("Timed out waiting for associates table")
            await browser.close()
            return

        # Switch to the target FC
        await _switch_fc(page, FC_SEARCH)

        # Wait for table to reload with new FC data
        if not await _wait_for_table(page):
            _write_error(f"Timed out after FC switch to {FC_SEARCH}")
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
            if "stationary" in dept:
                row["resolved_sc_code"] = "019209516"
            elif "box" in dept:
                row["resolved_sc_code"] = "019034514"
            elif "bagg" in dept:
                row["resolved_sc_code"] = "019034295"
            elif "cps" in dept or "central problem" in dept:
                row["resolved_sc_code"] = "019357098"
            elif "special" in dept:
                row["resolved_sc_code"] = "002172268"
            else:
                continue
        else:
            continue

        # Elapsed seconds — apply per-building timezone correction
        ts_dt = _parse_drax_ts(row.get("timestamp"))
        if ts_dt:
            raw_secs = int((now - ts_dt).total_seconds()) + TZ_OFFSET_SECS
            row["elapsed_secs"] = max(0, raw_secs)
        else:
            row["elapsed_secs"] = None

        row["drax_url"] = (
            f"{DRAX_BASE}/associates/{row['associate_id']}/"
            if row.get("associate_id") else ASSOC_URL
        )
        matches.append(row)

    # Sort: highest idle time first
    matches.sort(key=lambda r: r.get("elapsed_secs") or 0, reverse=True)
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
    print(f"[INFO] Done — {len(matches)} associates written to {OUT_FILE.name}")


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
