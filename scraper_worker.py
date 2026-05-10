"""
scraper_worker.py — Simple Playwright scraper for DRAX /associates/.

Goes to drax.walmart.com/associates/, finds everyone with:
  - Status: IN
  - Current Dept: Stationary Picking OR Box Finishing

Writes results.json then exits. Run as subprocess via scraper.py.
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

# ── Extract all rows from the associates DataTable ────────────────────────────
_EXTRACT_JS = """
() => {
  // Find the DataTable on the page
  const tables = $('table').filter(function() {
    return $.fn.DataTable && $.fn.DataTable.isDataTable(this);
  });
  if (!tables.length) return { headers: [], rows: [] };

  const tbl   = tables.first();
  const dt    = tbl.DataTable();
  const node  = tbl[0];

  const headers = Array.from(node.querySelectorAll('thead th'))
                       .map(th => th.innerText.trim().toLowerCase());

  const rows = [];
  dt.rows().every(function() {
    const cells = Array.from(this.node().querySelectorAll('td'));
    const raw   = cells.map(c => c.innerText.trim());

    const obj = { _raw: raw };

    // Extract associate_id from any link in the row
    const link = this.node().querySelector('a[href*="/associates/"]');
    if (link) {
      const parts = link.href.split('/associates/');
      obj.associate_id = parts.length > 1 ? parts[1].split('/')[0] : null;
    } else {
      obj.associate_id = null;
    }

    // Map headers to fields
    headers.forEach((h, i) => {
      const v = raw[i] || '';
      if (/\bname\b|\bassociate\b/.test(h))              obj.name         = v;
      else if (/status/.test(h))                            obj.status       = v;
      else if (/current.?dept/.test(h))                     obj.current_dept = v;
      else if (/home.?dept/.test(h))                        obj.home_dept    = v;
      else if (/\bsc.?code\b/.test(h))                      obj.sc_code      = v;
      else if (/win|badge|assoc.+#/.test(h))                obj.win          = v;
      else if (/shift/.test(h))                             obj.shift        = v;
      else if (/timestamp|last.?scan/.test(h))              obj.timestamp    = v;
      else if (/idle.?hour/.test(h))                        obj.idle_hours   = parseFloat(v) || 0;
      else if (/hours/.test(h))                             obj.total_hours  = parseFloat(v) || 0;
    });

    rows.push(obj);
  });

  return { headers, rows };
}
"""


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
            "$('table').filter(function(){ "
            "  return $.fn.DataTable && $.fn.DataTable.isDataTable(this); "
            "}).length > 0",
            timeout=40_000,
        )
        await page.wait_for_function(
            "() => $('table').filter(function(){ "
            "  return $.fn.DataTable && $.fn.DataTable.isDataTable(this) && "
            "  $(this).DataTable().rows().count() > 0; "
            "}).length > 0",
            timeout=40_000,
        )
        return True
    except Exception:
        return False


async def main() -> None:
    storage = json.loads(AUTH_FILE.read_text()) if AUTH_FILE.exists() else None
    print(f"[INFO] Navigating to {ASSOC_URL}")

    async with async_playwright() as pw:
        browser, ctx = await _launch(pw, headless=bool(storage), storage=storage)
        page = await ctx.new_page()
        await page.goto(ASSOC_URL, wait_until="domcontentloaded", timeout=60_000)

        # Handle SSO redirect
        if not page.url.startswith(DRAX_BASE):
            print(f"[INFO] SSO redirect detected — waiting up to 3 min for login...")
            await page.wait_for_url(f"{DRAX_BASE}/**", timeout=180_000)

        if not await _wait_for_table(page):
            _write_error("Timed out waiting for associates table on /associates/")
            await browser.close()
            return

        payload = await page.evaluate(_EXTRACT_JS)
        await ctx.storage_state(path=str(AUTH_FILE))
        await browser.close()

    headers = payload.get("headers", [])
    all_rows = payload.get("rows", [])
    print(f"[INFO] Got {len(all_rows)} rows — headers: {headers}")

    # Filter: Status=IN and sc_code OR current_dept matches our targets
    matches = []
    for row in all_rows:
        status  = row.get("status", "").strip().upper()
        sc_code = row.get("sc_code", "").strip()
        dept    = row.get("current_dept", "").strip().lower()

        if status != "IN":
            continue

        # Match by sc_code first (exact), fall back to dept name
        if sc_code in TARGET_SC_CODES:
            row["resolved_sc_code"] = sc_code
        elif any(t in dept for t in TARGET_DEPTS):
            row["resolved_sc_code"] = "019209516" if "stationary" in dept else "019034514"
        else:
            continue

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

    result = {
        "associates":   matches,
        "dept_totals":  dept_totals,
        "all_count":    len(all_rows),
        "scraped_at":   datetime.now().isoformat(timespec="seconds"),
        "error":        None,
    }
    OUT_FILE.write_text(json.dumps(result))
    print(f"[INFO] Done — {len(matches)} associates written to results.json")


def _write_error(msg: str) -> None:
    OUT_FILE.write_text(json.dumps({
        "associates":   [],
        "dept_totals":  {
            code: {"label": label, "total": 0, "flagged": 0}
            for code, label in TARGET_SC_CODES.items()
        },
        "all_count":    0,
        "scraped_at":   datetime.now().isoformat(timespec="seconds"),
        "error":        msg,
    }))
    print(f"[ERROR] {msg}")


if __name__ == "__main__":
    asyncio.run(main())
