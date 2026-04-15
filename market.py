"""
market.py — Market Data & Zoning Analysis Module
=================================================
Enriches DealData with external market data, zoning analysis, and debt market context.

Pipeline position: called after deal_data.py, before risk.py.

Steps run IN ORDER:
    1. Municipal Registry Lookup  (local CSV via pandas)
    2. Census Geocoder            (tract + Opportunity Zone)
    3. Census ACS API             (demographics)
    4. FRED API                   (interest rates)
    5. Prompt 5B                  (Debt Market Snapshot Narrative — Sonnet)
    6. HUD Fair Market Rents      (by county FIPS)
    7. FEMA Flood Zone            (by lat/lon)
    8–10. Zoning code scrape + Prompts 3A / 3B / 3C

Every external call is wrapped in try/except — failures log warnings and
return None, never crash the pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from io import StringIO
from typing import Any, Dict, List, Optional

import anthropic
import pandas as pd
import requests

from config import (
    MODEL_HAIKU,
    MODEL_SONNET,
    MUNICIPAL_REGISTRY_CSV,
)
from models.models import DealData, MarketData, ZoningData

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30  # seconds for all HTTP calls


# ═══════════════════════════════════════════════════════════════════════════
# US STATE FIPS MAPPING
# ═══════════════════════════════════════════════════════════════════════════

STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06",
    "CO": "08", "CT": "09", "DE": "10", "DC": "11", "FL": "12",
    "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18",
    "IA": "19", "KS": "20", "KY": "21", "LA": "22", "ME": "23",
    "MD": "24", "MA": "25", "MI": "26", "MN": "27", "MS": "28",
    "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38",
    "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44",
    "SC": "45", "SD": "46", "TN": "47", "TX": "48", "UT": "49",
    "VT": "50", "VA": "51", "WA": "53", "WV": "54", "WI": "55",
    "WY": "56",
}

NEW_ENGLAND_STATES = {"CT", "MA", "RI"}


# ═══════════════════════════════════════════════════════════════════════════
# NUMERIC HELPERS
# ═══════════════════════════════════════════════════════════════════════════

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
        return v if v >= -999999 else None
    except (TypeError, ValueError):
        return None


def _fmt_rate(val: Optional[float]) -> str:
    """Format a decimal rate as a percentage string for prompt injection, or 'data unavailable'."""
    if val is None:
        return "data unavailable"
    return f"{val * 100:.2f}"


# ═══════════════════════════════════════════════════════════════════════════
# NAME-MATCHING HELPERS (from registry_acs_enricher.py)
# ═══════════════════════════════════════════════════════════════════════════

def _clean_name(raw: str) -> str:
    """Strip state/county suffix from Census name. 'Abington township, ...' -> 'abington township'"""
    return re.sub(r",.*$", "", raw).strip().lower()


def _norm(name: str) -> str:
    """Strip common type suffixes for fuzzy name matching."""
    n = str(name).lower().strip()
    for suffix in [" township", " borough", " city", " town", " village",
                   " county", " municipality", " cdp", " (balance)", " (pt.)"]:
        if n.endswith(suffix):
            return n[:-len(suffix)].strip()
    return n


def _match_acs(mname: str, mtype: str, state: str,
               places: dict, subdivisions: dict) -> dict:
    """
    Tiered ACS lookup for a municipality name.
    Mirrors the proven match logic from registry_acs_enricher.py.
    Returns dict with keys from the ACS data dicts (any may be None).
    """
    empty = {k: None for k in ["population", "median_hh_income", "median_gross_rent",
                                "owner_occupied", "renter_occupied",
                                "unemployed", "labor_force", "fips_place"]}

    name_lc = str(mname).lower().strip()
    mtype_lc = str(mtype).lower() if mtype else ""
    state_uc = str(state).upper().strip()

    if "(unincorporated)" in name_lc:
        name_lc = name_lc.replace("(unincorporated)", "").strip()

    norm = _norm(name_lc)

    # 1. Exact match in places
    if name_lc in places:
        return places[name_lc]

    # 2. Township / town / borough / village -> subdivisions first
    if any(t in mtype_lc for t in ["township", "town", "borough", "village"]):
        for key in [name_lc, norm]:
            if key in subdivisions:
                return subdivisions[key]

    # 3. New England towns (MA / CT / RI) — plain name against subdivisions
    if state_uc in NEW_ENGLAND_STATES:
        for key in [name_lc, norm, name_lc + " town", norm + " town"]:
            if key in subdivisions:
                return subdivisions[key]

    # 4. Normalized name across both dicts
    for lookup in [places, subdivisions]:
        if norm in lookup:
            return lookup[norm]

    # 5. Partial match in places
    for key, d in places.items():
        if key.startswith(norm + " ") or key == norm:
            return d

    return empty


# ═══════════════════════════════════════════════════════════════════════════
# STEP 1 — MUNICIPAL REGISTRY LOOKUP
# ═══════════════════════════════════════════════════════════════════════════

def _lookup_municipal_registry(deal: DealData) -> Optional[pd.Series]:
    """
    Load municipal_registry.csv and match by:
      primary key:  fips_county
      fallback key: municipality_name + state (case-insensitive exact match)

    Returns the matched row as a pandas Series, or None.
    """
    try:
        df = pd.read_csv(MUNICIPAL_REGISTRY_CSV, dtype=str)
    except Exception as exc:
        logger.warning("Failed to load municipal registry: %s", exc)
        return None

    fips_county = deal.address.fips_code
    city = deal.address.city
    state = deal.address.state

    # Primary key: fips_county
    if fips_county:
        fips_match = df[df["fips_county"].str.strip() == fips_county.strip()]
        if len(fips_match) > 0:
            logger.info("Municipal registry: matched by fips_county=%s", fips_county)
            return fips_match.iloc[0]

    # Fallback: municipality_name + state (case-insensitive)
    if city and state:
        city_lower = city.strip().lower()
        state_upper = state.strip().upper()
        mask = (
            df["municipality_name"].str.strip().str.lower() == city_lower
        ) & (
            df["state"].str.strip().str.upper() == state_upper
        )
        name_match = df[mask]
        if len(name_match) > 0:
            logger.info("Municipal registry: matched by name=%s, state=%s", city, state)
            return name_match.iloc[0]

    logger.warning("Municipal registry: no match for fips=%s, city=%s, state=%s",
                    fips_county, city, state)
    return None


def _apply_registry(row: pd.Series, deal: DealData) -> None:
    """Write matched registry fields to DealData."""

    def _get(field: str) -> Optional[str]:
        val = row.get(field)
        if pd.isna(val) or str(val).strip() == "":
            return None
        return str(val).strip()

    # Zoning URLs
    deal.zoning.municipal_code_url = _get("zoning_chapter_url") or _get("code_base_url")
    deal.zoning.zoning_code_chapter = _get("zoning_chapter_ref")

    # Store additional registry fields in provenance for downstream use
    prov = deal.provenance.field_sources
    for field in ["code_platform", "code_base_url", "zoning_chapter_url",
                  "assessor_url", "gis_parcel_url", "recorder_of_deeds_url",
                  "tax_collector_url"]:
        val = _get(field)
        if val:
            prov[field] = val

    # Population (write to market_data if available)
    pop = _safe_int(_get("population"))
    if pop:
        deal.market_data.population_3mi = pop
        prov["population_source"] = "municipal_registry"

    # Median household income
    income = _safe_float(_get("median_household_income"))
    if income:
        deal.market_data.median_hh_income_3mi = income
        prov["median_hh_income_source"] = "municipal_registry"

    # Median gross rent — store in provenance (no direct model field)
    rent = _safe_float(_get("median_gross_rent"))
    if rent:
        prov["median_gross_rent"] = str(rent)

    # School district
    sd = _get("school_district")
    if sd:
        prov["school_district"] = sd

    # FIPS place
    fp = _get("fips_place")
    if fp:
        prov["fips_place"] = fp

    prov["municipal_registry"] = "matched"


# ═══════════════════════════════════════════════════════════════════════════
# STEP 2 — CENSUS GEOCODER (tract + OZ lookup)
# ═══════════════════════════════════════════════════════════════════════════

_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/geographies/address"

# Module-level OZ cache (downloaded once per session)
_oz_tracts_cache: Optional[set] = None

_HUD_OZ_URL = (
    "https://hudgis-hud.opendata.arcgis.com/api/download/v1/items/"
    "ef143299845841f8abb95969c01f88b5/csv?layers=13"
)


def _normalize_address_for_geocoding(raw_address: str) -> str:
    """Normalize range addresses and non-standard formats for geocoding.

    '2-8 s. 46th street, 19139' → '2 S 46th Street, Philadelphia, PA 19139'
    """
    addr = raw_address.strip()
    # Remove range portion: "2-8 S 46th" → "2 S 46th"
    addr = re.sub(r'^(\d+)\s*[-–]\s*\d+\s+', r'\1 ', addr)
    # Expand abbreviations: S→South, N→North, E→East, W→West
    addr = re.sub(r'\bS\.?\s+', 'South ', addr)
    addr = re.sub(r'\bN\.?\s+', 'North ', addr)
    addr = re.sub(r'\bE\.?\s+', 'East ', addr)
    addr = re.sub(r'\bW\.?\s+', 'West ', addr)
    # Expand "St" at end → "Street" (but not "St" in a name like "St. Louis")
    addr = re.sub(r'\b[Ss]t\.?$', 'Street', addr)
    addr = re.sub(r'\b[Ss]treet,', 'Street,', addr)
    # If no city/state, append Philadelphia PA
    if ',' not in addr:
        addr += ', Philadelphia, PA'
    elif addr.count(',') == 1 and re.search(r'\d{5}', addr):
        # Has zip but no city: "2 South 46th Street, 19139" → add Philadelphia PA
        addr = re.sub(r',\s*(\d{5})', r', Philadelphia, PA \1', addr)
    if addr != raw_address:
        logger.info("GEOCODE: normalized '%s' → '%s'", raw_address, addr)
    return addr


def _geocode_fallback(addr, full_address: str) -> None:
    """Try Google Maps geocoding; if unavailable use Philadelphia centroid."""
    import os
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if api_key:
        try:
            import urllib.parse
            encoded = urllib.parse.quote(full_address)
            geo_url = (
                "https://maps.googleapis.com/maps/api/geocode/json"
                f"?address={encoded}&key={api_key}"
            )
            resp = requests.get(geo_url, timeout=10)
            results = resp.json().get("results", [])
            if results:
                loc = results[0]["geometry"]["location"]
                addr.latitude = loc["lat"]
                addr.longitude = loc["lng"]
                logger.info(
                    "GEOCODE fallback (Google Maps): lat=%.6f, lon=%.6f for '%s'",
                    addr.latitude, addr.longitude, full_address)
                return
            else:
                logger.warning("GEOCODE fallback (Google Maps): no results for '%s'", full_address)
        except Exception as e:
            logger.warning("GEOCODE fallback (Google Maps) failed: %s", e)
    if addr.latitude is None or addr.longitude is None:
        addr.latitude = 39.9526
        addr.longitude = -75.1652
        logger.warning(
            "GEOCODE fallback: Philadelphia centroid used for '%s' — maps will be inaccurate",
            full_address)


def _census_geocode(deal: DealData) -> None:
    """
    Call the Census Bureau Geocoder to get census tract GEOID,
    school district, and lat/lon from the property address.
    Then check Opportunity Zone status.
    """
    addr = deal.address
    full = addr.full_address
    if not full:
        logger.warning("Census Geocoder: no address — skipping")
        return

    full = _normalize_address_for_geocoding(full)
    logger.info("Census Geocoder: geocoding address '%s'", full)

    # Parse address components for the geocoder
    params = {
        "address": full,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "format": "json",
    }

    lat = None
    lon = None

    try:
        resp = requests.get(_GEOCODER_URL, params=params, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Census Geocoder FAILED for '%s': %s", full, exc)
        _geocode_fallback(addr, full)
        return

    matches = data.get("result", {}).get("addressMatches", [])
    if not matches:
        logger.warning("Census Geocoder: no address matches for '%s'", full)
        _geocode_fallback(addr, full)
        return

    match = matches[0]

    # Extract coordinates
    coords = match.get("coordinates", {})
    lat = _safe_float(coords.get("y"))
    lon = _safe_float(coords.get("x"))
    if lat is not None:
        addr.latitude = lat
        logger.info("Census Geocoder: lat=%.6f, lon=%.6f for '%s'", lat, lon or 0, full)
    if lon is not None:
        addr.longitude = lon
    if lat is None or lon is None:
        logger.warning("Census Geocoder: coordinates missing for '%s'", full)
        _geocode_fallback(addr, full)

    # Extract census tract GEOID
    geos = match.get("geographies", {})
    tracts = geos.get("Census Tracts", [])
    if tracts:
        geoid = tracts[0].get("GEOID", "")
        if geoid:
            addr.census_tract = geoid
            logger.info("Census Geocoder: tract=%s, lat=%.4f, lon=%.4f",
                        geoid, lat or 0, lon or 0)

    # Extract school district (only if not already set from registry)
    if not deal.provenance.field_sources.get("school_district"):
        unified_sds = geos.get("Unified School Districts", [])
        if unified_sds:
            sd_name = unified_sds[0].get("NAME", "")
            if sd_name:
                deal.provenance.field_sources["school_district"] = sd_name

    # Extract FIPS code from matched address if not already set
    if not addr.fips_code:
        address_components = match.get("addressComponents", {})
        state_fips = tracts[0].get("STATE", "") if tracts else ""
        county_fips = tracts[0].get("COUNTY", "") if tracts else ""
        if state_fips and county_fips:
            addr.fips_code = state_fips + county_fips

    # Opportunity Zone check
    _check_opportunity_zone(deal)

    deal.provenance.field_sources["census_geocoder"] = "census_geocoder_current"


def _load_oz_tracts() -> set:
    """Download the HUD Opportunity Zone tract list (cached per session)."""
    global _oz_tracts_cache
    if _oz_tracts_cache is not None:
        return _oz_tracts_cache

    logger.info("Downloading HUD Opportunity Zone tract list...")
    try:
        resp = requests.get(_HUD_OZ_URL, timeout=60)
        resp.raise_for_status()
        df = pd.read_csv(StringIO(resp.text), dtype=str)

        # Find GEOID column
        geoid_col = None
        for candidate in ["GEOID", "geoid", "TRACTCE", "GEOID10", "GEOID20", "tract_geoid"]:
            if candidate in df.columns:
                geoid_col = candidate
                break
        if geoid_col is None:
            for col in df.columns:
                sample = df[col].dropna().iloc[0] if df[col].notna().any() else ""
                if re.match(r"^\d{11}$", str(sample)):
                    geoid_col = col
                    break

        if geoid_col:
            _oz_tracts_cache = set(df[geoid_col].dropna().str.strip().str.zfill(11).tolist())
            logger.info("OZ tracts loaded: %d", len(_oz_tracts_cache))
        else:
            logger.warning("OZ dataset: could not identify GEOID column")
            _oz_tracts_cache = set()
    except Exception as exc:
        logger.warning("OZ tract download failed: %s", exc)
        _oz_tracts_cache = set()

    return _oz_tracts_cache


def _check_opportunity_zone(deal: DealData) -> None:
    """Check if the deal's census tract is in an Opportunity Zone."""
    tract = deal.address.census_tract
    if not tract:
        return

    oz_tracts = _load_oz_tracts()
    if not oz_tracts:
        return

    ct = str(tract).strip().zfill(11)
    is_oz = ct in oz_tracts
    deal.provenance.field_sources["opportunity_zone"] = str(is_oz)
    logger.info("Opportunity Zone: tract=%s, is_oz=%s", ct, is_oz)


# ═══════════════════════════════════════════════════════════════════════════
# STEP 3 — CENSUS ACS API (demographics)
# ═══════════════════════════════════════════════════════════════════════════

_ACS_BASE = "https://api.census.gov/data/2022/acs/acs5"

_ACS_VARS = "NAME,B01003_001E,B19013_001E,B25064_001E,B25003_002E,B25003_003E,B23025_005E,B23025_003E"


def _fetch_acs_places(state_fips: str) -> dict:
    """Fetch ACS demographics for all places in a state. Returns dict keyed by cleaned name."""
    try:
        resp = requests.get(_ACS_BASE, params={
            "get": _ACS_VARS,
            "for": "place:*",
            "in": f"state:{state_fips}",
        }, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("ACS places fetch failed (state %s): %s", state_fips, exc)
        return {}

    result = {}
    for row in data[1:]:  # skip header
        name = _clean_name(row[0])
        result[name] = {
            "population":      _safe_int(row[1]),
            "median_hh_income": _safe_float(row[2]),
            "median_gross_rent": _safe_float(row[3]),
            "owner_occupied":  _safe_int(row[4]),
            "renter_occupied": _safe_int(row[5]),
            "unemployed":      _safe_int(row[6]),
            "labor_force":     _safe_int(row[7]),
            "fips_place":      row[8] + row[9],  # state + place
        }
    return result


def _fetch_acs_subdivisions(state_fips: str) -> dict:
    """Fetch ACS demographics for all county subdivisions in a state."""
    try:
        resp = requests.get(_ACS_BASE, params={
            "get": _ACS_VARS,
            "for": "county subdivision:*",
            "in": f"state:{state_fips}",
        }, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("ACS subdivisions fetch failed (state %s): %s", state_fips, exc)
        return {}

    result = {}
    for row in data[1:]:
        name = _clean_name(row[0])
        result[name] = {
            "population":      _safe_int(row[1]),
            "median_hh_income": _safe_float(row[2]),
            "median_gross_rent": _safe_float(row[3]),
            "owner_occupied":  _safe_int(row[4]),
            "renter_occupied": _safe_int(row[5]),
            "unemployed":      _safe_int(row[6]),
            "labor_force":     _safe_int(row[7]),
            "fips_place":      None,
        }
    return result


def _apply_acs(deal: DealData, acs: dict) -> None:
    """Write ACS demographic data to DealData. Only overwrites None fields."""
    md = deal.market_data
    prov = deal.provenance.field_sources

    # Population — only write if still None
    pop = acs.get("population")
    if pop and md.population_3mi is None:
        md.population_3mi = pop
        prov["population_source"] = "census_acs_2022"

    # Median household income — only if still None
    income = acs.get("median_hh_income")
    if income and md.median_hh_income_3mi is None:
        md.median_hh_income_3mi = income
        prov["median_hh_income_source"] = "census_acs_2022"

    # Median gross rent — only if still None
    rent = acs.get("median_gross_rent")
    if rent and not prov.get("median_gross_rent"):
        prov["median_gross_rent"] = str(rent)

    # Derived rates
    owner = acs.get("owner_occupied")
    renter = acs.get("renter_occupied")
    if owner is not None and renter is not None and (owner + renter) > 0:
        total_occ = owner + renter
        owner_rate = round(owner / total_occ, 4)
        renter_rate = round(renter / total_occ, 4)
        prov["owner_occupancy_rate"] = str(owner_rate)
        if md.pct_renter_occ_3mi is None:
            md.pct_renter_occ_3mi = renter_rate
            prov["renter_occupancy_source"] = "census_acs_2022"

    unemployed = acs.get("unemployed")
    labor_force = acs.get("labor_force")
    if unemployed is not None and labor_force is not None and labor_force > 0:
        unemp_rate = round(unemployed / labor_force, 4)
        if md.unemployment_rate is None:
            md.unemployment_rate = unemp_rate
            prov["unemployment_source"] = "census_acs_2022"

    # FIPS place
    fp = acs.get("fips_place")
    if fp and not prov.get("fips_place"):
        prov["fips_place"] = fp

    prov["census_demographics"] = "census_acs_2022"


def _fetch_acs_demographics(deal: DealData) -> None:
    """
    Step 3: Fetch Census ACS demographics for the deal's municipality.
    Uses state + place/subdivision matching from registry_acs_enricher.py.
    """
    state = deal.address.state
    city = deal.address.city
    if not state or not city:
        logger.warning("ACS demographics: missing state or city — skipping")
        return

    state_fips = STATE_FIPS.get(state.strip().upper())
    if not state_fips:
        logger.warning("ACS demographics: unknown state '%s' — skipping", state)
        return

    logger.info("Fetching ACS demographics for %s, %s (FIPS %s)...", city, state, state_fips)

    places = _fetch_acs_places(state_fips)
    subdivisions = _fetch_acs_subdivisions(state_fips)

    if not places and not subdivisions:
        logger.warning("ACS demographics: no data retrieved for state %s", state)
        return

    # Determine municipality type from registry if available
    mtype = deal.provenance.field_sources.get("municipality_type", "")

    acs = _match_acs(city, mtype, state, places, subdivisions)

    if any(v is not None for v in acs.values()):
        _apply_acs(deal, acs)
        logger.info("ACS demographics: pop=%s, income=%s, unemp=%s",
                     acs.get("population"), acs.get("median_hh_income"),
                     acs.get("unemployed"))
    else:
        logger.warning("ACS demographics: no match for %s, %s", city, state)


# ═══════════════════════════════════════════════════════════════════════════
# STEP 4 — FRED API (interest rates)
# ═══════════════════════════════════════════════════════════════════════════

_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

_FRED_SERIES = {
    "DGS10":        "dgs10_rate",
    "SOFR":         "sofr_rate",
    "MORTGAGE30US": "mortgage30_rate",
    "CPIAUCSL":     "cpi_yoy",
}


def _fetch_fred_series(series_id: str) -> Optional[float]:
    """Fetch the most recent observation for a FRED series (no API key required)."""
    is_cpi = series_id == "CPIAUCSL"
    import os
    params = {
        "series_id": series_id,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 13 if is_cpi else 1,
        "api_key": os.environ.get("FRED_API_KEY", ""),
    }
    try:
        resp = requests.get(_FRED_BASE, params=params, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        if not obs:
            return None

        if is_cpi:
            return _cpi_yoy(obs)

        val = obs[0].get("value", ".")
        return _safe_float(val)
    except Exception as exc:
        logger.warning("FRED fetch failed (%s): %s", series_id, exc)
        return None


def _cpi_yoy(obs: List[dict]) -> Optional[float]:
    """Calculate CPI year-over-year % change from 13 monthly observations."""
    vals = []
    for o in obs:
        v = _safe_float(o.get("value", "."))
        if v is not None:
            vals.append(v)
    # latest = vals[0], 12 months ago = vals[12]
    if len(vals) >= 13 and vals[12] > 0:
        return round((vals[0] / vals[12] - 1) * 100, 2)
    if len(vals) >= 2 and vals[-1] > 0:
        return round((vals[0] / vals[-1] - 1) * 100, 2)
    return None


def _fetch_all_fred(deal: DealData) -> Dict[str, Optional[float]]:
    """
    Step 4: Fetch all four FRED series. Returns dict with MarketData field names.
    CPI is returned as a percentage (e.g. 3.2 = 3.2%).
    Rate series are returned as decimals (e.g. 0.0425 = 4.25%).
    """
    results: Dict[str, Optional[float]] = {}
    for series_id, field_name in _FRED_SERIES.items():
        val = _fetch_fred_series(series_id)
        if val is not None:
            if field_name == "cpi_yoy":
                # CPI is already computed as percentage by _cpi_yoy; convert to decimal
                val = round(val / 100.0, 4)
            else:
                # FRED returns rates as percentages (4.25); convert to decimal
                val = round(val / 100.0, 6)
        results[field_name] = val

    return results


# ═══════════════════════════════════════════════════════════════════════════
# STEP 5 — PROMPT 5B: DEBT MARKET SNAPSHOT NARRATIVE (Sonnet)
# ═══════════════════════════════════════════════════════════════════════════

_SYSTEM_5B = (
    "You are a senior CRE debt analyst writing a market context paragraph for a\n"
    "formal investment underwriting report.\n"
    "\n"
    "RULES:\n"
    "- Exactly one paragraph. No headers, no bullets.\n"
    "- Cover: (a) current rate environment — always name 10-yr Treasury. Reference SOFR\n"
    "  only if floating-rate or construction-to-perm loan. (b) proposed rate vs. market.\n"
    "  (c) DSCR trajectory and refinance risk over hold period.\n"
    "  (d) one sentence on CPI vs. underwritten expense growth assumption.\n"
    "- Do not recommend whether to proceed. State facts and implications only.\n"
    '- If a FRED field is "data unavailable": acknowledge and work around it.\n'
    "- Tone: Precise, institutional, neutral. Length: 100–150 words. Output plain text only."
)

_USER_5B = (
    "Property: {property_address} | Asset: {asset_type} | Hold: {hold_period} yrs\n"
    "Data pull date: {data_pull_date}\n"
    "Underwritten expense growth: {expense_growth_rate}%\n"
    "\n"
    "FRED live data:\n"
    "  10-yr Treasury (DGS10): {dgs10_rate}% | SOFR: {sofr_rate}%\n"
    "  30-yr mortgage: {mortgage30_rate}% | CPI YoY: {cpi_yoy}%\n"
    "\n"
    "Deal debt structure:\n"
    "  Type: {loan_type} | Amount: ${loan_amount} | Rate: {loan_rate}%\n"
    "  Rate type: {rate_type} | LTV: {ltv}% | DSCR Yr1: {dscr_yr1}x\n"
    "  Amort: {amortization} yrs | Term: {loan_term} yrs\n"
    "\n"
    "Write the debt market context paragraph now. Output plain text only."
)


def _generate_debt_market_narrative(deal: DealData, data_pull_date: str) -> None:
    """Step 5: Generate Prompt 5B — Debt Market Snapshot Narrative."""
    md = deal.market_data
    assumptions = deal.assumptions
    addr = deal.address

    loan_amount = assumptions.purchase_price * assumptions.ltv_pct

    user_msg = _USER_5B.format(
        property_address=addr.full_address,
        asset_type=deal.asset_type.value,
        hold_period=assumptions.hold_period,
        data_pull_date=data_pull_date,
        expense_growth_rate=f"{assumptions.expense_growth_rate * 100:.1f}",
        dgs10_rate=_fmt_rate(md.dgs10_rate),
        sofr_rate=_fmt_rate(md.sofr_rate),
        mortgage30_rate=_fmt_rate(md.mortgage30_rate),
        cpi_yoy=_fmt_rate(md.cpi_yoy),
        loan_type=getattr(assumptions, "loan_type", None) or "Permanent",
        loan_amount=f"{loan_amount:,.0f}",
        loan_rate=f"{assumptions.interest_rate * 100:.2f}",
        rate_type=getattr(assumptions, "rate_type", None) or "fixed",
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


# ═══════════════════════════════════════════════════════════════════════════
# STEP 6 — HUD FAIR MARKET RENTS
# ═══════════════════════════════════════════════════════════════════════════

_HUD_FMR_LIST_COUNTIES = "https://www.huduser.gov/hudapi/public/fmr/listCounties"
_HUD_FMR_DATA = "https://www.huduser.gov/hudapi/public/fmr/data"


def _get_hud_api_key() -> Optional[str]:
    """Read HUD API key from env var."""
    key = os.environ.get("HUD_API_KEY", "")
    if not key:
        logger.warning("HUD API key not configured")
        return None
    return key


def _fetch_hud_fmr(deal: DealData) -> None:
    """
    Step 6: Fetch HUD Fair Market Rents by county FIPS.
    1. GET /fmr/listCounties/{state_fips}
    2. GET /fmr/data/{fips_county}
    """
    hud_key = _get_hud_api_key()
    if not hud_key:
        return

    fips_county = deal.address.fips_code
    state = deal.address.state
    if not fips_county and not state:
        logger.warning("HUD FMR: no FIPS county or state — skipping")
        return

    headers = {"Authorization": f"Bearer {hud_key}"}

    # If we have a FIPS county code, try direct lookup
    entity_id = None
    if fips_county and len(fips_county) >= 5:
        entity_id = fips_county

    # If no FIPS county, try listCounties to find it
    if not entity_id and state:
        state_fips = STATE_FIPS.get(state.strip().upper())
        if state_fips:
            try:
                resp = requests.get(
                    f"{_HUD_FMR_LIST_COUNTIES}/{state_fips}",
                    headers=headers,
                    timeout=_REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                counties = resp.json()
                # Try to match by county name from registry
                county_name = deal.provenance.field_sources.get("county")
                if county_name and isinstance(counties, list):
                    cn_lower = county_name.lower()
                    for c in counties:
                        if cn_lower in str(c.get("county_name", "")).lower():
                            entity_id = c.get("fips_code") or c.get("county_code")
                            break
            except Exception as exc:
                logger.warning("HUD FMR listCounties failed: %s", exc)

    if not entity_id:
        logger.warning("HUD FMR: could not determine county entity — skipping")
        return

    # Fetch FMR data
    try:
        resp = requests.get(
            f"{_HUD_FMR_DATA}/{entity_id}",
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        basicdata = data.get("basicdata", data)

        md = deal.market_data
        # Map bedroom counts: 0BR=Efficiency, 1BR, 2BR, 3BR, 4BR
        md.fmr_studio = _safe_float(basicdata.get("Efficiency")) or md.fmr_studio
        md.fmr_1br = _safe_float(basicdata.get("One-Bedroom")) or md.fmr_1br
        md.fmr_2br = _safe_float(basicdata.get("Two-Bedroom")) or md.fmr_2br
        md.fmr_3br = _safe_float(basicdata.get("Three-Bedroom")) or md.fmr_3br
        # 4BR stored in provenance (no model field)
        fmr_4br = _safe_float(basicdata.get("Four-Bedroom"))
        if fmr_4br:
            deal.provenance.field_sources["fmr_4br"] = str(fmr_4br)

        deal.provenance.field_sources["hud_fmr"] = f"hud_fmr_{datetime.utcnow().strftime('%Y-%m-%d')}"
        logger.info("HUD FMR: studio=%s, 1BR=%s, 2BR=%s, 3BR=%s",
                     md.fmr_studio, md.fmr_1br, md.fmr_2br, md.fmr_3br)
    except Exception as exc:
        logger.warning("HUD FMR data fetch failed (entity %s): %s", entity_id, exc)


# ═══════════════════════════════════════════════════════════════════════════
# STEP 7 — FEMA FLOOD ZONE
# ═══════════════════════════════════════════════════════════════════════════

_FEMA_NFHL_URL = "https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/28/query"


def _fetch_fema_flood(deal: DealData) -> None:
    """Step 7: Query FEMA NFHL for flood zone at the property's lat/lon."""
    lat = deal.address.latitude
    lon = deal.address.longitude
    if lat is None or lon is None:
        logger.warning("FEMA: no lat/lon — skipping flood zone lookup")
        return

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
            logger.info("FEMA: no flood zone features at (%.4f, %.4f)", lat, lon)
            return

        attrs = features[0].get("attributes", {})
        md = deal.market_data
        md.fema_flood_zone = attrs.get("FLD_ZONE") or md.fema_flood_zone
        md.fema_panel_number = attrs.get("DFIRM_ID") or md.fema_panel_number
        deal.provenance.field_sources["fema_flood"] = f"fema_nfhl_{datetime.utcnow().strftime('%Y-%m-%d')}"
        logger.info("FEMA: zone=%s, panel=%s", md.fema_flood_zone, md.fema_panel_number)
    except Exception as exc:
        logger.warning("FEMA NFHL fetch failed (%.4f, %.4f): %s", lat, lon, exc)


# ═══════════════════════════════════════════════════════════════════════════
# EPA ENVIROFACTS — ENVIRONMENTAL FLAGS
# ═══════════════════════════════════════════════════════════════════════════

_EPA_BASE = "https://enviro.epa.gov/enviro/efservice"


def _fetch_epa_flags(zip_code: str) -> List[str]:
    """Query EPA EnviroFacts for environmental program flags near a ZIP code."""
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


# ═══════════════════════════════════════════════════════════════════════════
# ZONING CODE SCRAPER
# ═══════════════════════════════════════════════════════════════════════════

def _scrape_zoning_code(url: str) -> Optional[str]:
    """Fetch zoning code page and extract text. Handles ecode360/Municode."""
    if not url:
        return None
    try:
        headers = {
            "User-Agent": "DealDesk-CRE-Underwriting/1.0 (research; municipal code lookup)",
        }
        resp = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        html = resp.text

        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) < 200:
            logger.warning("Zoning code scrape returned too little text (%d chars)", len(text))
            return None

        return text[:12000]
    except Exception as exc:
        logger.warning("Zoning code scrape failed (%s): %s", url, exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# LLM HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _get_anthropic_api_key() -> Optional[str]:
    """Read Anthropic API key from env var."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        logger.warning("ANTHROPIC_API_KEY not configured")
        return None
    return key


def _call_llm(model: str, system: str, user_msg: str, max_tokens: int = 4096) -> Optional[dict]:
    """Send a Claude API call expecting JSON. Returns parsed dict or None."""
    client = anthropic.Anthropic(api_key=_get_anthropic_api_key())
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
    """Send a Claude API call expecting plain text. Returns string or None."""
    client = anthropic.Anthropic(api_key=_get_anthropic_api_key())
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


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT 3A — ZONING PARAMETER EXTRACTION (Haiku)
# ═══════════════════════════════════════════════════════════════════════════

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

    sv = data.get("source_verification") or {}
    z.source_verified = not sv.get("source_mismatch", False)
    z.source_notes    = sv.get("source_notes")


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT 3B — BUILDABLE CAPACITY ANALYSIS (Sonnet)
# ═══════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT 3C — HIGHEST & BEST USE OPINION (Sonnet)
# ═══════════════════════════════════════════════════════════════════════════

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
    "- Acknowledge data limitations. Length: 3-4 paragraphs."
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

    deal.narratives.buildable_capacity = z.buildable_capacity_narrative
    deal.narratives.highest_best_use   = z.hbu_narrative


# ═══════════════════════════════════════════════════════════════════════════
# MARKET CONTEXT SUMMARY (for Prompt 3C)
# ═══════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════
# SECRETS HELPER
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

def enrich_market_data(deal: DealData) -> DealData:
    """
    Master market enrichment function — populates DealData with external
    market data, zoning analysis, and debt market narrative.

    Steps run IN ORDER:
        1. Municipal Registry Lookup
        2. Census Geocoder (tract + OZ)
        3. Census ACS API (demographics)
        4. FRED API (interest rates)
        5. Prompt 5B — Debt Market Snapshot
        6. HUD Fair Market Rents
        7. FEMA Flood Zone
        8. EPA Environmental Flags
        9–11. Zoning scrape + Prompts 3A / 3B / 3C

    Any failure logs a warning and continues — the pipeline never crashes here.

    Args:
        deal: DealData with address, asset_type, assumptions already set.

    Returns:
        The same DealData object, enriched with market_data, zoning, and narratives.
    """
    md = deal.market_data
    addr = deal.address
    assumptions = deal.assumptions
    data_pull_date = datetime.utcnow().strftime("%Y-%m-%d")

    # ── STEP 1: Municipal Registry Lookup ─────────────────────────
    logger.info("Step 1: Municipal registry lookup...")
    muni_row = _lookup_municipal_registry(deal)
    if muni_row is not None:
        # Store municipality_type for ACS matching
        mtype = muni_row.get("municipality_type")
        if pd.notna(mtype):
            deal.provenance.field_sources["municipality_type"] = str(mtype).strip()
        _apply_registry(muni_row, deal)
    else:
        logger.info("Step 1: no registry match — continuing")

    # ── STEP 2: Census Geocoder (tract + OZ) ─────────────────────
    logger.info("Step 2: Census Geocoder...")
    _census_geocode(deal)

    # ── STEP 3: Census ACS API (demographics) ────────────────────
    logger.info("Step 3: Census ACS demographics...")
    _fetch_acs_demographics(deal)

    # ── STEP 4: FRED API (interest rates) ─────────────────────────
    logger.info("Step 4: FRED macro rates...")
    fred_data = _fetch_all_fred(deal)
    md.dgs10_rate      = fred_data.get("dgs10_rate") or md.dgs10_rate
    md.sofr_rate       = fred_data.get("sofr_rate") or md.sofr_rate
    md.mortgage30_rate = fred_data.get("mortgage30_rate") or md.mortgage30_rate
    md.cpi_yoy         = fred_data.get("cpi_yoy") or md.cpi_yoy
    deal.provenance.field_sources["fred_rates"] = f"fred_{data_pull_date}"
    deal.provenance.fred_pull_date = data_pull_date
    logger.info("FRED: DGS10=%s, SOFR=%s, MTG30=%s, CPI=%s",
                md.dgs10_rate, md.sofr_rate, md.mortgage30_rate, md.cpi_yoy)

    # ── STEP 5: Prompt 5B — Debt Market Snapshot (immediately after FRED) ─
    logger.info("Step 5: Prompt 5B — Debt Market Snapshot...")
    _generate_debt_market_narrative(deal, data_pull_date)

    # ── STEP 6: HUD Fair Market Rents ─────────────────────────────
    logger.info("Step 6: HUD Fair Market Rents...")
    _fetch_hud_fmr(deal)

    # ── STEP 7: FEMA Flood Zone ───────────────────────────────────
    logger.info("Step 7: FEMA Flood Zone...")
    _fetch_fema_flood(deal)

    # ── STEP 8: EPA Environmental Flags ───────────────────────────
    logger.info("Step 8: EPA environmental flags...")
    if addr.zip_code:
        epa_flags = _fetch_epa_flags(addr.zip_code)
        if epa_flags:
            md.epa_env_flags = epa_flags
            deal.provenance.field_sources["epa_flags"] = f"epa_envirofacts_{data_pull_date}"
            logger.info("EPA: flags=%s", epa_flags)
    else:
        logger.warning("EPA: no ZIP code — skipping")

    # ── Data pull date ────────────────────────────────────────────
    md.data_pull_date = data_pull_date

    # ── STEP 9: Zoning Code Scrape + Prompts 3A/3B/3C ────────────
    logger.info("Step 9: Zoning code analysis...")
    zoning_code_text = None

    scrape_url = deal.zoning.municipal_code_url
    if scrape_url:
        logger.info("Scraping zoning code from %s...", scrape_url)
        zoning_code_text = _scrape_zoning_code(scrape_url)
        if zoning_code_text:
            deal.provenance.field_sources["zoning_scrape"] = scrape_url
    else:
        logger.info("No zoning code URL — zoning prompts will have limited data")

    # Prompt 3A — Zoning Parameter Extraction (Haiku)
    if zoning_code_text:
        logger.info("Running Prompt 3A — Zoning Parameter Extraction...")
        code_platform = deal.provenance.field_sources.get("code_platform", "unknown")
        user_msg = _USER_3A.format(
            property_address=addr.full_address,
            expected_zoning_code=deal.zoning.zoning_code or "unknown",
            municipality_name=addr.city or "unknown",
            state=addr.state or "unknown",
            code_platform=code_platform,
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

    # Prompt 3B — Buildable Capacity Analysis (Sonnet)
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

    # Prompt 3C — Highest & Best Use (Sonnet)
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

    logger.info("Market data enrichment complete for %s", deal.deal_id)
    return deal
