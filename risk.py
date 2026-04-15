"""
risk.py — Insurance & Risk Analysis Module
===========================================
Runs Prompt 4B (Insurance Coverage Analysis) against DealData and writes
exactly 6 fields to DealData.insurance:

    insurance_narrative_p1        — §16.3 paragraph 1
    insurance_narrative_p2        — §16.3 paragraph 2
    insurance_narrative_p3        — §16.3 paragraph 3
    insurance_kpi_strip           — §16.3 KPI bar (6 metrics)
    insurance_summary_table       — §16.3 coverage table (7 rows)
    insurance_proforma_line_item  — Year 1 total insurance cost → financials.py

Pipeline position: runs after market.py (Stage 5), before financials.py (Stage 6).
On any failure: log warning, all 6 fields stay None, pipeline continues.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import anthropic
import os

from config import MODEL_SONNET


def _get_anthropic_api_key():
    return os.environ.get("ANTHROPIC_API_KEY", "") or None
from models.models import DealData

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT 4B — INSURANCE COVERAGE ANALYSIS (Sonnet)
# ═══════════════════════════════════════════════════════════════════════════

_SYSTEM_4B = (
    "You are a commercial real estate insurance specialist and risk analyst writing an\n"
    "insurance coverage analysis for a formal investment underwriting report.\n"
    "Your audience is an investment committee — be precise, factual, and actionable.\n\n"
    "RULES:\n"
    "- Base all conclusions on the data provided. Do not invent coverage details.\n"
    "- If a coverage area is not applicable (e.g., builder's risk for stabilized hold),\n"
    "  state clearly that it is not required.\n"
    "- Flood insurance: FEMA zone starting with A or V = REQUIRED. Zone X = not federally\n"
    "  required (state this explicitly).\n"
    "- Cost benchmarks: use industry-standard ranges — do not present a single number as certain.\n"
    "- Insurance flags go in insurance_summary_table flag field — NOT in narratives.\n"
    "- Tone: Professional, precise, non-alarmist. Flag real risks; do not manufacture them.\n\n"
    "OUTPUT — return JSON with exactly these 6 keys:\n\n"
    "insurance_narrative_p1: Overall insurance profile, property insurance, and general\n"
    "  liability. 100–140 words.\n\n"
    "insurance_narrative_p2: Flood, environmental, and climate risk. Reference FEMA zone\n"
    "  explicitly. 100–140 words.\n\n"
    "insurance_narrative_p3: Builder's risk (or note N/A), loss of rents, umbrella/excess,\n"
    "  and cost outlook. 100–140 words.\n\n"
    "insurance_kpi_strip: Object with 6 key metrics for the §16.3 KPI bar.\n\n"
    "insurance_summary_table: Array of one row per coverage type.\n\n"
    "insurance_proforma_line_item: Single float — estimated Year 1 total annual insurance\n"
    "  cost (property + liability + flood if required + umbrella). This value feeds directly\n"
    "  into the financial model. If insufficient data to estimate, return null.\n\n"
    "Return ONLY valid JSON. No markdown, no preamble."
)

_USER_4B = (
    "Analyze insurance requirements for the subject property.\n\n"
    "Property: {property_address}\n"
    "Asset type: {asset_type}\n"
    "Investment strategy: {investment_strategy}\n"
    "Building SF: {building_sf}\n"
    "Year built: {year_built}\n"
    "Purchase price: ${purchase_price}\n"
    "Total project cost: ${total_project_cost}\n"
    "Number of units: {num_units}\n\n"
    "Environmental & Climate Data:\n"
    "  FEMA flood zone: {fema_flood_zone}\n"
    "  FEMA panel: {fema_panel_number}\n"
    "  EPA flags: {epa_env_flags}\n"
    "  First Street — flood: {first_street_flood} | fire: {first_street_fire}\n"
    "  First Street — heat: {first_street_heat}  | wind: {first_street_wind}\n"
    "  Phase I/II summary: {phase_esa_summary}\n\n"
    "Construction (null if not applicable):\n"
    "  Construction period: {const_period_months} months\n"
    "  Construction budget: ${total_project_cost}\n\n"
    "Current insurance on file: {current_insurance_info}\n\n"
    "Return JSON with exactly these 6 keys:\n"
    '{{\n'
    '  "insurance_narrative_p1": null,\n'
    '  "insurance_narrative_p2": null,\n'
    '  "insurance_narrative_p3": null,\n'
    '  "insurance_kpi_strip": {{\n'
    '    "flood_zone": null,\n'
    '    "flood_insurance_required": null,\n'
    '    "est_property_insurance_annual": null,\n'
    '    "est_flood_insurance_annual": null,\n'
    '    "est_total_insurance_annual": null,\n'
    '    "coverage_gaps_flagged": null\n'
    '  }},\n'
    '  "insurance_summary_table": [\n'
    '    {{\n'
    '      "coverage_type": null,\n'
    '      "required": null,\n'
    '      "est_annual_cost": null,\n'
    '      "notes": null,\n'
    '      "flag": null\n'
    '    }}\n'
    '  ],\n'
    '  "insurance_proforma_line_item": null\n'
    '}}'
)


# ═══════════════════════════════════════════════════════════════════════════
# LLM CALL
# ═══════════════════════════════════════════════════════════════════════════

def _call_sonnet(system: str, user_msg: str) -> Optional[dict]:
    """Send a single Sonnet call and parse the JSON response. Returns None on failure."""
    client = anthropic.Anthropic(api_key=_get_anthropic_api_key())
    try:
        response = client.messages.create(
            model=MODEL_SONNET,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(raw)
    except (json.JSONDecodeError, anthropic.APIError, IndexError, KeyError) as exc:
        logger.warning("Prompt 4B Sonnet call failed: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# RESULT APPLICATION
# ═══════════════════════════════════════════════════════════════════════════

def _apply_4b(data: dict, deal: DealData) -> None:
    """Map Prompt 4B response onto DealData.insurance — exactly 6 fields."""
    ins = deal.insurance
    ins.insurance_narrative_p1       = data.get("insurance_narrative_p1")
    ins.insurance_narrative_p2       = data.get("insurance_narrative_p2")
    ins.insurance_narrative_p3       = data.get("insurance_narrative_p3")
    ins.insurance_kpi_strip          = data.get("insurance_kpi_strip")
    ins.insurance_summary_table      = data.get("insurance_summary_table")
    ins.insurance_proforma_line_item = _safe_float(data.get("insurance_proforma_line_item"))


def _safe_float(val) -> Optional[float]:
    """Convert to float, returning None on failure."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ═══════════════════════════════════════════════════════════════════════════
# INPUT FORMATTING
# ═══════════════════════════════════════════════════════════════════════════

def _fmt(val, fallback: str = "Not available") -> str:
    """Format a value for prompt injection — returns fallback if None/empty."""
    if val is None or val == "" or val == []:
        return fallback
    if isinstance(val, list):
        return ", ".join(str(v) for v in val) if val else fallback
    return str(val)


def _total_project_cost(deal: DealData) -> float:
    """Estimate total project cost for the insurance prompt.

    If financials.py has already run, use fo.total_uses (authoritative).
    Otherwise compute from assumptions — same line items as the Excel S&U tab.
    Renovations are below-the-line, not in S&U, so excluded here.
    """
    fo = deal.financial_outputs
    if fo.total_uses and fo.total_uses > 0:
        return fo.total_uses

    a = deal.assumptions
    transfer_tax = a.purchase_price * a.transfer_tax_rate
    professional = (a.legal_closing + a.title_insurance + a.legal_bank +
                    a.appraisal + a.environmental + a.architect +
                    a.structural + a.geotech + a.surveyor + a.civil_eng +
                    a.meps + a.legal_zoning)
    financing = (a.acq_fee_fixed + a.mortgage_carry + a.mezz_interest)
    origination = a.purchase_price * a.ltv_pct * a.origination_fee_pct
    soft = (a.working_capital + a.marketing + a.re_tax_carry +
            a.prop_ins_carry + a.dev_fee + a.dev_pref + a.permits)
    hard = (a.stormwater + a.demo + a.const_hard +
            a.const_reserve + a.gc_overhead)
    return (a.purchase_price + transfer_tax +
            a.tenant_buyout + professional + financing + origination +
            soft + hard)


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

def analyze_insurance(deal: DealData) -> DealData:
    """
    Run Prompt 4B — Insurance Coverage Analysis.

    Reads FEMA flood zone, EPA flags, and climate risk data from
    DealData.market_data. Writes exactly 6 fields to DealData.insurance.

    On any failure: logs warning, all insurance fields stay None,
    pipeline continues. financials.py falls back to assumptions.insurance.

    Args:
        deal: DealData with market_data already populated by market.py.

    Returns:
        The same DealData object with insurance analysis populated.
    """
    logger.info("Running Prompt 4B — Insurance Coverage Analysis...")

    md = deal.market_data
    a = deal.assumptions
    total_cost = _total_project_cost(deal)

    # Determine construction applicability
    const_months = a.const_period_months if a.const_period_months > 0 else None

    user_msg = _USER_4B.format(
        property_address=deal.address.full_address,
        asset_type=deal.asset_type.value,
        investment_strategy=deal.investment_strategy.value,
        building_sf=_fmt(a.gba_sf, "unknown"),
        year_built=_fmt(a.year_built, "unknown"),
        purchase_price=f"{a.purchase_price:,.0f}",
        total_project_cost=f"{total_cost:,.0f}",
        num_units=_fmt(a.num_units, "unknown"),
        fema_flood_zone=_fmt(md.fema_flood_zone, "Not determined"),
        fema_panel_number=_fmt(md.fema_panel_number, "Not determined"),
        epa_env_flags=_fmt(md.epa_env_flags, "None identified"),
        first_street_flood=_fmt(md.first_street_flood, "N/A"),
        first_street_fire=_fmt(md.first_street_fire, "N/A"),
        first_street_heat=_fmt(md.first_street_heat, "N/A"),
        first_street_wind=_fmt(md.first_street_wind, "N/A"),
        phase_esa_summary="No Phase I/II ESA on file",  # TODO: pull from DealData.extracted_docs when Phase I/II upload is added to frontend
        const_period_months=_fmt(const_months, "null"),
        current_insurance_info="Not available",  # TODO: pull from DealData.extracted_docs when Phase I/II upload is added to frontend
    )

    result = _call_sonnet(_SYSTEM_4B, user_msg)

    if result:
        _apply_4b(result, deal)
        logger.info(
            "Prompt 4B complete — insurance proforma: %s",
            deal.insurance.insurance_proforma_line_item,
        )
    else:
        logger.warning("Prompt 4B failed — all insurance fields remain None; "
                       "financials.py will use assumptions.insurance default")

    return deal
