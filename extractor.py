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
from typing import Optional

import anthropic
import pymupdf4llm
import streamlit as st

from config import ANTHROPIC_SECRET_KEY, MODEL_HAIKU
from models.models import DealData, ExtractedDocumentData

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
    client = anthropic.Anthropic(
        api_key=st.secrets[ANTHROPIC_SECRET_KEY]["api_key"],
    )
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
    ext.lot_sf_extracted      = data.get("lot_sf")
    ext.year_built_extracted  = data.get("year_built")
    ext.description_extracted = data.get("property_description")
    ext.image_placements      = {"images": data.get("images", [])}

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

def extract_documents(
    deal: DealData,
    om_pdf_path: Optional[str] = None,
    rent_roll_pdf_path: Optional[str] = None,
    financials_pdf_path: Optional[str] = None,
) -> DealData:
    """
    Run document extraction prompts against uploaded PDFs.

    Each prompt runs independently — a failure in one does not block the others.
    On any parse failure, that section stays at ExtractedDocumentData defaults.
    """
    # ── Prompt 1A — Offering Memorandum ───────────────────────
    if om_pdf_path:
        om_md = pdf_to_markdown(om_pdf_path)
        user_msg = USER_1A.format(om_text=om_md, images_json="[]")
        result = _call_haiku(SYSTEM_1A, user_msg)
        if result:
            _apply_1a(result, deal)
            logger.info("Prompt 1A complete — OM extracted")
        else:
            logger.warning("Prompt 1A failed — continuing with defaults")

    # ── Prompt 1B — Rent Roll ─────────────────────────────────
    if rent_roll_pdf_path:
        rr_md = pdf_to_markdown(rent_roll_pdf_path)
        user_msg = USER_1B.format(rent_roll_text=rr_md)
        result = _call_haiku(SYSTEM_1B, user_msg)
        if result:
            _apply_1b(result, deal)
            logger.info("Prompt 1B complete — Rent roll extracted")
        else:
            logger.warning("Prompt 1B failed — continuing with defaults")

    # ── Prompt 1C — Financial Statements ──────────────────────
    if financials_pdf_path:
        fin_md = pdf_to_markdown(financials_pdf_path)
        user_msg = USER_1C.format(financial_statement_text=fin_md)
        result = _call_haiku(SYSTEM_1C, user_msg)
        if result:
            _apply_1c(result, deal)
            logger.info("Prompt 1C complete — Financials extracted")
        else:
            logger.warning("Prompt 1C failed — continuing with defaults")

    # Record extraction model in provenance
    deal.provenance.extractor_model = MODEL_HAIKU

    return deal
