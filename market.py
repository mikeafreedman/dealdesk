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

import csv as csv_mod
import io
import json
import logging
import math
import os
import re
from collections import Counter
from datetime import datetime
from io import StringIO
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

import anthropic
import pandas as pd
import requests

from config import (
    MODEL_HAIKU,
    MODEL_SONNET,
    MUNICIPAL_REGISTRY_CSV,
)
from models.models import (
    DealData, MarketData, ParcelData, ZoningData,
    RentComp, SaleComp,
    RenovationTier, RENOVATION_TIER_MULTIPLIERS, RENOVATION_DOWNTIME_MONTHS,
)

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

    # Fallback: municipality_name + state — try progressively looser matches
    if city and state:
        city_lower = city.strip().lower()
        state_upper = state.strip().upper()
        state_mask = df["state"].str.strip().str.upper() == state_upper
        state_df = df[state_mask]

        if len(state_df) == 0:
            logger.warning("Municipal registry: no entries for state=%s", state)
        else:
            names_lower = state_df["municipality_name"].str.strip().str.lower()

            # Match 1: exact
            m = state_df[names_lower == city_lower]
            if len(m) == 0:
                # Match 2: registry name contains city
                m = state_df[names_lower.str.contains(city_lower, regex=False, na=False)]
            if len(m) == 0:
                # Match 3: city contains registry name (e.g. "washington" in "washington, d.c.")
                m = state_df[names_lower.apply(
                    lambda n: n.split(",")[0].strip() in city_lower
                    or city_lower in n.split(",")[0].strip()
                )]
            if len(m) == 0:
                # Match 4: normalize both sides
                def _norm_city(n):
                    for strip in ["city of ", "town of ", "village of ",
                                  "township of ", "borough of "]:
                        if n.startswith(strip):
                            n = n[len(strip):]
                    for strip in [" city", " town", " village", " township",
                                  " borough", ", d.c.", ", dc"]:
                        if n.endswith(strip):
                            n = n[:-len(strip)]
                    return n.strip()
                city_norm = _norm_city(city_lower)
                m = state_df[names_lower.apply(lambda n: _norm_city(n) == city_norm)]

            if len(m) > 0:
                logger.info(
                    "Municipal registry: matched '%s' → '%s', %s",
                    city, m.iloc[0]["municipality_name"], state)
                return m.iloc[0]

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
    # Strip hyphenated range portion: "2-8 S 46th" → "2 S 46th" (Census accepts only single street number)
    addr = re.sub(r'^(\d+)\s*[-–]\s*\d+\s+', r'\1 ', addr)
    # Drop the trailing period on directional abbreviations. Census accepts the
    # abbreviated form ("S", "NE") but rejects the periods ("s.", "ne.").
    # Two-letter compounds first so "ne." isn't partially replaced.
    addr = re.sub(r'(?i)\b(ne|nw|se|sw)\.\b', r'\1', addr)
    addr = re.sub(r'(?i)\b([nsew])\.\b', r'\1', addr)
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
    """
    Three-tier dynamic geocoding fallback:
    Tier 1: Google Maps Geocoding API (if key configured)
    Tier 2: Nominatim / OpenStreetMap (no key required)
    Tier 3: Census Bureau place centroid lookup via /geocoder/locations/address
             at the city level (drops street number, just city + state)
    """
    import os, urllib.parse

    # ── Tier 1: Google Maps Geocoding API ────────────────────────────
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if api_key:
        try:
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
                # Extract FIPS from Google result if available
                for comp in results[0].get("address_components", []):
                    if "administrative_area_level_2" in comp.get("types", []):
                        pass  # county name only, not FIPS
                logger.info(
                    "GEOCODE T1 (Google): lat=%.6f, lon=%.6f",
                    addr.latitude, addr.longitude)
                return
            logger.warning("GEOCODE T1 (Google): no results for '%s'", full_address)
        except Exception as exc:
            logger.warning("GEOCODE T1 (Google) failed: %s", exc)

    # ── Tier 2: Nominatim / OpenStreetMap (no API key) ───────────────
    try:
        nom_url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": full_address,
            "format": "json",
            "limit": 1,
            "countrycodes": "us",
        }
        headers = {"User-Agent": "DealDesk-CRE-Underwriting/1.0"}
        resp = requests.get(nom_url, params=params, headers=headers, timeout=15)
        results = resp.json()
        if results:
            addr.latitude = float(results[0]["lat"])
            addr.longitude = float(results[0]["lon"])
            logger.info(
                "GEOCODE T2 (Nominatim): lat=%.6f, lon=%.6f for '%s'",
                addr.latitude, addr.longitude, full_address)
            return
        logger.warning("GEOCODE T2 (Nominatim): no results for '%s'", full_address)
    except Exception as exc:
        logger.warning("GEOCODE T2 (Nominatim) failed: %s", exc)

    # ── Tier 3: Census city-level centroid (drop street, keep city+state) ──
    city = (addr.city or "").strip()
    state = (addr.state or "").strip()
    if city and state:
        try:
            city_addr = f"{city}, {state}"
            resp = requests.get(
                "https://geocoding.geo.census.gov/geocoder/locations/address",
                params={
                    "address": city_addr,
                    "benchmark": "Public_AR_Current",
                    "format": "json",
                },
                timeout=15,
            )
            resp.raise_for_status()
            matches = resp.json().get("result", {}).get("addressMatches", [])
            if matches:
                coords = matches[0].get("coordinates", {})
                lat = _safe_float(coords.get("y"))
                lon = _safe_float(coords.get("x"))
                if lat and lon:
                    addr.latitude = lat
                    addr.longitude = lon
                    logger.info(
                        "GEOCODE T3 (Census city): lat=%.6f, lon=%.6f for '%s'",
                        lat, lon, city_addr)
                    return
        except Exception as exc:
            logger.warning("GEOCODE T3 (Census city) failed: %s", exc)

    # ── Absolute last resort — log clearly that coordinates are unknown ──
    if addr.latitude is None or addr.longitude is None:
        # Use 0,0 instead of a wrong city — this will cause FEMA/maps to
        # fail gracefully rather than silently return wrong data
        addr.latitude = 0.0
        addr.longitude = 0.0
        logger.error(
            "GEOCODE: ALL fallbacks failed for '%s' — coordinates set to 0,0. "
            "Maps, FEMA, and location analyses will be unavailable.",
            full_address)


def _lookup_fips_from_latlon(addr) -> None:
    """Use FCC Census Block API to get FIPS from lat/lon."""
    lat = addr.latitude
    lon = addr.longitude
    if not lat or not lon or lat == 0.0:
        return
    try:
        resp = requests.get(
            "https://geo.fcc.gov/api/census/block/find",
            params={
                "latitude": lat,
                "longitude": lon,
                "format": "json",
            },
            timeout=10,
        )
        data = resp.json()
        county_fips = data.get("County", {}).get("FIPS", "")
        if county_fips and len(county_fips) >= 5:
            addr.fips_code = county_fips[:5]
            logger.info("FIPS (FCC API): county_fips=%s", addr.fips_code)
    except Exception as exc:
        logger.warning("FIPS lookup (FCC API) failed: %s", exc)


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

    # Build multi-part components: strip hyphenated ranges from street number
    street = (addr.street or "").strip()
    street = re.sub(r'^(\d+)\s*[-–]\s*\d+\s+', r'\1 ', street)
    street = re.sub(r'(?i)\b([nsew])\.\b', r'\1', street)
    street = re.sub(r'(?i)\b(ne|nw|se|sw)\.\b', r'\1', street)
    city = (addr.city or "").strip()
    state = (addr.state or "").strip()
    zip_code = (addr.zip_code or "").strip()

    logger.info("GEOCODE input: street='%s' city='%s' state='%s' zip='%s'",
                street, city, state, zip_code)

    # Use multi-part query when we have the components, else fall back to single-line
    if street and city and state:
        params = {
            "street": street,
            "city": city,
            "state": state,
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "format": "json",
        }
        if zip_code:
            params["zip"] = zip_code
    else:
        params = {
            "address": full,
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "format": "json",
        }
    logger.info("Census Geocoder: geocoding address '%s'", full)

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
    if lat is not None and lon is not None:
        logger.info("GEOCODE result: lat=%.6f lng=%.6f", lat, lon)
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

    # Fallback FIPS lookup via FCC if Census didn't populate it
    if not addr.fips_code and addr.latitude and addr.latitude != 0.0:
        _lookup_fips_from_latlon(addr)


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
    """Fetch the most recent observation for a FRED series."""
    is_cpi = series_id == "CPIAUCSL"
    api_key = os.environ.get("FRED_API_KEY", "")
    if not api_key:
        logger.warning("FRED %s: skipped — FRED_API_KEY not configured", series_id)
        return None
    params = {
        "series_id": series_id,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 13 if is_cpi else 1,
        "api_key": api_key,
    }
    try:
        resp = requests.get(_FRED_BASE, params=params, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        if not obs:
            logger.warning("FRED %s: failed - no observations returned", series_id)
            return None

        if is_cpi:
            val = _cpi_yoy(obs)
            if val is not None:
                logger.info("FRED %s: %.4f", series_id, val)
            return val

        val = _safe_float(obs[0].get("value", "."))
        if val is not None:
            logger.info("FRED %s: %.4f", series_id, val)
        return val
    except Exception as exc:
        logger.warning("FRED %s: failed - %s", series_id, exc)
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
        loan_type=getattr(assumptions, "loan_type", None) or "Acquisition",
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

_HUD_FMR_DATA = "https://www.huduser.gov/hudapi/public/fmr/data"

COUNTY_FIPS = {
    ("Philadelphia", "PA"): "42101",
    ("Washington",   "DC"): "11001",
    ("Baltimore",    "MD"): "24510",
    ("Montgomery",   "MD"): "24031",
    ("Prince George's", "MD"): "24033",
    ("New Castle",   "DE"): "10003",
    ("Burlington",   "NJ"): "34005",
    ("Camden",       "NJ"): "34007",
    ("Bucks",        "PA"): "42017",
    ("Delaware",     "PA"): "42045",
    ("Montgomery",   "PA"): "42091",
    ("Chester",      "PA"): "42029",
}


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
    headers = {"Authorization": f"Bearer {hud_key}"}

    entity_id = None
    if fips_county and len(fips_county) >= 5:
        entity_id = fips_county[:5]

    if not entity_id:
        county_name = deal.provenance.field_sources.get("county") or deal.address.city
        if county_name and state:
            county_key = county_name.strip().replace(" County", "")
            entity_id = COUNTY_FIPS.get((county_key, state.strip().upper()))

    if not entity_id:
        logger.warning("HUD FMR: could not determine county entity — skipping")
        return

    # HUD expects 5-digit county FIPS suffixed with 99999 (state+county+99999)
    if len(entity_id) == 5 and entity_id.isdigit():
        entity_id = entity_id + "99999"

    logger.info("HUD FMR: querying entity_id=%s", entity_id)

    # Fetch FMR data
    try:
        resp = requests.get(
            f"{_HUD_FMR_DATA}/{entity_id}",
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        # County response: data.basicdata is a list of sub-areas; metro: dict
        basic = data.get("basicdata", data) if isinstance(data, dict) else {}
        if isinstance(basic, list):
            basicdata = basic[0] if basic else {}
        else:
            basicdata = basic

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
        if md.fmr_2br is not None:
            logger.info("HUD FMR 2BR: $%.0f", md.fmr_2br)
    except Exception as exc:
        logger.warning("HUD FMR data fetch failed (entity %s): %s", entity_id, exc)


# ═══════════════════════════════════════════════════════════════════════════
# STEP 7 — FEMA FLOOD ZONE
# ═══════════════════════════════════════════════════════════════════════════

_FEMA_ENDPOINTS = [
    ("hazards-arcgis",
     "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"),
    ("msc-fallback",
     "https://msc.fema.gov/arcgis/rest/services/NFHL_Prod/NFHLREST_Admin/MapServer/28/query"),
]


def _fetch_fema_flood_zone(deal, lat: float, lng: float) -> Optional[str]:
    """Query FEMA NFHL for the flood zone at (lat, lng).

    Tries the updated hazards.fema.gov ArcGIS path first, falls back to MSC.
    Writes results to deal.market_data and returns the zone string (or None).
    """
    if lat is None or lng is None:
        logger.warning("FEMA: no lat/lon — skipping flood zone lookup")
        return None

    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF",
        "returnGeometry": "false",
        "f": "json",
    }

    for label, url in _FEMA_ENDPOINTS:
        try:
            resp = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
            if resp.status_code == 404:
                logger.warning("FEMA %s: 404 at %s", label, url)
                continue
            resp.raise_for_status()
            features = resp.json().get("features", [])
            if not features:
                logger.info("FEMA %s: no flood zone features at (%.4f, %.4f)",
                            label, lat, lng)
                return None
            attrs = features[0].get("attributes", {}) or {}
            zone = attrs.get("FLD_ZONE")
            md = deal.market_data
            md.fema_flood_zone = zone or md.fema_flood_zone
            sfha = attrs.get("SFHA_TF")
            if sfha is not None and hasattr(md, "fema_sfha"):
                md.fema_sfha = sfha
            deal.provenance.field_sources["fema_flood"] = (
                f"fema_{label}_{datetime.utcnow().strftime('%Y-%m-%d')}"
            )
            logger.info("FEMA %s: zone=%s, sfha=%s subty=%s",
                        label, zone, sfha, attrs.get("ZONE_SUBTY"))
            return zone
        except Exception as exc:
            logger.warning("FEMA %s fetch failed (%.4f, %.4f): %s",
                           label, lat, lng, exc)
            continue

    logger.warning(
        "FEMA: all endpoints failed for (%.4f, %.4f) — zone set to 'Not Determined'",
        lat, lng,
    )
    return None


def _fetch_fema_flood(deal: DealData) -> None:
    """Step 7 wrapper: pulls lat/lon from the deal and delegates to the cascade."""
    _fetch_fema_flood_zone(deal, deal.address.latitude, deal.address.longitude)


# ═══════════════════════════════════════════════════════════════════════════
# TRANSIT & AMENITIES (OSM Overpass + Google Places)
# ═══════════════════════════════════════════════════════════════════════════

_OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
]


def _overpass_post(query: str):
    """POST an Overpass QL query, trying mirrors in order. Raises on final failure."""
    last_exc = None
    for url in _OVERPASS_MIRRORS:
        try:
            r = requests.post(
                url, data={"data": query},
                headers={"User-Agent": "DealDesk-CRE/1.0"},
                timeout=_REQUEST_TIMEOUT,
            )
            if r.status_code == 200:
                return r
            last_exc = Exception(f"{r.status_code} from {url}")
        except Exception as exc:
            last_exc = exc
            continue
    raise last_exc or Exception("all Overpass mirrors failed")


def _haversine_miles(lat1, lon1, lat2, lon2) -> float:
    from math import radians, sin, cos, asin, sqrt
    R = 3958.756
    lat1, lat2 = radians(lat1), radians(lat2)
    dlat = lat2 - lat1
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))


def _fetch_transit_and_amenities(deal: DealData) -> None:
    """Populate market_data.transit_options (OSM Overpass) and
    market_data.nearby_amenities (Google Places Nearby Search)."""
    addr = deal.address
    lat, lng = addr.latitude, addr.longitude
    if not lat or not lng or lat == 0.0:
        logger.warning("TRANSIT/AMENITY: skipping — no geocoordinates")
        return

    md = deal.market_data

    # ── Transit via OSM Overpass (no key) ────────────────────────────
    try:
        # Broader SEPTA/transit coverage — rail, subway entrances, tram,
        # stop_position, bus stops, bus stations. 800m radius for everything
        # so stations that sit just outside a tight radius (e.g. subway
        # entrance set back from street) are still captured.
        radius = 800
        q = (
            "[out:json][timeout:25];"
            "("
            f"node[\"railway\"=\"station\"](around:{radius},{lat},{lng});"
            f"node[\"railway\"=\"subway_entrance\"](around:{radius},{lat},{lng});"
            f"node[\"railway\"=\"tram_stop\"](around:{radius},{lat},{lng});"
            f"node[\"public_transport\"=\"station\"](around:{radius},{lat},{lng});"
            f"node[\"public_transport\"=\"stop_position\"](around:{radius},{lat},{lng});"
            f"node[\"highway\"=\"bus_stop\"](around:{radius},{lat},{lng});"
            f"node[\"amenity\"=\"bus_station\"](around:{radius},{lat},{lng});"
            ");"
            "out body 40;"
        )
        r = requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": q},
            headers={"User-Agent": "DealDesk-CRE/1.0"},
            timeout=_REQUEST_TIMEOUT,
        )
        logger.info("OVERPASS STATUS: %d", r.status_code)
        r.raise_for_status()
        elements = r.json().get("elements", []) or []
        logger.info("OVERPASS RESULTS: %d elements found", len(elements))
        stops = []
        for el in elements:
            tags = el.get("tags", {}) or {}
            name = (tags.get("name") or tags.get("ref")
                    or tags.get("route_ref") or "Unnamed stop")
            if tags.get("railway") in ("station", "subway_entrance", "tram_stop"):
                mode = "Rail/Subway"
            elif tags.get("highway") == "bus_stop" or tags.get("amenity") == "bus_station":
                mode = "Bus"
            elif tags.get("public_transport"):
                mode = "Transit"
            else:
                mode = "Transit"
            d = _haversine_miles(lat, lng, el.get("lat", lat), el.get("lon", lng))
            stops.append({
                "mode": mode,
                "route": tags.get("network") or tags.get("operator") or "SEPTA",
                "distance": f"{d:.2f} mi",
                "destination": name,
                "_d": d,
            })
        stops.sort(key=lambda s: s["_d"])
        md.transit_options = [{k: v for k, v in s.items() if k != "_d"} for s in stops[:10]]
        logger.info("TRANSIT: %d stops found", len(md.transit_options))
    except Exception as exc:
        logger.warning("TRANSIT (Overpass) failed: %s", exc)

    # ── Amenities via Google Places Nearby Search ────────────────────
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        logger.warning("AMENITY: GOOGLE_MAPS_API_KEY not configured — skipping")
        return

    # Places API (New) — POST with field mask. Legacy nearbysearch is deprecated.
    categories = [
        (["supermarket", "grocery_store"], "Grocery"),
        (["hospital"],                     "Healthcare"),
        (["university"],                   "Education"),
        (["park"],                         "Park"),
        (["restaurant"],                   "Dining"),
    ]
    amenities = []
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location",
    }
    for types, label in categories:
        try:
            payload = {
                "includedTypes": types,
                "maxResultCount": 3,
                "locationRestriction": {
                    "circle": {
                        "center": {"latitude": lat, "longitude": lng},
                        "radius": 1200,
                    }
                },
            }
            r = requests.post(
                "https://places.googleapis.com/v1/places:searchNearby",
                headers=headers, json=payload, timeout=_REQUEST_TIMEOUT,
            )
            data = r.json() or {}
            if r.status_code != 200:
                logger.warning("AMENITY %s: %s %s", label, r.status_code,
                               (data.get("error", {}) or {}).get("message", "")[:100])
                continue
            for place in (data.get("places") or [])[:3]:
                loc = place.get("location") or {}
                plat = loc.get("latitude"); plng = loc.get("longitude")
                name = (place.get("displayName") or {}).get("text") or "—"
                addr_str = place.get("formattedAddress") or ""
                if plat is None or plng is None:
                    continue
                d = _haversine_miles(lat, lng, plat, plng)
                amenities.append({
                    "category": label,
                    "name": name,
                    "distance": f"{d:.2f} mi",
                    "notes": addr_str[:60],
                })
        except Exception as exc:
            logger.warning("AMENITY %s failed: %s", label, exc)
    md.nearby_amenities = amenities
    logger.info("AMENITY: %d amenities found (Google Places)", len(amenities))

    # ── Fallback: OSM Overpass for amenities when Google Places returns nothing
    if not amenities:
        try:
            q = (
                "[out:json][timeout:25];("
                f"node[\"shop\"~\"supermarket|convenience\"](around:1200,{lat},{lng});"
                f"node[\"amenity\"=\"hospital\"](around:2000,{lat},{lng});"
                f"node[\"amenity\"=\"university\"](around:2000,{lat},{lng});"
                f"node[\"leisure\"=\"park\"](around:1200,{lat},{lng});"
                f"node[\"amenity\"=\"restaurant\"](around:800,{lat},{lng});"
                ");out body 25;"
            )
            r = _overpass_post(q)
            r.raise_for_status()
            for el in (r.json().get("elements") or []):
                tags = el.get("tags", {}) or {}
                if tags.get("shop") in ("supermarket", "convenience"):
                    label = "Grocery"
                elif tags.get("amenity") == "hospital":
                    label = "Healthcare"
                elif tags.get("amenity") == "university":
                    label = "Education"
                elif tags.get("leisure") == "park":
                    label = "Park"
                elif tags.get("amenity") == "restaurant":
                    label = "Dining"
                else:
                    continue
                d = _haversine_miles(lat, lng, el.get("lat", lat), el.get("lon", lng))
                amenities.append({
                    "category": label,
                    "name": tags.get("name") or "—",
                    "distance": f"{d:.2f} mi",
                    "notes": (tags.get("addr:street") or tags.get("operator") or "OpenStreetMap")[:60],
                })
            amenities.sort(key=lambda a: float(a["distance"].split()[0]))
            md.nearby_amenities = amenities[:15]
            logger.info("AMENITY: %d amenities found (OSM fallback)", len(md.nearby_amenities))
        except Exception as exc:
            logger.warning("AMENITY (OSM fallback) failed: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════
# STEP 7B — PROPERTY RECORDS (Philadelphia OPA / DC DCGIS)
# ═══════════════════════════════════════════════════════════════════════════

_PHL_CARTO_SQL = "https://phl.carto.com/api/v2/sql"
_DC_PROPERTY_URL = (
    "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/"
    "Property_and_Land_WebMercator/MapServer/56/query"
)


def _split_street(street: str) -> tuple:
    """Split '2-8 S 46th Street' → ('2', 'S 46th Street'). First token is number."""
    s = (street or "").strip()
    if not s:
        return ("", "")
    parts = s.split(None, 1)
    if len(parts) < 2:
        return (parts[0], "")
    number = re.match(r'^(\d+)', parts[0])
    return (number.group(1) if number else parts[0], parts[1])


def _fetch_property_records(deal: DealData) -> None:
    """Step 7B: Fetch parcel/owner/zoning from city/county property records.

    Source cascade:
    1. Philadelphia OPA API (Philly only)
    2. DC DCGIS REST API (DC only)
    3. County/municipal GIS ArcGIS REST endpoint (from registry gis_parcel_url)
    4. Census TIGER parcel lookup (last resort — address only, no ownership)
    """
    addr = deal.address
    state = (addr.state or "").strip().upper()
    city = (addr.city or "").strip()

    if deal.parcel_data is None:
        deal.parcel_data = ParcelData()
    pd_obj = deal.parcel_data  # avoid shadowing builtin 'pd'

    # ── SOURCE 1: Philadelphia OPA ────────────────────────────────────
    if state == "PA" and city.lower() == "philadelphia":
        _fetch_phl_opa(deal, pd_obj, addr)
        return

    # ── SOURCE 2: DC DCGIS ───────────────────────────────────────────
    if state == "DC":
        _fetch_dc_dcgis(deal, pd_obj, addr)
        return

    # ── SOURCE 3: County GIS ArcGIS REST endpoint ─────────────────────
    # Many counties expose their parcel layer at a standard ArcGIS REST URL.
    # We first check deal.provenance for a gis_parcel_url from the registry.
    # Format: {base_url}/query?where=ADDR LIKE '%{number}%{street}%'&outFields=*&f=json
    gis_url = deal.provenance.field_sources.get("gis_parcel_url")
    if gis_url:
        _fetch_arcgis_parcel(deal, pd_obj, addr, gis_url)
        if pd_obj.parcel_id:  # success — stop here
            return

    # ── SOURCE 4: Nominatim / OSM building footprint lookup ──────────
    # Last resort: get at minimum the address confirmation from OSM
    _fetch_osm_parcel(deal, pd_obj, addr)


def _fetch_phl_opa(deal: DealData, pd_obj: ParcelData, addr) -> None:
    """Philadelphia Office of Property Assessment via Carto SQL API."""
    street_number, street_name = _split_street(addr.street)
    if not street_number or not street_name:
        logger.warning("PROPERTY RECORDS (PHL): no street number/name — skipping")
        return
    # OPA's `location` field is uppercase, abbreviated, no apartment numbers
    # e.g. "2-08 S 46TH ST". Pick the most distinctive token (skip directionals).
    name_tokens = street_name.upper().split() if street_name else []
    DIRECTIONALS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}
    name_token = next((t for t in name_tokens if t not in DIRECTIONALS), "")
    patterns = [
        f"{street_number} %{name_token}%",        # "2 %46TH%"
        f"{street_number}-%{name_token}%",        # "2-%46TH%" (range form, e.g. 2-08)
        f"%{street_number}%{name_token}%",        # very loose
    ]
    rows = []
    matched_pattern = None
    last_status = None
    for pat in patterns:
        query = (
            "SELECT parcel_number, owner_1, owner_2, "
            "category_code_description, total_area, total_livable_area, "
            "number_stories, year_built, market_value, "
            "taxable_land, taxable_building, "
            "sale_date, sale_price, zoning, location "
            "FROM opa_properties_public "
            f"WHERE location ILIKE '{pat}' LIMIT 5"
        )
        logger.info("OPA QUERY: pattern='%s' sql=%s", pat, query)
        try:
            resp = requests.get(
                "https://phl.carto.com/api/v2/sql",
                params={"q": query}, timeout=_REQUEST_TIMEOUT
            )
            last_status = resp.status_code
            resp.raise_for_status()
            rows = resp.json().get("rows", [])
            logger.info("OPA RESPONSE: status=%d rows=%d pattern='%s'",
                        resp.status_code, len(rows), pat)
            if rows:
                matched_pattern = pat
                break
        except Exception as exc:
            logger.warning("PROPERTY RECORDS (PHL) failed (pattern '%s'): %s", pat, exc)
            continue
    if not rows:
        logger.warning("OPA: no parcel found for '%s %s' (last_status=%s)",
                       street_number, street_name, last_status)
        return
    logger.info("OPA: matched pattern '%s' (%d rows)", matched_pattern, len(rows))
    row = rows[0]
    parcel_number = row.get("parcel_number") or ""
    owner = " ".join(filter(None, [row.get("owner_1"), row.get("owner_2")])).strip()
    zoning = row.get("zoning") or ""
    market_value = _safe_float(row.get("market_value"))
    taxable_land = _safe_float(row.get("taxable_land"))
    taxable_bldg = _safe_float(row.get("taxable_building"))
    pd_obj.parcel_id = parcel_number or pd_obj.parcel_id
    pd_obj.owner_name = owner or pd_obj.owner_name
    pd_obj.zoning_code = zoning or pd_obj.zoning_code
    pd_obj.assessed_value = market_value if market_value is not None else pd_obj.assessed_value
    if taxable_land is not None:
        pd_obj.land_value = taxable_land
    if taxable_bldg is not None:
        pd_obj.improvement_value = taxable_bldg
    pd_obj.last_sale_date = row.get("sale_date") or pd_obj.last_sale_date
    pd_obj.last_sale_price = _safe_float(row.get("sale_price")) or pd_obj.last_sale_price
    pd_obj.lot_area_sf = _safe_float(row.get("total_area")) or pd_obj.lot_area_sf
    pd_obj.building_sf = _safe_float(row.get("total_livable_area")) or pd_obj.building_sf
    year_built = row.get("year_built")
    if year_built:
        try:
            pd_obj.year_built = int(year_built)
        except (TypeError, ValueError):
            pass
    if zoning and not deal.zoning.zoning_code:
        deal.zoning.zoning_code = zoning
    deal.provenance.field_sources["property_records"] = "phl_opa"
    logger.info(
        "PROPERTY RECORDS: found parcel %s, owner=%s, zoning=%s, assessed=$%s",
        parcel_number, owner, zoning,
        f"{market_value:,.0f}" if market_value is not None else "n/a",
    )


def _fetch_dc_dcgis(deal: DealData, pd_obj: ParcelData, addr) -> None:
    """DC Office of Tax & Revenue via DCGIS REST API."""
    street_number, street_name = _split_street(addr.street)
    parcel_search = f"{street_number}%" if street_number else "%"
    params = {
        "where": f"SSL LIKE '{parcel_search}'",
        "outFields": "*",
        "f": "json",
    }
    dc_url = (
        "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/"
        "Property_and_Land_WebMercator/MapServer/56/query"
    )
    try:
        resp = requests.get(dc_url, params=params, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        features = resp.json().get("features", [])
    except Exception as exc:
        logger.warning("PROPERTY RECORDS (DC) failed: %s", exc)
        return
    if not features:
        logger.info("PROPERTY RECORDS (DC): no match for '%s'", parcel_search)
        return
    attrs = features[0].get("attributes", {}) or {}
    parcel_number = attrs.get("SSL") or ""
    owner = attrs.get("OWNERNAME") or ""
    zoning = attrs.get("ZONING") or ""
    market_value = _safe_float(attrs.get("ASSESSMENT") or attrs.get("TOTVAL"))
    pd_obj.parcel_id = parcel_number or pd_obj.parcel_id
    pd_obj.owner_name = owner or pd_obj.owner_name
    pd_obj.zoning_code = zoning or pd_obj.zoning_code
    pd_obj.assessed_value = market_value if market_value is not None else pd_obj.assessed_value
    if zoning and not deal.zoning.zoning_code:
        deal.zoning.zoning_code = zoning
    deal.provenance.field_sources["property_records"] = "dc_dcgis"
    logger.info(
        "PROPERTY RECORDS: found parcel %s, owner=%s, zoning=%s, assessed=$%s",
        parcel_number, owner, zoning,
        f"{market_value:,.0f}" if market_value is not None else "n/a",
    )


def _fetch_arcgis_parcel(deal: DealData, pd_obj: ParcelData, addr, gis_url: str) -> None:
    """Generic ArcGIS REST parcel query for any county that has a GIS parcel URL
    in the municipal registry. Covers hundreds of counties nationwide.

    Most county GIS parcel layers expose fields like:
    OWNER, ADDRESS, PARCEL_ID, ZONING, ASSESSED_VALUE, SALE_DATE, SALE_PRICE
    with slight naming variations. We try common field name variants.
    """
    street_number, street_name = _split_street(addr.street)
    if not street_number:
        logger.warning("PROPERTY RECORDS (ArcGIS): no street number — skipping")
        return

    # Normalize the GIS URL: strip trailing slashes, ensure /query suffix
    base = gis_url.rstrip("/")
    if not base.endswith("/query"):
        base = base + "/query"

    # Try multiple common address field names used by counties
    address_fields = ["SITEADDRESS", "SITE_ADDRESS", "ADDRESS", "FULL_ADDRESS",
                      "PROP_ADDRESS", "LOCATION", "ADDR", "SITUS_ADDRESS"]

    # Build address search term (number + first token of street name)
    name_token = street_name.split()[0] if street_name else ""
    search_term = f"{street_number}%{name_token}%"

    for field in address_fields:
        try:
            params = {
                "where": f"UPPER({field}) LIKE UPPER('{search_term}')",
                "outFields": "*",
                "returnGeometry": "false",
                "f": "json",
            }
            resp = requests.get(base, params=params, timeout=_REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if "error" in data:
                continue
            features = data.get("features", [])
            if not features:
                continue

            attrs = features[0].get("attributes", {}) or {}

            # Extract fields with fallbacks for common naming variants
            def _get_field(*names):
                for n in names:
                    v = attrs.get(n) or attrs.get(n.upper()) or attrs.get(n.lower())
                    if v and str(v).strip() not in ("", "None", "null", "0"):
                        return str(v).strip()
                return None

            parcel_id = _get_field("PARCEL_ID", "PARCELID", "APN", "PIN",
                                   "PARCEL_NUM", "OBJECTID")
            owner = _get_field("OWNER", "OWNERNAME", "OWNER_NAME", "OWNER1",
                               "TAXPAYER_NAME", "OWNER_FULL")
            zoning = _get_field("ZONING", "ZONE_CODE", "ZONING_CODE",
                                "CURRENT_ZONING", "ZONE")
            assessed_raw = _get_field("ASSESSED_VALUE", "TOTAL_VALUE", "TOTVAL",
                                      "MARKET_VALUE", "APPR_VALUE", "TOTAL_ASSESSED")
            sale_date = _get_field("SALE_DATE", "LAST_SALE_DATE", "DEED_DATE",
                                   "TRANSFER_DATE")
            sale_price_raw = _get_field("SALE_PRICE", "LAST_SALE_PRICE",
                                        "TRANSFER_VALUE", "DEED_PRICE")
            year_built_raw = _get_field("YEAR_BUILT", "YR_BUILT", "BUILT_YEAR",
                                        "CONSTRUCTION_YEAR")

            assessed = _safe_float(assessed_raw)
            sale_price = _safe_float(sale_price_raw)

            if parcel_id:
                pd_obj.parcel_id = parcel_id
            if owner:
                pd_obj.owner_name = owner
            if zoning:
                pd_obj.zoning_code = zoning
                if not deal.zoning.zoning_code:
                    deal.zoning.zoning_code = zoning
            if assessed is not None:
                pd_obj.assessed_value = assessed
            if sale_date:
                pd_obj.last_sale_date = sale_date
            if sale_price is not None:
                pd_obj.last_sale_price = sale_price
            if year_built_raw:
                try:
                    pd_obj.year_built = int(year_built_raw)
                except (TypeError, ValueError):
                    pass

            deal.provenance.field_sources["property_records"] = f"arcgis_gis:{gis_url[:60]}"
            logger.info(
                "PROPERTY RECORDS (ArcGIS): parcel=%s, owner=%s, zoning=%s, assessed=$%s",
                parcel_id, owner, zoning,
                f"{assessed:,.0f}" if assessed else "n/a",
            )
            return  # success — stop trying field names

        except Exception as exc:
            logger.warning("PROPERTY RECORDS (ArcGIS field=%s): %s", field, exc)
            continue

    logger.info("PROPERTY RECORDS (ArcGIS): no match at %s", gis_url[:80])


def _fetch_osm_parcel(deal: DealData, pd_obj: ParcelData, addr) -> None:
    """Last-resort: query Nominatim for address confirmation + basic info."""
    lat = addr.latitude
    lon = addr.longitude
    if not lat or lat == 0.0:
        return
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json"},
            headers={"User-Agent": "DealDesk-CRE/1.0"},
            timeout=10,
        )
        data = resp.json()
        address_data = data.get("address", {})
        # OSM doesn't provide ownership/assessed value, but confirms
        # the address and may give a postcode/suburb
        suburb = address_data.get("suburb") or address_data.get("neighbourhood")
        if suburb and not deal.provenance.field_sources.get("neighborhood"):
            deal.provenance.field_sources["neighborhood"] = suburb
            logger.info("OSM reverse geocode: neighborhood=%s", suburb)
    except Exception as exc:
        logger.warning("OSM reverse geocode failed: %s", exc)


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
# COMP PIPELINE — ZORI, Census rents, Craigslist, OPA sales, Redfin
# ═══════════════════════════════════════════════════════════════════════════

# Canonical Craigslist subdomain slugs. Widen as needed.
_CRAIGSLIST_SLUGS: Dict[tuple, str] = {
    ("PA", "Philadelphia"):   "philadelphia",
    ("PA", "Pittsburgh"):     "pittsburgh",
    ("NJ", "Newark"):         "newjersey",
    ("NJ", "Jersey City"):    "newjersey",
    ("NY", "New York"):       "newyork",
    ("NY", "Brooklyn"):       "newyork",
    ("DC", "Washington"):     "washingtondc",
    ("MD", "Baltimore"):      "baltimore",
    ("VA", "Richmond"):       "richmond",
    ("MA", "Boston"):         "boston",
    ("IL", "Chicago"):        "chicago",
    ("TX", "Houston"):        "houston",
    ("TX", "Dallas"):         "dallas",
    ("CA", "Los Angeles"):    "losangeles",
    ("CA", "San Francisco"):  "sfbay",
    ("FL", "Miami"):          "miami",
    ("FL", "Tampa"):          "tampa",
    ("GA", "Atlanta"):        "atlanta",
    ("CO", "Denver"):         "denver",
    ("AZ", "Phoenix"):        "phoenix",
    ("WA", "Seattle"):        "seattle",
    ("OR", "Portland"):       "portland",
    ("MN", "Minneapolis"):    "minneapolis",
    ("MO", "St. Louis"):      "stlouis",
    ("OH", "Columbus"):       "columbus",
    ("OH", "Cleveland"):      "cleveland",
    ("MI", "Detroit"):        "detroit",
    ("NC", "Charlotte"):      "charlotte",
    ("NC", "Raleigh"):        "raleigh",
    ("TN", "Nashville"):      "nashville",
    ("TN", "Memphis"):        "memphis",
}


def _get_craigslist_city_slug(state: str, city: str) -> str:
    key = (state.upper()[:2], city.strip())
    if key in _CRAIGSLIST_SLUGS:
        return _CRAIGSLIST_SLUGS[key]
    for (s, _c), slug in _CRAIGSLIST_SLUGS.items():
        if s == state.upper()[:2]:
            return slug
    logger.warning("CRAIGSLIST: no slug for %s, %s", city, state)
    return ""


def _fetch_zori_rent(zip_code: str, md: MarketData) -> None:
    """Zillow ZORI: ZIP-level median asking rent + YoY trend. Writes md.zori_*."""
    url = (
        "https://files.zillowstatic.com/research/public_csvs/zori/"
        "Zip_zori_uc_sfrcondomfr_sm_month.csv"
    )
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        reader = csv_mod.DictReader(io.StringIO(resp.text))
        target_row = None
        for row in reader:
            if str(row.get("RegionName", "")).zfill(5) == zip_code.zfill(5):
                target_row = row
                break
        if not target_row:
            logger.warning("ZORI: no data for zip %s", zip_code)
            return
        date_cols = sorted([
            k for k in target_row
            if k and len(k) == 10 and k[4] == "-" and k[7] == "-"
            and target_row[k]
        ])
        if not date_cols:
            return
        latest = _safe_float(target_row[date_cols[-1]])
        md.zori_median_rent = latest
        if len(date_cols) >= 13:
            prior = _safe_float(target_row[date_cols[-13]])
            if prior and prior > 0 and latest:
                pct = ((latest - prior) / prior) * 100
                md.zori_rent_trend = (
                    f"+{pct:.1f}% YoY" if pct >= 0 else f"{pct:.1f}% YoY"
                )
        logger.info(
            "ZORI: zip=%s rent=$%.0f trend=%s",
            zip_code, latest or 0, md.zori_rent_trend,
        )
    except Exception as exc:
        logger.warning("ZORI failed: %s", exc)


def _fetch_census_rents(state_fips: str, county_fips: str,
                         tract: str, md: MarketData) -> None:
    """ACS 5-yr 2022 median contract rent by bedroom (B25031_004E/5E/6E)."""
    if not all([state_fips, county_fips, tract]):
        logger.warning("CENSUS RENTS: missing FIPS/tract")
        return
    # Tract codes at ACS are 6-digit. Input tract may be 11-digit GEOID —
    # in that case the last 6 digits are the tract number.
    tract_clean = tract.replace(".", "")
    if len(tract_clean) >= 11:
        tract_clean = tract_clean[-6:]
    tract_clean = tract_clean.zfill(6)
    variables = "B25031_004E,B25031_005E,B25031_006E"
    url = (
        f"https://api.census.gov/data/2022/acs/acs5"
        f"?get={variables}"
        f"&for=tract:{tract_clean}"
        f"&in=state:{state_fips}%20county:{county_fips}"
    )
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if len(data) < 2:
            return
        row = dict(zip(data[0], data[1]))
        md.census_median_rent_1br = _safe_float(row.get("B25031_004E"))
        md.census_median_rent_2br = _safe_float(row.get("B25031_005E"))
        md.census_median_rent_3br = _safe_float(row.get("B25031_006E"))
        logger.info(
            "CENSUS RENTS: 1BR=$%s 2BR=$%s 3BR=$%s",
            md.census_median_rent_1br,
            md.census_median_rent_2br,
            md.census_median_rent_3br,
        )
    except Exception as exc:
        logger.warning("CENSUS RENTS failed: %s", exc)


def _fetch_craigslist_rentals(zip_code: str, city_slug: str,
                               deal: DealData,
                               max_results: int = 10) -> None:
    """Craigslist 2BR RSS → append RentComp rows into deal.comps.rent_comps.

    Craigslist frequently blocks non-browser user agents and has deprecated
    many RSS endpoints, so this fetch is treated as best-effort and warnings
    are not escalated.
    """
    if not city_slug:
        return
    url = (
        f"https://{city_slug}.craigslist.org/search/apa"
        f"?postal={zip_code}&bedrooms=2&format=rss"
    )
    try:
        resp = requests.get(
            url, timeout=_REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; DealDesk/1.0)"},
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = root.findall(".//item") or root.findall("channel/item")
        count = 0
        for item in items[:max_results]:
            title_elem = item.find("title")
            title = (title_elem.text if title_elem is not None else "") or ""
            price_m = re.search(r"\$([0-9,]+)", title)
            price = (
                _safe_float(price_m.group(1).replace(",", ""))
                if price_m else None
            )
            br_m = re.search(
                r"(\d)\s*(?:br|bed|bedroom)", title, re.IGNORECASE,
            )
            beds = int(br_m.group(1)) if br_m else 0
            if price and price > 200:
                # Use the existing Pydantic RentComp (models.py line 446).
                # Field names: address, unit_type, beds, monthly_rent, source.
                # Listing title goes into `source_notes` via source field.
                deal.comps.rent_comps.append(RentComp(
                    address=zip_code,
                    unit_type=f"{beds}BR" if beds else "Unknown",
                    beds=beds or None,
                    monthly_rent=price,
                    source=f"Craigslist: {title[:60]}",
                ))
                count += 1
        logger.info("CRAIGSLIST: zip=%s fetched %d listings", zip_code, count)
    except Exception as exc:
        logger.warning("CRAIGSLIST failed (%s/%s): %s",
                       city_slug, zip_code, exc)


def _fetch_opa_nearby_sales(lat: float, lon: float, deal: DealData) -> None:
    """Philadelphia OPA: recent nearby multifamily sales → SaleComp list."""
    r = 0.005  # ~0.35 mi at PHL latitude
    sql = (
        "SELECT location, sale_date, sale_price, total_area, "
        "total_livable_area, number_stories, unit_count, "
        "category_code_description, parcel_number, lat, lng "
        "FROM opa_properties_public "
        f"WHERE lat BETWEEN {lat - r} AND {lat + r} "
        f"AND lng BETWEEN {lon - r} AND {lon + r} "
        "AND sale_price > 50000 "
        "AND sale_date >= '2022-01-01' "
        "AND category_code_description ILIKE '%multi%' "
        "ORDER BY sale_date DESC LIMIT 10"
    )
    try:
        resp = requests.get(
            "https://phl.carto.com/api/v2/sql",
            params={"q": sql}, timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json().get("rows", [])
        logger.info("OPA NEARBY SALES: %d comps", len(rows))
        for row in rows:
            price = _safe_float(row.get("sale_price"))
            area = (_safe_float(row.get("total_livable_area"))
                    or _safe_float(row.get("total_area")))
            units = _safe_float(row.get("unit_count")) or 1
            dlat = float(row.get("lat") or lat) - lat
            dlon = float(row.get("lng") or lon) - lon
            dist = math.sqrt(dlat ** 2 + dlon ** 2) * 69.0
            # Existing Pydantic SaleComp field names: sq_ft, num_units,
            # distance_miles, source.
            deal.comps.sale_comps.append(SaleComp(
                address=row.get("location", ""),
                sale_date=str(row.get("sale_date", ""))[:10],
                sale_price=price,
                price_per_sf=(price / area
                              if price and area and area > 0 else None),
                price_per_unit=(price / units
                                if price and units > 0 else None),
                num_units=int(units) if units else None,
                sq_ft=int(area) if area else None,
                distance_miles=round(dist, 2),
                source=f"OPA: {row.get('category_code_description', '') or 'Multifamily'}",
            ))
    except Exception as exc:
        logger.warning("OPA NEARBY SALES failed: %s", exc)


def _fetch_redfin_sales(zip_code: str, deal: DealData) -> None:
    """Redfin gis-csv export for recent sales in the ZIP.

    Redfin rate-limits unauthenticated requests and can return 403 or change
    the region_id contract; treat as best-effort. Requires a browser UA.
    """
    url = (
        "https://www.redfin.com/stingray/api/gis-csv"
        f"?al=1&num_homes=50&ord=redfin-recommended-asc"
        f"&page_number=1&region_id={zip_code}&region_type=2"
        f"&sold_within_days=730&uipt=2,3&v=8"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/csv,*/*",
        "Referer": "https://www.redfin.com/",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code != 200:
            logger.warning(
                "REDFIN: status=%d zip=%s", resp.status_code, zip_code,
            )
            return
        lines = resp.text.splitlines()
        header_idx = next(
            (i for i, l in enumerate(lines)
             if "ADDRESS" in l.upper() or "PRICE" in l.upper()),
            0,
        )
        reader = csv_mod.DictReader(
            io.StringIO("\n".join(lines[header_idx:])),
        )
        count = 0
        for row in reader:
            price = _safe_float(
                str(row.get("PRICE", "")).replace("$", "").replace(",", ""),
            )
            if not price or price < 50000:
                continue
            sqft = _safe_float(
                str(row.get("SQUARE FEET", "")).replace(",", ""),
            )
            ppsf = _safe_float(
                str(row.get("$/SQUARE FEET", "")).replace("$", "")
                                                  .replace(",", ""),
            )
            beds = _safe_float(str(row.get("BEDS", "")))
            deal.comps.sale_comps.append(SaleComp(
                address=row.get("ADDRESS", ""),
                sale_date=str(row.get("SOLD DATE", ""))[:10],
                sale_price=price,
                price_per_sf=ppsf,
                sq_ft=int(sqft) if sqft else None,
                num_units=int(beds) if beds else None,
                source=f"Redfin: {int(beds) if beds else '?'}BR",
            ))
            count += 1
            if count >= 8:
                break
        logger.info("REDFIN: zip=%s fetched %d comps", zip_code, count)
    except Exception as exc:
        logger.warning("REDFIN failed: %s", exc)


def _compute_market_rents(deal: DealData) -> None:
    """Compute the quality-adjusted market rent for every unit.

    - Picks the dominant bedroom count from the rent roll
    - Pulls the matching HUD FMR (fmr_studio/fmr_1br/2br/3br)
    - Applies the renovation tier multiplier
    - Writes the result to deal.assumptions.quality_adjusted_market_rent and
      to each unit_mix row's ``market_rent`` key
    - Cross-checks (log-only) against ZORI and Census medians
    """
    a  = deal.assumptions
    md = deal.market_data

    tier_val = getattr(a, "renovation_tier",
                       RenovationTier.LIGHT_COSMETIC.value)
    # Accept either a RenovationTier enum or its string value.
    if isinstance(tier_val, RenovationTier):
        tier_val = tier_val.value
    multiplier = RENOVATION_TIER_MULTIPLIERS.get(tier_val, 0.90)

    units = getattr(deal.extracted_docs, "unit_mix", []) or []
    if not units:
        logger.warning("MARKET RENTS: no units in rent roll — skipping")
        return

    # Parse dominant bedroom count. unit_mix rows may carry either an integer
    # `beds`/`bedrooms` key or a string `unit_type` like "1BR" / "2 Bed".
    br_counts: Counter = Counter()
    for u in units:
        br_val = u.get("beds") or u.get("bedrooms")
        if br_val is None:
            ut = str(u.get("unit_type", ""))
            m = re.search(r"(\d)", ut)
            br_val = int(m.group(1)) if m else 0
            if "studio" in ut.lower():
                br_val = 0
        try:
            br_counts[int(br_val)] += int(u.get("count") or 1)
        except (TypeError, ValueError):
            br_counts[0] += 1
    dominant_br = br_counts.most_common(1)[0][0] if br_counts else 2

    # Real HUD FMR attribute names on MarketData: fmr_studio/1br/2br/3br.
    # fmr_4br is not in the model; it lives in provenance.field_sources.
    fmr_map = {
        0: md.fmr_studio,
        1: md.fmr_1br,
        2: md.fmr_2br,
        3: md.fmr_3br,
    }
    base_fmr = fmr_map.get(dominant_br) or md.fmr_2br
    if not base_fmr:
        logger.warning(
            "MARKET RENTS: no HUD FMR available for %dBR — "
            "cannot compute market rents", dominant_br,
        )
        return

    computed_rent = round(float(base_fmr) * multiplier, 0)
    a.quality_adjusted_market_rent = computed_rent

    logger.info(
        "MARKET RENTS: tier=%s multiplier=%.2f HUD_FMR_%dBR=$%.0f "
        "→ computed_market_rent=$%.0f",
        tier_val, multiplier, dominant_br, base_fmr, computed_rent,
    )
    if md.zori_median_rent:
        logger.info(
            "MARKET RENTS cross-check: ZORI_zip=$%.0f computed=$%.0f diff=%.1f%%",
            md.zori_median_rent, computed_rent,
            ((computed_rent - md.zori_median_rent) / md.zori_median_rent * 100),
        )
    census_rent = {
        1: md.census_median_rent_1br,
        2: md.census_median_rent_2br,
        3: md.census_median_rent_3br,
    }.get(dominant_br)
    if census_rent:
        logger.info(
            "MARKET RENTS cross-check: Census_%dBR=$%.0f computed=$%.0f diff=%.1f%%",
            dominant_br, census_rent, computed_rent,
            ((computed_rent - census_rent) / census_rent * 100),
        )

    for u in units:
        u["market_rent"] = computed_rent
    logger.info(
        "MARKET RENTS: wrote $%.0f market_rent to %d units",
        computed_rent, len(units),
    )


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

    # ── STEP 7B: Property records (parcel, owner, zoning) ─────────
    logger.info("Step 7B: Property records...")
    _fetch_property_records(deal)

    # ── STEP 7C: Philadelphia zoning fallback via Atlas/ArcGIS ────
    if (not deal.zoning.zoning_code
            and (addr.state or "").upper() == "PA"
            and (addr.city or "").lower() == "philadelphia"
            and addr.latitude and addr.longitude):
        try:
            r = requests.get(
                "https://services.arcgis.com/fLeGjb7u4uXqeF9q/arcgis/rest/services/"
                "Zoning_BaseDistricts/FeatureServer/0/query",
                params={
                    "geometry": f"{addr.longitude},{addr.latitude}",
                    "geometryType": "esriGeometryPoint",
                    "inSR": "4326",
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": "CODE,LONG_CODE",
                    "returnGeometry": "false",
                    "f": "json",
                },
                timeout=_REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            feats = r.json().get("features", []) or []
            if feats:
                attrs = feats[0].get("attributes", {}) or {}
                zc = attrs.get("CODE") or attrs.get("LONG_CODE")
                if zc:
                    deal.zoning.zoning_code = zc
                    if deal.parcel_data:
                        if not deal.parcel_data.zoning_code:
                            deal.parcel_data.zoning_code = zc
                    else:
                        deal.parcel_data = ParcelData(zoning_code=zc)
                    logger.info("ATLAS zoning lookup: %s", zc)
        except Exception as exc:
            logger.warning("ATLAS zoning lookup failed: %s", exc)

    # ── STEP 7D: Transit & Amenities (OSM + Google Places) ───────
    logger.info("Step 7D: Transit & Amenities...")
    _fetch_transit_and_amenities(deal)

    # ── STEP 7E: Comp pipeline + quality-adjusted market rent ────
    #   Runs here because HUD FMR (Step 6), lat/lon (Step 2), OPA
    #   zoning (Step 7B) and census_tract/fips_code are all populated.
    logger.info("Step 7E: Comp pipeline...")
    _zip    = getattr(deal.address, "zip_code", "") or ""
    _city   = getattr(deal.address, "city", "") or ""
    _state  = getattr(deal.address, "state", "") or ""
    _slug   = _get_craigslist_city_slug(_state, _city) if _state and _city else ""
    _lat    = getattr(deal.address, "latitude", None)
    _lon    = getattr(deal.address, "longitude", None)

    if _zip:
        _fetch_zori_rent(_zip, deal.market_data)

    _full_fips   = str(deal.address.fips_code or "")
    _state_fips  = _full_fips[:2] if len(_full_fips) >= 2 else ""
    _county_fips = _full_fips[2:] if len(_full_fips) >= 5 else ""
    _tract       = str(deal.address.census_tract or "")
    if _state_fips and _county_fips and _tract:
        _fetch_census_rents(_state_fips, _county_fips, _tract, deal.market_data)

    if _zip and _slug:
        _fetch_craigslist_rentals(_zip, _slug, deal)

    # OPA nearby-sales is Philly-specific; only run when we're in PA/Philly.
    if (_state or "").upper() == "PA" and (_city or "").lower() == "philadelphia":
        if _lat and _lon:
            _fetch_opa_nearby_sales(_lat, _lon, deal)

    if _zip:
        _fetch_redfin_sales(_zip, deal)

    # Quality-adjusted market-rent engine (requires HUD FMR from Step 6).
    _compute_market_rents(deal)

    logger.info(
        "COMPS SUMMARY: %d rent comps, %d sale comps | market_rent=$%s",
        len(deal.comps.rent_comps),
        len(deal.comps.sale_comps),
        deal.assumptions.quality_adjusted_market_rent or "N/A",
    )

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

    # ── MARKET DATA SUMMARY ──────────────────────────────────────
    _opa_found = bool(deal.parcel_data and deal.parcel_data.parcel_id)
    logger.info("=== MARKET DATA SUMMARY ===")
    logger.info("  Geocode: lat=%.6f lng=%.6f", addr.latitude or 0, addr.longitude or 0)
    logger.info("  FRED: T10=%.2f%% SOFR=%.2f%% MTG30=%.2f%% CPI=%.2f%%",
                (md.dgs10_rate or 0) * 100, (md.sofr_rate or 0) * 100,
                (md.mortgage30_rate or 0) * 100, (md.cpi_yoy or 0) * 100)
    logger.info("  HUD FMR 2BR: $%.0f", md.fmr_2br or 0)
    logger.info("  OPA parcel: %s", "found" if _opa_found else "not found")
    logger.info("  FEMA zone: %s", md.fema_flood_zone or "not determined")
    logger.info("  EPA flags: %d", len(md.epa_env_flags or []))
    logger.info("===========================")

    logger.info("Market data enrichment complete for %s", deal.deal_id)
    return deal
