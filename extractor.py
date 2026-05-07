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
from pathlib import Path
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
    "Output ONLY valid JSON. No markdown, no preamble.\n\n"
    "FIELD LOCATION GUIDANCE (where to find key fields in a typical OM):\n"
    "  asking_price    — 'Offering Summary', 'Investment Highlights', or page 1 headline\n"
    "  year_built      — 'Property Overview' sidebar, 'Building Details' table\n"
    "  zoning_code     — 'Zoning' row in property details table, or municipal summary section\n"
    "  num_units       — 'Property Overview', 'Offering Summary', or rent roll header\n"
    "  full_address    — Always extract verbatim, including range formats\n"
    "                    (e.g. '2-8 S. 46th St.', '100-104 Main Ave').\n"
    "                    Set address_is_range: true if the street number contains\n"
    "                    a hyphen or slash (e.g. '2-8', '100/102').\n\n"
    "ZONING EXTRACTION:\n"
    "  - Extract the zoning designation exactly as written (e.g. 'RM-1', 'CMX-2', 'R-10A').\n"
    "  - If overlay districts are mentioned, extract as an array: e.g. ['RM-1', 'AHO'].\n"
    "  - If a zoning description paragraph exists, extract the first 200 characters\n"
    "    into zoning_description.\n"
    "  - Set address_is_range: true if the street number is a range (contains hyphen).\n\n"
    "DATA PROVENANCE:\n"
    "  For the five most important fields (asking_price, year_built, num_units,\n"
    "  zoning_code, full_address), add a sibling _source field indicating where\n"
    "  in the document the value was found, e.g.:\n"
    "    'asking_price_source': 'Offering Summary table, page 1'\n"
    "    'year_built_source': 'Property Overview sidebar'\n"
    "  Use null for _source if the field itself is null.\n\n"
    "WORKED EXTRACTION EXAMPLE:\n"
    "Document excerpt:\n"
    "  '2-8 S. 46th Street, Philadelphia, PA 19139 | 13-Unit Multifamily\n"
    "   Asking Price: $1,600,000 | Year Built: 1925 | Zoning: RM-1\n"
    "   Gross Building Area: 11,250 SF | Lot Size: 4,280 SF'\n\n"
    "Correct JSON (excerpt):\n"
    "  {\n"
    "    \"full_address\": \"2-8 S. 46th Street, Philadelphia, PA 19139\",\n"
    "    \"address_is_range\": true,\n"
    "    \"asking_price\": 1600000,\n"
    "    \"asking_price_source\": \"header line, page 1\",\n"
    "    \"year_built\": 1925,\n"
    "    \"year_built_source\": \"property details table\",\n"
    "    \"zoning_code\": \"RM-1\",\n"
    "    \"zoning_overlays\": [],\n"
    "    \"total_sf\": 11250,\n"
    "    \"lot_sf\": 4280,\n"
    "    \"total_units\": 13\n"
    "  }\n\n"
    "VERIFICATION BEFORE RETURNING:\n"
    "  - All numeric fields are numbers, not formatted strings\n"
    "  - address_is_range is present and is a boolean\n"
    "  - No invented values — every non-null field came from the document\n"
    "  - _source fields present for the 5 key fields listed above"
)

SYSTEM_1B = (
    "You are a commercial real estate analyst specializing in rent roll analysis.\n"
    "Extract every unit-level row from the rent roll. Return structured JSON.\n\n"
    "RULES:\n"
    "- Extract ONLY data explicitly present. Return null for missing fields.\n"
    "- The source may be a standalone rent roll OR a rent-roll table embedded\n"
    "  inside a longer offering memorandum. Search the ENTIRE text for any\n"
    "  tabular or list structure that enumerates units or tenants (look for\n"
    "  column headers like 'Unit', 'Unit #', 'Apt', 'Suite', 'Tenant',\n"
    "  'Bedrooms', 'SF', 'Monthly Rent', 'Rent/SF'). A bulleted or numbered\n"
    "  per-unit list (e.g. 'Unit 1 — 2BR — $1,800') also counts.\n"
    "UNIT MIX EXPANSION RULE — MANDATORY:\n"
    "  If the document shows a summary table instead of individual unit rows, e.g.:\n"
    "    5 x 2BR/1BA  ->  $1,800/mo\n"
    "    8 x 1BR/1BA  ->  $1,500/mo\n"
    "  You MUST expand this into 13 individual unit records:\n"
    "    Units 1-5:  unit_type='2BR', monthly_rent=1800, status='occupied', synthetic=true\n"
    "    Units 6-13: unit_type='1BR', monthly_rent=1500, status='occupied', synthetic=true\n"
    "  Set synthetic=true on every generated record.\n"
    "  Set total_units to the SUM of all counts in the summary table.\n"
    "  NEVER return a units array shorter than total_units.\n"
    "  NEVER return an empty units array when the document states a unit count with rents.\n\n"
    "MIXED-USE BUILDINGS:\n"
    "  If the building contains both residential units and commercial/retail space,\n"
    "  include the commercial space as a unit record with unit_type='Commercial'.\n"
    "  Commercial units affect GPR and occupancy — do not omit them.\n\n"
    "LEASE DATE NORMALIZATION:\n"
    "  Short-form dates (e.g. '12/26', 'Dec-26') mean month/year.\n"
    "  Normalize to ISO format: '12/26' -> '2026-12-01' (assume 20XX for 2-digit years).\n"
    "  If only a year is given (e.g. 'Exp. 2027'), use '2027-12-31'.\n\n"
    "OCR/SCAN QUALITY:\n"
    "  If the source text shows signs of poor OCR (garbled characters, missing columns,\n"
    "  inconsistent spacing), set extraction_quality='low' in the response and describe\n"
    "  in extraction_notes what was and was not recoverable.\n\n"
    "- Monthly rents as numbers (strip '$' and ','). Dates in ISO format.\n"
    "- Lease status: \"occupied\" | \"vacant\" | \"month-to-month\" | \"notice\" | \"pending\"\n"
    "- Unit type: \"Studio\" | \"1BR\" | \"2BR\" | \"3BR\" | \"4BR+\" | \"Commercial\" | \"Other\"\n"
    "Output ONLY valid JSON.\n\n"
    "VERIFICATION BEFORE RETURNING:\n"
    "  - len(units) == total_units (if summary was expanded, verify the count)\n"
    "  - All monthly_rent values are numbers, not strings\n"
    "  - All dates are ISO format or null\n"
    "  - synthetic=true on any unit generated from a summary table\n"
    "  - No invented tenant names — use null if not in the document"
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
    "MULTI-LABEL RULE:\n"
    "  If the document clearly contains more than one type (e.g. an OM with an\n"
    "  embedded rent roll and T-12), return ALL present types in doc_types_present.\n"
    "  Set primary_type to the dominant document type.\n"
    "  For combo documents, also return primary_pages and secondary_pages as\n"
    "  approximate page ranges (e.g. 'pp. 1-18' for the OM section, 'pp. 19-22'\n"
    "  for the embedded rent roll). Use null if page ranges cannot be determined.\n\n"
    "CONFIDENCE SCORING:\n"
    "  confidence_score: 0.0-1.0 (float, not string)\n"
    "    1.0 = document header/title makes type unambiguous\n"
    "    0.7-0.9 = content clearly matches type but no explicit label\n"
    "    0.5-0.7 = probable match based on content structure\n"
    "    <0.5 = ambiguous — flag for manual review\n"
    "  If confidence_score < 0.7, set needs_manual_review: true.\n\n"
    "Output ONLY valid JSON. No markdown, no preamble.\n\n"
    "VERIFICATION BEFORE RETURNING:\n"
    "  - doc_types_present is a non-empty array using only the slugs listed above\n"
    "  - primary_type is a single slug string\n"
    "  - confidence_score is a float between 0.0 and 1.0\n"
    "  - needs_manual_review is a boolean"
)

USER_CLASSIFY = (
    "Classify the document below. Return a single JSON object.\n\n"
    "DOCUMENT TEXT (first 8000 chars):\n{doc_text}\n\n"
    "Return JSON:\n"
    '{{\n'
    '  "doc_types_present": ["om"],\n'
    '  "primary_type": "om",\n'
    '  "confidence": "high",\n'
    '  "confidence_score": 0.95,\n'
    '  "needs_manual_review": false,\n'
    '  "primary_pages": null,\n'
    '  "secondary_pages": null,\n'
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
    "- phase1_status: \"complete\" | \"draft\" | \"pending\" | \"n/a\".\n\n"
    "WORKED EXAMPLE — distinguishing REC vs HREC vs vapor flag:\n"
    "Document excerpt:\n"
    "  'Section 5.2 — Recognized Environmental Conditions:\n"
    "   The historical use of the property as a dry cleaner from 1962-1989\n"
    "   was documented in the 1995 NFA letter from PADEP (Document #87654).\n"
    "   The site received a No Further Action determination on 1996-04-12.\n"
    "   Adjacent gas station UST at 100 Main (closed 2003) — groundwater\n"
    "   plume direction unknown; Phase II soil-gas sampling recommended.'\n\n"
    "Correct extraction:\n"
    "  - The dry cleaner is an HREC (resolved by NFA letter) — goes in\n"
    "    historical_recognized_conditions, NOT in RECs.\n"
    "  - The adjacent gas station UST is a REC (active groundwater concern,\n"
    "    Phase II recommended) — goes in recognized_environmental_conditions.\n"
    "  - vapor_intrusion_flag = true (soil-gas sampling implies VI concern).\n"
    "  - phase2_recommended = true (consultant explicitly recommends it).\n\n"
    "VERIFICATION BEFORE RETURNING:\n"
    "  - Each REC entry is a short factual sentence, not a paragraph\n"
    "  - HRECs only appear when the report explicitly calls them historical /\n"
    "    closed / NFA / resolved\n"
    "  - vapor_intrusion_flag is a boolean (true/false), not a string\n"
    "  - phase1_status is one of the four enum values\n"
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
    "- One lease object per distinct lease found; do not dedupe unit IDs.\n\n"
    "ESCALATION TYPE DISAMBIGUATION — the most common edge case:\n"
    "  fixed:        rent is flat for the entire term (no escalations).\n"
    "  stepped:      rent steps up by a SPECIFIC SCHEDULE — e.g. '$2,000/mo\n"
    "                Years 1-3, $2,200/mo Years 4-5'. The schedule is in the\n"
    "                document; the steps are not formula-driven.\n"
    "  CPI:          rent escalates by a published CPI index (e.g. 'annual\n"
    "                escalation = CPI-U Northeast urban').\n"
    "  market_reset: rent re-prices to fair market value at a specified date\n"
    "                (e.g. 'rent resets to FMV at end of Year 5').\n"
    "  none:         single-month or other lease with no escalation language.\n\n"
    "  EDGE CASE — '3% annual escalation':\n"
    "    A flat percentage written into the lease (e.g. '3% per annum') is\n"
    "    'fixed' if the percentage is a known constant in the document — set\n"
    "    escalation_type='fixed' and escalation_amount=0.03.\n"
    "    It is NOT 'stepped' (no schedule of dollar steps) and NOT 'CPI'\n"
    "    (not indexed). Reserve 'stepped' for explicit dollar-amount\n"
    "    schedules.\n\n"
    "WORKED EXAMPLE:\n"
    "Document excerpt:\n"
    "  'Tenant: Bright Coffee Co. Suite 101. 1,250 SF. Term: 5 years\n"
    "   commencing 2025-08-01. Base rent: $35.00/SF NNN, escalating 3%\n"
    "   annually on each anniversary. Tenant pays its pro-rata share of\n"
    "   real estate taxes, insurance, and CAM. 2 x 5yr renewal options at\n"
    "   the then-current market rent. Personal guaranty by principal\n"
    "   capped at 12 months base rent.'\n\n"
    "Correct extraction:\n"
    "  unit_id='Suite 101', tenant_name='Bright Coffee Co.',\n"
    "  base_rent_psf=35.00, term_months=60,\n"
    "  commencement_date='2025-08-01', expiration_date='2030-07-31',\n"
    "  escalation_type='fixed', escalation_amount=0.03,\n"
    "  cam_structure='full_NNN',\n"
    "  renewal_options=['2 × 5yr at FMV'],\n"
    "  personal_guaranty='12 months base rent (principal)'\n"
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
    "- Dates ISO (YYYY-MM-DD). Dollar amounts as numbers.\n\n"
    "WORKED EXAMPLE — summarizing an easement in ≤25 words:\n"
    "Document excerpt:\n"
    "  'Item 8. Right-of-way and easement granted to Philadelphia Electric\n"
    "   Company for the installation, operation, maintenance, repair,\n"
    "   replacement, and removal of underground electric distribution\n"
    "   lines, conduits, manholes, and appurtenances thereto, recorded\n"
    "   April 12, 1962 in Deed Book 1234, Page 567 (Document #PH-1962-44321),\n"
    "   crossing the southerly 8 feet of the property.'\n\n"
    "Correct entry:\n"
    "  exception_type='easement',\n"
    "  recording_date='1962-04-12',\n"
    "  document_id='PH-1962-44321 (DB 1234, p. 567)',\n"
    "  grantor='Property Owner',\n"
    "  grantee='Philadelphia Electric Company',\n"
    "  summary='8-ft underground utility easement along southern boundary for electric lines.'\n"
    "  (summary = 14 words; well under the 25-word cap)\n\n"
    "VERIFICATION BEFORE RETURNING:\n"
    "  - Each exception summary is ≤ 25 words AND contains the location/scope\n"
    "  - Dates are ISO format or null\n"
    "  - exception_type is one of the enum values exactly\n"
    "  - title_insurance_amount is a number, not a formatted string\n"
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
    "- If the report states a deferred maintenance total, extract it explicitly.\n\n"
    "CONDITION DISAMBIGUATION — anchor classifications to remaining useful life:\n"
    "  excellent:   New / recently replaced / >75% RUL remaining\n"
    "  good:        Functional, minor wear, 50-75% RUL remaining\n"
    "  fair:        Aging, periodic repair likely, 25-50% RUL remaining\n"
    "  poor:        Material deterioration, replacement within 5 years\n"
    "  end_of_life: Replacement required <2 years OR documented failure\n"
    "  When the report uses softer language ('serviceable', 'adequate'),\n"
    "  map to 'fair'. When it uses 'recently upgraded', map to 'good' unless\n"
    "  the upgrade was full replacement (then 'excellent').\n\n"
    "WORKED EXAMPLE:\n"
    "Document excerpt:\n"
    "  'Roof: Modified bitumen, installed 2008. Estimated 5 years of\n"
    "   remaining useful life. Recommend full replacement within the next\n"
    "   3-5 years; budget $84,000 (Year 2029).\n"
    "   HVAC: Eight rooftop units, mixed ages 2012-2018. Two units showing\n"
    "   compressor wear; recommend replacement of those two units in 2026\n"
    "   ($14,000 each, $28,000 total). Remaining six units serviceable.'\n\n"
    "Correct extraction:\n"
    "  pca_building_systems = [\n"
    "    {system='Roof', age_years=18, condition='fair',\n"
    "     remaining_useful_life=5, replacement_cost=84000,\n"
    "     notes='Modified bitumen, full replacement targeted ~2029.'},\n"
    "    {system='HVAC', age_years=null, condition='fair',\n"
    "     remaining_useful_life=null, replacement_cost=28000,\n"
    "     notes='8 RTUs mixed ages; 2 require 2026 replacement at $14K each.'}\n"
    "  ]\n"
    "  pca_capex_by_year = {'2026': 28000, '2029': 84000}\n"
    "  pca_immediate_repairs = [\n"
    "    {item='HVAC RTU compressor replacement (2 units)',\n"
    "     cost=28000, priority='short_term'}\n"
    "  ]\n"
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
    "Output ONLY valid JSON.\n\n"
    "CANONICAL EXPENSE CATEGORIES — map all label variants to these exact keys:\n"
    "  'real_estate_taxes'   <- Taxes, RE Tax, Property Tax, RE Taxes\n"
    "  'insurance'           <- Insurance, Hazard Insurance, Property Insurance\n"
    "  'utilities'           <- Utilities, Electric, Gas, Water/Sewer, Common Area Util.\n"
    "  'repairs_maintenance' <- R&M, Repairs, Repairs & Maintenance, Maintenance\n"
    "  'management_fee'      <- Mgmt, Management, Property Management Fee\n"
    "  'payroll'             <- Payroll, Labor, Staffing, Resident Manager\n"
    "  'landscaping'         <- Landscaping, Grounds, Exterior Maintenance\n"
    "  'trash_removal'       <- Trash, Trash Removal, Refuse\n"
    "  'professional_fees'   <- Legal, Accounting, Professional Fees\n"
    "  'marketing_leasing'   <- Marketing, Advertising, Leasing Commissions\n"
    "  'reserves'            <- Reserves, Cap Ex Reserve, Replacement Reserve\n"
    "  'other_expenses'      <- Any expense that does not match a category above\n"
    "  When using a canonical key, add a sibling '_label_original' field containing\n"
    "  the exact label text from the source document.\n\n"
    "PARTIAL-YEAR NORMALIZATION:\n"
    "  If the statement covers fewer than 12 months, annualize all figures\n"
    "  (divide by actual months, multiply by 12). Set:\n"
    "    annualized: true\n"
    "    months_covered: <integer, e.g. 9>\n"
    "  State this clearly in extraction_notes.\n\n"
    "IN-PLACE vs. PRO FORMA DISAMBIGUATION:\n"
    "  If the document contains both 'Actual' and 'Pro Forma' columns,\n"
    "  extract ONLY from the 'Actual' column.\n"
    "  Set proforma_column_present: true to flag its existence.\n"
    "  Do NOT extract or blend pro forma figures into the actual values.\n\n"
    "VERIFICATION BEFORE RETURNING:\n"
    "  - All expense keys use the canonical names listed above\n"
    "  - All dollar amounts are numbers, not formatted strings\n"
    "  - noi = effective_gross_income - total_operating_expenses (verify arithmetic)\n"
    "  - If annualized=true, months_covered is present and is an integer 1-11\n"
    "  - No invented expense lines — every line came from the document"
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
    '  "address_is_range": null, "zoning_overlays": [], "zoning_description": null,\n'
    '  "asking_price_source": null, "year_built_source": null,\n'
    '  "num_units_source": null, "zoning_code_source": null, "address_source": null,\n'
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


def extract_pdf_photos(
    pdf_path: str,
    out_dir: Path,
    min_side_px: int = 400,
    max_photos: int = 12,
) -> List[str]:
    """Extract photo-sized raster images from a PDF using PyMuPDF.

    Saves each qualifying image to out_dir as {basename}_p{page}_i{idx}.png
    and returns the list of absolute paths. Skips images smaller than
    min_side_px on the short edge (filters out logos, icons, line art).

    Returns an empty list on any fitz error so pipeline stays non-fatal.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("extract_pdf_photos: PyMuPDF not available — skipping")
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    basename = Path(pdf_path).stem[:40]
    saved: List[str] = []

    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        logger.warning("extract_pdf_photos: fitz.open failed for %s: %s", pdf_path, exc)
        return []

    try:
        for page_idx, page in enumerate(doc):
            if len(saved) >= max_photos:
                break
            for img_idx, img in enumerate(page.get_images(full=True)):
                if len(saved) >= max_photos:
                    break
                xref = img[0]
                try:
                    pix = fitz.Pixmap(doc, xref)
                except Exception:
                    continue
                try:
                    # Drop tiny rasters (logos, icons, rules)
                    if min(pix.width, pix.height) < min_side_px:
                        continue
                    # Convert CMYK / alpha to RGB so PIL + Playwright can render
                    if pix.n - pix.alpha >= 4:
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    out_path = out_dir / f"{basename}_p{page_idx + 1:03d}_i{img_idx:02d}.png"
                    pix.save(str(out_path))
                    saved.append(str(out_path))
                finally:
                    pix = None
    finally:
        doc.close()

    logger.info(
        "extract_pdf_photos: %s → %d photo(s) saved to %s",
        Path(pdf_path).name, len(saved), out_dir,
    )
    return saved


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

            # Extract actual photo rasters from this non-foreign OM so
            # the report's photo gallery can display real property images
            # from the broker package (in addition to Google Street View).
            try:
                from config import OUTPUTS_DIR
                photo_dir = Path(OUTPUTS_DIR) / f"{deal.deal_id}_photos"
                saved = extract_pdf_photos(f["path"], photo_dir)
                if saved:
                    ext.pdf_photo_paths.extend(saved)
            except Exception as _pe:
                logger.warning("EXTRACTOR [1A]: photo extraction failed for '%s': %s",
                               f["path"], _pe)
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
