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
from typing import Any, List, Optional

import anthropic
import pymupdf4llm

from config import ANTHROPIC_API_KEY, MODEL_HAIKU
from models.models import CompsData, CommercialComp, DealData, ExtractedDocumentData, RentComp, SaleComp

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

def _call_haiku(system: str, user_msg: str) -> Optional[dict]:
    """Send a single Haiku extraction call. Returns parsed JSON or None."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=MODEL_HAIKU,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(raw)
    except (json.JSONDecodeError, anthropic.APIError, IndexError, KeyError) as exc:
        logger.warning("Haiku extraction call failed: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT 1A — Offering Memorandum
# ═══════════════════════════════════════════════════════════════════════════

def _apply_1a(data: dict, deal: DealData) -> None:
    """Map Prompt 1A response fields onto DealData."""
    ext = deal.extracted_docs

    ext.property_name         = data.get("property_name")
    ext.asking_price          = data.get("asking_price")
    ext.deal_source           = data.get("deal_source")
    ext.broker_name           = data.get("broker_name")
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
    ext.description_extracted = data.get("property_description")
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
    if not addr.full_address and data.get("full_address"):
        addr.full_address = data["full_address"]
    if not addr.city and data.get("city"):
        addr.city = data["city"]
    if not addr.state and data.get("state"):
        addr.state = data["state"]
    if not addr.zip_code and data.get("zip_code"):
        addr.zip_code = data["zip_code"]


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT 1B — Rent Roll
# ═══════════════════════════════════════════════════════════════════════════

def _apply_1b(data: dict, deal: DealData) -> None:
    """Map Prompt 1B response fields onto DealData."""
    ext = deal.extracted_docs

    ext.unit_mix             = data.get("units")
    ext.total_units_from_rr  = data.get("total_units")
    ext.total_monthly_rent   = data.get("total_monthly_rent_in_place")
    ext.avg_rent_per_unit    = data.get("avg_rent_per_unit")
    ext.occupancy_rate       = data.get("occupancy_rate")


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT 1C — Financial Statements / T-12
# ═══════════════════════════════════════════════════════════════════════════

def _apply_1c(data: dict, deal: DealData) -> None:
    """Map Prompt 1C response fields onto DealData."""
    ext = deal.extracted_docs

    ext.gross_potential_rent_t12   = data.get("gross_potential_rent")
    ext.effective_gross_income_t12 = data.get("effective_gross_income")
    ext.total_expenses_t12         = data.get("total_operating_expenses")
    ext.noi_t12                    = data.get("noi")

    # Flatten operating_expenses dict → {key: net_to_owner or gross_amount}
    raw_expenses = data.get("operating_expenses") or {}
    flat: dict[str, float] = {}
    for key, val in raw_expenses.items():
        if isinstance(val, dict):
            flat[key] = val.get("net_to_owner") or val.get("gross_amount")
        elif isinstance(val, (int, float)):
            flat[key] = val
    ext.expense_line_items = flat if flat else None

    cam = data.get("cam_reimbursements") or {}
    ext.cam_reimbursements_t12 = cam.get("net_to_owner")

    # Full NNN reconciliation dict (if present)
    if cam.get("breakdown"):
        ext.nnn_reconciliation = cam


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

# Scalar merge mapping: source JSON key → ExtractedDocumentData attribute
_SCALAR_MAP = [
    ("property_name",                "property_name"),
    ("asking_price",                 "asking_price"),
    ("year_built",                   "year_built"),
    ("num_units",                    "num_units_extracted"),
    ("total_units",                  "num_units_extracted"),
    ("gross_building_area",          "gross_building_area"),
    ("total_sf",                     "gba_sf_extracted"),
    ("lot_size_sf",                  "lot_size_sf"),
    ("lot_sf",                       "lot_sf_extracted"),
    ("property_type",                "property_type_extracted"),
    ("zoning_code",                  "zoning_code"),
    ("occupancy_rate",               "occupancy_rate"),
    ("total_monthly_rent_in_place",  "total_monthly_rent"),
    ("avg_rent_per_unit",            "avg_rent_per_unit"),
    ("avg_rent_per_sf",              "avg_rent_per_sf"),
    ("gross_potential_rent",         "gross_potential_rent_extracted"),
    ("noi",                          "noi_extracted"),
    ("effective_gross_income",       "egi_extracted"),
    ("total_expenses",               "total_expenses_extracted"),
]

# List merge mapping: source JSON key → ExtractedDocumentData attribute (append)
_LIST_MAP = [
    ("units",              "unit_mix"),
    ("unit_mix_summary",   "unit_mix_summary"),
    ("notable_tenants",    "notable_tenants"),
    ("deal_highlights",    "deal_highlights"),
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
    Run document extraction prompts against every uploaded PDF.

    Every file — regardless of its original type label — is converted to text
    and run through ALL three extraction prompts (1A, 1B, 1C). Results are
    merged with scalar=first-populated-wins, list=append semantics.
    """
    # ── Collect every non-None path into `all_files`, deduped ─────────
    all_files: list = []

    def _add(path_like):
        if path_like is None:
            return
        # Accept raw paths and also tuples/dicts/objects with a path attr
        p = None
        if isinstance(path_like, str):
            p = path_like
        elif isinstance(path_like, (tuple, list)) and path_like:
            # allow (label, path) form
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
    prompts = [
        ("1A", SYSTEM_1A, USER_1A, {"images_json": "[]"}, "om_text"),
        ("1B", SYSTEM_1B, USER_1B, {},                     "rent_roll_text"),
        ("1C", SYSTEM_1C, USER_1C, {},                     "financial_statement_text"),
    ]

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

        for tag, system, user_tpl, extra_kwargs, text_kw in prompts:
            try:
                user_msg = user_tpl.format(**{text_kw: md, **extra_kwargs})
                result = _call_haiku(system, user_msg)
                if not result:
                    logger.warning("EXTRACTOR: %s returned no result for '%s'", tag, path)
                    continue
                if tag == "1A":
                    # Preserve legacy OM-specific behavior (comps, address, etc.)
                    try:
                        _apply_1a(result, deal)
                    except Exception as exc:
                        logger.warning("EXTRACTOR: _apply_1a failed for '%s': %s", path, exc)
                    logger.info("EXTRACTOR: 1A complete for '%s'", path)
                elif tag == "1B":
                    n_units = len(result.get("units") or [])
                    logger.info("EXTRACTOR: 1B complete for '%s' — %d units found",
                                path, n_units)
                else:
                    # 1C: also run legacy mapper so expense_line_items/NNN get set
                    try:
                        _apply_1c(result, deal)
                    except Exception as exc:
                        logger.warning("EXTRACTOR: _apply_1c failed for '%s': %s", path, exc)
                    logger.info("EXTRACTOR: 1C complete for '%s'", path)

                _merge_extraction(ext, result, source=tag, file=path)
            except Exception as exc:
                logger.warning("EXTRACTOR: %s failed for '%s': %s", tag, path, exc)

    # Record extraction model in provenance
    deal.provenance.extractor_model = MODEL_HAIKU

    return deal
