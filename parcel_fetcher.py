"""
parcel_fetcher.py — DealDesk Parcel & Zoning Fetcher
=====================================================
Registry-routed fetcher for US municipal parcel + zoning data.

Parcel source cascade (best to generic):
    1. Philadelphia OPA (Carto SQL)             — Philly only
    2. DC DCGIS REST                            — DC only
    3. County/municipal GIS (ArcGIS FeatureServer)
       — any registry row with gis_parcel_url
    4. OSM Nominatim reverse geocode            — address confirmation fallback

Zoning source cascade:
    1. ecode360-specific scraper  (~3,400 municipalities)
    2. amlegal-specific scraper
    3. municode-specific scraper
    4. Generic HTML-strip fallback

The actual LLM-based structured zoning extraction (FAR, setbacks, permitted
uses, max height) is performed by Prompt 3A in market.py. This module's job
is to return CLEAN zoning code text so Prompt 3A has good input.

Public API:
    fetch_parcel(deal)              — populates deal.parcel_data
    fetch_zoning_text(url, code_platform) -> Optional[str]
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests

from models.models import DealData, ParcelData

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
_REQUEST_TIMEOUT = 30  # seconds for all HTTP calls

_PHL_CARTO_SQL = "https://phl.carto.com/api/v2/sql"
_DC_PROPERTY_URL = (
    "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/"
    "Property_and_Land_WebMercator/MapServer/56/query"
)


# ── Internal helpers ─────────────────────────────────────────────────────────
def _safe_float(val) -> Optional[float]:
    if val is None or val == "." or val == "":
        return None
    try:
        v = float(val)
        return v if v >= -999999 else None
    except (TypeError, ValueError):
        return None


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


# ═══════════════════════════════════════════════════════════════════════════
# PARCEL ADAPTERS
# ═══════════════════════════════════════════════════════════════════════════

def _fetch_phl_opa(deal: DealData, pd_obj: ParcelData, addr) -> None:
    """Philadelphia Office of Property Assessment via Carto SQL API."""
    street_number, street_name = _split_street(addr.street)
    if not street_number or not street_name:
        logger.warning("PROPERTY RECORDS (PHL): no street number/name — skipping")
        return
    # OPA `location` field is uppercase, abbreviated, no apartment numbers
    # e.g. "2-08 S 46TH ST". Skip directionals; use the distinctive street token.
    name_tokens = street_name.upper().split() if street_name else []
    DIRECTIONALS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}
    # Strip trailing "." on the token before the set check so "S." / "N." /
    # "E." / "W." / "N.E." / etc. are recognised as directionals and skipped.
    # Without this, "2-8 S. 46th Street" picks "S." as the distinctive token
    # and OPA's location filter never finds the parcel.
    name_token = next(
        (t for t in name_tokens if t.replace(".", "") not in DIRECTIONALS),
        "",
    )
    patterns = [
        f"{street_number} %{name_token}%",   # "2 %46TH%"
        f"{street_number}-%{name_token}%",   # "2-%46TH%" (range form)
        f"%{street_number}%{name_token}%",   # very loose
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
        logger.info("OPA QUERY: pattern='%s'", pat)
        try:
            resp = requests.get(
                _PHL_CARTO_SQL,
                params={"q": query}, timeout=_REQUEST_TIMEOUT,
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
    street_number, _ = _split_street(addr.street)
    parcel_search = f"{street_number}%" if street_number else "%"
    params = {
        "where": f"SSL LIKE '{parcel_search}'",
        "outFields": "*",
        "f": "json",
    }
    try:
        resp = requests.get(_DC_PROPERTY_URL, params=params, timeout=_REQUEST_TIMEOUT)
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
    """Generic ArcGIS REST parcel query for any county with a GIS parcel URL in
    the municipal registry. Tries common address-field-name and parcel-ID
    variants used across county GIS systems.
    """
    street_number, street_name = _split_street(addr.street)
    if not street_number:
        logger.warning("PROPERTY RECORDS (ArcGIS): no street number — skipping")
        return

    base = gis_url.rstrip("/")
    if not base.endswith("/query"):
        base = base + "/query"

    address_fields = ["SITEADDRESS", "SITE_ADDRESS", "ADDRESS", "FULL_ADDRESS",
                      "PROP_ADDRESS", "LOCATION", "ADDR", "SITUS_ADDRESS"]
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
    """Last-resort: Nominatim reverse geocode for address confirmation + neighborhood."""
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
        suburb = address_data.get("suburb") or address_data.get("neighbourhood")
        if suburb and not deal.provenance.field_sources.get("neighborhood"):
            deal.provenance.field_sources["neighborhood"] = suburb
            logger.info("OSM reverse geocode: neighborhood=%s", suburb)
    except Exception as exc:
        logger.warning("OSM reverse geocode failed: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════
# PARCEL DISPATCHER — public entry point
# ═══════════════════════════════════════════════════════════════════════════

def fetch_parcel(deal: DealData) -> None:
    """Populate deal.parcel_data from the best available source.

    Source cascade:
        1. Philly OPA   (state=PA, city=Philadelphia)
        2. DC DCGIS     (state=DC)
        3. Generic ArcGIS FeatureServer (from registry gis_parcel_url in provenance)
        4. OSM Nominatim reverse geocode (address confirmation only)
    """
    addr = deal.address
    state = (addr.state or "").strip().upper()
    city = (addr.city or "").strip()

    if deal.parcel_data is None:
        deal.parcel_data = ParcelData()
    pd_obj = deal.parcel_data

    # SOURCE 1: Philadelphia OPA
    if state == "PA" and city.lower() == "philadelphia":
        _fetch_phl_opa(deal, pd_obj, addr)
        return

    # SOURCE 2: DC DCGIS
    if state == "DC":
        _fetch_dc_dcgis(deal, pd_obj, addr)
        return

    # SOURCE 3: Generic ArcGIS REST endpoint from the registry
    gis_url = deal.provenance.field_sources.get("gis_parcel_url")
    if gis_url:
        _fetch_arcgis_parcel(deal, pd_obj, addr, gis_url)
        if pd_obj.parcel_id:
            return

    # SOURCE 4: OSM Nominatim fallback
    _fetch_osm_parcel(deal, pd_obj, addr)


# ═══════════════════════════════════════════════════════════════════════════
# ZONING TEXT SCRAPERS — platform-aware
# ═══════════════════════════════════════════════════════════════════════════

# Patterns removed from every scraper's output to reduce LLM noise.
_COMMON_NOISE = [
    r"Accept All Cookies[^.]*\.",
    r"We use cookies[^.]*\.",
    r"Cookie (Policy|Settings|Preferences)[^.]*\.",
    r"Skip to (Main |)[Cc]ontent",
    r"(Main |Site )?Navigation",
    r"Table of Contents",
    r"Breadcrumbs?",
    r"Print this page",
    r"Previous Section",
    r"Next Section",
    r"Follow us on[^.]*\.",
]


def _strip_html_to_text(html: str) -> str:
    """Regex-only HTML → text. Removes script/style/nav/header/footer tags
    and collapses whitespace.
    """
    # Drop script/style blocks entirely.
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Drop nav/header/footer blocks — typical navigation noise.
    for tag in ("nav", "header", "footer", "aside"):
        html = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", " ",
                      html, flags=re.DOTALL | re.IGNORECASE)
    # Strip remaining tags.
    text = re.sub(r"<[^>]+>", " ", html)
    # HTML entities we commonly see.
    text = (text.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&#160;", " "))
    # Remove known common noise phrases.
    for noise in _COMMON_NOISE:
        text = re.sub(noise, " ", text, flags=re.IGNORECASE)
    # Collapse whitespace.
    return re.sub(r"\s+", " ", text).strip()


def _scrape_ecode360(html: str) -> str:
    """ecode360 chapter pages put the body in #content-area / .code-content.
    We isolate that region before generic stripping to drop the big left-rail
    nav of all chapters in the code.
    """
    # Pattern A: the main content container ecode360 uses
    m = re.search(
        r'<div[^>]+(?:id="content-area"|class="[^"]*code-content[^"]*")[^>]*>(.*?)</div>\s*(?:<div[^>]+id="footer"|<footer)',
        html, flags=re.DOTALL | re.IGNORECASE,
    )
    body = m.group(1) if m else html
    return _strip_html_to_text(body)


def _scrape_amlegal(html: str) -> str:
    """American Legal Publishing (amlegal.com / codelibrary.amlegal.com).
    Content is typically inside <div id="main-content"> or <article>.
    """
    m = re.search(
        r'<(?:div|article)[^>]+(?:id="main-content"|class="[^"]*document-content[^"]*"|role="main")[^>]*>(.*?)</(?:div|article)>',
        html, flags=re.DOTALL | re.IGNORECASE,
    )
    body = m.group(1) if m else html
    return _strip_html_to_text(body)


def _scrape_municode(html: str) -> str:
    """Municode / CivicPlus library.municode.com pages.
    Main content under <div id="codeBody"> / <div class="chunk-content">.
    """
    m = re.search(
        r'<div[^>]+(?:id="codeBody"|class="[^"]*chunk-content[^"]*"|id="ChunkContainer")[^>]*>(.*?)</div>\s*(?:<div[^>]+class="[^"]*footer|<footer)',
        html, flags=re.DOTALL | re.IGNORECASE,
    )
    body = m.group(1) if m else html
    return _strip_html_to_text(body)


def _scrape_generic(html: str) -> str:
    """Platform-agnostic fallback. Just the generic HTML strip."""
    return _strip_html_to_text(html)


_PLATFORM_SCRAPERS = {
    "ecode360":  _scrape_ecode360,
    "amlegal":   _scrape_amlegal,
    "municode":  _scrape_municode,
}


def fetch_zoning_text(url: str, code_platform: Optional[str] = None,
                      max_chars: int = 12000) -> Optional[str]:
    """Fetch a zoning-chapter URL and return cleaned text for LLM extraction
    (Prompt 3A in market.py consumes this).

    Dispatches by `code_platform` (from the registry's code_platform column):
        - "ecode360"  → ecode360 scraper
        - "amlegal"   → amlegal scraper
        - "municode"  → municode scraper
        - anything else → generic HTML-strip fallback
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
    except Exception as exc:
        logger.warning("Zoning code scrape failed (%s): %s", url, exc)
        return None

    platform_key = (code_platform or "").strip().lower()
    scraper = _PLATFORM_SCRAPERS.get(platform_key, _scrape_generic)
    try:
        text = scraper(html)
    except Exception as exc:
        # Platform-specific scraper blew up (unexpected HTML) — fall back clean.
        logger.warning("Zoning scraper (%s) failed, falling back: %s",
                       platform_key or "generic", exc)
        text = _scrape_generic(html)

    if not text or len(text) < 200:
        logger.warning("Zoning code scrape returned too little text (%d chars, platform=%s)",
                       len(text) if text else 0, platform_key or "generic")
        return None

    logger.info("Zoning text scraped: %d chars (platform=%s)",
                len(text), platform_key or "generic")
    return text[:max_chars]
