"""
word_builder.py — Report Generation Module
===========================================
Generates the final PDF underwriting report in three stages:

    1. Prompt 4-MASTER (Sonnet) — batched generation of all narrative sections.
    2. Prompt 5D (Sonnet, investor_mode only) — rewrites 9 narrative blocks
       in LP-appropriate language.
    3. docxtpl template population → LibreOffice headless PDF conversion.

Pipeline position: Stage 7 — runs after financials.py.
Output: {deal_id}_report.pdf saved to outputs/.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
from pathlib import Path
from typing import Optional

import anthropic
from docxtpl import DocxTemplate, InlineImage
from docx.shared import Mm
from docx.oxml.ns import qn

from config import (
    ANTHROPIC_API_KEY,
    MODEL_SONNET,
    OUTPUTS_DIR,
    PDF_CONVERSION_TIMEOUT,
    WORD_TEMPLATE,
)
from map_builder import build_all_maps, MapImages
from chart_builder import build_all_charts, ChartImages
from map_builder import build_all_maps, MapImages
from chart_builder import build_all_charts, ChartImages
from models.models import DealData

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT 4-MASTER — ALL REPORT NARRATIVE SECTIONS (Sonnet)
# ═══════════════════════════════════════════════════════════════════════════

_SYSTEM_4MASTER = (
    "You are a senior commercial real estate analyst and investment writer producing\n"
    "the narrative sections of a formal institutional investment underwriting report.\n\n"
    "You will receive a complete deal data object and generate written narrative\n"
    "for every report section listed.\n\n"
    "GLOBAL WRITING RULES:\n"
    "- Voice: Senior analyst writing for an investment committee. Precise, data-grounded.\n"
    "- Tense: Present for market conditions, past for historical facts, future for projections.\n"
    "- Never use: \"pleased to present,\" \"exciting opportunity,\" \"unique,\" \"best-in-class.\"\n"
    "- Every claim must be grounded in the data provided. No invented facts.\n"
    "- All numbers must match the data exactly — never round or paraphrase figures.\n"
    "- Return ONLY valid JSON. No markdown, no preamble, no commentary.\n\n"
    "SECTION REQUIREMENTS (word counts are targets — +/-15% acceptable):\n\n"
    "exec_overview_p1 (100-130 words): Address, asset type, strategy, asking price, thesis,\n"
    "  physical characteristics, current occupancy/NOI.\n"
    "exec_overview_p2 (80-110 words): Submarket, vacancy trends, rent growth, market conditions.\n"
    "exec_overview_p3 (80-100 words): Hold period, target IRR, exit strategy, risk-return rationale.\n"
    "exec_pullquote (15-25 words): Quotable deal thesis sentence. No hedging.\n"
    "deal_thesis (60-80 words): Why this property, strategy, market, at this time.\n"
    "opportunity_1/2/3 (15-25 words each): Three strongest value creation levers.\n"
    "prop_desc_p1 (80-110 words): Building type, construction, condition, layout, parking.\n"
    "prop_desc_p2 (60-80 words): Unit mix, interior conditions, renovation opportunity.\n"
    "prop_desc_p3 (60-80 words): Tenant profile, occupancy, lease terms.\n"
    "prop_desc_p4 (50-70 words): Utilities and infrastructure systems.\n"
    "utilities_analysis (50-70 words): Systems condition, deferred maintenance.\n"
    "ownership_narrative (80-110 words): Chain of title, entity structure, notable events.\n"
    "liens_narrative (50-70 words): Recorded encumbrances. If none, state clearly.\n"
    "location_pullquote (15-25 words): Location's strongest attribute.\n"
    "location_overview_p1 (90-120 words): Neighborhood, submarket, major employers, transit.\n"
    "location_overview_p2 (80-100 words): Demographics, income, renter composition, trends.\n"
    "transportation_analysis (60-80 words): Transit access, Walk Score, highways, parking.\n"
    "neighborhood_trend_narrative (100-130 words): Population/income trends, neighborhood trajectory.\n"
    "supply_pipeline_narrative (90-120 words): Competing supply within 1 mile, absorption,\n"
    "  impact on rent growth and exit caps. If Shonda at Binswanger was flagged for CoStar\n"
    "  data, acknowledge the data limitation explicitly.\n"
    "rent_roll_intro (50-70 words): Total units, occupancy, rent roll framing.\n"
    "rent_comp_narrative (80-100 words): Rents vs. comp set, upside assessment.\n"
    "commercial_comp_narrative (70-90 words): Commercial comp analysis. Abbreviate if no retail.\n"
    "sale_comp_narrative (80-100 words): Price vs. closed sales per-unit and per-SF.\n"
    "financial_pullquote (15-25 words): Financial thesis pull-quote.\n"
    "sources_uses_narrative (70-90 words): Total project cost, equity, debt, what capital pays for.\n"
    "proforma_narrative (100-130 words): 10-yr revenue trajectory, expense management, NOI growth.\n"
    "proforma_pullquote (15-25 words): Pro forma pull-quote (NOI growth or cash-on-cash).\n"
    "sensitivity_narrative (70-90 words): Sensitivity matrix — what passes/fails threshold.\n"
    "  IMPORTANT: If sensitivity_matrix is empty or all zeros, write exactly:\n"
    "  'Sensitivity analysis requires stabilized revenue data. Matrix will be populated\n"
    "  following lease-up and rent roll stabilization.' Do not report zeros as results.\n"
    "exit_narrative (70-90 words): Exit cap assumption, terminal value, net proceeds.\n"
    "capital_stack_narrative (80-100 words): LTV, debt terms, equity split, structure rationale.\n"
    "capital_structure_pullquote (15-25 words): Capital structure pull-quote.\n"
    "debt_comparison_narrative (60-80 words): Two alternative debt structures considered.\n"
    "waterfall_narrative (70-90 words): Promote structure, pref return, alignment of interests.\n"
    "environmental_intro (60-80 words): Environmental screening overview.\n"
    "phase_esa_narrative (70-90 words): Phase I/II findings. If none on file, state and flag.\n"
    "climate_risk_narrative (70-90 words): First Street scores interpreted for hold period.\n"
    "legal_status_narrative (70-90 words): Encumbrances, easements, legal matters.\n"
    "violations_narrative (50-70 words): Code violations or permit issues. If none, state clearly.\n"
    "regulatory_approvals_narrative (50-70 words): Required approvals, variances, special exceptions.\n"
    "due_diligence_overview (60-80 words): DD flag methodology and flag distribution summary.\n"
    "dd_checklist_intro (40-60 words): DD checklist scope framing.\n"
    "timeline_narrative (60-80 words): Phases from acquisition through stabilization.\n"
    "recommendation_narrative_p1 (100-130 words): Recommendation, primary rationale, key metrics.\n"
    "recommendation_narrative_p2 (80-110 words): Top risks and why manageable; next action.\n"
    "recommendation_pullquote (15-25 words): Recommendation pull-quote. Direct and declarative.\n"
    "risk_1/2/3 (25-35 words each): Three primary investment risks, concise statements.\n"
    "conclusion_1-5 (20-30 words each): Five thematic single-sentence conclusions.\n"
    "bottom_line (40-60 words): The last word on the deal before next steps.\n"
    "next_step_1-6 (15-25 words each): Six prioritized next steps beginning with action verbs.\n"
    "methodology_notes (80-100 words): Data sources, extraction methods, API pull dates,\n"
    "  DealDesk pipeline version. Municipal data sourced from DealDesk Municipal Registry\n"
    "  covering approximately 6,400 U.S. municipalities (data/municipal_registry.csv).\n"
    "  Use exactly '6,400' — do not use any other number for the registry size.\n"
    "photo_gallery_intro (30-50 words): Gallery context sentence.\n"
    "maps_intro (30-50 words): Maps section context sentence.\n"
    "fema_flood_narrative (50-70 words): Flood zone interpretation.\n"
    "construction_budget_narrative (70-90 words): Construction budget breakdown (if value_add)."
)

_USER_4MASTER = (
    "Generate all report narrative sections for the deal below.\n"
    "Return a single JSON object where every key is a report placeholder name.\n\n"
    "COMPLETE DEAL DATA:\n"
    "{deal_data_json}\n\n"
    "Generate all narrative sections now. Return ONLY the JSON object."
)


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT 5D — INVESTOR-FACING NARRATIVE REWRITE (Sonnet)
# ═══════════════════════════════════════════════════════════════════════════

# The 9 narrative keys rewritten for LP-appropriate language
_INVESTOR_REWRITE_KEYS = [
    "exec_overview_p1",
    "exec_overview_p2",
    "exec_overview_p3",
    "deal_thesis",
    "sources_uses_narrative",
    "capital_stack_narrative",
    "recommendation_narrative_p1",
    "recommendation_narrative_p2",
    "bottom_line",
]

_SYSTEM_5D = (
    "You are a senior CRE investment writer rewriting narrative sections of an\n"
    "underwriting report for an LP investor audience.\n\n"
    "RULES:\n"
    "- Rewrite each section in LP-appropriate language: professional, forward-looking,\n"
    "  focused on risk-adjusted returns and alignment of interests.\n"
    "- Remove internal-only language: GP negotiation details, proprietary sourcing\n"
    "  advantages, internal hurdle rate discussions, DD flag details.\n"
    "- Preserve all factual data — numbers, dates, metrics must match exactly.\n"
    "- Maintain the same word count targets as the original sections.\n"
    "- Material risk disclosures must NEVER be suppressed or softened.\n"
    "- Voice: Confident, institutional, suitable for LP distribution.\n"
    "- Return ONLY valid JSON with exactly the 9 keys provided. No extras."
)

_USER_5D = (
    "Rewrite these 9 narrative blocks for an LP investor audience.\n\n"
    "DEAL DATA:\n{deal_data_json}\n\n"
    "CURRENT NARRATIVES TO REWRITE:\n{narratives_json}\n\n"
    "Return JSON with exactly these 9 keys, each containing the rewritten text:\n"
    "{keys_list}\n\n"
    "Return ONLY the JSON object."
)


# ═══════════════════════════════════════════════════════════════════════════
# LLM CALL
# ═══════════════════════════════════════════════════════════════════════════

def _call_sonnet(system: str, user_msg: str, max_tokens: int = 8192) -> Optional[dict]:
    """Send a single Sonnet call and parse the JSON response. Returns None on failure."""
    client = anthropic.Anthropic(
        api_key=ANTHROPIC_API_KEY,
    )
    try:
        response = client.messages.create(
            model=MODEL_SONNET,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(raw)
    except (json.JSONDecodeError, anthropic.APIError, IndexError, KeyError) as exc:
        logger.warning("Sonnet call failed: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# NARRATIVE GENERATION
# ═══════════════════════════════════════════════════════════════════════════

def _generate_narratives(deal: DealData) -> None:
    """Run Prompt 4-MASTER to populate all narrative fields on DealData."""
    logger.info("Running Prompt 4-MASTER — all report narratives...")

    deal_json = deal.model_dump_json(indent=2)
    user_msg = _USER_4MASTER.format(deal_data_json=deal_json)

    result = _call_sonnet(_SYSTEM_4MASTER, user_msg)

    if result is None:
        # Retry once per catalog spec
        logger.warning("Prompt 4-MASTER first attempt failed — retrying...")
        result = _call_sonnet(_SYSTEM_4MASTER, user_msg)

    if result is None:
        logger.error("Prompt 4-MASTER failed twice — narratives will be empty strings")
        return

    # Apply all returned keys to the narratives model
    narr = deal.narratives
    for key, value in result.items():
        if hasattr(narr, key) and isinstance(value, str):
            setattr(narr, key, value)

    logger.info("Prompt 4-MASTER complete — %d narrative keys populated", len(result))


def _rewrite_investor_narratives(deal: DealData) -> None:
    """Run Prompt 5D to rewrite 9 narrative blocks for LP audience."""
    logger.info("Running Prompt 5D — investor narrative rewrite...")

    narr = deal.narratives
    current = {k: getattr(narr, k, "") or "" for k in _INVESTOR_REWRITE_KEYS}

    deal_json = deal.model_dump_json(indent=2)
    keys_list = json.dumps(_INVESTOR_REWRITE_KEYS)
    user_msg = _USER_5D.format(
        deal_data_json=deal_json,
        narratives_json=json.dumps(current, indent=2),
        keys_list=keys_list,
    )

    result = _call_sonnet(_SYSTEM_5D, user_msg, max_tokens=4096)

    if result is None:
        logger.warning("Prompt 5D failed — investor narratives unchanged")
        return

    for key in _INVESTOR_REWRITE_KEYS:
        if key in result and isinstance(result[key], str):
            setattr(narr, key, result[key])

    logger.info("Prompt 5D complete — %d investor narrative keys rewritten",
                sum(1 for k in _INVESTOR_REWRITE_KEYS if k in result))


# ═══════════════════════════════════════════════════════════════════════════
# TEMPLATE CONTEXT BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def _build_context(deal: DealData) -> dict:
    """Build the full template context dict from DealData for docxtpl."""
    narr = deal.narratives
    a = deal.assumptions
    fo = deal.financial_outputs
    md = deal.market_data
    ins = deal.insurance
    ext = deal.extracted_docs

    suppressed = deal.suppressed_sections

    ctx = {}

    # Cover page
    ctx["cover_title"] = deal.cover_title
    ctx["report_date"] = deal.report_date or ""
    ctx["deal_id"] = deal.deal_id or ""
    ctx["deal_code"] = deal.deal_code or ""
    ctx["sponsor_name"] = deal.sponsor_name
    ctx["sponsor_description"] = deal.sponsor_description

    # Property basics
    ctx["property_name"] = ext.property_name or ""
    ctx["full_address"] = deal.address.full_address
    ctx["city"] = deal.address.city
    ctx["state"] = deal.address.state
    ctx["zip_code"] = deal.address.zip_code
    ctx["asset_type"] = deal.asset_type.value
    ctx["investment_strategy"] = deal.investment_strategy.value
    ctx["asking_price"] = f"${deal.assumptions.purchase_price:,.0f}" if deal.assumptions.purchase_price else "Not disclosed"
    ctx["purchase_price"] = a.purchase_price
    ctx["num_units"] = a.num_units
    ctx["building_sf"] = a.gba_sf
    ctx["lot_sf"] = a.lot_sf
    ctx["year_built"] = a.year_built
    ctx["hold_period"] = a.hold_period
    ctx["deal_description"] = deal.deal_description

    # Parcel data
    if deal.parcel_data:
        ctx["parcel_data"] = deal.parcel_data.model_dump()
    else:
        ctx["parcel_data"] = {}

    # Zoning
    ctx["zoning"] = deal.zoning.zoning_code or "Pending verification"
    ctx["zoning_code"] = deal.zoning.zoning_code or ""

    # Market data
    ctx["census_tract"] = deal.address.census_tract or ""
    ctx["fips_code"] = deal.address.fips_code or ""
    ctx["fema_flood_zone"] = md.fema_flood_zone or ""
    ctx["fema_panel_number"] = md.fema_panel_number or ""

    # Financial outputs
    ctx["total_uses"] = fo.total_uses
    ctx["total_sources"] = fo.total_sources
    ctx["total_equity_required"] = fo.total_equity_required
    ctx["initial_loan_amount"] = fo.initial_loan_amount
    ctx["noi_yr1"] = fo.noi_yr1
    ctx["dscr_yr1"] = fo.dscr_yr1
    ctx["going_in_cap_rate"] = fo.going_in_cap_rate
    ctx["lp_irr"] = fo.lp_irr
    ctx["gp_irr"] = fo.gp_irr
    ctx["project_irr"] = fo.project_irr
    ctx["lp_equity_multiple"] = fo.lp_equity_multiple
    ctx["gp_equity_multiple"] = fo.gp_equity_multiple
    ctx["cash_on_cash_yr1"] = fo.cash_on_cash_yr1
    ctx["gross_sale_price"] = fo.gross_sale_price
    ctx["net_sale_proceeds"] = fo.net_sale_proceeds
    ctx["sensitivity_matrix"] = fo.sensitivity_matrix
    ctx["pro_forma_years"] = fo.pro_forma_years

    # Insurance
    ctx["insurance_narrative_p1"] = ins.insurance_narrative_p1 or ""
    ctx["insurance_narrative_p2"] = ins.insurance_narrative_p2 or ""
    ctx["insurance_narrative_p3"] = ins.insurance_narrative_p3 or ""
    ctx["insurance_kpi_strip"] = ""
    ctx["insurance_summary_table"] = ""

    # DD Flags
    ctx["dd_flags"] = [f.model_dump() for f in deal.dd_flags]

    # Recommendation
    ctx["recommendation"] = deal.recommendation.value if deal.recommendation else ""
    ctx["recommendation_one_line"] = deal.recommendation_one_line or ""

    # Waterfall
    ctx["pref_return"] = a.pref_return
    ctx["gp_equity_pct"] = a.gp_equity_pct
    ctx["lp_equity_pct"] = a.lp_equity_pct
    ctx["waterfall_tiers"] = [t.model_dump() for t in a.waterfall_tiers]

    # Extracted doc data
    ctx["unit_mix"] = ext.unit_mix or []
    ctx["occupancy_rate"] = ext.occupancy_rate
    ctx["image_placements"] = ext.image_placements or {}

    # Provenance
    ctx["provenance"] = deal.provenance.model_dump()


    # ── Comparable market data tables (Section 11) ────────────────────────
    # Rent comp table rows (Table 24 — 7 cols: Property, Type, Beds, SF, Rent/Mo, $/SF, Distance)
    rent_rows = []
    for c in (deal.comps.rent_comps or []):
        rent_rows.append({
            "property":     c.address or "",
            "type":         c.unit_type or "",
            "beds":         str(c.beds) if c.beds is not None else "",
            "sf":           f"{c.sq_ft:,}" if c.sq_ft else "",
            "monthly_rent": f"${c.monthly_rent:,.0f}" if c.monthly_rent else "",
            "rent_per_sf":  f"${c.rent_per_sf:.2f}" if c.rent_per_sf else "",
            "distance":     f"{c.distance_miles:.1f} mi" if c.distance_miles else "",
        })
    ctx["rent_comp_rows"] = rent_rows

    # Commercial comp table rows (Table 25 — 5 cols: Address, Use, SF, Rent/SF, Type)
    comm_rows = []
    for c in (deal.comps.commercial_comps or []):
        comm_rows.append({
            "address":      c.address or "",
            "use_type":     c.use_type or "",
            "sf":           f"{c.sq_ft:,}" if c.sq_ft else "",
            "rent_per_sf":  f"${c.asking_rent_per_sf:.2f}/SF" if c.asking_rent_per_sf else "",
            "lease_type":   c.lease_type or "",
        })
    ctx["commercial_comp_rows"] = comm_rows

    # Sale comp table rows (Table 26 — 6 cols: Address, SF, Units, Price, $/SF, Cap Rate)
    sale_rows = []
    for c in (deal.comps.sale_comps or []):
        sale_rows.append({
            "address":       c.address or "",
            "sf":            f"{c.sq_ft:,}" if c.sq_ft else "",
            "units":         str(c.num_units) if c.num_units else "",
            "sale_price":    f"${c.sale_price:,.0f}" if c.sale_price else "",
            "price_per_sf":  f"${c.price_per_sf:.0f}" if c.price_per_sf else "",
            "cap_rate":      f"{c.cap_rate:.2%}" if c.cap_rate else "",
        })
    ctx["sale_comp_rows"] = sale_rows


    # Comparable market data table rows (Section 11)
    rent_rows = []
    for c in (deal.comps.rent_comps or []):
        rent_rows.append({
            "property":     c.address or "",
            "type":         c.unit_type or "",
            "beds":         str(c.beds) if c.beds is not None else "",
            "sf":           f"{c.sq_ft:,}" if c.sq_ft else "",
            "monthly_rent": f"${c.monthly_rent:,.0f}" if c.monthly_rent else "",
            "rent_per_sf":  f"${c.rent_per_sf:.2f}" if c.rent_per_sf else "",
            "distance":     f"{c.distance_miles:.1f} mi" if c.distance_miles else "",
        })
    ctx["rent_comp_rows"] = rent_rows

    comm_rows = []
    for c in (deal.comps.commercial_comps or []):
        comm_rows.append({
            "address":     c.address or "",
            "use_type":    c.use_type or "",
            "sf":          f"{c.sq_ft:,}" if c.sq_ft else "",
            "rent_per_sf": f"${c.asking_rent_per_sf:.2f}/SF" if c.asking_rent_per_sf else "",
            "lease_type":  c.lease_type or "",
        })
    ctx["commercial_comp_rows"] = comm_rows

    sale_rows = []
    for c in (deal.comps.sale_comps or []):
        sale_rows.append({
            "address":      c.address or "",
            "sf":           f"{c.sq_ft:,}" if c.sq_ft else "",
            "units":        str(c.num_units) if c.num_units else "",
            "sale_price":   f"${c.sale_price:,.0f}" if c.sale_price else "",
            "price_per_sf": f"${c.price_per_sf:.0f}" if c.price_per_sf else "",
            "cap_rate":     f"{c.cap_rate:.2%}" if c.cap_rate else "",
        })
    ctx["sale_comp_rows"] = sale_rows

    # Section suppression flags
    ctx["suppressed_sections"] = suppressed
    for sid in ["s01", "s02", "s03", "s04", "s05", "s06", "s07", "s08", "s09",
                "s10", "s11", "s12", "s13", "s14", "s15", "s16", "s17", "s18",
                "s19", "s20", "s21", "s22"]:
        ctx[f"show_{sid}"] = sid not in suppressed

    # All narrative fields
    for field_name in narr.model_fields:
        ctx[field_name] = getattr(narr, field_name) or ""

    # Investor mode flag
    ctx["investor_mode"] = deal.investor_mode

    # ── Zoning standards table rows ───────────────────────────────
    z = deal.zoning
    ctx["zoning_standards_rows"] = [
        {"parameter": "Zoning District",      "standard": z.zoning_code or "",         "proposed": "", "code_section": ""},
        {"parameter": "District Name",         "standard": z.zoning_district or "",     "proposed": "", "code_section": ""},
        {"parameter": "Max Height (ft)",       "standard": z.max_height_ft or "",       "proposed": "", "code_section": ""},
        {"parameter": "Max Stories",           "standard": z.max_stories or "",         "proposed": "", "code_section": ""},
        {"parameter": "Min Lot Area (SF)",     "standard": z.min_lot_area_sf or "",     "proposed": "", "code_section": ""},
        {"parameter": "Max Lot Coverage",      "standard": f"{z.max_lot_coverage_pct:.0%}" if z.max_lot_coverage_pct else "", "proposed": "", "code_section": ""},
        {"parameter": "Max FAR",               "standard": z.max_far or "",             "proposed": "", "code_section": ""},
        {"parameter": "Front Setback (ft)",    "standard": z.front_setback_ft or "",   "proposed": "", "code_section": ""},
        {"parameter": "Rear Setback (ft)",     "standard": z.rear_setback_ft or "",    "proposed": "", "code_section": ""},
        {"parameter": "Side Setback (ft)",     "standard": z.side_setback_ft or "",    "proposed": "", "code_section": ""},
        {"parameter": "Min Parking Spaces",    "standard": z.min_parking_spaces or "", "proposed": "", "code_section": ""},
        {"parameter": "Permitted Uses",        "standard": ", ".join(z.permitted_uses) if z.permitted_uses else "", "proposed": "", "code_section": ""},
    ]

    # ── Pro forma table rows (formatted for report) ───────────────
    pf_rows = []
    for yr in (fo.pro_forma_years or []):
        ds = yr.get("debt_service", 0) or 0
        noi = yr.get("noi", 0) or 0
        pf_rows.append({
            "year":          yr.get("year", ""),
            "gpr":           f"${yr.get('gpr', 0):,.0f}",
            "egi":           f"${yr.get('egi', 0):,.0f}",
            "opex":          f"${yr.get('opex', 0):,.0f}",
            "noi":           f"${noi:,.0f}",
            "debt_service":  f"${ds:,.0f}",
            "cfbt":          f"${yr.get('fcf', 0):,.0f}",
            "coc":           f"{yr.get('cash_on_cash', 0):.1%}",
            "dscr":          f"{noi/ds:.2f}x" if ds > 0 else "N/A",
        })
    ctx["pro_forma_table_rows"] = pf_rows

    # ── Sensitivity matrix rows (formatted for report) ────────────
    sens_rows = []
    rent_axis  = fo.sensitivity_axis_rent_growth or []
    cap_axis   = fo.sensitivity_axis_exit_cap or []
    matrix     = fo.sensitivity_matrix or []
    for i, row in enumerate(matrix):
        rg_label = f"{rent_axis[i]:.1%}" if i < len(rent_axis) else ""
        sens_rows.append({
            "rent_growth": rg_label,
            "values": [f"{v:.1%}" if v else "—" for v in row],
        })
    ctx["sensitivity_rows"]      = sens_rows
    ctx["sensitivity_cap_axis"]  = [f"{c:.1%}" for c in cap_axis]

    # ── Monte Carlo results ───────────────────────────────────────
    ctx["monte_carlo_results"] = fo.monte_carlo_results or {}

    # ── Full market data object (for demographic table) ───────────
    ctx["market_data"] = deal.market_data.model_dump() if deal.market_data else {}

    # ── Waterfall formatted rows ──────────────────────────────────
    wf_rows = []
    tier_labels = ["Tier 1 — Pref Return + ROC", "Tier 2", "Tier 3", "Tier 4"]
    for i, t in enumerate(a.waterfall_tiers):
        wf_rows.append({
            "tier":     tier_labels[i] if i < len(tier_labels) else f"Tier {i+1}",
            "hurdle":   f"{t.hurdle_value:.1%}" if t.hurdle_value else "",
            "lp_split": f"{t.lp_share:.0%}",
            "gp_split": f"{1 - t.lp_share:.0%}",
            "promote":  f"{1 - t.lp_share:.0%}",
        })
    if hasattr(a, "residual_tier") and a.residual_tier:
        wf_rows.append({
            "tier":     "Residual (above Tier 4)",
            "hurdle":   "Above all hurdles",
            "lp_split": f"{a.residual_tier.lp_share:.0%}",
            "gp_split": f"{a.residual_tier.gp_share:.0%}",
            "promote":  f"{a.residual_tier.gp_share:.0%}",
        })
    ctx["waterfall_tier_rows"] = wf_rows

    # ── FRED rates (formatted for debt section) ───────────────────
    md = deal.market_data
    ctx["dgs10_rate"]      = f"{md.dgs10_rate:.2%}"      if md.dgs10_rate      else "N/A"
    ctx["sofr_rate"]       = f"{md.sofr_rate:.2%}"       if md.sofr_rate       else "N/A"
    ctx["mortgage30_rate"] = f"{md.mortgage30_rate:.2%}" if md.mortgage30_rate else "N/A"
    ctx["cpi_yoy"]         = f"{md.cpi_yoy:.2%}"         if md.cpi_yoy         else "N/A"

    # ── Opportunity zone flag ─────────────────────────────────────
    ctx["is_opportunity_zone"] = (
        deal.provenance.field_sources.get("opportunity_zone", "False") == "True"
    )

    # ── EPA environmental flags ───────────────────────────────────
    ctx["epa_env_flags"] = md.epa_env_flags or []

    # ── HUD Fair Market Rents ─────────────────────────────────────
    ctx["fmr_studio"] = f"${md.fmr_studio:,.0f}" if md.fmr_studio else "N/A"
    ctx["fmr_1br"]    = f"${md.fmr_1br:,.0f}"    if md.fmr_1br    else "N/A"
    ctx["fmr_2br"]    = f"${md.fmr_2br:,.0f}"    if md.fmr_2br    else "N/A"
    ctx["fmr_3br"]    = f"${md.fmr_3br:,.0f}"    if md.fmr_3br    else "N/A"

    # ── Debt market narrative ─────────────────────────────────────
    ctx["debt_market_narrative"] = md.debt_market_narrative or ""

    # ── HBU and buildable capacity ────────────────────────────────
    ctx["hbu_narrative"]           = z.hbu_narrative or ""
    ctx["hbu_conclusion"]          = z.hbu_conclusion or ""
    ctx["buildable_capacity_narrative"] = z.buildable_capacity_narrative or ""

    return ctx


# ═══════════════════════════════════════════════════════════════════════════
# DOCX GENERATION & PDF CONVERSION
# ═══════════════════════════════════════════════════════════════════════════

def _strip_highlight(doc):
    """Remove yellow highlight formatting from all runs."""
    for para in doc.paragraphs:
        for run in para.runs:
            rPr = run._r.get_or_add_rPr()
            highlight = rPr.find(qn('w:highlight'))
            if highlight is not None:
                rPr.remove(highlight)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    for run in para.runs:
                        rPr = run._r.get_or_add_rPr()
                        highlight = rPr.find(qn('w:highlight'))
                        if highlight is not None:
                            rPr.remove(highlight)



# ═══════════════════════════════════════════════════════════════════════════
# IMAGE CONTEXT BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def _build_image_context(deal: DealData, tpl: DocxTemplate) -> dict:
    """
    Generate all map and chart images and return them as InlineImage objects
    ready for docxtpl template substitution.

    Each image slot gets either a real InlineImage or None.
    Template placeholders that receive None remain as grey placeholder boxes.
    """
    ctx = {}

    # ── Maps ──────────────────────────────────────────────────────────────
    try:
        maps = build_all_maps(deal)
    except Exception as exc:
        logger.error("map_builder failed: %s", exc)
        maps = MapImages()

    def _inline(png_bytes, w_mm=160, h_mm=100):
        """Wrap PNG bytes as a docxtpl InlineImage."""
        if not png_bytes:
            return None
        import tempfile, os
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        tmp.write(png_bytes)
        tmp.close()
        try:
            return InlineImage(tpl, tmp.name, width=Mm(w_mm), height=Mm(h_mm))
        except Exception as exc:
            logger.warning("InlineImage creation failed: %s", exc)
            return None
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    ctx["img_aerial_map"]       = _inline(maps.aerial)
    ctx["img_neighborhood_map"] = _inline(maps.neighborhood)
    ctx["img_fema_map"]         = _inline(maps.fema)

    # ── Charts ────────────────────────────────────────────────────────────
    try:
        charts = build_all_charts(deal)
    except Exception as exc:
        logger.error("chart_builder failed: %s", exc)
        charts = ChartImages()

    ctx["img_demographic_chart"]   = _inline(charts.demographic,   w_mm=160, h_mm=90)
    ctx["img_proforma_chart"]      = _inline(charts.proforma,      w_mm=160, h_mm=90)
    ctx["img_irr_heatmap"]         = _inline(charts.irr_heatmap,   w_mm=160, h_mm=90)
    ctx["img_capital_stack"]       = _inline(charts.capital_stack, w_mm=80,  h_mm=100)
    ctx["img_financing_chart"]     = _inline(charts.financing,     w_mm=160, h_mm=80)
    ctx["img_risk_matrix"]         = _inline(charts.risk_matrix,   w_mm=160, h_mm=90)
    ctx["img_gantt_chart"]         = _inline(charts.gantt,         w_mm=160, h_mm=80)

    available = sum(1 for v in ctx.values() if v is not None)
    logger.info("Image context built — %d/%d images available", available, len(ctx))
    return ctx



# ===================================================================
# IMAGE CONTEXT BUILDER
# ===================================================================

def _build_image_context(deal: DealData, tpl: DocxTemplate) -> dict:
    """
    Generate all map and chart images and return them as InlineImage
    objects ready for docxtpl template substitution.
    Each image slot gets either a real InlineImage or None.
    """
    import tempfile, os
    ctx = {}

    def _inline(png_bytes, w_mm=160, h_mm=100):
        if not png_bytes:
            return None
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        tmp.write(png_bytes)
        tmp.close()
        try:
            return InlineImage(tpl, tmp.name, width=Mm(w_mm), height=Mm(h_mm))
        except Exception as exc:
            logger.warning("InlineImage creation failed: %s", exc)
            return None
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    # Maps
    try:
        maps = build_all_maps(deal)
    except Exception as exc:
        logger.error("map_builder failed: %s", exc)
        maps = MapImages()

    ctx["img_aerial_map"]       = _inline(maps.aerial)
    ctx["img_neighborhood_map"] = _inline(maps.neighborhood)
    ctx["img_fema_map"]         = _inline(maps.fema)

    # Charts
    try:
        charts = build_all_charts(deal)
    except Exception as exc:
        logger.error("chart_builder failed: %s", exc)
        charts = ChartImages()

    ctx["img_demographic_chart"] = _inline(charts.demographic,   w_mm=160, h_mm=90)
    ctx["img_proforma_chart"]    = _inline(charts.proforma,      w_mm=160, h_mm=90)
    ctx["img_irr_heatmap"]       = _inline(charts.irr_heatmap,   w_mm=160, h_mm=90)
    ctx["img_capital_stack"]     = _inline(charts.capital_stack, w_mm=80,  h_mm=100)
    ctx["img_financing_chart"]   = _inline(charts.financing,     w_mm=160, h_mm=80)
    ctx["img_risk_matrix"]       = _inline(charts.risk_matrix,   w_mm=160, h_mm=90)
    ctx["img_gantt_chart"]       = _inline(charts.gantt,         w_mm=160, h_mm=80)

    available = sum(1 for v in ctx.values() if v is not None)
    logger.info("Image context: %d/%d images available", available, len(ctx))
    return ctx


def _populate_docx(deal: DealData) -> Path:
    """Populate DealDesk_Report_Template_v4.docx with template context. Returns docx path."""
    ctx = _build_context(deal)

    tpl = DocxTemplate(str(WORD_TEMPLATE))

    # Generate and merge image context
    img_ctx = _build_image_context(deal, tpl)
    ctx.update(img_ctx)

    tpl.render(ctx)
    _strip_highlight(tpl.docx)

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    docx_path = OUTPUTS_DIR / f"{deal.deal_id}_report.docx"
    tpl.save(str(docx_path))
    logger.info("DOCX generated: %s", docx_path)
    return docx_path


def _update_toc_fields(docx_path: Path) -> None:
    """
    Open the DOCX in a live Word instance via win32com, update all fields
    (including the Table of Contents), save, and close.

    This must run on Windows with Microsoft Word installed.  It is required
    because docxtpl renders the template but cannot update Word fields — the
    TOC field remains empty until Word recalculates it.

    Falls back gracefully with a warning if win32com is unavailable (e.g.
    on Streamlit Community Cloud) — the PDF will render with a blank TOC
    but all other content will be correct.
    """
    try:
        import win32com.client as win32
    except ImportError:
        logger.warning(
            "win32com not available — TOC fields will not be updated. "
            "Install pywin32 to enable automatic TOC generation."
        )
        return

    try:
        word = win32.Dispatch("Word.Application")
        word.Visible = False
        word.DisplayAlerts = False
        try:
            doc = word.Documents.Open(str(docx_path.resolve()))
            doc.Fields.Update()          # updates all fields including TOC
            doc.TablesOfContents(1).Update()  # explicitly refresh TOC entries + page numbers
            doc.Save()
            doc.Close()
        finally:
            word.Quit()
        logger.info("TOC fields updated: %s", docx_path.name)
    except Exception as exc:
        logger.warning(
            "win32com TOC update failed: %s — proceeding with blank TOC", exc
        )


def _convert_to_pdf(docx_path: Path) -> Path:
    """Convert DOCX to PDF via LibreOffice headless. Returns PDF path."""
    if platform.system() == "Windows":
        soffice = r"C:\Program Files\LibreOffice\program\soffice.exe"
    else:
        soffice = "soffice"

    output_dir = docx_path.parent
    try:
        subprocess.run(
            [
                soffice,
                "--headless",
                "--convert-to", "pdf",
                "--outdir", str(output_dir),
                str(docx_path),
            ],
            timeout=PDF_CONVERSION_TIMEOUT,
            check=True,
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"LibreOffice PDF conversion timed out after {PDF_CONVERSION_TIMEOUT}s"
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"LibreOffice PDF conversion failed: {exc.stderr.decode()}")
    except FileNotFoundError:
        raise RuntimeError(
            f"LibreOffice not found at: {soffice}. "
            "Please verify the installation path."
        )

    pdf_path = output_dir / f"{docx_path.stem}.pdf"
    if not pdf_path.exists():
        raise RuntimeError(f"PDF not found after conversion: {pdf_path}")

    logger.info("PDF generated: %s", pdf_path)
    return pdf_path


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

def generate_report(deal: DealData) -> DealData:
    """
    Generate the full PDF underwriting report.

    1. Calls Prompt 4-MASTER (Sonnet) to generate all narrative sections.
    2. If investor_mode is True, calls Prompt 5D to rewrite 9 narrative
       blocks in LP-appropriate language.
    3. Populates DealDesk_Report_Template_v4.docx via docxtpl.
    4. Converts to PDF via LibreOffice headless (60s timeout).

    Args:
        deal: DealData with all upstream modules already populated.

    Returns:
        The same DealData object with output_pdf_path set.
    """
    # Stage 1: Generate all narratives
    _generate_narratives(deal)

    # Stage 2: Investor mode rewrite (if applicable)
    if deal.investor_mode:
        _rewrite_investor_narratives(deal)

    # Stage 3: Populate DOCX template
    docx_path = _populate_docx(deal)

    # Stage 3B: Update Word fields (TOC page numbers) before PDF conversion
    _update_toc_fields(docx_path)

    # Stage 4: Convert to PDF
    pdf_path = _convert_to_pdf(docx_path)
    deal.output_pdf_path = str(pdf_path)

    logger.info("Report generation complete: %s", pdf_path)
    return deal
