"""
scraper_worker.py — Playwright scraper for DRAX building_overview.

Targets associates currently coded as:
  - Stationary Pick  (019209516)
  - Box Finishing    (019034514)

Detects the active shift window automatically, navigates building_overview
with date_hour_after/before params, extracts the DataTable, filters target
SC codes, then enriches each associate from their individual DRAX page.

Run as a subprocess (via scraper.py). Writes results.json on exit.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
AUTH_FILE  = BASE_DIR / "auth_state.json"
OUT_FILE   = BASE_DIR / "results.json"

DRAX_BASE          = "https://drax.walmart.com"
IDLE_THRESHOLD_HRS = 0.25          # 15 min
ENRICH_CONCURRENCY = 10

TARGET_SC_CODES: dict[str, str] = {
    "019209516": "Stationary Pick",
    "019034514": "Box Finishing",
}

# Text patterns that may appear in the SC code / activity column on overview page
_SC_ALIASES: dict[str, str] = {
    "019209516": "019209516",
    "stationary pick": "019209516",
    "stationary picking": "019209516",
    "019034514": "019034514",
    "box finishing": "019034514",
    "box finish": "019034514",
}


# ── Shift window helpers ──────────────────────────────────────────────────────

def _shift_window() -> tuple[str, str]:
    """
    Return (date_hour_after, date_hour_before) for the CURRENT shift.
    Day  shift: 06:30 – 18:30 same day
    Night shift: 18:30 today – 06:30 tomorrow
    Format: 'YYYY-MM-DD HH:MM'
    """
    now = datetime.now()
    day_start = now.replace(hour=6,  minute=30, second=0, microsecond=0)
    day_end   = now.replace(hour=18, minute=30, second=0, microsecond=0)

    if day_start <= now < day_end:
        after  = day_start.strftime("%Y-%m-%d %H:%M")
        before = day_end.strftime("%Y-%m-%d %H:%M")
    else:
        # Night shift
        if now < day_start:
            # Early morning — night shift started yesterday
            ns_start = (now - timedelta(days=1)).replace(hour=18, minute=30, second=0, microsecond=0)
            ns_end   = day_start
        else:
            # Post 18:30 — night shift started today
            ns_start = day_end
            ns_end   = (now + timedelta(days=1)).replace(hour=6, minute=30, second=0, microsecond=0)
        after  = ns_start.strftime("%Y-%m-%d %H:%M")
        before = ns_end.strftime("%Y-%m-%d %H:%M")

    return after, before


def _overview_url(after: str, before: str) -> str:
    after_enc  = after.replace(" ", "+").replace(":", "%3A")
    before_enc = before.replace(" ", "+").replace(":", "%3A")
    return (
        f"{DRAX_BASE}/building_overview/"
        f"?date_hour_after={after_enc}&date_hour_before={before_enc}"
    )


def _assoc_url(assoc_id: str) -> str:
    return f"{DRAX_BASE}/associates/{assoc_id}/?date={date.today().isoformat()}"


# ── JavaScript extractors ─────────────────────────────────────────────────────

# Discovers ALL DataTables on the page and returns a summary per table
_DISCOVER_JS = """
() => {
  const summary = [];
  if (typeof $ === 'undefined' || !$.fn.DataTable) return summary;
  $('table').each(function () {
    if (!$.fn.DataTable.isDataTable(this)) return;
    const dt = $(this).DataTable();
    const headers = Array.from(this.querySelectorAll('thead th'))
                         .map(th => th.innerText.trim());
    summary.push({ id: this.id, rows: dt.rows().count(), headers });
  });
  return summary;
}
"""

# Extracts all rows from the DataTable with the given element id.
# Falls back to the first DataTable if id is null/empty.
_EXTRACT_JS = """
(tableId) => {
  let dt;
  if (tableId) {
    dt = $('#' + tableId).DataTable();
  } else {
    const first = $('table').filter(function () {
      return $.fn.DataTable.isDataTable(this);
    }).first();
    if (!first.length) return { headers: [], rows: [] };
    dt = first.DataTable();
    tableId = first.attr('id') || '';
  }

  const headers = Array.from(
    document.getElementById(tableId)
      ? document.getElementById(tableId).querySelectorAll('thead th')
      : []
  ).map(th => th.innerText.trim().toLowerCase());

  const rows = [];
  dt.rows().every(function () {
    const cells = Array.from(this.node().querySelectorAll('td'));
    const obj = { _raw: cells.map(c => c.innerText.trim()) };

    // Try to grab associate_id from any link in the row
    const link = this.node().querySelector('a[href*="/associates/"]');
    const idMatch = link && link.href.match(/\\/associates\\/(\\d+)\\//);
    obj.associate_id = idMatch ? idMatch[1] : null;

    // Map known headers → obj fields
    headers.forEach((h, i) => {
      const val = cells[i] ? cells[i].innerText.trim() : '';
      if (/\\bname\\b/.test(h))                  obj.name        = val;
      else if (/win|badge|employee.?id/.test(h)) obj.win         = val;
      else if (/shift/.test(h))                  obj.shift       = val;
      else if (/dept|department/.test(h))        obj.dept_name   = val;
      else if (/activity|sc.?code|labor.?code|code/.test(h)) obj.sc_code = val;
      else if (/idle.?hr|idle.?hour/.test(h))    obj.idle_hours  = parseFloat(val) || 0;
      else if (/idle.?%|idle.?pct/.test(h))      obj.idle_pct    = val;
      else if (/total.?hr|total.?hour/.test(h))  obj.total_hours = parseFloat(val) || 0;
    });

    // Fallbacks: if header mapping missed fields, sniff raw cells
    if (!obj.associate_id) {
      cells.forEach(c => {
        const a = c.querySelector('a[href*="/associates/"]');
        if (a) {
          const m = a.href.match(/\\/associates\\/(\\d+)\\//);
          if (m) obj.associate_id = m[1];
        }
      });
    }

    rows.push(obj);
  });
  return { headers, rows };
}
"""

_ENRICH_JS = """
() => {
  const tsRe = /^\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}$/;
  const lastTs = Array.from(document.querySelectorAll('.card-body td'))
    .map(td => td.innerText.trim())
    .find(t => tsRe.test(t)) || null;
  const isIn = !!document.querySelector('.card-body .fa-check-circle');
  const tbodyRows = document.querySelectorAll('table tbody tr');
  let isOnLunch = false;
  if (tbodyRows.length > 0) {
    const lastCells = tbodyRows[tbodyRows.length - 1].querySelectorAll('td');
    isOnLunch = Array.from(lastCells).some(
      td => td.innerText.toLowerCase().includes('lunch')
    );
  }
  return { lastTs, isIn, isOnLunch };
}
"""


# ── Playwright helpers ────────────────────────────────────────────────────────

async def _launch(pw: Any, headless: bool, storage: dict | None = None):
    lkw: dict[str, Any] = {"headless": headless}
    if not headless:
        lkw["args"] = ["--start-maximized"]
    ckw: dict[str, Any] = {"viewport": {"width": 1600, "height": 900}}
    if storage:
        ckw["storage_state"] = storage
    try:
        b = await pw.chromium.launch(channel="msedge", **lkw)
    except Exception:
        b = await pw.chromium.launch(**lkw)
    return b, await b.new_context(**ckw)


async def _wait_for_any_datatable(page: Any) -> bool:
    """Wait until at least one DataTable with rows exists on the page."""
    try:
        await page.wait_for_selector("table", timeout=30_000)
        await page.wait_for_function(
            "() => typeof $ !== 'undefined' && "
            "$('table').filter(function(){ return $.fn.DataTable && "
            "$.fn.DataTable.isDataTable(this); }).length > 0",
            timeout=30_000,
        )
        await page.wait_for_function(
            "() => { "
            "  let found = false; "
            "  $('table').each(function(){ "
            "    if ($.fn.DataTable.isDataTable(this) && "
            "        $(this).DataTable().rows().count() > 0) found = true; "
            "  }); "
            "  return found; "
            "}",
            timeout=30_000,
        )
        return True
    except Exception:
        return False


def _parse_drax_ts(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        dt = datetime.strptime(
            f"{date.today().year}-{ts}", "%Y-%m-%d %H:%M:%S"
        )
        return dt.isoformat()
    except ValueError:
        return None


def _resolve_sc_code(raw: str) -> str | None:
    """Map a raw SC-code cell value to one of our canonical keys, or None."""
    key = raw.strip().lower()
    return _SC_ALIASES.get(key) or _SC_ALIASES.get(raw.strip())


# ── Enrichment ────────────────────────────────────────────────────────────────

async def _enrich_one(ctx: Any, row: dict, sem: asyncio.Semaphore) -> None:
    async with sem:
        page = await ctx.new_page()
        try:
            await page.goto(
                _assoc_url(row["associate_id"]),
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await page.wait_for_selector(".card-body", timeout=15_000)
            info = await page.evaluate(_ENRICH_JS)
            row["last_scan_iso"] = _parse_drax_ts(info.get("lastTs"))
            row["is_clocked_in"] = info.get("isIn", True)
            row["is_on_lunch"]   = info.get("isOnLunch", False)
        except Exception as exc:
            print(f"[WARN] enrich failed for {row.get('name', '?')}: {exc}")
            row["last_scan_iso"] = None
            row["is_clocked_in"] = True
            row["is_on_lunch"]   = False
        finally:
            await page.close()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    after, before = _shift_window()
    url = _overview_url(after, before)
    print(f"[INFO] Shift window: {after}  →  {before}")
    print(f"[INFO] URL: {url}")

    storage = json.loads(AUTH_FILE.read_text()) if AUTH_FILE.exists() else None

    async with async_playwright() as pw:
        # ── Phase 1: extract building overview table ──────────────────────────
        browser, ctx = await _launch(pw, headless=bool(storage), storage=storage)
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)

        # Handle SSO if redirected
        if "login" in page.url.lower() or "sso" in page.url.lower():
            print("[INFO] SSO login required — waiting up to 3 min…")
            await page.wait_for_url(f"{DRAX_BASE}/**", timeout=180_000)

        if not await _wait_for_any_datatable(page):
            _write_error("Timed out waiting for a DataTable on building_overview")
            await browser.close()
            return

        # Discover tables
        summary = await page.evaluate(_DISCOVER_JS)
        print(f"[INFO] DataTables found: {summary}")

        # Pick the table with the most rows
        best = max(summary, key=lambda t: t["rows"]) if summary else None
        table_id = best["id"] if best else None
        print(f"[INFO] Using table: id={table_id!r}  rows={best['rows'] if best else 0}")

        payload = await page.evaluate(_EXTRACT_JS, table_id)
        await page.close()

        all_rows: list[dict] = payload.get("rows", [])
        headers:  list[str]  = payload.get("headers", [])
        print(f"[INFO] Phase 1 OK — {len(all_rows)} rows, headers={headers}")

        # ── Filter to target SC codes ─────────────────────────────────────────
        targets: list[dict] = []
        for row in all_rows:
            sc_raw = row.get("sc_code", "")
            resolved = _resolve_sc_code(sc_raw)
            if not resolved:
                # Also scan raw cells for a match
                for cell in row.get("_raw", []):
                    resolved = _resolve_sc_code(cell)
                    if resolved:
                        row["sc_code"] = cell
                        break
            if resolved and row.get("associate_id"):
                row["resolved_sc_code"] = resolved
                targets.append(row)

        print(f"[INFO] Phase 2 — enriching {len(targets)} associates in target codes")

        # ── Phase 2: enrich individual associate pages ────────────────────────
        sem = asyncio.Semaphore(ENRICH_CONCURRENCY)
        await asyncio.gather(*[_enrich_one(ctx, r, sem) for r in targets])

        await ctx.storage_state(path=str(AUTH_FILE))
        await browser.close()

    _write_result(targets, after, before)


# ── Output writers ────────────────────────────────────────────────────────────

def _write_result(targets: list[dict], after: str, before: str) -> None:
    threshold_mins = IDLE_THRESHOLD_HRS * 60
    dept_totals: dict[str, dict] = {
        code: {"label": label, "total": 0, "flagged": 0}
        for code, label in TARGET_SC_CODES.items()
    }

    for row in targets:
        sc = row.get("resolved_sc_code", "")
        if sc in dept_totals:
            dept_totals[sc]["total"] += 1

        last_iso  = row.get("last_scan_iso")
        idle_mins: float | None = None
        if last_iso:
            idle_mins = (
                datetime.now() - datetime.fromisoformat(last_iso)
            ).total_seconds() / 60

        row["current_idle_mins"] = idle_mins
        row["drax_url"]          = _assoc_url(row["associate_id"])
        row["is_flagged"] = (
            row.get("is_clocked_in", True)
            and not row.get("is_on_lunch", False)
            and idle_mins is not None
            and idle_mins >= threshold_mins
        )

        if row["is_flagged"] and sc in dept_totals:
            dept_totals[sc]["flagged"] += 1

    # Sort: flagged first, then by longest idle
    targets.sort(key=lambda r: (not r["is_flagged"], -(r.get("current_idle_mins") or 0)))

    result = {
        "associates":   targets,
        "dept_totals":  dept_totals,
        "all_count":    len(targets),
        "scraped_at":   datetime.now().isoformat(timespec="seconds"),
        "shift_after":  after,
        "shift_before": before,
        "error":        None,
    }
    OUT_FILE.write_text(json.dumps(result))
    print(f"[INFO] Results written → {OUT_FILE}  ({len(targets)} associates)")


def _write_error(msg: str) -> None:
    result = {
        "associates":   [],
        "dept_totals":  {
            code: {"label": label, "total": 0, "flagged": 0}
            for code, label in TARGET_SC_CODES.items()
        },
        "all_count":    0,
        "scraped_at":   datetime.now().isoformat(timespec="seconds"),
        "shift_after":  None,
        "shift_before": None,
        "error":        msg,
    }
    OUT_FILE.write_text(json.dumps(result))
    print(f"[ERROR] {msg}")


if __name__ == "__main__":
    asyncio.run(main())
