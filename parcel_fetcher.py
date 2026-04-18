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

from models.models import DealData, DeedRecord, ParcelData
from iasworld_fetcher import fetch_iasworld

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
_REQUEST_TIMEOUT = 30  # seconds for all HTTP calls

_PHL_CARTO_SQL = "https://phl.carto.com/api/v2/sql"
_DC_PROPERTY_URL = (
    "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/"
    "Property_and_Land_WebMercator/MapServer/56/query"
)

# NYC ACRIS — three linked Socrata datasets on data.cityofnewyork.us.
# Joined by document_id: Legals (address → docs) → Master (doc metadata) →
# Parties (grantor/grantee). borough field encoded as string "1".."5".
_NYC_LEGALS_URL  = "https://data.cityofnewyork.us/resource/8h5j-fqxa.json"
_NYC_MASTER_URL  = "https://data.cityofnewyork.us/resource/bnx9-e6tj.json"
_NYC_PARTIES_URL = "https://data.cityofnewyork.us/resource/636b-3b5g.json"

_NYC_CITY_TO_BOROUGH = {
    "NEW YORK": 1, "NEW YORK CITY": 1, "NYC": 1, "MANHATTAN": 1,
    "BRONX": 2, "THE BRONX": 2,
    "BROOKLYN": 3,
    "QUEENS": 4,
    "STATEN ISLAND": 5,
}
_NYC_COUNTY_TO_BOROUGH = {
    "NEW YORK": 1, "MANHATTAN": 1,
    "BRONX": 2,
    "KINGS": 3, "BROOKLYN": 3,
    "QUEENS": 4,
    "RICHMOND": 5, "STATEN ISLAND": 5,
}


# ── Internal helpers ─────────────────────────────────────────────────────────
def _safe_float(val) -> Optional[float]:
    if val is None or val == "." or val == "":
        return None
    try:
        v = float(val)
        return v if v >= -999999 else None
    except (TypeError, ValueError):
        return None


def _coerce_arcgis_date(val) -> Optional[str]:
    """Esri REST returns date fields as epoch-millisecond integers. Convert
    numerics to ISO ``YYYY-MM-DD``; pass strings through trimmed to 10 chars;
    reject obviously-sentinel zero values.
    """
    if val is None or val == "" or val == 0:
        return None
    # Numeric epoch-ms (int or float)
    if isinstance(val, (int, float)):
        from datetime import datetime, timezone
        try:
            # Milliseconds if > 10^11, else seconds
            secs = val / 1000.0 if val > 1e11 else float(val)
            return datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    s = str(val).strip()
    # Pure-digit string is almost certainly epoch ms
    if s.isdigit() and len(s) >= 10:
        from datetime import datetime, timezone
        try:
            n = int(s)
            secs = n / 1000.0 if n > 1e11 else float(n)
            return datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    return s[:10] if s else None


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

    # Follow-on: pull deed/title transfer history for this parcel.
    if parcel_number:
        _fetch_phl_deed_history(pd_obj, parcel_number)


def _fetch_phl_deed_history(pd_obj: ParcelData, parcel_number: str,
                            limit: int = 10) -> None:
    """Pull recorded deed transfers for a Philadelphia parcel from the
    rtt_summary (Real Estate Transfer Tax) Carto dataset, linked by
    opa_account_num. Populates pd_obj.deed_history with up to `limit`
    records, most-recent first.
    """
    # Numeric strip — opa_account_num on rtt_summary is digits-only.
    acct = re.sub(r"\D", "", parcel_number or "")
    if not acct:
        return
    query = (
        "SELECT document_id, recording_date, document_type, "
        "grantors, grantees, consideration_amount "
        "FROM rtt_summary "
        f"WHERE opa_account_num = '{acct}' "
        "ORDER BY recording_date DESC "
        f"LIMIT {limit}"
    )
    try:
        resp = requests.get(_PHL_CARTO_SQL, params={"q": query},
                            timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        rows = resp.json().get("rows", []) or []
    except Exception as exc:
        logger.warning("DEED HISTORY (PHL) failed for parcel %s: %s", acct, exc)
        return
    if not rows:
        logger.info("DEED HISTORY (PHL): no records for parcel %s", acct)
        return
    for r in rows:
        pd_obj.deed_history.append(DeedRecord(
            recording_date=r.get("recording_date"),
            document_type=r.get("document_type"),
            grantor=r.get("grantors"),
            grantee=r.get("grantees"),
            consideration_amount=_safe_float(r.get("consideration_amount")),
            document_id=r.get("document_id"),
        ))
    logger.info("DEED HISTORY (PHL): %d records pulled for parcel %s",
                len(rows), acct)


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
    # Most-recent sale fields — DCGIS commonly exposes SALEDATE / SALEPRICE.
    sale_date = _coerce_arcgis_date(
        attrs.get("SALEDATE") or attrs.get("SALE_DATE") or attrs.get("LASTSALEDATE")
    )
    sale_price = _safe_float(attrs.get("SALEPRICE") or attrs.get("SALE_PRICE") or attrs.get("LASTSALEPRICE"))
    if sale_date:
        pd_obj.last_sale_date = sale_date
    if sale_price is not None:
        pd_obj.last_sale_price = sale_price
    if zoning and not deal.zoning.zoning_code:
        deal.zoning.zoning_code = zoning
    # Reuse the ArcGIS multi-sale scanner — DCGIS attribute dicts follow the
    # same Esri schema, so SALEDATE1/2/3 columns (if present) get picked up.
    _extract_arcgis_multi_sale_deeds(attrs, pd_obj)
    deal.provenance.field_sources["property_records"] = "dc_dcgis"
    logger.info(
        "PROPERTY RECORDS: found parcel %s, owner=%s, zoning=%s, assessed=$%s, deeds=%d",
        parcel_number, owner, zoning,
        f"{market_value:,.0f}" if market_value is not None else "n/a",
        len(pd_obj.deed_history),
    )


_ARCGIS_DATE_FIELD_RE = re.compile(
    r"^(SALE|DEED|TRANSFER|CONVEY)_?DATE(?:_?(\d+))?$", re.IGNORECASE
)


def _extract_arcgis_multi_sale_deeds(attrs: dict, pd_obj: ParcelData) -> None:
    """Scan a single ArcGIS feature's attributes for multi-sale patterns like
    SALE_DATE_1 / SALE_PRICE_1 (or SALEDATE1 / SALEPRICE1, TRANSFER_DATE_2, etc.)
    and append each pair to pd_obj.deed_history. Avoids a second API call by
    reusing the primary parcel query's response.

    Matches date-field names by the shared regex ``(SALE|DEED|TRANSFER|CONVEY)_?DATE(_?N)?``
    and derives the sibling price field by swapping DATE → PRICE. Grantor/grantee
    are usually not carried on the parcel feature, so those stay None.
    """
    if not attrs:
        return
    pairs: list[tuple[str, str, str]] = []  # (suffix, date_key, price_key)
    for key in attrs.keys():
        m = _ARCGIS_DATE_FIELD_RE.match(str(key))
        if not m:
            continue
        suffix = m.group(2) or ""  # "", "1", "2", ...
        # Map date-key → matching price-key by substituting DATE → PRICE
        price_key_candidates = []
        k = str(key)
        for tok in ("DATE", "Date", "date"):
            if tok in k:
                price_key_candidates.append(k.replace(tok, "PRICE", 1))
                price_key_candidates.append(k.replace(tok, "Price", 1))
                price_key_candidates.append(k.replace(tok, "price", 1))
                break
        # Also try the VALUE variant (some schemas use SALE_VALUE_1)
        for tok in ("DATE", "Date", "date"):
            if tok in k:
                price_key_candidates.append(k.replace(tok, "VALUE", 1))
                break
        price_key = next((pk for pk in price_key_candidates if pk in attrs), None)
        pairs.append((suffix, str(key), price_key or ""))

    if not pairs:
        return

    # Sort by numeric suffix descending (most-recent first when schemas use
    # _1 = most-recent convention; the ambiguity is unavoidable, but any
    # deterministic ordering is better than dict insertion order).
    def _suffix_key(p):
        s = p[0]
        return int(s) if s.isdigit() else 0

    pairs.sort(key=_suffix_key)

    seen_dates = set()
    added = 0
    for _suffix, date_key, price_key in pairs:
        date_val = attrs.get(date_key)
        if not date_val or str(date_val).strip() in ("", "0", "None", "null"):
            continue
        date_str = _coerce_arcgis_date(date_val)
        if not date_str or date_str in seen_dates:
            continue
        seen_dates.add(date_str)
        price_val = _safe_float(attrs.get(price_key)) if price_key else None
        # Identify document type from field-name prefix
        prefix_m = re.match(r"^([A-Za-z]+)", date_key)
        prefix = (prefix_m.group(1) if prefix_m else "SALE").upper()
        doc_type_map = {
            "SALE":     "Deed (recorded sale)",
            "DEED":     "Deed",
            "TRANSFER": "Transfer / conveyance",
            "CONVEY":   "Conveyance",
        }
        pd_obj.deed_history.append(DeedRecord(
            recording_date=date_str,
            document_type=doc_type_map.get(prefix, "Deed"),
            grantor=None,
            grantee=None,
            consideration_amount=price_val,
            document_id=None,
        ))
        added += 1
    if added:
        logger.info("DEED HISTORY (ArcGIS multi-sale): %d rows from attribute scan", added)


def _resolve_parcel_layer_url(rest_url: str) -> Optional[str]:
    """Given any of the accepted ArcGIS URL forms, return a specific
    layer query URL of the form .../MapServer/<id>/query.

    Accepted inputs:
      - .../MapServer/<id>/query           (already specific — returned as-is)
      - .../MapServer/<id>                 (append /query)
      - .../MapServer                      (scan layers[], pick parcel-like one)
      - .../FeatureServer/<id>[/query]     (same treatment as MapServer)
      - .../rest/services                  (scan services+folders for a
                                            parcel service, then its layers)
    Caches discovery results in _ARCGIS_LAYER_CACHE.
    """
    if not rest_url:
        return None
    url = rest_url.rstrip("/")
    low = url.lower()

    # Already a /query endpoint — use as-is
    if low.endswith("/query"):
        return url
    # /MapServer/<id> or /FeatureServer/<id> — append /query
    if re.search(r"/(map|feature)server/\d+$", low):
        return url + "/query"
    # Root /MapServer or /FeatureServer — look inside for a parcel-ish layer
    if re.search(r"/(map|feature)server$", low):
        return _find_parcel_layer_on_server(url)
    # Catalog root /rest/services — crawl services + folders
    if low.endswith("/rest/services") or low.endswith("/services"):
        return _find_parcel_layer_on_catalog(url)
    # Unknown shape — last-ditch treat it as a /query endpoint
    return url + "/query"


_PARCEL_LAYER_RE = re.compile(
    r"(parcel|property|tax[_ ]?lot|real[_ ]?estate|cadastre|cadastral|assess)",
    re.IGNORECASE,
)


def _find_parcel_layer_on_server(server_url: str) -> Optional[str]:
    """Inside a MapServer/FeatureServer root, pick the first parcel-looking
    feature layer. Returns .../MapServer/<id>/query URL or None.
    """
    cache_key = ("layer", server_url)
    if cache_key in _ARCGIS_LAYER_CACHE:
        return _ARCGIS_LAYER_CACHE[cache_key]
    try:
        resp = requests.get(server_url, params={"f": "json"},
                            timeout=_REQUEST_TIMEOUT,
                            headers={"User-Agent": "DealDesk/1.0"})
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception as exc:
        logger.info("ArcGIS: layer discovery failed at %s: %s",
                    server_url[:80], exc)
        _ARCGIS_LAYER_CACHE[cache_key] = None
        return None
    layers = data.get("layers", []) or []
    # Rank layers: prefer canonical parcel feature layers; de-prioritize
    # annotation/label/anno/outline layers which don't carry attributes.
    _ANNO_RE = re.compile(r"(anno|label|outline|extent|index|overlay|buffer)", re.I)

    def _score(L):
        n = (L.get("name") or "").lower()
        if _ANNO_RE.search(n):
            return 7  # push annotation layers to the back
        if n in ("parcels", "parcel"):
            return 0
        if n.startswith("parcel") and "poly" in n:
            return 0  # "Parcelpoly" is the canonical feature layer in many schemas
        if n == "property" or n == "properties":
            return 1
        if _PARCEL_LAYER_RE.search(n):
            return 2
        return 9
    layers_sorted = sorted(layers, key=_score)
    for L in layers_sorted[:5]:
        if _score(L) <= 2:
            url = f"{server_url}/{L['id']}/query"
            _ARCGIS_LAYER_CACHE[cache_key] = url
            return url
    _ARCGIS_LAYER_CACHE[cache_key] = None
    return None


def _find_parcel_layer_on_catalog(catalog_url: str) -> Optional[str]:
    """Given a /rest/services catalog root, scan services + folders for
    one whose name hints at parcels/property/assessment, then drill into
    it to find a parcel feature layer. Returns .../MapServer/<id>/query or None.
    """
    cache_key = ("catalog", catalog_url)
    if cache_key in _ARCGIS_LAYER_CACHE:
        return _ARCGIS_LAYER_CACHE[cache_key]
    try:
        resp = requests.get(catalog_url, params={"f": "json"},
                            timeout=_REQUEST_TIMEOUT,
                            headers={"User-Agent": "DealDesk/1.0"})
        resp.raise_for_status()
        root = resp.json() or {}
    except Exception as exc:
        logger.info("ArcGIS: catalog fetch failed at %s: %s",
                    catalog_url[:80], exc)
        _ARCGIS_LAYER_CACHE[cache_key] = None
        return None

    # Collect all candidate services (root + each folder)
    candidates: list = []
    def _collect(json_obj):
        for s in json_obj.get("services", []) or []:
            name = s.get("name", "")
            if (s.get("type") in ("MapServer", "FeatureServer")
                    and _PARCEL_LAYER_RE.search(name)):
                candidates.append((name, s["type"]))
    _collect(root)
    for folder in root.get("folders", []) or []:
        try:
            r2 = requests.get(f"{catalog_url}/{folder}", params={"f": "json"},
                              timeout=_REQUEST_TIMEOUT,
                              headers={"User-Agent": "DealDesk/1.0"})
            if r2.status_code != 200:
                continue
            _collect(r2.json() or {})
        except Exception:
            continue

    # Rank services so canonical "Parcels" / "Parcel" / "ParcelsFabric"
    # come before "ParcelAnnoiMAPS" / "ParcelLabels" / assessment subsets.
    _ANNO_SVC_RE = re.compile(r"(anno|label|outline|extent|buffer|centroid)", re.I)

    def _svc_score(sc):
        name, _type = sc
        leaf = name.rsplit("/", 1)[-1].lower()
        if _ANNO_SVC_RE.search(leaf):
            return 7
        if leaf in ("parcels", "parcel"):
            return 0
        if leaf.startswith("parcels") or leaf.startswith("parcel"):
            return 1
        if leaf in ("property", "properties"):
            return 2
        return 3

    candidates.sort(key=_svc_score)

    # Try each candidate until one yields a parcel-like layer
    for svc_name, svc_type in candidates[:8]:
        svc_url = f"{catalog_url}/{svc_name}/{svc_type}"
        found = _find_parcel_layer_on_server(svc_url)
        if found:
            logger.info("ArcGIS: resolved catalog %s -> %s",
                        catalog_url[:60], found[-90:])
            _ARCGIS_LAYER_CACHE[cache_key] = found
            return found

    logger.info("ArcGIS: no parcel layer found in catalog %s "
                "(%d candidates)", catalog_url[:60], len(candidates))
    _ARCGIS_LAYER_CACHE[cache_key] = None
    return None


def _fetch_arcgis_parcel(deal: DealData, pd_obj: ParcelData, addr, gis_url: str) -> None:
    """Generic ArcGIS REST parcel query for any county with a GIS parcel URL in
    the municipal registry. Accepts any of: a specific /MapServer/<n>/query
    URL, a /MapServer root, or a /rest/services catalog root — resolves to
    a parcel feature layer via _resolve_parcel_layer_url and queries it.
    """
    street_number, street_name = _split_street(addr.street)
    if not street_number:
        logger.warning("PROPERTY RECORDS (ArcGIS): no street number — skipping")
        return

    base = _resolve_parcel_layer_url(gis_url)
    if not base:
        logger.info("PROPERTY RECORDS (ArcGIS): could not resolve parcel layer at %s",
                    gis_url[:80])
        return

    # Full-address field-name variants across common county GIS schemas.
    # Includes singular "address" field names plus the specific ones used
    # by the metro-override counties (Cook, LA, King) and common Esri
    # defaults. Maricopa's split-components fields (PHYSICAL_STREET_NUM +
    # PHYSICAL_STREET_NAME) are handled via a separate code path below.
    address_fields = [
        # Common defaults
        "SITEADDRESS", "SITE_ADDRESS", "ADDRESS", "FULL_ADDRESS",
        "PROP_ADDRESS", "LOCATION", "ADDR", "SITUS_ADDRESS",
        # Specific to metro overrides + other county schemas
        "SitusFullAddress",     # CA LA County
        "SitusAddress",         # CA LA County
        "street_address",       # IL Cook County (lowercase)
        "ADDR_FULL",            # WA King County
        "PHYSICAL_ADDRESS",     # AZ Maricopa County (combined)
        "PROPERTY_ADDRESS", "PROPERTYADDRESS",
    ]
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
            sale_date = _coerce_arcgis_date(
                _get_field("SALE_DATE", "LAST_SALE_DATE", "DEED_DATE",
                           "TRANSFER_DATE")
            )
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

            # Deed history from multi-sale attribute patterns on the same
            # feature (SALE_DATE_1/2/3, SALEDATE1/2, etc.) — no extra API call.
            _extract_arcgis_multi_sale_deeds(attrs, pd_obj)

            deal.provenance.field_sources["property_records"] = f"arcgis_gis:{gis_url[:60]}"
            logger.info(
                "PROPERTY RECORDS (ArcGIS): parcel=%s, owner=%s, zoning=%s, assessed=$%s, deeds=%d",
                parcel_id, owner, zoning,
                f"{assessed:,.0f}" if assessed else "n/a",
                len(pd_obj.deed_history),
            )
            return  # success — stop trying field names

        except Exception as exc:
            logger.warning("PROPERTY RECORDS (ArcGIS field=%s): %s", field, exc)
            continue

    logger.info("PROPERTY RECORDS (ArcGIS): no match at %s", gis_url[:80])


# ───────────────────────────────────────────────────────────────────────────
# ArcGIS sibling-layer discovery — pulls historical sales/deeds from a
# dedicated "Sales" layer on the same MapServer as the parcel layer,
# linked by parcel_id. Complements _extract_arcgis_multi_sale_deeds which
# only mines multi-column attributes on the parcel feature itself.
# ───────────────────────────────────────────────────────────────────────────

# Module-level cache: MapServer root URL → (layers, tables) discovery result.
# Avoids repeated layer-list fetches when multiple deals target the same county.
_ARCGIS_LAYER_CACHE: dict = {}

_ARCGIS_SALES_LAYER_RE = re.compile(
    r"(sale|deed|transfer|convey|rtt|recorded?doc|ownersh)", re.IGNORECASE
)
# Field names for the parcel_id join on sales-layer features. Tried in order.
_ARCGIS_SALES_PID_FIELDS = (
    "PARCEL_ID", "PARCELID", "PARCEL_NUM", "PARCEL_NUMBER", "APN", "PIN",
    "TAX_ID", "TAX_PARCEL", "ACCOUNT", "ACCOUNT_NUMBER",
)


def _derive_arcgis_root(gis_url: str) -> Optional[str]:
    """Strip '/<layerId>[/query]' from a parcel query URL to get the
    MapServer or FeatureServer root (which lists available layers).
    """
    url = (gis_url or "").rstrip("/")
    if url.endswith("/query"):
        url = url[: -len("/query")]
    # Trim the last path segment if it's an integer layer id.
    m = re.match(r"^(.*/(?:Map|Feature)Server)(?:/\d+)?$", url, re.IGNORECASE)
    return m.group(1) if m else None


def _discover_arcgis_sales_layers(root_url: str) -> list:
    """Fetch the MapServer/FeatureServer root and return a list of layer/table
    descriptors whose names hint at sales or deed history:
        [{"id": N, "name": "Sales History", "kind": "layer"|"table"}, ...]
    Cached by root URL. Returns [] on failure.
    """
    if root_url in _ARCGIS_LAYER_CACHE:
        return _ARCGIS_LAYER_CACHE[root_url]
    try:
        resp = requests.get(
            root_url, params={"f": "json"}, timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception as exc:
        logger.info("ArcGIS sibling: layer discovery failed at %s: %s",
                    root_url[:80], exc)
        _ARCGIS_LAYER_CACHE[root_url] = []
        return []

    candidates: list = []
    for kind in ("layers", "tables"):
        for item in data.get(kind, []) or []:
            name = item.get("name") or ""
            if _ARCGIS_SALES_LAYER_RE.search(name):
                candidates.append({
                    "id": item.get("id"),
                    "name": name,
                    "kind": kind.rstrip("s"),
                })

    _ARCGIS_LAYER_CACHE[root_url] = candidates
    if candidates:
        logger.info("ArcGIS sibling: %d candidate sales/deed layer(s) at %s: %s",
                    len(candidates), root_url[:80],
                    [c["name"] for c in candidates])
    return candidates


def _fetch_arcgis_sibling_sales(pd_obj: ParcelData, gis_url: str) -> None:
    """Append historical sale/deed records from a sibling sales layer on the
    same MapServer as the parcel layer. No-op when no parcel_id, no layers
    found, or the sibling layer rejects every candidate join field.
    """
    if not pd_obj.parcel_id:
        return
    root = _derive_arcgis_root(gis_url)
    if not root:
        return
    candidates = _discover_arcgis_sales_layers(root)
    if not candidates:
        return

    # Normalize parcel_id — strip dashes/dots/spaces; counties often store
    # the key without punctuation even when the parcel layer returns it with.
    pid = str(pd_obj.parcel_id).strip()
    pid_variants = [pid, pid.replace("-", ""), pid.replace(".", ""),
                    pid.replace(" ", ""), re.sub(r"[-.\s]", "", pid)]
    pid_variants = [v for v in set(pid_variants) if v]

    # Dedupe set from existing deed_history so a parcel-attribute sale and a
    # sibling-layer sale for the same date don't both appear.
    seen_dates = {d.recording_date for d in pd_obj.deed_history if d.recording_date}
    added = 0
    for cand in candidates[:2]:  # try up to 2 candidate layers
        layer_url = f"{root}/{cand['id']}/query"
        rows = _query_arcgis_sales_layer(layer_url, pid_variants)
        if not rows:
            continue
        for r in rows:
            rec_date = _coerce_arcgis_date(
                r.get("SALE_DATE") or r.get("SALEDATE") or r.get("DEED_DATE")
                or r.get("RECORDING_DATE") or r.get("TRANSFER_DATE")
                or r.get("DATE_OF_SALE") or r.get("date_of_sale")
                or r.get("Sale_Date") or r.get("Deed_Date")
            )
            if not rec_date or rec_date in seen_dates:
                continue
            seen_dates.add(rec_date)
            amt = _safe_float(
                r.get("SALE_PRICE") or r.get("SALEPRICE")
                or r.get("SALE_AMOUNT") or r.get("AMOUNT")
                or r.get("CONSIDERATION") or r.get("PRICE")
                or r.get("Sale_Price") or r.get("sale_price")
            )
            grantor = (r.get("GRANTOR") or r.get("SELLER")
                       or r.get("Grantor") or r.get("grantor")
                       or r.get("FROM_NAME") or r.get("TRANSFEROR"))
            grantee = (r.get("GRANTEE") or r.get("BUYER")
                       or r.get("Grantee") or r.get("grantee")
                       or r.get("TO_NAME") or r.get("TRANSFEREE")
                       or r.get("NEW_OWNER"))
            doc_type = (r.get("DOC_TYPE") or r.get("DEED_TYPE")
                        or r.get("DOCUMENT_TYPE") or r.get("TYPE")
                        or cand["name"] or "Recorded sale")
            doc_id = (r.get("DOC_NUMBER") or r.get("DOCUMENT_NUMBER")
                      or r.get("BOOK_PAGE") or r.get("RECORDING_NUMBER")
                      or r.get("INSTRUMENT_NUMBER"))
            pd_obj.deed_history.append(DeedRecord(
                recording_date=rec_date,
                document_type=str(doc_type).title() if doc_type else None,
                grantor=str(grantor).strip() if grantor else None,
                grantee=str(grantee).strip() if grantee else None,
                consideration_amount=amt,
                document_id=str(doc_id).strip() if doc_id else None,
            ))
            added += 1
        if added:
            # Re-sort full deed_history by recording_date desc
            pd_obj.deed_history.sort(
                key=lambda d: d.recording_date or "", reverse=True,
            )
            logger.info(
                "ArcGIS sibling: added %d deed rows from layer '%s' at %s",
                added, cand["name"], root[:80],
            )
            break  # stop after first productive layer


def _fetch_arcgis_sales_explicit(pd_obj: ParcelData, sales_url: str,
                                  pid_field: str = "PIN") -> None:
    """Pull sales history from an explicit sales-layer URL using the
    configured parcel_id field (no auto-discovery, no field-name fuzzing).
    Used when a metro override pins the exact sales-layer endpoint.
    """
    if not pd_obj.parcel_id:
        return
    pid = str(pd_obj.parcel_id).strip()
    variants = [pid, pid.replace("-", ""), pid.replace(" ", "")]
    rows: list = []
    for v in set(variants):
        try:
            resp = requests.get(
                sales_url,
                params={
                    "where": f"{pid_field} = '{v}'",
                    "outFields": "*",
                    "returnGeometry": "false",
                    "f": "json",
                    "resultRecordCount": 25,
                    "orderByFields": "SaleDate DESC" if "King" in sales_url or "king" in sales_url.lower() else None,
                },
                timeout=_REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                continue
            data = resp.json() or {}
            feats = data.get("features", []) or []
            if feats:
                rows = [f.get("attributes", {}) or {} for f in feats]
                break
        except Exception as exc:
            logger.info("Explicit sales query failed for pid=%s: %s", v, exc)
            continue
    if not rows:
        logger.info("Explicit sales layer: no rows for pid=%s at %s",
                    pid, sales_url[:60])
        return

    seen_dates = {d.recording_date for d in pd_obj.deed_history if d.recording_date}
    added = 0
    for r in rows:
        rec_date = _coerce_arcgis_date(
            r.get("SaleDate") or r.get("SALE_DATE") or r.get("SALEDATE")
            or r.get("DEED_DATE") or r.get("RECORDING_DATE")
        )
        if not rec_date or rec_date in seen_dates:
            continue
        seen_dates.add(rec_date)
        amt = _safe_float(
            r.get("SalePrice") or r.get("SALE_PRICE") or r.get("SALEPRICE")
        )
        grantor = (r.get("Sellername") or r.get("SELLER") or r.get("GRANTOR")
                   or r.get("seller_name"))
        grantee = (r.get("buyername") or r.get("BUYER") or r.get("GRANTEE")
                   or r.get("buyer_name"))
        doc_id = (r.get("RecNumber") or r.get("ExciseTaxNum")
                  or r.get("DOC_NUMBER") or r.get("RECORDING_NUMBER"))
        pd_obj.deed_history.append(DeedRecord(
            recording_date=rec_date,
            document_type="Recorded sale",
            grantor=(str(grantor).strip() if grantor else None),
            grantee=(str(grantee).strip() if grantee else None),
            consideration_amount=amt,
            document_id=(str(doc_id).strip() if doc_id else None),
        ))
        added += 1
    if added:
        pd_obj.deed_history.sort(key=lambda d: d.recording_date or "", reverse=True)
        # Fill last_sale_* from the most recent
        mr = pd_obj.deed_history[0]
        if mr.recording_date and not pd_obj.last_sale_date:
            pd_obj.last_sale_date = mr.recording_date
        if mr.consideration_amount and not pd_obj.last_sale_price:
            pd_obj.last_sale_price = mr.consideration_amount
        if mr.grantee and not pd_obj.owner_name:
            pd_obj.owner_name = mr.grantee
    logger.info("Explicit sales layer: added %d rows for pid=%s", added, pid)


def _query_arcgis_sales_layer(layer_url: str, pid_variants: list) -> list:
    """Query a sales-layer URL by trying each parcel-id variant against each
    common parcel-id field name. Returns the first non-empty row set.
    """
    for field in _ARCGIS_SALES_PID_FIELDS:
        for pid in pid_variants:
            try:
                resp = requests.get(
                    layer_url,
                    params={
                        "where": f"UPPER({field}) = UPPER('{pid}')",
                        "outFields": "*",
                        "returnGeometry": "false",
                        "f": "json",
                        "resultRecordCount": 25,
                    },
                    timeout=_REQUEST_TIMEOUT,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json() or {}
                if "error" in data:
                    continue
                feats = data.get("features", []) or []
            except Exception:
                continue
            if feats:
                return [f.get("attributes", {}) or {} for f in feats]
    return []


_ACRIS_ORDINAL_WORDS = {
    "FIRST": "1", "SECOND": "2", "THIRD": "3", "FOURTH": "4", "FIFTH": "5",
    "SIXTH": "6", "SEVENTH": "7", "EIGHTH": "8", "NINTH": "9", "TENTH": "10",
    "ELEVENTH": "11", "TWELFTH": "12", "THIRTEENTH": "13", "FOURTEENTH": "14",
    "FIFTEENTH": "15", "SIXTEENTH": "16", "SEVENTEENTH": "17",
    "EIGHTEENTH": "18", "NINETEENTH": "19", "TWENTIETH": "20",
}
_ACRIS_DROP_TOKENS = {
    "STREET", "ST", "STR", "STRET", "AVENUE", "AVE", "ROAD", "RD",
    "BOULEVARD", "BLVD", "PLACE", "PL", "DRIVE", "DR", "LANE", "LN",
    "COURT", "CT", "WAY", "PARKWAY", "PKWY", "TERRACE", "TER",
    "N", "S", "E", "W", "NE", "NW", "SE", "SW",
    "NORTH", "SOUTH", "EAST", "WEST",
    "APT", "UNIT", "SUITE", "STE", "FLOOR", "FL", "#",
}
_ACRIS_ORDINAL_DIGIT_RE = re.compile(r"^(\d+)(ST|ND|RD|TH)$")

_ACRIS_DIGIT_TO_WORD = {
    "1": "FIRST", "2": "SECOND", "3": "THIRD", "4": "FOURTH", "5": "FIFTH",
    "6": "SIXTH", "7": "SEVENTH", "8": "EIGHTH", "9": "NINTH", "10": "TENTH",
    "11": "ELEVENTH", "12": "TWELFTH", "13": "THIRTEENTH",
    "14": "FOURTEENTH", "15": "FIFTEENTH", "16": "SIXTEENTH",
    "17": "SEVENTEENTH", "18": "EIGHTEENTH", "19": "NINETEENTH",
    "20": "TWENTIETH",
}


_ACRIS_DIRECTIONAL_PREFIXES = {
    "N": ("N", "NORTH"), "NORTH": ("N", "NORTH"),
    "S": ("S", "SOUTH"), "SOUTH": ("S", "SOUTH"),
    "E": ("E", "EAST"),  "EAST": ("E", "EAST"),
    "W": ("W", "WEST"),  "WEST": ("W", "WEST"),
}


def _acris_like_variants(signature: set, original_name: str) -> list:
    """Produce indexed prefix-anchored LIKE fragments covering ACRIS's
    spelling variants. `LIKE 'FOO%'` is indexed on Socrata; `'%FOO%'` is
    not and times out on 20M rows.

    For each distinctive token in the signature, we generate:
      - Digit tokens: ``5 %``, ``5TH%``, ``FIFTH%`` (word form for 1-20)
      - Named tokens: ``BROADWAY%``

    When the original street name begins with a directional
    ("W 57TH STREET", "EAST 82ND ST"), we also emit directional-prefixed
    variants ("W 57 %", "W 57TH%", "WEST 57 %", "WEST 57TH%") since ACRIS
    stores full names including the directional.
    """
    # Detect leading directional (if any) from the raw input
    first_raw = (original_name or "").upper().split()
    leading_dir = first_raw[0].rstrip(".") if first_raw else ""
    dir_variants = _ACRIS_DIRECTIONAL_PREFIXES.get(leading_dir, ())

    def _digit_forms(t: str) -> list:
        forms = [f"{t} "]
        last_two = int(t) % 100
        last = int(t) % 10
        suffix = "TH" if 11 <= last_two <= 13 else {1: "ST", 2: "ND", 3: "RD"}.get(last, "TH")
        forms.append(f"{t}{suffix}")
        word = _ACRIS_DIGIT_TO_WORD.get(t)
        if word:
            forms.append(word)
        return forms

    frags: list = []
    for t in signature:
        forms = _digit_forms(t) if t.isdigit() else [t]
        # Bare form (no directional prefix)
        for f in forms:
            frags.append(f"{f}%")
        # Directional-prefixed forms (e.g. "W 57TH%", "WEST 57TH%")
        for dv in dir_variants:
            for f in forms:
                frags.append(f"{dv} {f}%")
    # Dedupe
    return sorted(set(frags))


def _acris_name_signature(name_upper: str) -> set:
    """Reduce a street name to a distinctive-token signature, normalizing
    ACRIS variants. Handles:
       - "5TH AVENUE" / "5 AVENUE" / "FIFTH AVENUE" → {'5'}
       - "WEST 47TH STREET" / "W 47 ST" → {'47'}
       - "BROADWAY" → {'BROADWAY'}
       - "CENTRAL PARK WEST" → {'CENTRAL','PARK'}
    """
    tokens = [t for t in re.split(r"[^A-Z0-9]+", name_upper) if t]
    sig: set = set()
    for t in tokens:
        if t in _ACRIS_DROP_TOKENS:
            continue
        # Ordinal word → digit
        if t in _ACRIS_ORDINAL_WORDS:
            sig.add(_ACRIS_ORDINAL_WORDS[t])
            continue
        # Digit ordinal suffix → digit
        m = _ACRIS_ORDINAL_DIGIT_RE.match(t)
        if m:
            sig.add(m.group(1))
            continue
        # Pure digit (ACRIS uses "5" for 5th Avenue)
        if t.isdigit():
            sig.add(t)
            continue
        # Otherwise keep as distinctive name token
        sig.add(t)
    return sig


_IASWORLD_URL_RE = re.compile(
    r"(propertyrecords\.|/pt/forms/|/pt/search/|iasworld)", re.IGNORECASE
)

# ────────────────────────────────────────────────────────────────────────
# Metro-area GIS overrides — for big metros not in the municipal registry.
# Keyed by (state_upper, city-name set), first match wins. Values:
#   arcgis_rest_url: str  — REST root or specific /MapServer/<n>/query URL
#   sales_layer_url: Optional[str] — adjacent "sales" layer query URL
#                                    (used by _fetch_arcgis_sibling_sales;
#                                    overrides auto-discovery when set)
# ────────────────────────────────────────────────────────────────────────

_MARICOPA_CITIES = frozenset({
    "phoenix", "scottsdale", "mesa", "tempe", "chandler", "glendale",
    "peoria", "gilbert", "surprise", "goodyear", "avondale", "buckeye",
    "fountain hills", "paradise valley", "litchfield park", "tolleson",
    "cave creek", "carefree", "queen creek", "wickenburg", "el mirage",
    "sun city", "sun city west", "youngtown", "guadalupe", "anthem",
})
_COOK_CITIES = frozenset({
    "chicago", "oak park", "evanston", "skokie", "cicero", "schaumburg",
    "arlington heights", "des plaines", "palatine", "orland park", "tinley park",
    "oak lawn", "berwyn", "mount prospect", "wheeling", "hoffman estates",
    "northbrook", "elk grove village", "lombard", "buffalo grove", "park ridge",
    "calumet city", "rolling meadows", "glenview", "oak forest", "glen ellyn",
    "la grange", "harvey", "chicago heights",
})
_LA_CITIES = frozenset({
    "los angeles", "long beach", "santa clarita", "glendale", "lancaster",
    "palmdale", "pomona", "torrance", "pasadena", "el monte", "downey",
    "inglewood", "west covina", "norwalk", "burbank", "compton", "carson",
    "south gate", "santa monica", "whittier", "hawthorne", "alhambra",
    "lakewood", "bellflower", "baldwin park", "lynwood", "redondo beach",
    "pico rivera", "montebello", "monterey park", "rosemead", "culver city",
    "arcadia", "diamond bar", "paramount", "rancho palos verdes", "covina",
    "glendora", "huntington park", "la mirada", "manhattan beach",
    "beverly hills", "west hollywood", "malibu", "calabasas",
})
_KING_CITIES = frozenset({
    "seattle", "bellevue", "kent", "renton", "federal way", "kirkland",
    "auburn", "redmond", "sammamish", "shoreline", "burien", "issaquah",
    "des moines", "bothell", "mercer island", "tukwila", "kenmore",
    "covington", "maple valley", "snoqualmie", "north bend", "woodinville",
    "pacific", "duvall", "carnation", "enumclaw", "black diamond",
    "sea-tac", "seatac", "normandy park", "lake forest park",
})

_METRO_GIS_OVERRIDES = [
    {
        "label": "AZ Maricopa",
        "state": "AZ",
        "cities": _MARICOPA_CITIES,
        # Maricopa Dynamic Query Service — full schema with owner, sale, deed
        "arcgis_rest_url":
            "https://gis.mcassessor.maricopa.gov/arcgis/rest/services/"
            "MaricopaDynamicQueryService/MapServer/3/query",
    },
    {
        "label": "IL Cook",
        "state": "IL",
        "cities": _COOK_CITIES,
        # Cook CookViewer3Parcels — address + assessed values, no owner/sale
        "arcgis_rest_url":
            "https://gis.cookcountyil.gov/traditional/rest/services/"
            "CookViewer3Parcels/MapServer/0/query",
    },
    {
        "label": "CA Los Angeles",
        "state": "CA",
        "cities": _LA_CITIES,
        # LA County Parcel layer — address + assessed values, no owner/sale
        "arcgis_rest_url":
            "https://public.gis.lacounty.gov/public/rest/services/"
            "LACounty_Cache/LACounty_Parcel/MapServer/0/query",
    },
    {
        "label": "WA King",
        "state": "WA",
        "cities": _KING_CITIES,
        # King County Parcels layer (primary)
        "arcgis_rest_url":
            "https://gismaps.kingcounty.gov/arcgis/rest/services/"
            "Property/KingCo_PropertyInfo/MapServer/2/query",
        # King has a separate sales layer with Sellername/Buyername/SaleDate/
        # SalePrice/RecNumber — richer than any attribute scan
        "sales_layer_url":
            "https://gismaps.kingcounty.gov/arcgis/rest/services/"
            "Property/KingCo_PropertyInfo/MapServer/3/query",
        "sales_pid_field": "PIN",
    },
]


def _apply_metro_gis_override(deal: DealData) -> Optional[dict]:
    """If this deal's (state, city) matches a known metro in
    _METRO_GIS_OVERRIDES AND no arcgis_rest_url is already set from the
    municipal registry, populate provenance.field_sources['arcgis_rest_url']
    (and optional sales_layer_url) from the override. Returns the matched
    override dict, or None.
    """
    prov = deal.provenance.field_sources
    if prov.get("arcgis_rest_url"):
        return None
    state = (deal.address.state or "").strip().upper()
    city = (deal.address.city or "").strip().lower()
    if not state or not city:
        return None
    for ov in _METRO_GIS_OVERRIDES:
        if ov["state"] == state and city in ov["cities"]:
            prov["arcgis_rest_url"] = ov["arcgis_rest_url"]
            if ov.get("sales_layer_url"):
                prov["sales_layer_url"] = ov["sales_layer_url"]
                prov["sales_pid_field"] = ov.get("sales_pid_field", "PIN")
            logger.info("METRO OVERRIDE: %s matched for %s, %s",
                        ov["label"], city, state)
            return ov
    return None


def _is_iasworld_url(url: str) -> bool:
    """Heuristic: does `url` look like a Tyler iasWorld property portal?
    iasWorld apps live at /pt/ and most county deployments use a hostname
    starting with "propertyrecords." (e.g. propertyrecords.montcopa.org).
    """
    return bool(_IASWORLD_URL_RE.search(url or ""))


def _resolve_nyc_borough(addr) -> Optional[int]:
    """Return ACRIS borough code (1..5) from deal address, or None if unresolved.
    Prefers city match, falls back to county (for addresses where city is a
    neighborhood like "Astoria" or just "New York" with an ambiguous borough).
    """
    city = (getattr(addr, "city", "") or "").strip().upper()
    county = (getattr(addr, "county", "") or "").strip().upper()
    # County often carries "Kings (geographic)" suffix — strip parens.
    county = re.sub(r"\s*\(.*\)$", "", county).strip()
    return _NYC_CITY_TO_BOROUGH.get(city) or _NYC_COUNTY_TO_BOROUGH.get(county)


def _fetch_nyc_acris(deal: DealData, pd_obj: ParcelData, addr) -> None:
    """NYC deed history via ACRIS Socrata API (Manhattan / Bronx / Brooklyn /
    Queens / Staten Island).

    Pipeline:
      1. Legals (8h5j-fqxa): address → document_ids for this property
      2. Master (bnx9-e6tj): filter to deed-type documents, pull date +
         consideration amount
      3. Parties (636b-3b5g): pull grantor (party_type=1) / grantee (=2) names

    Populates pd_obj.deed_history, sets parcel_id to borough-block-lot if
    missing, and fills owner_name / last_sale_* from the most-recent deed
    if not already set by a prior fetcher.
    """
    street_number, street_name = _split_street(addr.street)
    if not street_number or not street_name:
        logger.warning("ACRIS: no street number/name — skipping")
        return

    # Try resolved borough first, then exhaustive fallback across all five
    # (queries are fast and most non-matching boroughs return 0 rows quickly).
    resolved = _resolve_nyc_borough(addr)
    boroughs_to_try = [resolved] if resolved else [1, 2, 3, 4, 5]

    # Match ACRIS street_name variants by normalizing to a canonical
    # signature. ACRIS has many spellings for the same street:
    # "5 AVENUE", "5TH AVENUE", "FIFTH AVENUE", "FIFTH AVE".
    # We reduce both sides to a set of normalized tokens (digits + core
    # words) and match if their signatures intersect on the distinctive
    # parts. Server query narrows to borough+street_number (both indexed);
    # Python does the name filter.
    name_upper = street_name.upper()
    target_sig = _acris_name_signature(name_upper)

    def _name_matches(candidate: str) -> bool:
        cand_upper = (candidate or "").upper()
        if not cand_upper:
            return False
        if cand_upper == name_upper:
            return True
        cand_sig = _acris_name_signature(cand_upper)
        if not target_sig or not cand_sig:
            return False
        # Require the full target signature to be a subset of candidate's.
        # e.g. target {'5'} ⊆ candidate {'5'} ⟹ match.
        return target_sig.issubset(cand_sig)

    # Step 1: Legals — find document_ids for this property.
    # Server query filters by borough + street_number + street_name variants
    # (covering "5 AVENUE" / "5TH AVENUE" / "FIFTH AVENUE" for ordinal streets).
    # Python-side signature match is kept as the final precision step.
    doc_ids: list = []
    borough_found: Optional[int] = None
    block = lot = None
    _ACRIS_TIMEOUT = 60
    like_frags = _acris_like_variants(target_sig, name_upper)
    # SoQL OR expression: (street_name like '%5%' OR street_name like '%FIFTH%')
    like_expr = " OR ".join(f"street_name like '{f}'" for f in like_frags)
    for b in boroughs_to_try:
        where = f"borough='{b}' AND street_number='{street_number}'"
        if like_expr:
            where += f" AND ({like_expr})"
        try:
            resp = requests.get(
                _NYC_LEGALS_URL,
                params={
                    "$where": where,
                    "$limit": 200,
                    "$select": "document_id, block, lot, street_number, street_name",
                },
                timeout=_ACRIS_TIMEOUT,
            )
            resp.raise_for_status()
            rows = resp.json() or []
        except Exception as exc:
            logger.warning("ACRIS Legals query failed (borough=%d): %s", b, exc)
            continue
        if not rows:
            continue
        # Filter by street-name match in Python
        matching = [r for r in rows if _name_matches(r.get("street_name", ""))]
        if matching:
            seen: set = set()
            for r in matching:
                did = r.get("document_id")
                if did and did not in seen:
                    seen.add(did)
                    doc_ids.append(did)
            borough_found = b
            first = matching[0]
            block = first.get("block")
            lot = first.get("lot")
            logger.info(
                "ACRIS Legals: borough=%d returned %d rows (%d street-matched, %d unique docs), BBL=%s-%s-%s",
                b, len(rows), len(matching), len(doc_ids), b, block, lot,
            )
            break

    if not doc_ids:
        logger.info("ACRIS: no legals match for %s %s (tried boroughs %s)",
                    street_number, street_name, boroughs_to_try)
        return

    # Set BBL as parcel_id if not already set
    if block and lot and not pd_obj.parcel_id:
        pd_obj.parcel_id = f"{borough_found}-{block}-{lot}"

    # Step 2: Master — pull metadata for candidate docs, then filter to
    # ownership-transfer types in Python (faster than SoQL LIKE, and lets
    # us catch the full taxonomy: DEED, QCLAIM, RTIFDD, EASE, AGMT, etc.).
    sample = doc_ids[:100]
    id_list = ",".join(f"'{d}'" for d in sample)
    where_m = f"document_id IN ({id_list})"
    try:
        resp = requests.get(
            _NYC_MASTER_URL,
            params={
                "$where": where_m,
                "$limit": 50,
                "$order": "recorded_datetime DESC",
            },
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        master_rows = resp.json() or []
    except Exception as exc:
        logger.warning("ACRIS Master query failed: %s", exc)
        return

    if not master_rows:
        logger.info("ACRIS: no master records for %d candidate docs", len(sample))
        return

    # Ownership-transfer doc_type codes only. Agreements (AGMT), easements
    # (EASE), mortgages, UCCs, satisfactions, and assignments-of-mortgage
    # are not title transfers and belong in the liens narrative, not here.
    _TRANSFER_DOC_TYPES = {
        "DEED", "DEEDO", "CORDD", "CORRDEED", "QCLAIM", "QCLD",
        "BARGNSALE", "BARGSALE", "SHERF", "REFEREE", "TAXSALE",
        "DEEDCOOPUNIT",
    }
    # Restrictive keyword list — only matches full doc-type descriptions
    # that are true ownership transfers (avoid "ASSIGNMENT OF DEED OF TRUST"
    # which is a mortgage assignment, not an ownership transfer).
    _TRANSFER_KEYWORDS = ("DEED", "QUITCLAIM", "CONVEYANCE", "BARGAIN AND SALE")
    _NON_TRANSFER_KEYWORDS = ("MORTGAGE", "ASSIGNMENT", "SATISFACTION", "UCC",
                              "MEMORANDUM", "LEASE", "AGREEMENT", "AGMT",
                              "EASEMENT", "SUBORDINATION")

    def _is_transfer(doc_type: str) -> bool:
        dt = (doc_type or "").upper()
        if not dt:
            return False
        if any(nk in dt for nk in _NON_TRANSFER_KEYWORDS):
            return False
        if dt in _TRANSFER_DOC_TYPES:
            return True
        return any(k in dt for k in _TRANSFER_KEYWORDS)

    transfer_rows = [r for r in master_rows if _is_transfer(r.get("doc_type"))]
    # Sort by recording date desc — master_rows order is arbitrary.
    transfer_rows.sort(
        key=lambda r: str(r.get("recorded_datetime") or r.get("document_date") or ""),
        reverse=True,
    )
    deed_doc_ids = [r["document_id"] for r in transfer_rows if r.get("document_id")]
    master_by_id = {r["document_id"]: r for r in transfer_rows}

    if not deed_doc_ids:
        logger.info("ACRIS: no transfer-type documents in %d master records "
                    "(doc_types seen: %s)",
                    len(master_rows),
                    sorted({r.get("doc_type") for r in master_rows})[:10])
        return

    # Step 3: Parties — grantor/grantee names
    id_list_p = ",".join(f"'{d}'" for d in deed_doc_ids)
    where_p = f"document_id IN ({id_list_p})"
    parties_rows: list = []
    try:
        resp = requests.get(
            _NYC_PARTIES_URL,
            params={"$where": where_p, "$limit": 500},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        parties_rows = resp.json() or []
    except Exception as exc:
        logger.warning("ACRIS Parties query failed: %s", exc)

    parties_by_doc: dict = {}
    for p in parties_rows:
        did = p.get("document_id")
        if not did:
            continue
        bucket = parties_by_doc.setdefault(did, {"grantor": [], "grantee": []})
        ptype = str(p.get("party_type") or "").strip()
        name = (p.get("name") or "").strip()
        if not name:
            continue
        if ptype == "1":
            bucket["grantor"].append(name)
        elif ptype == "2":
            bucket["grantee"].append(name)

    # Step 4: Assemble DeedRecord list (most-recent first)
    for doc_id in deed_doc_ids:
        m = master_by_id.get(doc_id, {})
        p = parties_by_doc.get(doc_id, {"grantor": [], "grantee": []})
        amt = _safe_float(m.get("document_amt"))
        rec_dt = m.get("recorded_datetime") or m.get("document_date")
        rec_date = _coerce_arcgis_date(rec_dt)
        pd_obj.deed_history.append(DeedRecord(
            recording_date=rec_date,
            document_type=(m.get("doc_type") or "DEED").title(),
            grantor=" / ".join(p["grantor"]) if p["grantor"] else None,
            grantee=" / ".join(p["grantee"]) if p["grantee"] else None,
            # document_amt == 0 in ACRIS means nominal/unreported (common for
            # intra-family, LLC reorg, etc.). Keep the 0 so the caller can
            # decide whether to render "—" or "$0".
            consideration_amount=amt,
            document_id=m.get("crfn") or doc_id,
        ))

    # Fill derived parcel fields from the most recent deed if not set
    if pd_obj.deed_history:
        most_recent = pd_obj.deed_history[0]
        if most_recent.grantee and not pd_obj.owner_name:
            pd_obj.owner_name = most_recent.grantee
        if most_recent.recording_date and not pd_obj.last_sale_date:
            pd_obj.last_sale_date = most_recent.recording_date
        if (most_recent.consideration_amount
                and most_recent.consideration_amount > 0
                and not pd_obj.last_sale_price):
            pd_obj.last_sale_price = most_recent.consideration_amount

    deal.provenance.field_sources["property_records"] = (
        deal.provenance.field_sources.get("property_records", "")
        + f" + nyc_acris_borough_{borough_found}"
    ).strip(" +")
    logger.info("ACRIS: pulled %d deed records for %s %s (borough=%d, BBL=%s-%s-%s)",
                len(pd_obj.deed_history), street_number, street_name,
                borough_found, borough_found, block, lot)


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

    After the primary fetchers run, a universal fallback synthesizes a
    single-row deed_history from last_sale_date/price + owner_name if the
    primary source did not populate a richer list — so every jurisdiction
    with a known most-recent sale produces at least one visible row.
    """
    addr = deal.address
    state = (addr.state or "").strip().upper()
    city = (addr.city or "").strip()

    if deal.parcel_data is None:
        deal.parcel_data = ParcelData()
    pd_obj = deal.parcel_data

    # Metro-area GIS overrides — for big metros not in the registry
    # (Phoenix/Maricopa, Chicago/Cook, LA, Seattle/King). Populates
    # provenance.field_sources['arcgis_rest_url'] when matched so the
    # normal ArcGIS path below can run.
    _apply_metro_gis_override(deal)

    # SOURCE 1: Philadelphia OPA
    if state == "PA" and city.lower() == "philadelphia":
        _fetch_phl_opa(deal, pd_obj, addr)
    # SOURCE 2: DC DCGIS
    elif state == "DC":
        _fetch_dc_dcgis(deal, pd_obj, addr)
    else:
        # SOURCE 3: ArcGIS REST — prefer the explicitly-curated
        # arcgis_rest_url (a real /rest/services/.../MapServer/<n>/query
        # endpoint). Fall back to gis_parcel_url only if it looks like a
        # REST URL; otherwise skip (most gis_parcel_url values are HTML
        # portal URLs, which the fetchers can't use).
        rest_url = deal.provenance.field_sources.get("arcgis_rest_url")
        portal_url = deal.provenance.field_sources.get("gis_parcel_url") or ""
        fetch_url = rest_url
        if not fetch_url and "/rest/services/" in portal_url.lower():
            fetch_url = portal_url
        if fetch_url:
            # Resolve once: if fetch_url is a catalog root or MapServer root,
            # _resolve_parcel_layer_url drills down to a specific parcel
            # layer's /query endpoint. Both the parcel fetch and the sibling
            # sales discovery need the same resolved URL to find siblings.
            resolved = _resolve_parcel_layer_url(fetch_url) or fetch_url
            _fetch_arcgis_parcel(deal, pd_obj, addr, resolved)
            # Sibling-layer discovery: pull multi-sale history from a
            # dedicated "Sales" / "Deeds" layer on the same MapServer, if
            # the county publishes one. Metro overrides can pin the exact
            # sales-layer URL (e.g. King County's KingCo_PropertyInfo/3);
            # otherwise fall back to automatic discovery.
            explicit_sales = deal.provenance.field_sources.get("sales_layer_url")
            if explicit_sales:
                _fetch_arcgis_sales_explicit(
                    pd_obj, explicit_sales,
                    pid_field=deal.provenance.field_sources.get("sales_pid_field") or "PIN",
                )
            else:
                _fetch_arcgis_sibling_sales(pd_obj, resolved)
        # SOURCE 4: OSM Nominatim fallback if ArcGIS didn't land a parcel_id
        if not pd_obj.parcel_id:
            _fetch_osm_parcel(deal, pd_obj, addr)

    # NYC ENRICHMENT: full deed history (grantor/grantee/consideration) from
    # ACRIS. Runs in addition to the above so NYC deals keep their ArcGIS
    # parcel fields and gain full title history on top. Skipped for PHL/DC
    # since those jurisdictions already populate rich deed_history above.
    if state == "NY" and _resolve_nyc_borough(addr) is not None:
        _fetch_nyc_acris(deal, pd_obj, addr)

    # TYLER iasWorld ENRICHMENT: scrape CAMA portal when the registry's
    # assessor_url points at one. Covers Montgomery PA (propertyrecords.
    # montcopa.org) and other PA counties using the same platform.
    # Detected by URL hostname/path — iasWorld portals live at paths
    # ending in "/pt/forms/" or hosts starting with "propertyrecords".
    assessor = deal.provenance.field_sources.get("assessor_url") or ""
    if assessor and _is_iasworld_url(assessor):
        try:
            fetch_iasworld(deal, pd_obj, addr, assessor)
        except Exception as exc:
            logger.warning("iasWorld fetch failed: %s", exc)

    # UNIVERSAL FALLBACK: synthesize one-row deed_history from last sale
    # when the primary source didn't populate a richer list. This means
    # jurisdictions without an open deed-history API still get a visible row.
    if not pd_obj.deed_history and pd_obj.last_sale_date:
        pd_obj.deed_history.append(DeedRecord(
            recording_date=pd_obj.last_sale_date,
            document_type="Deed (most recent sale on file)",
            grantor=None,
            grantee=pd_obj.owner_name,
            consideration_amount=pd_obj.last_sale_price,
            document_id=pd_obj.deed_book_page,
        ))
        logger.info(
            "DEED HISTORY (fallback): synthesized 1 row from last_sale_date=%s",
            pd_obj.last_sale_date,
        )


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
        # Browser-like UA: amlegal / municode / ecode360 all tend to 403 the
        # identifying bot UA. Full Accept headers reduce the 403 rate further.
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
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
