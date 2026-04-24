"""
DealDesk CRE Underwriting — Global Configuration
Pure constants: API refs, model strings, paths, template routing.
"""

import logging
import os
from dotenv import load_dotenv
load_dotenv(override=True)
from pathlib import Path

# ── Anthropic key validation (fail-fast at startup) ─────────
_key = os.getenv("ANTHROPIC_API_KEY", "NOT_FOUND")
print(f"DEBUG ANTHROPIC KEY AT INIT: starts={_key[:12] if len(_key) > 12 else _key}, len={len(_key)}")
if not _key or not _key.startswith("sk-ant-"):
    raise RuntimeError(
        "ANTHROPIC_API_KEY is missing or malformed. "
        "Set it in .env before starting the server."
    )

# ── Root paths ────────────────────────────────────────────────
PROJECT_ROOT       = Path(__file__).resolve().parent
DATA_DIR           = PROJECT_ROOT / "data"
TEMPLATES_DIR      = DATA_DIR / "templates"
OUTPUT_DIR         = DATA_DIR / "output"
OUTPUTS_DIR        = PROJECT_ROOT / "outputs"
WORD_TEMPLATES_DIR = PROJECT_ROOT / "templates"

# ── Excel template routing ────────────────────────────────────
# Driven entirely by InvestmentStrategy — asset_type plays no role.
from models.models import InvestmentStrategy

logger = logging.getLogger(__name__)

_STRATEGY_TEMPLATE_MAP: dict[InvestmentStrategy, Path] = {
    InvestmentStrategy.STABILIZED_HOLD: TEMPLATES_DIR / "hold_template_v3.xlsx",
    InvestmentStrategy.VALUE_ADD:       TEMPLATES_DIR / "hold_template_v3.xlsx",
    InvestmentStrategy.OPPORTUNISTIC:   TEMPLATES_DIR / "sale_template_v3.xlsx",
}

_DEFAULT_TEMPLATE = TEMPLATES_DIR / "hold_template_v3.xlsx"


def get_excel_template(strategy: InvestmentStrategy | None) -> Path:
    """
    Return the correct Excel template Path for a given investment strategy.

    Falls back to Hold_Template_v3.xlsx with a warning if strategy is None
    or not recognised.

    Raises
    ------
    FileNotFoundError  Template file does not exist on disk.
    """
    if strategy is None or strategy not in _STRATEGY_TEMPLATE_MAP:
        logger.warning(
            "Unrecognised or missing investment_strategy %r — "
            "defaulting to Hold_Template_v3.xlsx",
            strategy,
        )
        path = _DEFAULT_TEMPLATE
    else:
        path = _STRATEGY_TEMPLATE_MAP[strategy]

    if not path.exists():
        raise FileNotFoundError(
            f"Excel template not found: {path}\n"
            f"Check that the file exists in {TEMPLATES_DIR}"
        )
    return path


# ── Reference data ────────────────────────────────────────────
MUNICIPAL_REGISTRY_CSV = DATA_DIR / "municipal_registry.csv"

# ── Anthropic / LLM ──────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL_HAIKU  = "claude-haiku-4-5-20251001"
MODEL_SONNET = "claude-sonnet-4-6"

# ── Prompt-architecture feature flags ───────────────────────
# Phase 1 rollout: specialist Prompt 4-REC replaces the eight
# Investment-Recommendation keys inside 4-MASTER Part 2. When
# True, 4-MASTER Part 2 no longer generates the rec keys and a
# dedicated Sonnet call handles them. Flip to False to fall
# back to the legacy behavior (rec keys produced by 4-MASTER).
#
# Validation — Run 1 (deal 7dad2c54, 2026-04-24): clean pass.
#   - NO-GO verdict matched the 4-MASTER baseline
#   - BEAT 0 precedence enforced (CLEAR FAIL → NO-GO, no override)
#   - BEAT 3 taxonomy applied correctly (2 WORKABLE RED, 5 AMBER)
# Diag counter at 1 of 5; consistency review at run 5.
# Reference: outputs/4REC_reasoning_run1_7dad2c54.txt
USE_4REC_SPECIALIST = True

# ── HUD ──────────────────────────────────────────────────────
HUD_API_KEY = os.environ.get("HUD_API_KEY", "")

# ── Email (SMTP) ─────────────────────────────────────────────
SMTP_PORT_DEFAULT = 587

# ── Google Maps / Street View ────────────────────────────────
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")

# ── Map image dimensions ─────────────────────────────────────
MAP_WIDTH  = 800   # pixels — Static Maps + tile stitching
MAP_HEIGHT = 500

# ── Street View image dimensions ─────────────────────────────
SV_WIDTH   = 800
SV_HEIGHT  = 500

# ── Google Aerial View API (preview — returns photographic stills) ─────
AERIAL_VIEW_API_URL = "https://aerialview.googleapis.com/v1/videos:lookupVideo"

# ── Google Address Validation API (USPS CASS standardization) ──────────
ADDRESS_VALIDATION_API_URL = "https://addressvalidation.googleapis.com/v1:validateAddress"

# ── Google Places API (New) — Nearby Search ─────────────────────────────
PLACES_NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"

# Places Aggregate endpoint (preview) — falls back to Places Nearby when
# not enabled on the project.
PLACES_AGGREGATE_URL = "https://places.googleapis.com/v1/places:searchNearby"

# ── Google Map Tiles API (session-based styled tiles) ──────────────────
MAP_TILES_SESSION_URL = "https://tile.googleapis.com/v1/createSession"
MAP_TILES_FETCH_URL   = "https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}"

# ── Google Maps Elevation API ──────────────────────────────────────────
ELEVATION_API_URL = "https://maps.googleapis.com/maps/api/elevation/json"

# ── Places search radius (meters) — 1 mile = 1,609.34 m ────────────────
PLACES_RADIUS_METERS = 1609

# ── POI categories for neighborhood context enrichment ─────────────────
# Slugs use the Places API (New) v1 table
# (https://developers.google.com/maps/documentation/places/web-service/place-types).
# `grocery_or_supermarket` from the legacy API was split into
# `grocery_store` + `supermarket` and returns 400 Bad Request on the new
# endpoint — do NOT revert without verifying upstream.
POI_TYPES = [
    "supermarket",
    "grocery_store",
    "transit_station",
    "subway_station",
    "bus_station",
    "school",
    "park",
    "restaurant",
    "bank",
    "pharmacy",
    "hospital",
    "gym",
    "shopping_mall",
]

# ── FRED (Federal Reserve Economic Data) ─────────────────────
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

