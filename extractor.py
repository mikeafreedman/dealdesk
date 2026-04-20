"""
extractor.py — Document Extraction Module
==========================================
Converts uploaded PDFs to structured DealData fields using PyMuPDF4LLM
and three Claude Haiku API calls:
    Prompt 1A — Offering Memorandum Parser
    Prompt 1B — Rent Roll Parser
    Prompt 1C — Financial Statement Parser

On any parse failure the pipeline continues with empty defaults.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, List, Optional

import anthropic
import pymupdf4llm

from config import ANTHROPIC_API_KEY, MODEL_HAIKU
from models.models import (
    CommercialComp, CompsData, DealData, ExtractedDocumentData,
    ImmediateRepairItem, LeaseAbstract, PCASystemCondition, RentComp, SaleComp,
    TitleException,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPTS (locked — FINAL_APPROVED_Prompt_Catalog_v4.md)
# ═══════════════════════════════════════════════════════════════════════════

SYSTEM_1A = (
    "You are a commercial real estate data extraction specialist. Extract factual data\n"
    "from offering memorandums and return structured JSON.\n\n"
    "EXTRACTION RULES:\n"
    "- Extract ONLY information explicitly present in the document.\n"
    "- If a field is not found, return null. Never guess or hallucinate.\n"
    "- Numbers without formatting (1500000 not \"$1,500,000\").\n"
    "- Percentages as decimals (0.065 not \"6.5%\").\n"
    "- Dates in ISO format (YYYY-MM-DD).\n"
    "- Ambiguous/inferred values: add a \"_confidence\": \"inferred\" sibling field.\n\n"
    "IMAGE CLASSIFICATION:\n"
    "For each image classify into: exterior | interior | aerial | site_plan |\n"
    "floor_plan | neighborhood | retail_facade | marketing | unknown\n\n"
    "For each image assign:\n"
    "  category, report_placement (hero/gallery/floor_plan/appendix/skip),\n"
    "  quality_rank (1-10), caption_suggestion (8 words max)\n\n"
    "Output ONLY valid JSON. No markdown, no preamble."
)

SYSTEM_1B = (
    "You are a commercial real estate analyst specializing in rent roll analysis.\n"
    "Extract all unit-level data from the rent roll. Return structured JSON.\n\n"
    "RULES:\n"
    "- Extract ONLY data explicitly present. Return null for missing fields.\n"
    "- Monthly rents as numbers. Dates in ISO format.\n"
    "- Lease status: \"occupied\" | \"vacant\" | \"month-to-month\" | \"notice\" | \"pending\"\n"
    "- Unit type: \"Studio\" | \"1BR\" | \"2BR\" | \"3BR\" | \"4BR+\" | \"Commercial\" | \"Other\"\n"
    "Output ONLY valid JSON."
)

SYSTEM_CLASSIFY = (
    "You are a commercial real estate document classifier. Given the text of\n"
    "an uploaded PDF, identify every document type it contains. A single PDF\n"
    "often bundles multiple types (e.g. an offering memorandum may include a\n"
    "rent roll and a T-12). Return every type that is present with any\n"
    "meaningful content.\n\n"
    "TYPES (use ONLY these slugs):\n"
    "  om                — Offering Memorandum / broker teaser / marketing package\n"
    "  rent_roll         — Unit-level rent roll with tenant / lease data\n"
    "  t12               — Trailing-12 operating statement / profit & loss / P&L\n"
    "  budget            — Forward operating budget / pro forma\n"
    "  environmental     — Phase I or Phase II ESA, Environmental Site Assessment\n"
    "  pca               — Property Condition Assessment / Engineering report\n"
    "  survey            — ALTA / boundary / topo survey\n"
    "  floor_plans       — Building / unit floor plans\n"
    "  site_plan         — Site plan / plot plan / civil drawings\n"
    "  appraisal         — Formal appraisal (MAI or otherwise)\n"
    "  title             — Title commitment / title report\n"
    "  zoning_letter     — Zoning verification letter / municipal zoning report\n"
    "  lease             — Individual lease document\n"
    "  other             — None of the above (describe in notes)\n\n"
    "Output ONLY valid JSON. No markdown, no preamble."
)

USER_CLASSIFY = (
    "Classify the document below. Return a single JSON object.\n\n"
    "DOCUMENT TEXT (first 8000 chars):\n{doc_text}\n\n"
    "Return JSON:\n"
    '{{\n'
    '  "doc_types_present": ["om"],\n'
    '  "primary_type": "om",\n'
    '  "confidence": "high",\n'
    '  "notes": "single sentence — what the document actually is"\n'
    '}}'
)


SYSTEM_1D = (
    "You are an environmental due diligence analyst specializing in Phase I / II\n"
    "Environmental Site Assessments (ASTM E1527-21). Extract findings and\n"
    "recommendations from the report text. Return structured JSON.\n\n"
    "RULES:\n"
    "- Every finding must be explicitly present in the document. Return null\n"
    "  for missing fields.\n"
    "- RECs (Recognized Environmental Conditions) are material findings —\n"
    "  extract each as a short sentence.\n"
    "- HRECs (Historical RECs) are resolved historical contamination.\n"
    "- vapor_intrusion_flag: true if the report flags vapor intrusion concerns.\n"
    "- phase2_recommended: true if the report recommends Phase II sampling.\n"
    "- phase1_status: \"complete\" | \"draft\" | \"pending\" | \"n/a\".\n"
    "Output ONLY valid JSON."
)

USER_1D = (
    "Extract environmental report findings: {env_text}\n\n"
    "Return JSON:\n"
    '{{\n'
    '  "phase1_status": null, "phase1_date": null, "phase1_consultant": null,\n'
    '  "recognized_environmental_conditions": [],\n'
    '  "historical_recognized_conditions": [],\n'
    '  "vapor_intrusion_flag": null,\n'
    '  "phase2_recommended": null,\n'
    '  "findings_summary": null,\n'
    '  "recommendations": null\n'
    '}}'
)


SYSTEM_1E = (
    "You are a commercial real estate lease abstractor. Extract the material\n"
    "business terms from one or more lease documents and return structured JSON.\n\n"
    "RULES:\n"
    "- Extract ONLY what is explicitly in the document. Null for missing fields.\n"
    "- escalation_type ∈ {fixed, stepped, CPI, market_reset, none}.\n"
    "- cam_structure ∈ {gross, modified_gross, base_year, expense_stop, pro_rata_NNN, full_NNN, other}.\n"
    "- Monthly rents as numbers. Dates ISO (YYYY-MM-DD).\n"
    "- renewal_options as short strings (e.g. \"2 × 5yr at FMV\").\n"
    "- One lease object per distinct lease found; do not dedupe unit IDs.\n"
    "Output ONLY valid JSON."
)

USER_1E = (
    "Extract all lease terms from the document below: {lease_text}\n\n"
    "Return JSON:\n"
    '{{\n'
    '  "leases": [{{\n'
    '    "unit_id": null, "tenant_name": null, "lease_type": null,\n'
    '    "commencement_date": null, "expiration_date": null, "term_months": null,\n'
    '    "base_rent_monthly": null, "base_rent_psf": null,\n'
    '    "escalation_type": null, "escalation_amount": null,\n'
    '    "cam_structure": null, "cam_base_year": null,\n'
    '    "ti_allowance_psf": null, "free_rent_months": null,\n'
    '    "renewal_options": [], "personal_guaranty": null,\n'
    '    "percentage_rent": null, "go_dark_allowed": null,\n'
    '    "kickout_clause": null, "radius_restriction": null,\n'
    '    "special_clauses": []\n'
    '  }}]\n'
    '}}'
)


SYSTEM_1F = (
    "You are a title abstractor. Extract the material content of a title\n"
    "commitment or preliminary title report and return structured JSON.\n\n"
    "RULES:\n"
    "- Schedule A → vesting, legal description, insured amount, effective date.\n"
    "- Schedule B → each exception / encumbrance as its own record.\n"
    "- exception_type ∈ {easement, covenant, restriction, lien, mortgage,\n"
    "  lease, agreement, reservation, encroachment, tax_exception, other}.\n"
    "- Summarize each exception in ≤ 25 words.\n"
    "- Dates ISO (YYYY-MM-DD). Dollar amounts as numbers.\n"
    "Output ONLY valid JSON."
)

USER_1F = (
    "Extract title commitment data from the document below: {title_text}\n\n"
    "Return JSON:\n"
    '{{\n'
    '  "title_commitment_date": null, "title_company": null,\n'
    '  "title_insurance_amount": null,\n'
    '  "title_vesting": null, "title_legal_description": null,\n'
    '  "title_exceptions": [{{\n'
    '    "exception_type": null, "recording_date": null, "document_id": null,\n'
    '    "grantor": null, "grantee": null, "summary": null\n'
    '  }}],\n'
    '  "title_easements": [],\n'
    '  "title_endorsements": []\n'
    '}}'
)


SYSTEM_1G = (
    "You are a building-systems engineer reviewing a Property Condition\n"
    "Assessment / engineering report. Extract findings and capex forecasts\n"
    "and return structured JSON.\n\n"
    "RULES:\n"
    "- condition ∈ {excellent, good, fair, poor, end_of_life}.\n"
    "- priority on immediate repairs ∈ {immediate, short_term, long_term}.\n"
    "- capex_by_year keyed by 4-digit calendar year (e.g. \"2026\"); totals in dollars.\n"
    "- If the report states a deferred maintenance total, extract it explicitly.\n"
    "Output ONLY valid JSON."
)

USER_1G = (
    "Extract PCA / engineering findings from the report below: {pca_text}\n\n"
    "Return JSON:\n"
    '{{\n'
    '  "pca_report_date": null, "pca_consultant": null,\n'
    '  "pca_overall_condition": null,\n'
    '  "pca_deferred_maintenance_total": null,\n'
    '  "pca_capex_12yr_total": null,\n'
    '  "pca_capex_by_year": {{}},\n'
    '  "pca_building_systems": [{{\n'
    '    "system": null, "age_years": null, "condition": null,\n'
    '    "remaining_useful_life": null, "replacement_cost": null, "notes": null\n'
    '  }}],\n'
    '  "pca_immediate_repairs": [{{\n'
    '    "item": null, "cost": null, "priority": null\n'
    '  }}],\n'
    '  "pca_ada_items": []\n'
    '}}'
)


SYSTEM_1C = (
    "You are a commercial real estate financial analyst specializing in T-12 normalization.\n"
    "Extract all financial data and return structured JSON.\n\n"
    "RULES:\n"
    "- All dollar amounts as numbers without formatting.\n"
    "- Normalize to annual amounts. Flag if figures appear monthly.\n"
    "- Create a named snake_case key for EVERY expense line item. Goal: zero \"other.\"\n"
    "  Example: \"Snow Removal\" → \"snow_removal\", \"R&M-HVAC\" → \"rm_hvac\"\n"
    "- NNN reconciliation: for each recoverable expense capture:\n"
    "    gross_amount, tenant_reimbursement, net_to_owner\n"
    "- If a field is not found: return null.\n"
    "Output ONLY valid JSON."
)

# ═══════════════════════════════════════════════════════════════════════════
# USER MESSAGE TEMPLATES
# ═══════════════════════════════════════════════════════════════════════════

USER_1A = (
    "Extract all property data from the offering memorandum below.\n\n"
    "DOCUMENT TEXT: {om_text}\n"
    "IMAGES (base64): {images_json}\n\n"
    "Return JSON:\n"
    '{{\n'
    '  "property_name": null, "full_address": null, "city": null, "state": null,\n'
    '  "zip_code": null, "asset_type": null, "asking_price": null,\n'
    '  "total_units": null, "total_sf": null, "lot_sf": null, "year_built": null,\n'
    '  "zoning_code": null, "deal_source": null, "broker_name": null,\n'
    '  "broker_firm": null, "broker_phone": null, "broker_email": null,\n'
    '  "cap_rate_listed": null, "noi_listed": null, "gross_scheduled_income": null,\n'
    '  "price_per_unit": null, "price_per_sf": null, "occupancy_rate": null,\n'
    '  "property_description": null, "deal_highlights": [], "unit_mix_summary": [],\n'
    '  "financial_highlights": {{}}, "notable_tenants": [],\n'
    '  "recent_renovations": null, "utilities_responsibility": null, "parking": null,\n'
    '  "images": [{{"image_index": 0, "category": null, "report_placement": null,\n'
    '              "quality_rank": null, "caption_suggestion": null}}],\n'
    '  "data_confidence": null, "extraction_notes": null\n'
    '}}'
)

USER_1B = (
    "Extract all rent roll data: {rent_roll_text}\n\n"
    "Return JSON:\n"
    '{{\n'
    '  "total_units": null, "total_occupied": null, "total_vacant": null,\n'
    '  "occupancy_rate": null, "total_monthly_rent_in_place": null,\n'
    '  "avg_rent_per_unit": null, "avg_rent_per_sf": null, "rent_roll_date": null,\n'
    '  "units": [{{"unit_id": null, "unit_type": null, "sf": null,\n'
    '             "monthly_rent": null, "market_rent": null,\n'
    '             "lease_start": null, "lease_end": null,\n'
    '             "status": null, "tenant_name": null, "notes": null}}],\n'
    '  "unit_mix_summary": [{{"unit_type": null, "count": null, "avg_sf": null,\n'
    '                        "avg_rent": null, "total_rent": null}}],\n'
    '  "lease_expiration_schedule": {{}},\n'
    '  "extraction_notes": null\n'
    '}}'
)

USER_1C = (
    "Extract all financial statement data: {financial_statement_text}\n\n"
    "Return JSON:\n"
    '{{\n'
    '  "statement_period": null, "statement_type": null,\n'
    '  "gross_potential_rent": null, "loss_to_lease": null,\n'
    '  "gross_scheduled_rent": null, "vacancy_loss": null,\n'
    '  "bad_debt_loss": null, "other_income": null,\n'
    '  "cam_reimbursements": {{"gross": null, "tenant_reimbursement": null,\n'
    '                         "net_to_owner": null, "breakdown": {{}}}},\n'
    '  "effective_gross_income": null,\n'
    '  "operating_expenses": {{\n'
    '    "[dynamic_snake_case_key]": {{"gross_amount": null,\n'
    '                                  "tenant_reimbursement": null,\n'
    '                                  "net_to_owner": null}}\n'
    '  }},\n'
    '  "total_operating_expenses": null, "noi": null,\n'
    '  "noi_margin": null, "expense_ratio": null,\n'
    '  "debt_service": null, "net_cash_flow": null,\n'
    '  "per_unit_metrics": {{"egi_per_unit": null, "expense_per_unit": null, "noi_per_unit": null}},\n'
    '  "normalization_adjustments": [],\n'
    '  "extraction_notes": null\n'
    '}}'
)


# ═══════════════════════════════════════════════════════════════════════════
# PDF → MARKDOWN
# ═══════════════════════════════════════════════════════════════════════════

def pdf_to_markdown(pdf_path: str) -> str:
    """Convert a PDF file to markdown text via PyMuPDF4LLM."""
    return pymupdf4llm.to_markdown(pdf_path)


# ═══════════════════════════════════════════════════════════════════════════
# HAIKU CALL HELPER
# ═══════════════════════════════════════════════════════════════════════════

_ISO_DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d",
    "%m/%d/%Y", "%m-%d-%Y",
    "%d %B %Y", "%B %d, %Y", "%b %d, %Y",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S",
]


def _coerce_iso_date(v) -> Optional[str]:
    """Coerce a mixed-format date string to ISO 'YYYY-MM-DD'. Returns the
    original string trimmed to 10 chars if parsing fails — never None
    unless the input was already None/empty."""
    if v is None or v == "":
        return None
    from datetime import datetime as _dt
    s = str(v).strip()
    # Fast path: already ISO-shaped
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    for fmt in _ISO_DATE_FORMATS:
        try:
            return _dt.strptime(s[:len(fmt) + 10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Last-resort: accept first 10 chars if they look date-ish
    return s[:10]


def _call_haiku(system: str, user_msg: str, _attempt: int = 1,
                max_attempts: int = 3) -> Optional[dict]:
    """Send a single Haiku extraction call. Returns parsed JSON or None.

    Retries up to `max_attempts` times on JSONDecodeError (Haiku
    occasionally truncates or omits a quote mid-response for long
    documents) and on transient API errors (500/529/overloaded). The
    retry prompt adds a terse reminder to output valid JSON only, which
    measurably reduces parse failures.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=MODEL_HAIKU,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text
        raw = (raw.strip().removeprefix("```json")
                  .removeprefix("```")
                  .removesuffix("```").strip())
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Haiku JSON parse failed (attempt %d/%d): %s",
                       _attempt, max_attempts, exc)
        if _attempt < max_attempts:
            import time as _time
            _time.sleep(1.5 * _attempt)
            reminder = (user_msg
                        + "\n\nREMINDER: Output must be strictly valid JSON. "
                          "No trailing commas, no unescaped quotes, no "
                          "markdown fences, no commentary. If you cannot "
                          "fit every field, return null for the field.")
            return _call_haiku(system, reminder, _attempt + 1, max_attempts)
        return None
    except anthropic.APIStatusError as exc:
        transient = any(code in str(exc) for code in
                        ["500", "502", "503", "529", "overloaded", "timeout"])
        logger.warning("Haiku API error (attempt %d/%d, transient=%s): %s",
                       _attempt, max_attempts, transient, exc)
        if transient and _attempt < max_attempts:
            import time as _time
            _time.sleep(5 * _attempt)
            return _call_haiku(system, user_msg, _attempt + 1, max_attempts)
        return None
    except (anthropic.APIError, IndexError, KeyError) as exc:
        logger.warning("Haiku extraction call failed: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT 1A — Offering Memorandum
# ═══════════════════════════════════════════════════════════════════════════

def _apply_1a(data: dict, deal: DealData) -> None:
    """Map Prompt 1A response fields onto DealData."""
    ext = deal.extracted_docs

    def _s(v):
        """Trim LLM string fields to avoid leading/trailing whitespace
        (which otherwise leaks into report rendering)."""
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    ext.property_name         = _s(data.get("property_name"))
    ext.asking_price          = data.get("asking_price")
    ext.deal_source           = _s(data.get("deal_source"))
    ext.broker_name           = _s(data.get("broker_name"))
    ext.broker_firm           = _s(data.get("broker_firm"))
    ext.broker_phone          = _s(data.get("broker_phone"))
    ext.broker_email          = _s(data.get("broker_email"))
    ext.num_units_extracted   = data.get("total_units")
    ext.gba_sf_extracted      = data.get("total_sf")
    ext.lot_sf_extracted  = data.get("lot_sf")

    # Only accept year_built if not flagged as inferred — a guessed year
    # is worse than no year because it populates the Excel model with false data.
    year_built_raw        = data.get("year_built")
    year_built_confidence = data.get("year_built_confidence", "").lower()
    if year_built_raw is not None and year_built_confidence != "inferred":
        ext.year_built_extracted = year_built_raw
    else:
        if year_built_confidence == "inferred":
            logger.info(
                "year_built suppressed — marked inferred by extractor (value was %s)",
                year_built_raw,
            )
        ext.year_built_extracted = None
    ext.description_extracted = _s(data.get("property_description"))
    ext.image_placements      = {"images": data.get("images", [])}

    # Extract comp data if present in the OM
    raw_rent    = data.get("rent_comps") or []
    raw_comm    = data.get("commercial_comps") or []
    raw_sale    = data.get("sale_comps") or []
    if any([raw_rent, raw_comm, raw_sale]):
        def _safe(cls, items):
            out = []
            for item in (items or []):
                if isinstance(item, dict) and any(v for v in item.values() if v is not None):
                    try:
                        out.append(cls(**{k: v for k, v in item.items() if k in cls.model_fields}))
                    except Exception:
                        pass
            return out
        ext.comps = CompsData(
            rent_comps=_safe(RentComp, raw_rent),
            commercial_comps=_safe(CommercialComp, raw_comm),
            sale_comps=_safe(SaleComp, raw_sale),
        )
        logger.info(
            "Prompt 1A comps extracted — %d rent, %d commercial, %d sale",
            len(ext.comps.rent_comps),
            len(ext.comps.commercial_comps),
            len(ext.comps.sale_comps),
        )

    # Backfill address from OM if not already set
    addr = deal.address
    if not addr.full_address and _s(data.get("full_address")):
        addr.full_address = _s(data["full_address"])
    if not addr.city and _s(data.get("city")):
        addr.city = _s(data["city"])
    if not addr.state and _s(data.get("state")):
        addr.state = _s(data["state"])
    if not addr.zip_code and _s(data.get("zip_code")):
        addr.zip_code = _s(data["zip_code"])


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT 1B — Rent Roll
# ═══════════════════════════════════════════════════════════════════════════

def _apply_1b(data: dict, deal: DealData) -> None:
    """Map Prompt 1B response fields onto DealData. First-populated-wins:
    don't overwrite a value from an earlier (more-trusted) file."""
    ext = deal.extracted_docs
    units = data.get("units") or []
    meaningful_units = [u for u in units if isinstance(u, dict) and any(
        u.get(k) for k in ("unit_id", "monthly_rent", "tenant_name", "sf")
    )]
    if not meaningful_units and not data.get("total_monthly_rent_in_place"):
        logger.info("APPLY [1B]: no meaningful rent roll data in response — skipping")
        return

    # Coerce lease dates to ISO — rent rolls often ship as "MM/DD/YYYY"
    # which breaks downstream date parsing.
    for u in meaningful_units:
        if u.get("lease_start"):
            u["lease_start"] = _coerce_iso_date(u["lease_start"])
        if u.get("lease_end"):
            u["lease_end"] = _coerce_iso_date(u["lease_end"])

    if meaningful_units and not ext.unit_mix:
        ext.unit_mix = meaningful_units
        logger.info("APPLY [1B]: unit_mix ← %d units", len(meaningful_units))
    elif meaningful_units:
        logger.info("APPLY [1B]: unit_mix already populated (%d) — keeping prior",
                    len(ext.unit_mix or []))
    for src, dst, label in [
        ("total_units",                 "total_units_from_rr", "total_units_from_rr"),
        ("total_monthly_rent_in_place", "total_monthly_rent",  "total_monthly_rent"),
        ("avg_rent_per_unit",           "avg_rent_per_unit",   "avg_rent_per_unit"),
        ("occupancy_rate",              "occupancy_rate",      "occupancy_rate"),
    ]:
        v = data.get(src)
        if v in (None, "", 0):
            continue
        if getattr(ext, dst) in (None, "", 0):
            setattr(ext, dst, v)
            logger.info("APPLY [1B]: %s ← %r", label, v)
        else:
            logger.info("APPLY [1B]: %s already set (%r) — keeping prior",
                        label, getattr(ext, dst))


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT 1C — Financial Statements / T-12
# ═══════════════════════════════════════════════════════════════════════════

def _apply_1c(data: dict, deal: DealData) -> None:
    """Map Prompt 1C response fields onto DealData. Validation gate: skip
    entirely when the response has no financial signal (all critical fields
    null — common when running 1C against a document that is not a T-12).
    First-populated-wins for each field."""
    ext = deal.extracted_docs
    critical = ("gross_potential_rent", "effective_gross_income",
                "total_operating_expenses", "noi")
    if not any(data.get(k) for k in critical):
        logger.info("APPLY [1C]: response has no T-12 signal (all of %s are null) — skipping",
                    list(critical))
        return

    for src, dst, label in [
        ("gross_potential_rent",       "gross_potential_rent_t12",   "gross_potential_rent_t12"),
        ("effective_gross_income",     "effective_gross_income_t12", "effective_gross_income_t12"),
        ("total_operating_expenses",   "total_expenses_t12",         "total_expenses_t12"),
        ("noi",                        "noi_t12",                    "noi_t12"),
    ]:
        v = data.get(src)
        if v in (None, "", 0):
            continue
        if getattr(ext, dst) in (None, "", 0):
            setattr(ext, dst, v)
            logger.info("APPLY [1C]: %s ← $%s", label,
                        f"{v:,.0f}" if isinstance(v, (int, float)) else v)
        else:
            logger.info("APPLY [1C]: %s already set ($%s) — keeping prior",
                        label, f"{getattr(ext, dst):,.0f}")

    # Flatten operating_expenses dict → {key: net_to_owner or gross_amount}
    raw_expenses = data.get("operating_expenses") or {}
    flat: dict[str, float] = {}
    for key, val in raw_expenses.items():
        if isinstance(val, dict):
            flat[key] = val.get("net_to_owner") or val.get("gross_amount")
        elif isinstance(val, (int, float)):
            flat[key] = val
    if flat and not ext.expense_line_items:
        ext.expense_line_items = flat
        logger.info("APPLY [1C]: expense_line_items ← %d keys (%s)",
                    len(flat), ", ".join(list(flat.keys())[:6]))

    cam = data.get("cam_reimbursements") or {}
    cam_net = cam.get("net_to_owner") if isinstance(cam, dict) else None
    if cam_net and not ext.cam_reimbursements_t12:
        ext.cam_reimbursements_t12 = cam_net
        logger.info("APPLY [1C]: cam_reimbursements_t12 ← $%s", f"{cam_net:,.0f}")
    if isinstance(cam, dict) and cam.get("breakdown") and not ext.nnn_reconciliation:
        ext.nnn_reconciliation = cam
        logger.info("APPLY [1C]: nnn_reconciliation captured")


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT 1D — Environmental (Phase I / II ESA)
# ═══════════════════════════════════════════════════════════════════════════

def _apply_1d(data: dict, deal: DealData) -> None:
    """Map Prompt 1D (environmental) response onto DealData."""
    ext = deal.extracted_docs
    recs = data.get("recognized_environmental_conditions") or []
    hrecs = data.get("historical_recognized_conditions") or []
    # A "Phase I completed, no RECs found" report is a real positive
    # signal and must be preserved. Use explicit None-checks for the
    # boolean flags rather than falsy-any so False values still count.
    signal = (
        bool(recs) or bool(hrecs)
        or bool(data.get("findings_summary"))
        or bool(data.get("phase1_status"))
        or bool(data.get("phase1_date"))
        or bool(data.get("phase1_consultant"))
        or data.get("vapor_intrusion_flag") is not None
        or data.get("phase2_recommended") is not None
        or bool(data.get("recommendations"))
    )
    if not signal:
        logger.info("APPLY [1D]: no environmental signal — skipping")
        return

    for src, dst in [
        ("phase1_status",            "phase1_status"),
        ("phase1_date",              "phase1_date"),
        ("phase1_consultant",        "phase1_consultant"),
        ("vapor_intrusion_flag",     "vapor_intrusion_flag"),
        ("phase2_recommended",       "phase2_recommended"),
        ("findings_summary",         "environmental_findings"),
        ("recommendations",          "environmental_recommendations"),
    ]:
        v = data.get(src)
        if v in (None, ""):
            continue
        # Normalize date fields to ISO
        if src == "phase1_date":
            v = _coerce_iso_date(v)
        if getattr(ext, dst) in (None, "", 0):
            setattr(ext, dst, v)
            logger.info("APPLY [1D]: %s ← %r", dst, v)

    if recs:
        existing = list(ext.recognized_environmental_conditions or [])
        ext.recognized_environmental_conditions = existing + [str(r) for r in recs]
        logger.info("APPLY [1D]: recognized_environmental_conditions +%d (total=%d)",
                    len(recs), len(ext.recognized_environmental_conditions))
    if hrecs:
        existing = list(ext.historical_recognized_conditions or [])
        ext.historical_recognized_conditions = existing + [str(r) for r in hrecs]
        logger.info("APPLY [1D]: historical_recognized_conditions +%d (total=%d)",
                    len(hrecs), len(ext.historical_recognized_conditions))


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT 1E — Lease abstraction
# ═══════════════════════════════════════════════════════════════════════════

def _apply_1e(data: dict, deal: DealData) -> None:
    ext = deal.extracted_docs
    raw = data.get("leases") or []
    if not isinstance(raw, list) or not raw:
        logger.info("APPLY [1E]: no leases in response — skipping")
        return
    added = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        if not any(item.get(k) for k in ("tenant_name", "base_rent_monthly",
                                          "commencement_date", "expiration_date")):
            continue
        # Normalize date fields to ISO before Pydantic validation
        for dk in ("commencement_date", "expiration_date"):
            if item.get(dk):
                item[dk] = _coerce_iso_date(item[dk])
        try:
            la = LeaseAbstract(**{
                k: v for k, v in item.items() if k in LeaseAbstract.model_fields
            })
            ext.lease_abstracts.append(la)
            added += 1
        except Exception as exc:
            logger.debug("APPLY [1E]: skipped malformed lease: %s", exc)
    logger.info("APPLY [1E]: appended %d lease(s) (total=%d)",
                added, len(ext.lease_abstracts))


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT 1F — Title commitment
# ═══════════════════════════════════════════════════════════════════════════

def _apply_1f(data: dict, deal: DealData) -> None:
    ext = deal.extracted_docs
    exceptions = data.get("title_exceptions") or []
    signal = any([
        data.get("title_commitment_date"), data.get("title_company"),
        data.get("title_vesting"), exceptions,
    ])
    if not signal:
        logger.info("APPLY [1F]: no title signal — skipping")
        return

    for src, dst in [
        ("title_commitment_date",   "title_commitment_date"),
        ("title_company",           "title_company"),
        ("title_insurance_amount",  "title_insurance_amount"),
        ("title_vesting",           "title_vesting"),
        ("title_legal_description", "title_legal_description"),
    ]:
        v = data.get(src)
        if v in (None, ""):
            continue
        if src == "title_commitment_date":
            v = _coerce_iso_date(v)
        if getattr(ext, dst) in (None, "", 0):
            setattr(ext, dst, v)
            logger.info("APPLY [1F]: %s ← %r", dst, str(v)[:60])

    added = 0
    for item in (exceptions or []):
        if not isinstance(item, dict):
            continue
        if not any(item.get(k) for k in ("exception_type", "summary", "document_id")):
            continue
        # Normalize exception recording date to ISO
        if item.get("recording_date"):
            item["recording_date"] = _coerce_iso_date(item["recording_date"])
        try:
            ext.title_exceptions.append(TitleException(**{
                k: v for k, v in item.items() if k in TitleException.model_fields
            }))
            added += 1
        except Exception:
            pass
    if added:
        logger.info("APPLY [1F]: appended %d title exception(s) (total=%d)",
                    added, len(ext.title_exceptions))

    ease = data.get("title_easements") or []
    if ease:
        ext.title_easements.extend([str(e) for e in ease])
    endo = data.get("title_endorsements") or []
    if endo:
        ext.title_endorsements.extend([str(e) for e in endo])


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT 1G — PCA / engineering report
# ═══════════════════════════════════════════════════════════════════════════

def _apply_1g(data: dict, deal: DealData) -> None:
    ext = deal.extracted_docs
    systems = data.get("pca_building_systems") or []
    immediate = data.get("pca_immediate_repairs") or []
    signal = any([
        systems, immediate,
        data.get("pca_overall_condition"),
        data.get("pca_deferred_maintenance_total"),
        data.get("pca_capex_12yr_total"),
    ])
    if not signal:
        logger.info("APPLY [1G]: no PCA signal — skipping")
        return

    for src, dst in [
        ("pca_report_date",               "pca_report_date"),
        ("pca_consultant",                "pca_consultant"),
        ("pca_overall_condition",         "pca_overall_condition"),
        ("pca_deferred_maintenance_total","pca_deferred_maintenance_total"),
        ("pca_capex_12yr_total",          "pca_capex_12yr_total"),
    ]:
        v = data.get(src)
        if v in (None, "", 0):
            continue
        if getattr(ext, dst) in (None, "", 0):
            setattr(ext, dst, v)
            logger.info("APPLY [1G]: %s ← %r", dst, str(v)[:60])

    cby = data.get("pca_capex_by_year")
    if isinstance(cby, dict) and cby and not ext.pca_capex_by_year:
        ext.pca_capex_by_year = {str(k): float(v) for k, v in cby.items()
                                  if isinstance(v, (int, float))}
        logger.info("APPLY [1G]: pca_capex_by_year ← %d years", len(ext.pca_capex_by_year))

    sys_added = 0
    for item in systems:
        if not isinstance(item, dict) or not item.get("system"):
            continue
        try:
            ext.pca_building_systems.append(PCASystemCondition(**{
                k: v for k, v in item.items() if k in PCASystemCondition.model_fields
            }))
            sys_added += 1
        except Exception:
            pass
    if sys_added:
        logger.info("APPLY [1G]: appended %d building system(s) (total=%d)",
                    sys_added, len(ext.pca_building_systems))

    rep_added = 0
    for item in immediate:
        if not isinstance(item, dict) or not item.get("item"):
            continue
        try:
            ext.pca_immediate_repairs.append(ImmediateRepairItem(**{
                k: v for k, v in item.items() if k in ImmediateRepairItem.model_fields
            }))
            rep_added += 1
        except Exception:
            pass
    if rep_added:
        logger.info("APPLY [1G]: appended %d immediate repair(s) (total=%d)",
                    rep_added, len(ext.pca_immediate_repairs))

    ada = data.get("pca_ada_items") or []
    if ada:
        ext.pca_ada_items.extend([str(a) for a in ada])


# ═══════════════════════════════════════════════════════════════════════════
# DOCUMENT CLASSIFIER
# ═══════════════════════════════════════════════════════════════════════════

def _classify_document(md_text: str) -> dict:
    """Run one quick Haiku call to detect which doc types are in the file.
    Falls back to a conservative guess (treat as OM) on failure."""
    snippet = md_text[:8000]
    user_msg = USER_CLASSIFY.format(doc_text=snippet)
    result = _call_haiku(SYSTEM_CLASSIFY, user_msg)
    if not result:
        logger.warning("CLASSIFY: LLM returned nothing — defaulting to {om}")
        return {"doc_types_present": ["om"], "primary_type": "om",
                "confidence": "low", "notes": "classifier failed"}
    # Sanity
    types = result.get("doc_types_present") or []
    if not isinstance(types, list) or not types:
        types = [result.get("primary_type") or "om"]
        result["doc_types_present"] = types
    logger.info("CLASSIFY: types=%s primary=%s conf=%s — %s",
                types, result.get("primary_type"),
                result.get("confidence"), (result.get("notes") or "")[:80])
    return result


# ═══════════════════════════════════════════════════════════════════════════
# SCHEMA SANITY CHECK (runs at module import)
# ═══════════════════════════════════════════════════════════════════════════

def _verify_scalar_map_schema() -> None:
    """Fail loudly if a _SCALAR_MAP or _LIST_MAP destination doesn't exist
    on ExtractedDocumentData. Prevents silent no-op merges from schema drift."""
    fields = set(ExtractedDocumentData.model_fields.keys())
    missing = []
    for _src, dst in _SCALAR_MAP + _LIST_MAP:
        if dst not in fields:
            missing.append(dst)
    if missing:
        logger.warning("EXTRACTOR SCHEMA: %d map destinations not on model: %s",
                       len(missing), missing)


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

# Scalar merge mapping: source JSON key → ExtractedDocumentData attribute.
# Every destination must exist on ExtractedDocumentData or the merge silently
# no-ops via AttributeError. This list is schema-validated at startup below.
_SCALAR_MAP = [
    # 1A — OM
    ("property_name",                "property_name"),
    ("asking_price",                 "asking_price"),
    ("deal_source",                  "deal_source"),
    ("broker_name",                  "broker_name"),
    ("year_built",                   "year_built_extracted"),
    ("num_units",                    "num_units_extracted"),
    ("total_units",                  "num_units_extracted"),
    ("total_sf",                     "gba_sf_extracted"),
    ("lot_sf",                       "lot_sf_extracted"),
    ("property_description",         "description_extracted"),
    # 1B — Rent Roll
    ("occupancy_rate",               "occupancy_rate"),
    ("total_monthly_rent_in_place",  "total_monthly_rent"),
    ("avg_rent_per_unit",            "avg_rent_per_unit"),
    # 1C — T-12
    ("gross_potential_rent",         "gross_potential_rent_t12"),
    ("effective_gross_income",       "effective_gross_income_t12"),
    ("total_operating_expenses",     "total_expenses_t12"),
    ("noi",                          "noi_t12"),
    # 1D — Environmental
    ("phase1_status",                "phase1_status"),
    ("phase1_date",                  "phase1_date"),
    ("phase1_consultant",            "phase1_consultant"),
    ("vapor_intrusion_flag",         "vapor_intrusion_flag"),
    ("phase2_recommended",           "phase2_recommended"),
    ("findings_summary",             "environmental_findings"),
    ("recommendations",              "environmental_recommendations"),
]

# List merge mapping: source JSON key → ExtractedDocumentData attribute (append)
# NOTE: unit_mix is handled by _apply_1b with first-populated-wins semantics;
# do NOT also append via _merge_extraction or rent rolls will double-count.
_LIST_MAP = [
    ("recognized_environmental_conditions",   "recognized_environmental_conditions"),
    ("historical_recognized_conditions",      "historical_recognized_conditions"),
    ("floor_plan_pages",                      "floor_plan_pages"),
    ("site_plan_pages",                       "site_plan_pages"),
]


def _merge_extraction(ext, data: dict, source: str, file: str = "") -> None:
    """Merge an extraction dict into ExtractedDocumentData.

    Scalars: first-populated-wins (don't overwrite None/""/0).
    Lists: always append.
    Silently skips attributes that don't exist on the model.
    """
    if not data:
        return
    tag = f"EXTRACTOR [{source}]"
    # Scalars
    for src_key, attr in _SCALAR_MAP:
        if src_key not in data:
            continue
        val = data.get(src_key)
        if val is None or val == "" or val == 0:
            continue
        try:
            current = getattr(ext, attr)
        except AttributeError:
            continue
        if current is None or current == "" or current == 0:
            try:
                setattr(ext, attr, val)
                logger.info("%s: set %s=%r (from %s)", tag, attr, val, file or src_key)
            except AttributeError:
                pass
    # Lists
    for src_key, attr in _LIST_MAP:
        src_list = data.get(src_key)
        if not src_list or not isinstance(src_list, list):
            continue
        try:
            current = getattr(ext, attr)
        except AttributeError:
            continue
        try:
            if current is None:
                setattr(ext, attr, list(src_list))
            else:
                current.extend(src_list)
            logger.info("%s: appended %d items to %s (from %s)",
                        tag, len(src_list), attr, file or src_key)
        except AttributeError:
            pass


def extract_documents(
    deal: DealData,
    om_pdf_path: Optional[str] = None,
    rent_roll_pdf_path: Optional[str] = None,
    financials_pdf_path: Optional[str] = None,
    construction_pdf_path: Optional[str] = None,
    uploaded_files: Optional[List[Any]] = None,  # type: ignore[name-defined]
) -> DealData:
    """
    Extract structured data from every uploaded PDF.

    Pipeline:
        1. Collect all uploaded files (regardless of user-applied type label).
        2. Convert each to markdown once.
        3. Classify each file via a fast Haiku call (detects every doc type
           the file contains — a single OM may bundle OM + rent roll + T-12).
        4. For each extractor (1A OM, 1B rent roll, 1C T-12, 1D environmental),
           sort files so the best-classified source runs FIRST. Subsequent
           files fill gaps only (first-populated-wins at the field level).
        5. Apply results with verbose logging + validation gates so silent
           failures become visible.
    """
    # ── Collect every non-None path into `all_files`, deduped ─────────
    all_files: list = []

    def _add(path_like):
        if path_like is None:
            return
        p = None
        if isinstance(path_like, str):
            p = path_like
        elif isinstance(path_like, (tuple, list)) and path_like:
            for item in path_like:
                if isinstance(item, str):
                    p = item
                    break
        elif isinstance(path_like, dict):
            p = path_like.get("path") or path_like.get("file_path") or path_like.get("filepath")
        else:
            p = getattr(path_like, "path", None) or getattr(path_like, "file_path", None)
        if p and p not in all_files:
            all_files.append(p)

    for p in (om_pdf_path, rent_roll_pdf_path, financials_pdf_path, construction_pdf_path):
        _add(p)
    if uploaded_files:
        try:
            for f in uploaded_files:
                _add(f)
        except TypeError:
            pass

    logger.info("EXTRACTOR: %d file(s) to process", len(all_files))
    ext = deal.extracted_docs

    # ── Pass 1: convert + classify every file ─────────────────────────
    # files is a list of dicts: {path, md, classification}
    files = []
    for path in all_files:
        try:
            md = pdf_to_markdown(path)
        except Exception as exc:
            logger.warning("EXTRACTOR: pdf_to_markdown failed for '%s': %s", path, exc)
            continue
        if not md or len(md) < 50:
            logger.warning("EXTRACTOR: skipping '%s' — only %d chars of text",
                           path, len(md) if md else 0)
            continue
        logger.info("EXTRACTOR: extracted %d chars from '%s'", len(md), path)
        classification = _classify_document(md)
        ext.document_classifications.append({
            "path": path,
            "doc_types_present": classification.get("doc_types_present", []),
            "primary_type": classification.get("primary_type"),
            "confidence": classification.get("confidence"),
            "notes": classification.get("notes"),
        })
        files.append({"path": path, "md": md, "classification": classification})

    if not files:
        logger.warning("EXTRACTOR: no readable files — aborting extraction")
        deal.provenance.extractor_model = MODEL_HAIKU
        return deal

    # Rank helper — larger score = process first for this extractor
    def _rank_for(prompt_tag: str, f: dict) -> int:
        types = set(f["classification"].get("doc_types_present") or [])
        primary = f["classification"].get("primary_type")
        # Map: extractor → tuple(strong_types, weak_types)
        match = {
            "1A": ({"om"},             {"appraisal", "rent_roll"}),
            "1B": ({"rent_roll"},      {"om", "appraisal"}),
            "1C": ({"t12"},            {"budget", "om", "appraisal"}),
            "1D": ({"environmental"},  set()),
            "1E": ({"lease"},          {"rent_roll"}),
            "1F": ({"title"},          set()),
            "1G": ({"pca"},            set()),
        }.get(prompt_tag, (set(), set()))
        strong, weak = match
        if primary in strong or (types & strong):
            return 100
        if types & weak:
            return 50
        return 1

    # Address-match gate — refuse to apply 1A/1B/1C/1E from files whose
    # OM describes a DIFFERENT property than the deal the user entered.
    # Protects against stale uploads persisting in the frontend queue
    # across sessions: a previous deal's OM shouldn't overwrite the new
    # deal's rent roll, leases, or financials.
    def _address_matches_deal(result: dict) -> bool:
        deal_street = (deal.address.full_address or deal.address.street or "")
        deal_street_lc = deal_street.lower()
        if not deal_street_lc:
            return True   # no deal address to check against — accept
        m = re.match(r"^\s*(\d+)", deal_street_lc)
        deal_num = m.group(1) if m else None
        skip = {"s", "n", "e", "w", "ne", "nw", "se", "sw", "s.", "n.", "e.", "w.",
                "st", "ave", "avenue", "road", "rd", "street", "blvd", "lane", "ln",
                "drive", "dr", "way", "place", "pl", "court", "ct",
                "philadelphia", "the", "of", "and", "apartments", "llc", "inc"}
        deal_tokens = [t for t in re.findall(r"[a-z0-9]+", deal_street_lc)
                       if t not in skip and len(t) >= 3]
        om_addr = (result.get("full_address") or "").lower()
        om_name = (result.get("property_name") or "").lower()
        om_city = (result.get("city") or "").lower()
        om_combined = " ".join([om_addr, om_name, om_city]).strip()
        if not om_combined:
            return True   # OM didn't return an address — can't disprove
        if deal_num and re.search(rf"\b{deal_num}\b", om_combined):
            return True
        for t in deal_tokens:
            if t in om_combined:
                return True
        return False

    # ── Pass 2: run each extractor in trust order ─────────────────────
    # 1A — OM (always run, starts with OM-classified files)
    for f in sorted(files, key=lambda x: _rank_for("1A", x), reverse=True):
        try:
            user_msg = USER_1A.format(om_text=f["md"], images_json="[]")
            result = _call_haiku(SYSTEM_1A, user_msg)
            if not result:
                logger.warning("EXTRACTOR [1A]: no result for '%s'", f["path"])
                continue
            # Address-match gate: if this OM is about a different property,
            # mark the file foreign and skip all downstream apply steps.
            if not _address_matches_deal(result):
                f["foreign"] = True
                logger.warning(
                    "EXTRACTOR [1A]: SKIPPING foreign OM '%s' — extracted "
                    "property='%s' address='%s' does not match deal address '%s'. "
                    "Upload queue likely contained a stale file from a prior session.",
                    f["path"],
                    (result.get("property_name") or "")[:40],
                    (result.get("full_address") or "")[:60],
                    deal.address.full_address or deal.address.street,
                )
                continue
            _apply_1a(result, deal)
            _merge_extraction(ext, result, source="1A", file=f["path"])
            logger.info("EXTRACTOR [1A]: complete for '%s'", f["path"])
        except Exception as exc:
            logger.warning("EXTRACTOR [1A]: failed for '%s': %s", f["path"], exc)

    # 1B — Rent Roll. Foreign files (OM about a different property) are
    # skipped entirely so a stale upload can't contaminate the rent roll.
    for f in sorted(files, key=lambda x: _rank_for("1B", x), reverse=True):
        if f.get("foreign"):
            logger.info("EXTRACTOR [1B]: skipping foreign file '%s'", f["path"])
            continue
        try:
            user_msg = USER_1B.format(rent_roll_text=f["md"])
            result = _call_haiku(SYSTEM_1B, user_msg)
            if not result:
                logger.warning("EXTRACTOR [1B]: no result for '%s'", f["path"])
                continue
            _apply_1b(result, deal)
            _merge_extraction(ext, result, source="1B", file=f["path"])
            logger.info("EXTRACTOR [1B]: complete for '%s' (%d raw units)",
                        f["path"], len(result.get("units") or []))
        except Exception as exc:
            logger.warning("EXTRACTOR [1B]: failed for '%s': %s", f["path"], exc)

    # 1C — T-12 (foreign-skip)
    for f in sorted(files, key=lambda x: _rank_for("1C", x), reverse=True):
        if f.get("foreign"):
            logger.info("EXTRACTOR [1C]: skipping foreign file '%s'", f["path"])
            continue
        try:
            user_msg = USER_1C.format(financial_statement_text=f["md"])
            result = _call_haiku(SYSTEM_1C, user_msg)
            if not result:
                logger.warning("EXTRACTOR [1C]: no result for '%s'", f["path"])
                continue
            _apply_1c(result, deal)
            _merge_extraction(ext, result, source="1C", file=f["path"])
            logger.info("EXTRACTOR [1C]: complete for '%s'", f["path"])
        except Exception as exc:
            logger.warning("EXTRACTOR [1C]: failed for '%s': %s", f["path"], exc)

    def _files_tagged(types_set: set) -> list:
        return [f for f in files
                if not f.get("foreign")   # drop OMs that mismatch the deal
                and (set(f["classification"].get("doc_types_present") or []) & types_set
                     or f["classification"].get("primary_type") in types_set)]

    # 1D — Environmental (gated on classification)
    env_candidates = _files_tagged({"environmental"})
    if not env_candidates:
        logger.info("EXTRACTOR [1D]: no environmental-classified files — skipping")
    else:
        for f in env_candidates:
            try:
                user_msg = USER_1D.format(env_text=f["md"])
                result = _call_haiku(SYSTEM_1D, user_msg)
                if not result:
                    logger.warning("EXTRACTOR [1D]: no result for '%s'", f["path"])
                    continue
                _apply_1d(result, deal)
                _merge_extraction(ext, result, source="1D", file=f["path"])
                logger.info("EXTRACTOR [1D]: complete for '%s'", f["path"])
            except Exception as exc:
                logger.warning("EXTRACTOR [1D]: failed for '%s': %s", f["path"], exc)

    # 1E — Lease abstraction (gated on lease classification; also runs on
    # rent-roll files since lease terms sometimes appear there as footnotes)
    lease_candidates = _files_tagged({"lease", "rent_roll"})
    if not lease_candidates:
        logger.info("EXTRACTOR [1E]: no lease-classified files — skipping")
    else:
        for f in lease_candidates:
            try:
                user_msg = USER_1E.format(lease_text=f["md"])
                result = _call_haiku(SYSTEM_1E, user_msg)
                if not result:
                    logger.warning("EXTRACTOR [1E]: no result for '%s'", f["path"])
                    continue
                _apply_1e(result, deal)
                _merge_extraction(ext, result, source="1E", file=f["path"])
                logger.info("EXTRACTOR [1E]: complete for '%s'", f["path"])
            except Exception as exc:
                logger.warning("EXTRACTOR [1E]: failed for '%s': %s", f["path"], exc)

    # 1F — Title commitment (gated on title classification)
    title_candidates = _files_tagged({"title"})
    if not title_candidates:
        logger.info("EXTRACTOR [1F]: no title-classified files — skipping")
    else:
        for f in title_candidates:
            try:
                user_msg = USER_1F.format(title_text=f["md"])
                result = _call_haiku(SYSTEM_1F, user_msg)
                if not result:
                    logger.warning("EXTRACTOR [1F]: no result for '%s'", f["path"])
                    continue
                _apply_1f(result, deal)
                _merge_extraction(ext, result, source="1F", file=f["path"])
                logger.info("EXTRACTOR [1F]: complete for '%s'", f["path"])
            except Exception as exc:
                logger.warning("EXTRACTOR [1F]: failed for '%s': %s", f["path"], exc)

    # 1G — PCA / engineering report (gated on PCA classification)
    pca_candidates = _files_tagged({"pca"})
    if not pca_candidates:
        logger.info("EXTRACTOR [1G]: no PCA-classified files — skipping")
    else:
        for f in pca_candidates:
            try:
                user_msg = USER_1G.format(pca_text=f["md"])
                result = _call_haiku(SYSTEM_1G, user_msg)
                if not result:
                    logger.warning("EXTRACTOR [1G]: no result for '%s'", f["path"])
                    continue
                _apply_1g(result, deal)
                _merge_extraction(ext, result, source="1G", file=f["path"])
                logger.info("EXTRACTOR [1G]: complete for '%s'", f["path"])
            except Exception as exc:
                logger.warning("EXTRACTOR [1G]: failed for '%s': %s", f["path"], exc)

    # ── Post-extraction summary log ───────────────────────────────────
    logger.info(
        "EXTRACTOR SUMMARY: files=%d units=%d t12_noi=%s phase1=%s RECs=%d "
        "leases=%d title_exceptions=%d pca_systems=%d immediate_repairs=%d",
        len(files),
        len(ext.unit_mix or []),
        f"${ext.noi_t12:,.0f}" if ext.noi_t12 else "—",
        ext.phase1_status or "—",
        len(ext.recognized_environmental_conditions or []),
        len(ext.lease_abstracts or []),
        len(ext.title_exceptions or []),
        len(ext.pca_building_systems or []),
        len(ext.pca_immediate_repairs or []),
    )

    deal.provenance.extractor_model = MODEL_HAIKU
    return deal


# Run the schema check at import so any future drift is caught immediately.
_verify_scalar_map_schema()
