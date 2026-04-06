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
import subprocess
from pathlib import Path
from typing import Optional

import anthropic
import streamlit as st
from docxtpl import DocxTemplate

from config import (
    ANTHROPIC_SECRET_KEY,
    MODEL_SONNET,
    OUTPUTS_DIR,
    PDF_CONVERSION_TIMEOUT,
    WORD_TEMPLATE,
)
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
    "  DealDesk pipeline version.\n"
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
        api_key=st.secrets[ANTHROPIC_SECRET_KEY]["api_key"],
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
    ctx["asking_price"] = ext.asking_price
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
    ctx["zoning"] = deal.zoning.model_dump()
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
    ctx["insurance_kpi_strip"] = ins.insurance_kpi_strip or {}
    ctx["insurance_summary_table"] = ins.insurance_summary_table or []

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

    return ctx


# ═══════════════════════════════════════════════════════════════════════════
# DOCX GENERATION & PDF CONVERSION
# ═══════════════════════════════════════════════════════════════════════════

def _populate_docx(deal: DealData) -> Path:
    """Populate DealDesk_Report_Template_v4.docx with template context. Returns docx path."""
    ctx = _build_context(deal)

    tpl = DocxTemplate(str(WORD_TEMPLATE))
    tpl.render(ctx)

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    docx_path = OUTPUTS_DIR / f"{deal.deal_id}_report.docx"
    tpl.save(str(docx_path))
    logger.info("DOCX generated: %s", docx_path)
    return docx_path


def _convert_to_pdf(docx_path: Path) -> Path:
    """Convert DOCX to PDF via LibreOffice headless. Returns PDF path."""
    output_dir = docx_path.parent
    try:
        subprocess.run(
            [
                "soffice",
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

    # Stage 4: Convert to PDF
    pdf_path = _convert_to_pdf(docx_path)
    deal.output_pdf_path = str(pdf_path)

    logger.info("Report generation complete: %s", pdf_path)
    return deal
