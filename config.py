"""
config.py — Building configuration for the multi-FC idle times dashboard.

Add more buildings here without touching any other file.
"""

from __future__ import annotations

# Per-building config
# tz_offset_secs: correction applied to (server_clock - drax_timestamp).
#   Server runs Eastern. DFW5 is Central (1 hr behind) → subtract 3600.
#   PHL5 is Eastern (same as server)                   → subtract 0.
BUILDINGS: dict[str, dict] = {
    "dfw5": {
        "id":             "dfw5",
        "name":           "DFW5",
        "label":          "Dallas / Fort Worth 5",
        "fc_search":      "DFW5",
        "tz_offset_secs": -3600,
    },
    "phl5": {
        "id":             "phl5",
        "name":           "PHL5",
        "label":          "Philadelphia 5",
        "fc_search":      "PHL5",
        "tz_offset_secs": 0,
    },
}

# Shared across all buildings
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

IDLE_THRESHOLD_HRS: float = 0.25
