"""
market.py — Market Data & Zoning Analysis Module
=================================================
Enriches DealData with external market data, zoning analysis, and debt market context.

Data sources (all free, no API keys except FRED):
    1. Census ACS 5-Year  — demographics (population, income, renter %)
    2. FRED               — DGS10, SOFR, MORTGAGE30US, CPIAUCSL
    3. HUD FMR            — Fair Market Rents by ZIP
    4. FEMA NFHL          — flood zone by lat/lon
    5. EPA EnviroFacts    — environmental flags by ZIP

Local data:
    6. Municipal registry CSV — zoning code URL, chapter reference, code platform
    7. Zoning code scrape   — HTML → text from municipal code URL

AI prompts (FINAL_APPROVED_Prompt_Catalog_v4.md):
    Prompt 3A — Zoning Parameter Extraction  (Haiku)
    Prompt 3B — Buildable Capacity Analysis  (Sonnet)
    Prompt 3C — Highest & Best Use Opinion   (Sonnet)
    Prompt 5B — Debt Market Snapshot         (Sonnet)

Every external call is wrapped in try/except — failures log warnings and
return None, never crash the pipeline.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import anthropic
import requests
import streamlit as st

from config import (
    ANTHROPIC_SECRET_KEY,
    MODEL_HAIKU,
    MODEL_SONNET,
    MUNICIPAL_REGISTRY_CSV,
)
from models.models import DealData, MarketData, ZoningData

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30  # seconds for all HTTP calls


# ═══════════════════════════════════════════════════════════════════════════
# 1. CENSUS ACS — Demographics
# ════════════════════���═════════════════════════════��════════════════════════

_CENSUS_BASE = "https://api.census.gov/data/2022/acs/acs5"

# ACS variable codes
_ACS_VARS = {
    "B01003_001E": "total_population",
    "B19013_001E": "median_hh_income",
    "B25003_001E": "total_tenure",       # total occupied units
    "B25003_003E": "renter_occupied",     # renter-occupied units
}


def _fetch_census_tract(state_fips: str, county_fips: str, tract: str) -> Optional[Dict[str, Any]]:
    """Fetch ACS demographics for a specific census tract."""
    var_list = ",".join(_ACS_VARS.keys())
    params = {
        "get": f"NAME,{var_list}",
        "for": f"tract:{tract}",
        "in": f"state:{state_fips} county:{county_fips}",
    }
    try:
        resp = requests.get(_CENSUS_BASE, params=params, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if len(data) < 2:
            return None
        header, row = data[0], data[1]
        result = dict(zip(header, row))
        return {
            "population": _safe_int(result.get("B01003_001E")),
            "median_hh_income": _safe_float(result.get("B19013_001E")),
            "pct_renter_occupied": _pct_renter(
                result.get("B25003_003E"), result.get("B25003_001E")
            ),
        }
    except Exception as exc:
        logger.warning("Census ACS fetch failed (tract %s): %s", tract, exc)
        return None


def _fetch_census_by_zip(zip_code: str) -> Optional[Dict[str, Any]]:
    """Fallback: fetch ACS demographics by ZCTA (ZIP Code Tabulation Area)."""
    var_list = ",".join(_ACS_VARS.keys())
    params = {
        "get": f"NAME,{var_list}",
        "for": f"zip code tabulation area:{zip_code}",
    }
    try:
        resp = requests.get(_CENSUS_BASE, params=params, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if len(data) < 2:
            return None
        header, row = data[0], data[1]
        result = dict(zip(header, row))
        return {
            "population": _safe_int(result.get("B01003_001E")),
            "median_hh_income": _safe_float(result.get("B19013_001E")),
            "pct_renter_occupied": _pct_renter(
                result.get("B25003_003E"), result.get("B25003_001E")
            ),
        }
    except Exception as exc:
        logger.warning("Census ACS fetch failed (ZIP %s): %s", zip_code, exc)
        return None


def _pct_renter(renter: Any, total: Any) -> Optional[float]:
    """Calculate renter percentage from raw Census values."""
    r, t = _safe_float(renter), _safe_float(total)
    if r is not None and t and t > 0:
        return round(r / t, 4)
    return None


# ══���═════════════════════════════════════════════════════��══════════════════
# 2. FRED — Macro Rates
# ════════���═════════════════════════════════════════��════════════════════════

_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

_FRED_SERIES = {
    "DGS10":        "dgs10_rate",
    "SOFR":         "sofr_rate",
    "MORTGAGE30US": "mortgage30_rate",
    "CPIAUCSL":     "cpi_yoy",
}


def _fetch_fred(series_id: str) -> Optional[float]:
    """Fetch the most recent observation for a FRED series."""
    params = {
        "series_id": series_id,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 12 if series_id == "CPIAUCSL" else 1,
    }
    try:
        resp = requests.get(_FRED_BASE, params=params, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        if not obs:
            return None

        if series_id == "CPIAUCSL":
            # CPI YoY: compare latest to 12 months ago
            return _cpi_yoy(obs)

        val = obs[0].get("value", ".")
        return _safe_float(val)
    except Exception as exc:
        logger.warning("FRED fetch failed (%s): %s", series_id, exc)
        return None


def _cpi_yoy(obs: List[dict]) -> Optional[float]:
    """Calculate CPI year-over-year % change from 12 monthly observations."""
    vals = []
    for o in obs:
        v = _safe_float(o.get("value", "."))
        if v is not None:
            vals.append(v)
    if len(vals) >= 12 and vals[11] > 0:
        return round((vals[0] - vals[11]) / vals[11], 4)
    if len(vals) >= 2 and vals[-1] > 0:
        return round((vals[0] - vals[-1]) / vals[-1], 4)
    return None


def _fetch_all_fred() -> Dict[str, Optional[float]]:
    """Fetch all four FRED series. Returns dict with DealData field names."""
    results: Dict[str, Optional[float]] = {}
    for series_id, field_name in _FRED_SERIES.items():
        val = _fetch_fred(series_id)
        # FRED returns rates as percentages (e.g. 4.25); convert to decimal
        if val is not None and field_name != "cpi_yoy":
            val = round(val / 100.0, 6)
        results[field_name] = val
    return results


# ═══════��═══════════════��═════════════════════════════���═════════════════════
# 3. HUD FMR �� Fair Market Rents
# ═════════════════════════════���═══════════════════════════════��═════════════

_HUD_FMR_BASE = "https://www.huduser.gov/hudapi/public/fmr/data"


def _fetch_hud_fmr(zip_code: str, hud_api_token: str) -> Optional[Dict[str, float]]:
    """Fetch Fair Market Rents by ZIP from HUD API."""
    url = f"{_HUD_FMR_BASE}/{zip_code}"
    headers = {"Authorization": f"Bearer {hud_api_token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        basicdata = data.get("basicdata", {})
        return {
            "fmr_studio": _safe_float(basicdata.get("Efficiency")),
            "fmr_1br":    _safe_float(basicdata.get("One-Bedroom")),
            "fmr_2br":    _safe_float(basicdata.get("Two-Bedroom")),
            "fmr_3br":    _safe_float(basicdata.get("Three-Bedroom")),
        }
    except Exception as exc:
        logger.warning("HUD FMR fetch failed (ZIP %s): %s", zip_code, exc)
        return None


# ═════��═══════════════════════��═════════════════════════════════��═══════════
# 4. FEMA NFHL — Flood Zone
# ═��══════════════════════════��══════════════════════════════��═══════════════

_FEMA_NFHL_URL = "https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/28/query"


def _fetch_fema_flood(lat: float, lon: float) -> Optional[Dict[str, str]]:
    """Query FEMA NFHL for flood zone at a lat/lon point."""
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,DFIRM_ID",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = requests.get(_FEMA_NFHL_URL, params=params, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        features = resp.json().get("features", [])
        if not features:
            return None
        attrs = features[0].get("attributes", {})
        return {
            "fema_flood_zone":   attrs.get("FLD_ZONE"),
            "fema_panel_number": attrs.get("DFIRM_ID"),
        }
    except Exception as exc:
        logger.warning("FEMA NFHL fetch failed (%.4f, %.4f): %s", lat, lon, exc)
        return None


# ═══���════════════════════════════════════════════════════════════���══════════
# 5. EPA EnviroFacts — Environmental Flags
# ═════════════════════════════════════════════════════════════════════���═════

_EPA_BASE = "https://enviro.epa.gov/enviro/efservice"


def _fetch_epa_flags(zip_code: str) -> List[str]:
    """
    Query EPA EnviroFacts for environmental program flags near a ZIP code.
    Returns list of program acronyms (e.g. ['RCRA', 'CERCLIS']).
    """
    programs = [
        ("RCRA", f"{_EPA_BASE}/RCR_INFO/ZIP_CODE/BEGINNING/{zip_code}/json"),
        ("CERCLIS", f"{_EPA_BASE}/SEMS_ACTIVE_SITES/ZIP_CODE/{zip_code}/json"),
        ("TRI", f"{_EPA_BASE}/TRI_FACILITY/ZIP_CODE/{zip_code}/json"),
    ]
    flags: List[str] = []
    for program_name, url in programs:
        try:
            resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    flags.append(program_name)
        except Exception as exc:
            logger.warning("EPA %s fetch failed (ZIP %s): %s", program_name, zip_code, exc)
    return flags


# ═══════════════════════════════════════════════════════════════��═══════════
# 6. MUNICIPAL REGISTRY CSV LOOKUP
# ══════════════���════════════════════════════════════════════════════════════

def _lookup_municipality(city: str, state: str) -> Optional[Dict[str, str]]:
    """
    Look up a municipality in the local CSV registry.
    Returns dict with code_platform, zoning_chapter_url, zoning_chapter_ref, etc.
    """
    if not city or not state:
        return None
    try:
        city_lower = city.strip().lower()
        state_upper = state.strip().upper()
        with open(MUNICIPAL_REGISTRY_CSV, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if (row.get("municipality_name", "").strip().lower() == city_lower
                        and row.get("state", "").strip().upper() == state_upper):
                    return row
        logger.info("Municipality not found in registry: %s, %s", city, state)
        return None
    except Exception as exc:
        logger.warning("Municipal registry lookup failed: %s", exc)
        return None


# ═════��═════════════════════════════════════════════════════════════════════
# 7. ZONING CODE SCRAPER
# ══���════════════════════════════════════════════════════════════════════════

def _scrape_zoning_code(url: str) -> Optional[str]:
    """
    Fetch the zoning code page and extract text content.
    Handles ecode360 and Municode platforms.
    Returns plain text or None.
    """
    if not url:
        return None
    try:
        headers = {
            "User-Agent": "DealDesk-CRE-Underwriting/1.0 (research; municipal code lookup)",
        }
        resp = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        html = resp.text

        # Strip HTML tags for a rough text extraction
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) < 200:
            logger.warning("Zoning code scrape returned too little text (%d chars)", len(text))
            return None

        # Truncate to ~12,000 chars to stay within Haiku token limits
        return text[:12000]
    except Exception as exc:
        logger.warning("Zoning code scrape failed (%s): %s", url, exc)
        return None


# ═════════════════════════════��══════════════════════════��══════════════════
# AI PROMPT HELPERS
# ══════���════════════════════════════════════════════���═══════════════════════

def _call_llm(model: str, system: str, user_msg: str, max_tokens: int = 4096) -> Optional[dict]:
    """Send a single Claude API call and parse the JSON response. Returns None on failure."""
    client = anthropic.Anthropic(
        api_key=st.secrets[ANTHROPIC_SECRET_KEY]["api_key"],
    )
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(raw)
    except (json.JSONDecodeError, anthropic.APIError, IndexError, KeyError) as exc:
        logger.warning("LLM call failed (%s): %s", model, exc)
        return None


def _call_llm_text(model: str, system: str, user_msg: str, max_tokens: int = 2048) -> Optional[str]:
    """Send a Claude API call expecting plain text (not JSON). Returns None on failure."""
    client = anthropic.Anthropic(
        api_key=st.secrets[ANTHROPIC_SECRET_KEY]["api_key"],
    )
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        return response.content[0].text.strip()
    except (anthropic.APIError, IndexError, KeyError) as exc:
        logger.warning("LLM text call failed (%s): %s", model, exc)
        return None


# ��═══════════════════════════════���═══════════════════════════════��══════════
# PROMPT 3A — ZONING PARAMETER EXTRACTION (Haiku)
# ══════════════════════════════════════���════════════════════════════════════

_SYSTEM_3A = (
    "You are a zoning code analyst. Extract dimensional standards, permitted uses,\n"
    "and zoning parameters from municipal code text.\n\n"
    "RULES:\n"
    "- Extract ONLY information explicitly present in the text. Return null if not found.\n"
    "- Dimensions in feet. FAR as decimal. Percentages as decimals.\n"
    "- List permitted_uses_by_right, special_exception, and prohibited separately.\n"
    "- SOURCE VERIFICATION: Compare expected_zoning_code to actual code found in text.\n"
    "  If different, set source_mismatch = true.\n"
    "Output ONLY valid JSON."
)

_USER_3A = (
    "Property: {property_address}\n"
    "Expected zoning code: {expected_zoning_code}\n"
    "Municipality: {municipality_name}, {state}\n"
    "Code platform: {code_platform} | Chapter: {chapter_reference}\n\n"
    "MUNICIPAL CODE TEXT: {zoning_code_text}\n\n"
    "Return JSON:\n"
    '{{\n'
    '  "zoning_code": null, "zoning_district_name": null,\n'
    '  "overlay_districts": [], "permitted_uses_by_right": [],\n'
    '  "permitted_uses_special_exception": [], "prohibited_uses": [],\n'
    '  "max_height_ft": null, "max_stories": null,\n'
    '  "min_lot_area_sf": null, "max_lot_coverage_pct": null, "max_far": null,\n'
    '  "front_setback_ft": null, "rear_setback_ft": null, "side_setback_ft": null,\n'
    '  "min_parking_spaces_per_unit": null, "parking_notes": null,\n'
    '  "density_notes": null,\n'
    '  "source_verification": {{"source_mismatch": false,\n'
    '                           "source_notes": null,\n'
    '                           "code_section_found": null}},\n'
    '  "extraction_notes": null\n'
    '}}'
)


def _apply_3a(data: dict, deal: DealData) -> None:
    """Map Prompt 3A response onto DealData.zoning."""
    z = deal.zoning
    z.zoning_code         = data.get("zoning_code") or z.zoning_code
    z.zoning_district     = data.get("zoning_district_name") or z.zoning_district
    z.overlay_districts   = data.get("overlay_districts") or z.overlay_districts
    z.permitted_uses      = data.get("permitted_uses_by_right") or z.permitted_uses
    z.conditional_uses    = data.get("permitted_uses_special_exception") or z.conditional_uses
    z.max_height_ft       = data.get("max_height_ft") or z.max_height_ft
    z.max_stories         = data.get("max_stories") or z.max_stories
    z.min_lot_area_sf     = data.get("min_lot_area_sf") or z.min_lot_area_sf
    z.max_lot_coverage_pct = data.get("max_lot_coverage_pct") or z.max_lot_coverage_pct
    z.max_far             = data.get("max_far") or z.max_far
    z.front_setback_ft    = data.get("front_setback_ft") or z.front_setback_ft
    z.rear_setback_ft     = data.get("rear_setback_ft") or z.rear_setback_ft
    z.side_setback_ft     = data.get("side_setback_ft") or z.side_setback_ft
    z.min_parking_spaces  = data.get("min_parking_spaces_per_unit") or z.min_parking_spaces

    # Source verification
    sv = data.get("source_verification") or {}
    z.source_verified = not sv.get("source_mismatch", False)
    z.source_notes    = sv.get("source_notes")


# ═══════════════════════════��═══════════════════════════════════════════════
# PROMPT 3B — BUILDABLE CAPACITY ANALYSIS (Sonnet)
# ══════��══════════════════���═════════════════════════════════════════════════

_SYSTEM_3B = (
    "You are a commercial real estate development analyst specializing in zoning capacity.\n"
    "Calculate maximum buildable development capacity from zoning parameters and parcel data.\n\n"
    "RULES:\n"
    "- Show calculation methodology in calculation_notes.\n"
    "- Calculate under CURRENT zoning only — no rezoning speculation.\n"
    "- Identify the binding constraint when multiple standards apply.\n"
    "- Return null with explanation if data is insufficient to calculate.\n"
    "Output ONLY valid JSON."
)

_USER_3B = (
    "Property: {property_address} | Asset type: {asset_type} | Strategy: {investment_strategy}\n"
    "Lot SF: {lot_sf} | Building SF: {building_sf} | Current units: {current_units}\n"
    "Zoning: {zoning_json}\n\n"
    "Return JSON:\n"
    '{{\n'
    '  "max_units_by_right": null, "max_buildable_sf": null,\n'
    '  "max_buildable_stories": null, "binding_constraint": null,\n'
    '  "binding_constraint_explanation": null, "units_per_acre": null,\n'
    '  "current_units_vs_max": null, "existing_nonconformities": [],\n'
    '  "variance_required_for_proposed_use": null,\n'
    '  "special_exception_required": null,\n'
    '  "calculation_notes": null, "data_gaps": []\n'
    '}}'
)


def _apply_3b(data: dict, deal: DealData) -> None:
    """Map Prompt 3B response onto DealData.zoning capacity fields."""
    z = deal.zoning
    z.max_buildable_units = data.get("max_units_by_right") or z.max_buildable_units
    z.max_buildable_sf    = data.get("max_buildable_sf") or z.max_buildable_sf
    z.buildable_capacity_narrative = data.get("calculation_notes") or z.buildable_capacity_narrative


# ═════��═════════════��══════════════════════════════════���════════════════════
# PROMPT 3C — HIGHEST & BEST USE OPINION (Sonnet)
# ═══��══════════════════════════════════════════════════════════════════���════

_SYSTEM_3C = (
    "You are a licensed MAI appraiser writing a highest and best use analysis for\n"
    "a formal investment underwriting report.\n\n"
    "Address all four HBU tests:\n"
    "  1. Legally permissible: What does current zoning allow?\n"
    "  2. Physically possible: What can the site support?\n"
    "  3. Financially feasible: What uses are economically viable?\n"
    "  4. Maximally productive: Which use generates the highest value?\n\n"
    "RULES:\n"
    "- Write in formal MAI appraisal report language. State conclusions directly.\n"
    "- Base all conclusions on data provided. No speculation beyond the data.\n"
    "- Acknowledge data limitations. Length: 3–4 paragraphs."
)

_USER_3C = (
    "Property: {property_address} | Asset type: {asset_type}\n"
    "Current use: {current_use} | Strategy: {investment_strategy}\n"
    "Zoning: {zoning_json}\n"
    "Buildable capacity: {buildable_capacity_json}\n"
    "Market context: {market_context_summary}\n\n"
    "Return JSON:\n"
    '{{\n'
    '  "hbu_conclusion": "AS VACANT: [x] / AS IMPROVED: [x]",\n'
    '  "legally_permissible": null, "physically_possible": null,\n'
    '  "financially_feasible": null, "maximally_productive": null,\n'
    '  "hbu_narrative": null, "alternative_uses_considered": [],\n'
    '  "confidence_level": "high|medium|low", "confidence_notes": null\n'
    '}}'
)


def _apply_3c(data: dict, deal: DealData) -> None:
    """Map Prompt 3C response onto DealData.zoning HBU fields."""
    z = deal.zoning
    z.hbu_narrative  = data.get("hbu_narrative") or z.hbu_narrative
    z.hbu_conclusion = data.get("hbu_conclusion") or z.hbu_conclusion

    # Also store in narratives for 4-MASTER consumption
    deal.narratives.buildable_capacity = z.buildable_capacity_narrative
    deal.narratives.highest_best_use   = z.hbu_narrative


# ════��════════════════════════���═══════════════════════════════���═════════════
# PROMPT 5B — DEBT MARKET SNAPSHOT NARRATIVE (Sonnet)
# ══════════════════════════════��════════════════════════════════════════════

_SYSTEM_5B = (
    "You are a senior CRE debt analyst writing a market context paragraph for a\n"
    "formal investment underwriting report.\n\n"
    "RULES:\n"
    "- Exactly one paragraph. No headers, no bullets.\n"
    "- Cover: (a) current rate environment — always name 10-yr Treasury. Reference SOFR\n"
    "  only if floating-rate or construction-to-perm loan. (b) proposed rate vs. market.\n"
    "  (c) DSCR trajectory and refinance risk over hold period.\n"
    "  (d) one sentence on CPI vs. underwritten expense growth assumption.\n"
    "- Do not recommend whether to proceed. State facts and implications only.\n"
    "- If a FRED field is \"data unavailable\": acknowledge and work around it.\n"
    "- Tone: Precise, institutional, neutral. Length: 100–150 words. Output plain text only."
)

_USER_5B = (
    "Property: {property_address} | Asset: {asset_type} | Hold: {hold_period} yrs\n"
    "Data pull date: {data_pull_date}\n"
    "Underwritten expense growth: {expense_growth_rate}%\n\n"
    "FRED live data:\n"
    "  10-yr Treasury (DGS10): {dgs10_rate}% | SOFR: {sofr_rate}%\n"
    "  30-yr mortgage: {mortgage30_rate}% | CPI YoY: {cpi_yoy}%\n\n"
    "Deal debt structure:\n"
    "  Type: {loan_type} | Amount: ${loan_amount} | Rate: {loan_rate}%\n"
    "  Rate type: {rate_type} | LTV: {ltv}% | DSCR Yr1: {dscr_yr1}x\n"
    "  Amort: {amortization} yrs | Term: {loan_term} yrs\n\n"
    "Write the debt market context paragraph now. Output plain text only."
)


# ═════��══════════════════════��══════════════════════════════════════════════
# NUMERIC HELPERS
# ═════��════════════════════════════════════════════��════════════════════════

def _safe_int(val: Any) -> Optional[int]:
    """Convert to int, returning None on failure or Census suppression codes."""
    try:
        v = int(val)
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None


def _safe_float(val: Any) -> Optional[float]:
    """Convert to float, returning None on failure or missing indicators."""
    if val is None or val == "." or val == "":
        return None
    try:
        v = float(val)
        return v if v >= -999999 else None  # Census uses large negatives for suppressed
    except (TypeError, ValueError):
        return None


def _fmt_rate(val: Optional[float]) -> str:
    """Format a decimal rate as a percentage string for prompt injection, or 'data unavailable'."""
    if val is None:
        return "data unavailable"
    return f"{val * 100:.2f}"


# ═══════════════════════════════════════════════════════════════════════════
# MARKET CONTEXT SUMMARY (for Prompt 3C)
# ═��═══════════════════════════════════════════���═════════════════════════════

def _build_market_context(md: MarketData) -> str:
    """Build a concise market context string for Prompt 3C input."""
    parts = []
    if md.population_3mi:
        parts.append(f"Population (3mi): {md.population_3mi:,}")
    if md.median_hh_income_3mi:
        parts.append(f"Median HH Income (3mi): ${md.median_hh_income_3mi:,.0f}")
    if md.pct_renter_occ_3mi:
        parts.append(f"Renter %: {md.pct_renter_occ_3mi:.1%}")
    if md.fmr_2br:
        parts.append(f"FMR 2BR: ${md.fmr_2br:,.0f}")
    if md.unemployment_rate:
        parts.append(f"Unemployment: {md.unemployment_rate:.1%}")
    return " | ".join(parts) if parts else "Limited market data available"


# ═════════════���═════════════════════════════════════════════════════════════
# PUBLIC API
# ════════════════════════════════════════════════════════════��══════════════

def enrich_market_data(deal: DealData) -> DealData:
    """
    Master market enrichment function — populates DealData with external
    market data, zoning analysis, and debt market narrative.

    Runs all API calls and AI prompts. Any failure logs a warning and
    continues — the pipeline never crashes here.

    Args:
        deal: DealData with address, asset_type, assumptions already set.

    Returns:
        The same DealData object, enriched with market_data, zoning, and narratives.
    """
    md = deal.market_data
    addr = deal.address
    assumptions = deal.assumptions
    data_pull_date = datetime.utcnow().strftime("%Y-%m-%d")

    # ── 1. Census ACS Demographics ──────��─────────────────────────
    logger.info("Fetching Census ACS data...")
    census = None
    if addr.census_tract and addr.fips_code and len(addr.fips_code) >= 5:
        state_fips = addr.fips_code[:2]
        county_fips = addr.fips_code[2:5]
        census = _fetch_census_tract(state_fips, county_fips, addr.census_tract)

    if not census and addr.zip_code:
        census = _fetch_census_by_zip(addr.zip_code)

    if census:
        md.population_3mi       = census.get("population") or md.population_3mi
        md.median_hh_income_3mi = census.get("median_hh_income") or md.median_hh_income_3mi
        md.pct_renter_occ_3mi   = census.get("pct_renter_occupied") or md.pct_renter_occ_3mi
        deal.provenance.field_sources["census_demographics"] = "census_acs_2022"
        logger.info("Census ACS: pop=%s, income=%s", md.population_3mi, md.median_hh_income_3mi)
    else:
        logger.warning("Census ACS: no data retrieved")

    # ── 2. FRED Macro Rates ───────────────────────────────────────
    logger.info("Fetching FRED macro rates...")
    fred_data = _fetch_all_fred()
    md.dgs10_rate      = fred_data.get("dgs10_rate") or md.dgs10_rate
    md.sofr_rate       = fred_data.get("sofr_rate") or md.sofr_rate
    md.mortgage30_rate = fred_data.get("mortgage30_rate") or md.mortgage30_rate
    md.cpi_yoy         = fred_data.get("cpi_yoy") or md.cpi_yoy
    deal.provenance.field_sources["fred_rates"] = f"fred_{data_pull_date}"
    deal.provenance.fred_pull_date = data_pull_date
    logger.info("FRED: DGS10=%s, SOFR=%s, MTG30=%s, CPI=%s",
                 md.dgs10_rate, md.sofr_rate, md.mortgage30_rate, md.cpi_yoy)

    # ─��� 3. HUD Fair Market Rents ─────��────────────────────────────
    logger.info("Fetching HUD Fair Market Rents...")
    hud_token = _get_secret("hud", "api_token")
    if hud_token and addr.zip_code:
        fmr = _fetch_hud_fmr(addr.zip_code, hud_token)
        if fmr:
            md.fmr_studio = fmr.get("fmr_studio") or md.fmr_studio
            md.fmr_1br    = fmr.get("fmr_1br") or md.fmr_1br
            md.fmr_2br    = fmr.get("fmr_2br") or md.fmr_2br
            md.fmr_3br    = fmr.get("fmr_3br") or md.fmr_3br
            deal.provenance.field_sources["hud_fmr"] = f"hud_fmr_{data_pull_date}"
            logger.info("HUD FMR: studio=%s, 1BR=%s, 2BR=%s, 3BR=%s",
                         md.fmr_studio, md.fmr_1br, md.fmr_2br, md.fmr_3br)
    else:
        logger.warning("HUD FMR: missing API token or ZIP — skipping")

    # ── 4. FEMA Flood Zone ────��───────────────────────────────────
    logger.info("Fetching FEMA flood zone...")
    if addr.latitude and addr.longitude:
        fema = _fetch_fema_flood(addr.latitude, addr.longitude)
        if fema:
            md.fema_flood_zone   = fema.get("fema_flood_zone") or md.fema_flood_zone
            md.fema_panel_number = fema.get("fema_panel_number") or md.fema_panel_number
            deal.provenance.field_sources["fema_flood"] = f"fema_nfhl_{data_pull_date}"
            logger.info("FEMA: zone=%s, panel=%s", md.fema_flood_zone, md.fema_panel_number)
    else:
        logger.warning("FEMA: no lat/lon — skipping flood zone lookup")

    # ─�� 5. EPA Environmental Flags ───────��────────────────────────
    logger.info("Fetching EPA environmental flags...")
    if addr.zip_code:
        epa_flags = _fetch_epa_flags(addr.zip_code)
        if epa_flags:
            md.epa_env_flags = epa_flags
            deal.provenance.field_sources["epa_flags"] = f"epa_envirofacts_{data_pull_date}"
            logger.info("EPA: flags=%s", epa_flags)
    else:
        logger.warning("EPA: no ZIP code — skipping")

    # ── 6. Data pull date ��────────────────────────────────────────
    md.data_pull_date = data_pull_date

    # ── 7. Municipal Registry Lookup + Zoning Scrape ──────────────
    logger.info("Looking up municipal registry...")
    muni = _lookup_municipality(addr.city, addr.state)
    zoning_code_text = None

    if muni:
        deal.zoning.municipal_code_url   = muni.get("zoning_chapter_url") or muni.get("code_base_url")
        deal.zoning.zoning_code_chapter  = muni.get("zoning_chapter_ref")

        scrape_url = muni.get("zoning_chapter_url") or muni.get("code_base_url")
        if scrape_url:
            logger.info("Scraping zoning code from %s...", scrape_url)
            zoning_code_text = _scrape_zoning_code(scrape_url)
            if zoning_code_text:
                deal.provenance.field_sources["zoning_scrape"] = scrape_url
    else:
        logger.info("Municipality not in registry — zoning prompts will have limited data")

    # ── 8. Prompt 3A — Zoning Parameter Extraction (Haiku) ────────
    if zoning_code_text:
        logger.info("Running Prompt 3A — Zoning Parameter Extraction...")
        user_msg = _USER_3A.format(
            property_address=addr.full_address,
            expected_zoning_code=deal.zoning.zoning_code or "unknown",
            municipality_name=addr.city or "unknown",
            state=addr.state or "unknown",
            code_platform=muni.get("code_platform", "unknown") if muni else "unknown",
            chapter_reference=deal.zoning.zoning_code_chapter or "unknown",
            zoning_code_text=zoning_code_text,
        )
        result = _call_llm(MODEL_HAIKU, _SYSTEM_3A, user_msg)
        if result:
            _apply_3a(result, deal)
            logger.info("Prompt 3A complete — zoning parameters extracted")
        else:
            logger.warning("Prompt 3A failed — continuing with existing zoning data")
    else:
        logger.info("Skipping Prompt 3A — no zoning code text available")

    # ── 9. Prompt 3B — Buildable Capacity Analysis (Sonnet) ───────
    logger.info("Running Prompt 3B — Buildable Capacity Analysis...")
    zoning_json = deal.zoning.model_dump_json(indent=2)
    user_msg = _USER_3B.format(
        property_address=addr.full_address,
        asset_type=deal.asset_type.value,
        investment_strategy=deal.investment_strategy.value,
        lot_sf=assumptions.lot_sf or "unknown",
        building_sf=assumptions.gba_sf or "unknown",
        current_units=assumptions.num_units or "unknown",
        zoning_json=zoning_json,
    )
    result = _call_llm(MODEL_SONNET, _SYSTEM_3B, user_msg)
    if result:
        _apply_3b(result, deal)
        logger.info("Prompt 3B complete — buildable capacity analyzed")
    else:
        logger.warning("Prompt 3B failed — continuing without buildable capacity")

    # ── 10. Prompt 3C — Highest & Best Use (Sonnet) ────────────��──
    logger.info("Running Prompt 3C — Highest & Best Use Analysis...")
    buildable_json = json.dumps({
        "max_buildable_units": deal.zoning.max_buildable_units,
        "max_buildable_sf":    deal.zoning.max_buildable_sf,
        "capacity_narrative":  deal.zoning.buildable_capacity_narrative,
    })
    market_context = _build_market_context(md)
    user_msg = _USER_3C.format(
        property_address=addr.full_address,
        asset_type=deal.asset_type.value,
        current_use=deal.asset_type.value,
        investment_strategy=deal.investment_strategy.value,
        zoning_json=zoning_json,
        buildable_capacity_json=buildable_json,
        market_context_summary=market_context,
    )
    result = _call_llm(MODEL_SONNET, _SYSTEM_3C, user_msg)
    if result:
        _apply_3c(result, deal)
        logger.info("Prompt 3C complete — HBU analysis written")
    else:
        logger.warning("Prompt 3C failed — continuing without HBU")

    # ── 11. Prompt 5B — Debt Market Snapshot (Sonnet) ─────────────
    logger.info("Running Prompt 5B — Debt Market Snapshot...")
    loan_amount = assumptions.purchase_price * assumptions.ltv_pct
    user_msg = _USER_5B.format(
        loan_type=getattr(assumptions, "loan_type", None) or "Permanent",
        rate_type=getattr(assumptions, "rate_type", None) or "fixed",
        property_address=addr.full_address,
        asset_type=deal.asset_type.value,
        hold_period=assumptions.hold_period,
        data_pull_date=data_pull_date,
        expense_growth_rate=f"{assumptions.expense_growth_rate * 100:.1f}",
        dgs10_rate=_fmt_rate(md.dgs10_rate),
        sofr_rate=_fmt_rate(md.sofr_rate),
        mortgage30_rate=_fmt_rate(md.mortgage30_rate),
        cpi_yoy=_fmt_rate(md.cpi_yoy),
        loan_amount=f"{loan_amount:,.0f}",
        loan_rate=f"{assumptions.interest_rate * 100:.2f}",
        ltv=f"{assumptions.ltv_pct * 100:.0f}",
        dscr_yr1="TBD",
        amortization=assumptions.amort_years,
        loan_term=assumptions.loan_term,
    )
    narrative = _call_llm_text(MODEL_SONNET, _SYSTEM_5B, user_msg)
    if narrative:
        md.debt_market_narrative = narrative
        deal.narratives.debt_market_narrative = narrative
        logger.info("Prompt 5B complete — debt market narrative written")
    else:
        logger.warning("Prompt 5B failed — continuing without debt market narrative")

    logger.info("Market data enrichment complete for %s", deal.deal_id)
    return deal


# ═══════════════════════════════════════════════════════���═══════════════════
# SECRETS HELPER
# ═══════════════════════════════════════════════════════════════════════════

def _get_secret(section: str, key: str) -> Optional[str]:
    """Safely retrieve a secret from st.secrets, returning None if not configured."""
    try:
        return st.secrets[section][key]
    except (KeyError, FileNotFoundError):
        return None
