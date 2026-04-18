"""
iasworld_fetcher.py — Tyler Technologies iasWorld property portal scraper
==========================================================================
Fetcher for counties using the Tyler iasWorld CAMA web portal. Covers
Montgomery PA (propertyrecords.montcopa.org) and other PA counties on the
same platform (Chester, Bucks, Delaware — when registered).

iasWorld is an ASP.NET WebForms application with:
  - Disclaimer-acceptance cookie gate (DISCLAIMER=1 set by POST to
    Disclaimer.aspx with btAgree=Agree)
  - ViewState / EventValidation form state on every page
  - Address search at /pt/search/commonsearch.aspx?mode=ADDRESS
    (note: the POST requires `mode=ADDRESS` uppercase in the form body,
    even though the URL param is lowercase)
  - Property detail at /pt/datalets/Datalet.aspx?sIndex=N&idx=M with
    sIndex bound to the search result index — so the detail URL is
    only valid within the session that performed the search.
  - Sales tab at /pt/datalets/Datalet.aspx?mode=sale_hist&sIndex=N&idx=M&LMparent=20

Public API:
    fetch_iasworld(pd_obj, deal, addr, base_url)
        Populate pd_obj.parcel_id / owner_name / last_sale_* and
        pd_obj.deed_history from the iasWorld portal at `base_url`.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests

from models.models import DealData, DeedRecord, ParcelData

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 20
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


# ────────────────────────────────────────────────────────────────────────
# ViewState helpers
# ────────────────────────────────────────────────────────────────────────

_VIEWSTATE_RE = re.compile(
    r'__VIEWSTATE[^>]+value="([^"]+)"', re.DOTALL,
)
_EVENTVALIDATION_RE = re.compile(
    r'__EVENTVALIDATION[^>]+value="([^"]+)"', re.DOTALL,
)
_VIEWSTATEGENERATOR_RE = re.compile(
    r'__VIEWSTATEGENERATOR[^>]+value="([^"]+)"', re.DOTALL,
)


def _extract_viewstate(html: str) -> Optional[tuple]:
    """Return (viewstate, viewstate_gen, event_validation) or None if missing."""
    vs = _VIEWSTATEGENERATOR_RE.search(html) and _VIEWSTATE_RE.search(html)
    if not vs:
        return None
    viewstate = _VIEWSTATE_RE.search(html)
    validation = _EVENTVALIDATION_RE.search(html)
    generator = _VIEWSTATEGENERATOR_RE.search(html)
    if not (viewstate and validation and generator):
        return None
    return (viewstate.group(1), generator.group(1), validation.group(1))


# ────────────────────────────────────────────────────────────────────────
# Core flow
# ────────────────────────────────────────────────────────────────────────

def _accept_disclaimer(sess: requests.Session, search_url: str) -> bool:
    """GET the search URL; if it redirects to Disclaimer.aspx, POST Agree.
    Returns True if the session is cleared to access search results.
    """
    r = sess.get(search_url, timeout=_REQUEST_TIMEOUT)
    if "Disclaimer" not in r.url:
        # Already past the gate (DISCLAIMER cookie persisted)
        return True
    state = _extract_viewstate(r.text)
    if not state:
        logger.warning("iasWorld: disclaimer page missing ViewState")
        return False
    viewstate, generator, validation = state
    resp = sess.post(
        r.url,
        data={
            "__VIEWSTATE": viewstate,
            "__VIEWSTATEGENERATOR": generator,
            "__EVENTVALIDATION": validation,
            "btAgree": "Agree",   # MUST be btAgree, not btDisagree
        },
        timeout=_REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    # After acceptance the portal sets a DISCLAIMER cookie and redirects
    return any(c.name == "DISCLAIMER" for c in sess.cookies)


def _address_search(sess: requests.Session, base_url: str,
                    street_number: str, street_name: str,
                    city: str = "") -> list:
    """Run an address search and return a list of result rows:
        [{"parcel_id": str, "owner": str, "address": str,
          "last_sale_date": str|None, "last_sale_price": str|None,
          "sindex": int, "idx": int}, ...]
    Empty list on no matches.
    """
    search_url = f"{base_url.rstrip('/')}/search/commonsearch.aspx?mode=address"
    # Load fresh form
    r = sess.get(search_url, timeout=_REQUEST_TIMEOUT)
    if "commonsearch" not in r.url:
        logger.warning("iasWorld: unexpected search page URL %s", r.url[:80])
        return []
    state = _extract_viewstate(r.text)
    if not state:
        logger.warning("iasWorld: search form missing ViewState")
        return []
    viewstate, generator, validation = state

    # Strip non-ASCII from street_name (iasWorld is case-insensitive but
    # punctuation-sensitive); use the distinctive last token.
    name_tokens = [t for t in re.split(r"[^A-Za-z0-9]+", street_name.upper()) if t]
    directionals = {"N", "S", "E", "W", "NE", "NW", "SE", "SW",
                    "NORTH", "SOUTH", "EAST", "WEST"}
    suffixes = {"STREET", "ST", "AVENUE", "AVE", "ROAD", "RD", "BOULEVARD",
                "BLVD", "PLACE", "PL", "DRIVE", "DR", "LANE", "LN", "COURT",
                "CT", "WAY", "PARKWAY", "PKWY", "TERRACE", "TER"}
    # Distinctive token: first non-directional, non-suffix
    distinctive = next(
        (t for t in name_tokens if t not in directionals and t not in suffixes),
        name_tokens[0] if name_tokens else "",
    )
    # Directional prefix if present in original
    adrdir = next((t for t in name_tokens if t in directionals), "")

    # City is intentionally omitted: iasWorld deployments key on TOWNSHIP/
    # BOROUGH names (e.g. "UPPER MERION") while the deal address carries
    # the postal city ("King of Prussia") — sending the latter filters to
    # zero results. Street number + distinctive name is selective enough.
    payload = {
        "__EVENTTARGET": "",
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": viewstate,
        "__VIEWSTATEGENERATOR": generator,
        "__EVENTVALIDATION": validation,
        "hdAction": "Search",
        "inpNumber": street_number,
        "inpAdrdir": adrdir,
        "inpStreet": distinctive,
        "inpSuffix1": "",
        "inpUnit": "",
        "mode": "ADDRESS",        # uppercase — form requires this even though URL is lowercase
        "btSearch": "Search",
    }
    resp = sess.post(search_url, data=payload, timeout=_REQUEST_TIMEOUT,
                     allow_redirects=True)
    if resp.status_code != 200:
        logger.warning("iasWorld search POST status %d", resp.status_code)
        return []

    return _parse_search_results(resp.text)


def _parse_search_results(html: str) -> list:
    """Parse search-result table rows. Returns list of dicts or []."""
    # Rows have onclick="javascript:selectSearchRow('../Datalets/Datalet.aspx?sIndex=N&idx=M')"
    row_re = re.compile(
        r"<tr[^>]*selectSearchRow\([\"']*\.\./Datalets/Datalet\.aspx"
        r"\?sIndex=(\d+)&(?:amp;)?idx=(\d+)[\"']*[^>]*>(.+?)</tr>",
        re.DOTALL,
    )
    out = []
    for m in row_re.finditer(html):
        sindex = int(m.group(1))
        idx = int(m.group(2))
        row_html = m.group(3)
        cells = []
        for td in re.finditer(r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL):
            txt = re.sub(r"<[^>]+>", " ", td.group(1))
            txt = re.sub(r"\s+", " ", txt).strip()
            cells.append(txt)
        if len(cells) < 5:
            continue
        # Column order (Montgomery PA — may vary across counties):
        #   0 parcel_id, 1 owner, 2 address, 3 last_sale_date,
        #   4 last_sale_price, 5 building_area, 6 map_code
        out.append({
            "sindex": sindex,
            "idx": idx,
            "parcel_id": cells[0].strip() or None,
            "owner": cells[1].strip() or None,
            "address": cells[2].strip() or None,
            "last_sale_date": cells[3].strip() or None,
            "last_sale_price": cells[4].strip() or None,
        })
    return out


def _fetch_sales_history(sess: requests.Session, base_url: str,
                        sindex: int, idx: int) -> list:
    """Fetch the sale_hist datalet tab and parse all sales rows.
    Returns list of dicts:
        {"sale_date": str, "sale_price": str, "tax_stamps": str,
         "deed_book_page": str, "grantor": str, "grantee": str,
         "date_recorded": str}
    """
    url = (f"{base_url.rstrip('/')}/datalets/datalet.aspx"
           f"?mode=sale_hist&sIndex={sindex}&idx={idx}&LMparent=20")
    try:
        r = sess.get(url, timeout=_REQUEST_TIMEOUT)
        if r.status_code != 200:
            logger.warning("iasWorld sale_hist status %d", r.status_code)
            return []
        html = r.text
    except Exception as exc:
        logger.warning("iasWorld sale_hist fetch failed: %s", exc)
        return []

    # Sales table has headers: Sale Date, Sale Price, Tax Stamps,
    # Deed Book and Page, Grantor, Grantee, Date Recorded.
    # Find all <table>s and pick the one whose header row matches.
    sales = []
    for table_match in re.finditer(r"<table[^>]*>(.*?)</table>", html, re.DOTALL):
        tbl = table_match.group(1)
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.DOTALL)
        if not rows:
            continue
        # Header check
        first_cells = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip().upper()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", rows[0], re.DOTALL)
        ]
        if not first_cells or "SALE DATE" not in " ".join(first_cells):
            continue
        # Parse data rows
        for row in rows[1:]:
            cells = [
                re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)
            ]
            if not cells or all(not c or c == "-" for c in cells):
                continue
            sales.append({
                "sale_date":       cells[0] if len(cells) > 0 else "",
                "sale_price":      cells[1] if len(cells) > 1 else "",
                "tax_stamps":      cells[2] if len(cells) > 2 else "",
                "deed_book_page":  cells[3] if len(cells) > 3 else "",
                "grantor":         cells[4] if len(cells) > 4 else "",
                "grantee":         cells[5] if len(cells) > 5 else "",
                "date_recorded":   cells[6] if len(cells) > 6 else "",
            })
        break  # only one sales table
    return sales


# ────────────────────────────────────────────────────────────────────────
# Helpers for value coercion
# ────────────────────────────────────────────────────────────────────────

def _parse_money(s: str) -> Optional[float]:
    """'$475,000' → 475000.0 ; '0' → 0.0 ; '' → None"""
    if not s:
        return None
    clean = re.sub(r"[^\d.-]", "", s)
    try:
        return float(clean) if clean else None
    except ValueError:
        return None


_DATE_SLASH_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$")


def _parse_date(s: str) -> Optional[str]:
    """'03/15/2013' or '03-15-2013' → '2013-03-15' ; '' → None"""
    if not s or s in ("-", "—"):
        return None
    m = _DATE_SLASH_RE.match(s.strip())
    if not m:
        return None
    mm, dd, yy = m.group(1), m.group(2), m.group(3)
    if len(yy) == 2:
        yy = "19" + yy if int(yy) > 50 else "20" + yy
    return f"{yy}-{int(mm):02d}-{int(dd):02d}"


# ────────────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────────────

def fetch_iasworld(deal: DealData, pd_obj: ParcelData, addr, base_url: str) -> None:
    """Populate parcel_data + deed_history from a Tyler iasWorld portal.

    `base_url` should point to the app root (e.g.
    "https://propertyrecords.montcopa.org/pt/"). If the assessor_url in the
    registry points elsewhere (e.g. just the host root), pass that and the
    scraper tacks on "/pt/" when probing.
    """
    if not addr or not addr.street:
        return
    # Split street — reuse parcel_fetcher's conventions inline.
    street_parts = (addr.street or "").strip().split(None, 1)
    if len(street_parts) < 2:
        return
    num_raw = street_parts[0]
    name_raw = street_parts[1]
    m_num = re.match(r"^(\d+)", num_raw)
    if not m_num:
        return
    street_number = m_num.group(1)

    # Normalize base URL — ensure it ends with /pt/ (iasWorld app root)
    bu = base_url.rstrip("/")
    if not bu.lower().endswith("/pt"):
        # Try appending /pt if the caller gave just the host root
        bu = bu + "/pt"

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    # Step 1: disclaimer
    try:
        search_url = f"{bu}/search/commonsearch.aspx?mode=address"
        ok = _accept_disclaimer(sess, search_url)
        if not ok:
            logger.warning("iasWorld: could not accept disclaimer at %s", bu)
            return
    except Exception as exc:
        logger.warning("iasWorld disclaimer failed at %s: %s", bu, exc)
        return

    # Step 2: address search
    try:
        results = _address_search(sess, bu, street_number, name_raw,
                                 city=getattr(addr, "city", "") or "")
    except Exception as exc:
        logger.warning("iasWorld search failed at %s: %s", bu, exc)
        return
    if not results:
        logger.info("iasWorld: no matches at %s for %s %s",
                    bu, street_number, name_raw[:30])
        return

    # Step 3: pick the best match by token-overlap against the input street.
    # For "160 GULPH HILLS RD" we need to prefer a result whose address
    # contains BOTH "GULPH" and "HILLS" over one that only contains "GULPH".
    # Directionals and suffixes are excluded from the signature since they
    # rarely discriminate between neighbors.
    SUFFIX_DROP = {"STREET", "ST", "AVENUE", "AVE", "ROAD", "RD",
                   "BOULEVARD", "BLVD", "PLACE", "PL", "DRIVE", "DR",
                   "LANE", "LN", "COURT", "CT", "WAY", "PARKWAY",
                   "PKWY", "TERRACE", "TER",
                   "N", "S", "E", "W", "NE", "NW", "SE", "SW",
                   "NORTH", "SOUTH", "EAST", "WEST"}
    input_tokens = {t.upper() for t in re.split(r"[^A-Za-z0-9]+", name_raw)
                    if t and t.upper() not in SUFFIX_DROP}

    def _match_score(r):
        a_upper = (r.get("address") or "").upper()
        a_tokens = {t for t in re.split(r"[^A-Za-z0-9]+", a_upper)
                    if t and t not in SUFFIX_DROP}
        overlap = len(input_tokens & a_tokens)
        # Penalize extra tokens in the result (a shorter address with full
        # overlap beats a longer one with the same overlap)
        extras = len(a_tokens - input_tokens)
        # Prefer exact street_number prefix
        num_prefix = 1 if a_upper.startswith(street_number + " ") else 0
        return (-overlap, extras, -num_prefix)
    results.sort(key=_match_score)
    best = results[0]
    logger.info("iasWorld: matched parcel %s (%s) at %s",
                best.get("parcel_id"), (best.get("address") or "")[:40], bu)

    # Step 4: populate ParcelData fields from the search row
    if best.get("parcel_id"):
        pd_obj.parcel_id = best["parcel_id"]
    if best.get("owner") and not pd_obj.owner_name:
        pd_obj.owner_name = best["owner"]
    last_date = _parse_date(best.get("last_sale_date") or "")
    last_price = _parse_money(best.get("last_sale_price") or "")
    if last_date and not pd_obj.last_sale_date:
        pd_obj.last_sale_date = last_date
    if last_price is not None and not pd_obj.last_sale_price:
        pd_obj.last_sale_price = last_price

    deal.provenance.field_sources["property_records"] = (
        (deal.provenance.field_sources.get("property_records", "") + " + iasworld")
        .strip(" +")
    )

    # Step 5: pull full sales history from the detail tab
    try:
        sales = _fetch_sales_history(sess, bu, best["sindex"], best["idx"])
    except Exception as exc:
        logger.warning("iasWorld sale_hist fetch failed: %s", exc)
        sales = []

    seen_dates = {d.recording_date for d in pd_obj.deed_history if d.recording_date}
    added = 0
    for s in sales:
        rec_date = _parse_date(s.get("date_recorded") or "") or _parse_date(s.get("sale_date") or "")
        if not rec_date or rec_date in seen_dates:
            continue
        seen_dates.add(rec_date)
        pd_obj.deed_history.append(DeedRecord(
            recording_date=rec_date,
            document_type="Deed",
            grantor=s.get("grantor") or None,
            grantee=s.get("grantee") or None,
            consideration_amount=_parse_money(s.get("sale_price") or ""),
            document_id=s.get("deed_book_page") or None,
        ))
        if s.get("deed_book_page") and s["deed_book_page"] != "-" and not pd_obj.deed_book_page:
            pd_obj.deed_book_page = s["deed_book_page"]
        added += 1

    if added:
        pd_obj.deed_history.sort(key=lambda d: d.recording_date or "", reverse=True)
    logger.info("iasWorld: pulled %d deed history records (parcel=%s)",
                added, pd_obj.parcel_id)
