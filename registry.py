"""
registry.py — DealDesk Municipal Registry Loader
=================================================
Wraps data/municipal_registry.csv so other modules (market.py,
parcel_fetcher.py) share a single loader + lookup implementation.

Exposes:
    lookup(deal)        — find the registry row for a DealData's address.
    apply(row, deal)    — write matched registry fields onto DealData.

Both functions were previously defined inline in market.py; moving them
here lets parcel_fetcher.py reuse the same lookup without creating an
import cycle.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from config import MUNICIPAL_REGISTRY_CSV
from models.models import DealData

logger = logging.getLogger(__name__)

# ── Cached DataFrame ─────────────────────────────────────────────────────────
_cached_df: Optional[pd.DataFrame] = None


def _load() -> Optional[pd.DataFrame]:
    """Lazy-load the registry once per process."""
    global _cached_df
    if _cached_df is not None:
        return _cached_df
    try:
        _cached_df = pd.read_csv(MUNICIPAL_REGISTRY_CSV, dtype=str)
        logger.info("Municipal registry loaded — %d rows", len(_cached_df))
    except Exception as exc:
        logger.warning("Failed to load municipal registry: %s", exc)
        _cached_df = None
    return _cached_df


# ── Internal scalar helpers (duplicated so registry doesn't depend on market) ─
def _safe_int(val) -> Optional[int]:
    try:
        v = int(val)
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None


def _safe_float(val) -> Optional[float]:
    if val is None or val == "." or val == "":
        return None
    try:
        v = float(val)
        return v if v >= -999999 else None
    except (TypeError, ValueError):
        return None


# ── Public API ───────────────────────────────────────────────────────────────
def lookup(deal: DealData) -> Optional[pd.Series]:
    """
    Match a DealData to a municipal-registry row.

      primary key:   fips_county
      fallback:      municipality_name + state (progressively looser matching)

    Returns the matched row as a pandas Series, or None.
    """
    df = _load()
    if df is None:
        return None

    fips_county = deal.address.fips_code
    city = deal.address.city
    state = deal.address.state

    # Primary: fips_county
    if fips_county:
        m = df[df["fips_county"].str.strip() == fips_county.strip()]
        if len(m) > 0:
            logger.info("Municipal registry: matched by fips_county=%s", fips_county)
            return m.iloc[0]

    # Fallback: municipality_name + state
    if city and state:
        city_lower = city.strip().lower()
        state_upper = state.strip().upper()
        state_mask = df["state"].str.strip().str.upper() == state_upper
        state_df = df[state_mask]

        if len(state_df) == 0:
            logger.warning("Municipal registry: no entries for state=%s", state)
        else:
            names_lower = state_df["municipality_name"].str.strip().str.lower()

            # 1. exact
            m = state_df[names_lower == city_lower]
            # 2. registry name contains city
            if len(m) == 0:
                m = state_df[names_lower.str.contains(city_lower, regex=False, na=False)]
            # 3. city contains registry name (e.g. "washington" in "washington, d.c.")
            if len(m) == 0:
                m = state_df[names_lower.apply(
                    lambda n: n.split(",")[0].strip() in city_lower
                    or city_lower in n.split(",")[0].strip()
                )]
            # 4. normalize both sides
            if len(m) == 0:
                def _norm(n: str) -> str:
                    for strip in ("city of ", "town of ", "village of ",
                                  "township of ", "borough of "):
                        if n.startswith(strip):
                            n = n[len(strip):]
                    for strip in (" city", " town", " village", " township",
                                  " borough", ", d.c.", ", dc"):
                        if n.endswith(strip):
                            n = n[:-len(strip)]
                    return n.strip()
                city_norm = _norm(city_lower)
                m = state_df[names_lower.apply(lambda n: _norm(n) == city_norm)]

            if len(m) > 0:
                logger.info("Municipal registry: matched '%s' → '%s', %s",
                            city, m.iloc[0]["municipality_name"], state)
                return m.iloc[0]

    logger.warning("Municipal registry: no match for fips=%s, city=%s, state=%s",
                   fips_county, city, state)
    return None


def apply(row: pd.Series, deal: DealData) -> None:
    """Write matched registry fields onto DealData (zoning URLs, population,
    income, school district, FIPS place, code platform). Idempotent.
    """

    def _get(field: str) -> Optional[str]:
        val = row.get(field)
        if pd.isna(val) or str(val).strip() == "":
            return None
        return str(val).strip()

    # Zoning URLs
    deal.zoning.municipal_code_url = _get("zoning_chapter_url") or _get("code_base_url")
    deal.zoning.zoning_code_chapter = _get("zoning_chapter_ref")

    # Registry URLs + code_platform into provenance (keys consumed elsewhere)
    prov = deal.provenance.field_sources
    for field in ("code_platform", "code_base_url", "zoning_chapter_url",
                  "assessor_url", "gis_parcel_url", "recorder_of_deeds_url",
                  "tax_collector_url"):
        val = _get(field)
        if val:
            prov[field] = val

    # Population
    pop = _safe_int(_get("population"))
    if pop:
        deal.market_data.population_3mi = pop
        prov["population_source"] = "municipal_registry"

    # Median household income
    income = _safe_float(_get("median_household_income"))
    if income:
        deal.market_data.median_hh_income_3mi = income
        prov["median_hh_income_source"] = "municipal_registry"

    # Median gross rent — provenance only (no direct model field)
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
