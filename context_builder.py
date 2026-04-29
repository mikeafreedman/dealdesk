"""
context_builder.py — Narrative + Template Context Builder
=========================================================
Generates the Sonnet narratives (Prompts 4-MASTER and 5D) and builds the
template context dict consumed by report_builder.py (HTML → Playwright → PDF).

Public entry point:
    generate_narratives(deal)   — run 4-MASTER, then 5D if investor_mode
    build_context(deal)         — build the full template context dict
    fetch_street_view_image()   — Google Street View fallback hero photo
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import anthropic

from config import (
    ANTHROPIC_API_KEY,
    MODEL_SONNET,
    USE_4REC_SPECIALIST,
)
from dd_flag_engine import get_zoning_flag
from financials import _compute_peak_funded_equity
from models.models import DealData, RecommendationVerdict, ScenarioVerdict

logger = logging.getLogger(__name__)


def validate_and_build_narrative_context(deal: DealData) -> dict:
    """Build the complete context dictionary for Sonnet narrative prompts.

    Pulls directly from the DealData object (computed by financials.py).
    Validates all required fields and logs the full context.

    CRITICAL: This function is the single source of truth for narrative context.
    Never build context inline or use cached/stale values.
    """
    a = deal.assumptions
    fo = deal.financial_outputs
    ext = deal.extracted_docs
    md = deal.market_data
    proforma = fo.pro_forma_years or []
    hold = a.hold_period or 10
    gba = a.gba_sf or 1

    total_project_cost = fo.total_uses or 0
    initial_loan = fo.initial_loan_amount or 0
    total_equity = fo.total_equity_required or 0
    equity_gap = total_equity  # total_equity IS the equity gap
    gp_equity = fo.gp_equity or 0
    lp_equity = fo.lp_equity or 0

    gpr_yr1 = fo.gross_potential_rent or 0
    egi_yr1 = fo.effective_gross_income or 0
    opex_yr1 = fo.total_operating_expenses or 0
    noi_yr1 = fo.noi_yr1 or 0
    fcf_yr1 = fo.free_cash_flow_yr1 or 0
    ds_yr1 = fo.debt_service_annual or 0

    last_yr = proforma[-1] if proforma else {}
    exit_noi = proforma[hold - 1].get("noi", 0) if len(proforma) >= hold else 0

    ctx = {
        # --- Property Info ---
        'property_name':         ext.property_name or deal.address.full_address or '',
        'address':               deal.address.full_address,
        'asset_type':            deal.asset_type.value,
        'strategy':              deal.investment_strategy.value,
        'building_sf':           a.gba_sf or 0,
        'lot_sf':                a.lot_sf or 0,
        'year_built':            a.year_built or 'Unknown',
        'num_units':             a.num_units or 1,
        'vacancy_status':        '100% Vacant' if noi_yr1 <= 0 else 'Occupied',

        # --- Acquisition ---
        'purchase_price':        a.purchase_price,
        'price_per_sf':          round(a.purchase_price / gba, 2) if gba > 0 else 0,
        'price_per_unit':        round(a.purchase_price / (a.num_units or 1), 0),
        'asking_price':          ext.asking_price or a.purchase_price,

        # --- Project Cost & Equity ---
        'total_project_cost':    total_project_cost,
        'hard_costs':            a.const_hard,
        'construction_reserve':  a.const_reserve,
        'initial_loan':          initial_loan,
        'ltv':                   a.ltv_pct,
        'equity_gap':            equity_gap,
        'gp_pct':                a.gp_equity_pct,
        'lp_pct':                a.lp_equity_pct,
        'gp_equity':             gp_equity,
        'lp_equity':             lp_equity,
        'total_equity':          total_equity,
        'debt_pct_of_cost':      initial_loan / max(total_project_cost, 1),
        'equity_pct_of_cost':    total_equity / max(total_project_cost, 1),

        # --- Financing ---
        'interest_rate':         a.interest_rate,
        'interest_rate_pct':     a.interest_rate * 100,
        'loan_term_years':       a.loan_term,
        'io_period_months':      a.io_period_months,
        'amort_years':           a.amort_years,
        'origination_fee_pct':   a.origination_fee_pct,
        'year1_debt_service':    ds_yr1,
        'annual_io_payment':     initial_loan * a.interest_rate,
        'annual_pi_payment':     ds_yr1 if a.io_period_months == 0 else 0,

        # --- Income & Expenses (Year 1) ---
        'year1_gpr':             gpr_yr1,
        'year1_egi':             egi_yr1,
        'year1_opex':            opex_yr1,
        'year1_noi':             noi_yr1,
        'year1_cfbt':            fcf_yr1,
        'year1_coc':             fo.cash_on_cash_yr1 or 0,
        'year1_dscr':            fo.dscr_yr1 or 0,
        'vacancy_rate':          a.vacancy_rate,
        'vacancy_rate_pct':      a.vacancy_rate * 100,
        'loss_to_lease':         a.loss_to_lease,
        'loss_to_lease_pct':     a.loss_to_lease * 100,
        'revenue_growth':        a.annual_rent_growth,
        'revenue_growth_pct':    a.annual_rent_growth * 100,
        'expense_growth':        a.expense_growth_rate,
        'expense_growth_pct':    a.expense_growth_rate * 100,
        'effective_rent_psf':    gpr_yr1 / gba if gba > 0 else 0,

        # --- Year 10 / Exit ---
        'year10_gpr':            last_yr.get('gpr', 0),
        'year10_noi':            last_yr.get('noi', 0),
        'exit_year':             hold,
        'exit_noi':              exit_noi,
        'exit_cap_rate':         a.exit_cap_rate,
        'exit_cap_rate_pct':     a.exit_cap_rate * 100,
        'gross_sale_price':      fo.gross_sale_price or 0,
        'net_sale_proceeds':     fo.net_sale_proceeds or 0,
        'net_equity_at_exit':    fo.net_equity_at_exit or 0,
        'hold_period':           hold,
        'disposition_cost_pct':  a.disposition_costs_pct,

        # --- Sensitivity stabilization ---
        'sensitivity_stabilized_year': fo.sensitivity_stabilized_year,
        'sensitivity_stabilized_noi':  fo.sensitivity_stabilized_noi or 0,
        'sensitivity_note':            fo.sensitivity_note or '',

        # --- Returns ---
        'project_irr':           fo.project_irr,
        'lp_irr':                fo.lp_irr,
        'gp_irr':                fo.gp_irr,
        'lp_equity_multiple':    fo.lp_equity_multiple,
        'equity_multiple':       fo.project_equity_multiple,
        'preferred_return':      a.pref_return,
        'preferred_return_pct':  a.pref_return * 100,
        'target_lp_irr':         a.target_lp_irr,
        'target_lp_irr_pct':     a.target_lp_irr * 100,
        'min_lp_irr':            a.min_lp_irr,
        'min_lp_irr_pct':        a.min_lp_irr * 100,

        # IRR display values
        'project_irr_display':   (f"{fo.project_irr * 100:.2f}%" if fo.project_irr is not None
                                  else "N/A (negative NOI — IRR non-convergent)"),
        'lp_irr_display':        (f"{fo.lp_irr * 100:.2f}%" if fo.lp_irr is not None else "N/A"),
        'lp_em_display':         (f"{fo.lp_equity_multiple:.2f}x" if fo.lp_equity_multiple is not None else "N/A"),

        # --- Market / Demographics ---
        'submarket':             (
            deal.provenance.field_sources.get("neighborhood")
            or f"{deal.address.city or ''}".strip()
            or "Local submarket"
        ),
        'city':                  deal.address.city,
        'state':                 deal.address.state,
        'zip_code':              deal.address.zip_code,
        'pop_3mi':               md.population_3mi or 0,
        'median_hh_income_3mi':  md.median_hh_income_3mi or 0,
        'renter_pct_3mi':        md.pct_renter_occ_3mi or 0,
        'unemployment_rate':     md.unemployment_rate or 0,

        # --- Due Diligence ---
        'zoning_code':           deal.zoning.zoning_code or 'Pending verification',
        'fema_flood_zone': (
            md.fema_flood_zone
            or 'Unavailable — FEMA NFHL lookup failed; flood zone must be '
               'verified before binding flood insurance'
        ),
        'epa_flags':             '; '.join(md.epa_env_flags) if md.epa_env_flags else 'None identified',
        'phase1_status':         'Not completed',
        'title_insurance':       a.title_insurance,

        # --- Insurance ---
        'insurance_proforma':    deal.insurance.insurance_proforma_line_item or a.insurance,
        'insurance_total_low':   (deal.insurance.insurance_proforma_line_item or a.insurance) * 0.8,
        'insurance_total_high':  (deal.insurance.insurance_proforma_line_item or a.insurance) * 1.2,

        # --- Report metadata ---
        'report_date':           deal.report_date or '',
        'deal_id':               deal.deal_id or '',
        'pipeline_version':      '1.0',
        'prompt_catalog_version': '4.0',
    }

    # ── Validation ────────────────────────────────────────────────
    critical_fields = [
        'total_project_cost', 'initial_loan', 'equity_gap',
        'gp_equity', 'lp_equity', 'year1_gpr', 'year1_noi',
    ]
    for field in critical_fields:
        val = ctx.get(field)
        if val is None or val == 0:
            logger.warning("NARRATIVE CTX WARNING: '%s' is %s — check financials.py output", field, val)

    # ── Mandatory log ─────────────────────────────────────────────
    logger.info("NARRATIVE CTX CHECK:")
    logger.info("  total_project_cost = $%.2f", ctx['total_project_cost'])
    logger.info("  initial_loan       = $%.2f  (%.0f%% LTV on TPC)", ctx['initial_loan'], ctx['ltv'] * 100)
    logger.info("  equity_gap         = $%.2f", ctx['equity_gap'])
    logger.info("  gp_equity          = $%.2f  (%.0f%%)", ctx['gp_equity'], ctx['gp_pct'] * 100)
    logger.info("  lp_equity          = $%.2f  (%.0f%%)", ctx['lp_equity'], ctx['lp_pct'] * 100)
    logger.info("  year1_noi          = $%.2f", ctx['year1_noi'])
    logger.info("  project_irr        = %s", ctx['project_irr_display'])
    logger.info("  lp_irr             = %s", ctx['lp_irr_display'])
    logger.info("  lp_em              = %s", ctx['lp_em_display'])

    return ctx


def fetch_street_view_image(address: str, deal_id: str, output_dir: str = "outputs") -> Optional[str]:
    """Fetch a Google Street View static image as a fallback hero photo.
    Returns path to saved image, or None if fetch fails."""
    import requests

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        logger.info("STREET VIEW: No API key configured — skipping photo fallback")
        return None

    encoded_address = requests.utils.quote(address)
    url = (f"https://maps.googleapis.com/maps/api/streetview"
           f"?size=800x600&location={encoded_address}"
           f"&fov=90&heading=235&pitch=10&key={api_key}")

    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200 and len(response.content) > 5000:
            os.makedirs(output_dir, exist_ok=True)
            img_path = os.path.join(output_dir, f"{deal_id}_street_view.jpg")
            with open(img_path, 'wb') as f:
                f.write(response.content)
            size = os.path.getsize(img_path)
            logger.info("STREET VIEW: saved to %s, size=%d bytes", img_path, size)
            if size < 1000:
                logger.warning("STREET VIEW: file too small — likely API error response image")
                return None
            return img_path
        else:
            logger.warning("STREET VIEW: fetch failed (status=%d, size=%d)",
                           response.status_code, len(response.content))
            return None
    except Exception as e:
        logger.warning("STREET VIEW: exception — %s", e)
        return None


def _claude_call(client, **kwargs):
    """Call Claude API with up to 3 retries on 500/529 errors."""
    for attempt in range(3):
        try:
            return client.messages.create(**kwargs)
        except Exception as e:
            err = str(e)
            if attempt < 2 and any(code in err for code in
                                   ["500", "529", "overloaded",
                                    "internal server"]):
                wait = 15 * (attempt + 1)
                logger.warning(
                    f"Anthropic API transient error (attempt "
                    f"{attempt+1}/3): {e}. Retrying in {wait}s..."
                )
                time.sleep(wait)
            else:
                raise


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT 4-REC — INVESTMENT RECOMMENDATION SPECIALIST (Sonnet)
# ═══════════════════════════════════════════════════════════════════════════
# Phase 1 of the prompt-architecture roadmap: pull the 8 recommendation
# keys out of the monolithic 4-MASTER prompt and produce them with a
# dedicated analytical brief. Prompt enforces a required
# <reasoning>...</reasoning> block before the JSON so the decision path
# is auditable. See _SYSTEM_4REC for the full reasoning procedure
# (Beat 0 synthesis rule + Beats 1-6). Feature-gated behind
# config.USE_4REC_SPECIALIST.
# ───────────────────────────────────────────────────────────────────────────

# Keys this specialist prompt owns. When USE_4REC_SPECIALIST is True,
# these are removed from 4-MASTER Part 2 before its call and produced
# by _run_4rec instead.
_4REC_KEYS = [
    "recommendation",
    "recommendation_one_line",
    "recommendation_narrative_p1",
    "recommendation_narrative_p2",
    "recommendation_pullquote",
    "risk_1",
    "risk_2",
    "risk_3",
]

_SYSTEM_4REC = (
    "You are a senior commercial real estate underwriter serving as the final\n"
    "decision-writer on an institutional investment committee memo. Your sole\n"
    "job in this call is to produce the Section 19 \"Investment Recommendation\"\n"
    "block of a DealDesk underwriting report — nothing else.\n\n"
    "You will receive a structured briefing on a single deal (the subject\n"
    "property, the economics, the market, the risks, and the user's pre-set\n"
    "decision threshold). You will return one JSON object with exactly these\n"
    "eight keys and no others:\n\n"
    "  recommendation               (verdict enum: \"GO\" | \"CONDITIONAL GO\" | \"NO-GO\")\n"
    "  recommendation_one_line      (15-30 words)\n"
    "  recommendation_narrative_p1  (100-130 words)\n"
    "  recommendation_narrative_p2  (80-110 words)\n"
    "  recommendation_pullquote     (15-25 words)\n"
    "  risk_1                       (25-35 words)\n"
    "  risk_2                       (25-35 words)\n"
    "  risk_3                       (25-35 words)\n\n"
    "═════════════════════════════════════════════════════════════════════\n"
    "REASONING PROCEDURE — WORK THROUGH THESE BEATS BEFORE WRITING JSON\n"
    "═════════════════════════════════════════════════════════════════════\n\n"
    "Before emitting JSON, reason through the six beats below. Do your\n"
    "reasoning in natural language BEFORE the JSON block, wrapped in\n"
    "<reasoning>...</reasoning> tags. The Python client will strip\n"
    "everything outside the JSON object before use. Example response\n"
    "shape:\n\n"
    "  <reasoning>\n"
    "  Beat 1: ...\n"
    "  Beat 2: ...\n"
    "  ...\n"
    "  Beat 6: ...\n"
    "  </reasoning>\n"
    "  {\n"
    "    \"recommendation\": \"...\",\n"
    "    ...\n"
    "  }\n\n"
    "The reasoning block is required — not optional — on every call. It is\n"
    "logged for audit but does not appear in the final report.\n\n"
    "BEAT 0 — VERDICT SYNTHESIS RULE (read before Beats 1-6)\n\n"
    "The six beats produce signals that sometimes conflict. When they do,\n"
    "apply this strict precedence to determine the final verdict:\n\n"
    "  BEAT 1 CLEAR FAIL       → verdict is NO-GO. No override possible.\n"
    "                            Even if DD flags are all green and MC is\n"
    "                            strong, a deal that cannot clear 85% of\n"
    "                            the hurdle cannot be GO or CONDITIONAL GO.\n\n"
    "  BEAT 3 BINDING RED      → verdict is NO-GO. No override possible.\n"
    "                            A binding red flag kills the deal\n"
    "                            regardless of how the economics read.\n\n"
    "  BEAT 1 CLEAR PASS +\n"
    "  BEAT 2 CORROBORATED +\n"
    "  BEAT 3 (0 RED, ≤2 AMBER) → verdict is GO.\n\n"
    "  All other combinations   → verdict is CONDITIONAL GO. This is the\n"
    "                             default when signals are mixed or when\n"
    "                             workable contingencies exist that do\n"
    "                             not rise to NO-GO but prevent a clean GO.\n\n"
    "Apply this rule deterministically — do not substitute judgment for\n"
    "the rule. The job of Beats 1-5 is to surface the signals; the job\n"
    "of this rule is to convert signals into a verdict.\n\n"
    "BEAT 1 — THRESHOLD TEST (verdict anchor)\n"
    "   The user has pre-declared their Go/No-Go hurdle on the Assumptions\n"
    "   screen. The briefing gives you:\n"
    "     hurdle_metric  : \"project_irr\" | \"lp_irr\" | \"stab_cap_rate\" | \"stab_coc\"\n"
    "     hurdle_value   : decimal (0.15 = 15%)\n"
    "     realized_value : the actual computed value of that metric\n"
    "   Classify the threshold test as:\n"
    "     CLEAR PASS     — realized ≥ hurdle × 1.05\n"
    "     MARGINAL PASS  — hurdle ≤ realized < hurdle × 1.05\n"
    "     MARGINAL FAIL  — hurdle × 0.85 ≤ realized < hurdle\n"
    "     CLEAR FAIL     — realized < hurdle × 0.85\n"
    "     UNCOMPUTABLE   — realized is null (MC non-convergent, negative NOI)\n"
    "   This is the PRIMARY anchor for the verdict — nothing else can\n"
    "   override a CLEAR FAIL into a GO, and nothing else can drag a\n"
    "   CLEAR PASS into a NO-GO.\n\n"
    "BEAT 2 — RISK-ADJUSTED CORROBORATION (supporting signals)\n"
    "   Even a CLEAR PASS on the hurdle metric is not sufficient if the\n"
    "   risk-adjusted picture is broken. Review the secondary signals:\n"
    "     dscr_yr1           — <1.20 = stressed; <1.00 = impaired\n"
    "     going_in_cap_rate  — compare to market cap rate if provided\n"
    "     cash_on_cash_yr1   — <5% = weak; negative = distressed\n"
    "     lp_equity_multiple — <1.80 = below typical institutional minimum\n"
    "     mc_prob_above_tgt  — Monte Carlo probability the hurdle clears;\n"
    "                          <40% = fragile, >70% = robust\n"
    "     price_solver       — if MC-solved price is materially below\n"
    "                          (>10%) the base purchase price, the deal\n"
    "                          is priced above the MC-indicated value.\n"
    "   Aggregate these into a single judgment: CORROBORATED / MIXED /\n"
    "   CONTRADICTED. A CLEAR PASS + MIXED or CONTRADICTED is a\n"
    "   CONDITIONAL GO candidate, not a straight GO.\n\n"
    "BEAT 3 — DD FLAG ABSORPTION\n\n"
    "   RED flag taxonomy:\n"
    "     BINDING RED   = flag where no structural remediation exists at\n"
    "                     the current purchase price. Examples: zoning\n"
    "                     precludes intended use and rezoning infeasible;\n"
    "                     environmental cleanup >15% of project cost;\n"
    "                     title defects that cannot be insured over.\n"
    "     WORKABLE RED  = flag with a clear remediation path but material\n"
    "                     cost/time impact. Examples: Phase II ESA\n"
    "                     required; single-tenant concentration with\n"
    "                     renewal risk; mechanical systems at end-of-life\n"
    "                     requiring immediate capex.\n\n"
    "   Policy:\n"
    "     1+ BINDING RED   → NO-GO required.\n"
    "     1+ WORKABLE RED  → maximum verdict is CONDITIONAL GO.\n"
    "     3+ AMBER         → maximum verdict is CONDITIONAL GO.\n"
    "     0 RED / ≤2 AMBER → flags do not constrain verdict.\n\n"
    "   If ambiguous whether a RED is binding or workable, treat as\n"
    "   BINDING.\n\n"
    "   Explicitly name RED and AMBER flags you are absorbing in your\n"
    "   reasoning; they become risk_1/risk_2 in the output if material.\n\n"
    "BEAT 4 — ASSET-TYPE / STRATEGY BRANCHING\n"
    "   The verdict framing shifts based on asset_type × investment_strategy:\n"
    "     Multifamily / Stabilized_hold:\n"
    "        Primary lens = DSCR + stabilized cap rate vs market; rent\n"
    "        growth durability; renewal retention.\n"
    "     Multifamily / Value-add:\n"
    "        Primary lens = lease-up risk + construction carry + exit\n"
    "        cap expansion risk; stabilized year NOI vs base purchase.\n"
    "     Mixed-Use / any:\n"
    "        Primary lens = residential + commercial lease risk split;\n"
    "        ground-floor retail absorption; mixed-use exit buyer pool.\n"
    "     Office / any:\n"
    "        Primary lens = WALT + tenant credit + leasing TI/commissions;\n"
    "        flag sub-70% occupancy or <3-year WALT explicitly.\n"
    "     Retail / any:\n"
    "        Primary lens = tenant mix + co-tenancy + NNN pass-through;\n"
    "        anchor status; center type.\n"
    "     Industrial / any:\n"
    "        Primary lens = clear height + dock count + last-mile\n"
    "        geography; single-tenant credit if applicable.\n"
    "     For_sale (opportunistic, any asset):\n"
    "        Primary lens = exit price certainty + construction duration\n"
    "        + carry cost; margin-of-safety on gross margin.\n"
    "   Use the correct lens in recommendation_narrative_p1.\n\n"
    "BEAT 5 — RISK TRIAGE (for risk_1, risk_2, risk_3)\n"
    "   Select the three HIGHEST-consequence risks from what the briefing\n"
    "   exposes. Rank by severity × probability (qualitative judgment).\n"
    "   Priority order when multiple are present:\n"
    "     1. Unresolved BINDING RED DD flag\n"
    "     2. Unresolved WORKABLE RED DD flag\n"
    "     3. Stressed DSCR (<1.20) or negative Year-1 NOI\n"
    "     4. Hurdle-metric shortfall (MARGINAL/CLEAR FAIL)\n"
    "     5. Construction / lease-up execution risk (value_add, for_sale)\n"
    "     6. Market-level risks: supply pipeline, cap-rate expansion,\n"
    "        tenant credit concentration\n"
    "     7. Capital structure: refi timing, LTV stress, exit financing\n"
    "     8. Entitlement / zoning / environmental risks (AMBER tier)\n"
    "   Each risk_n is a STANDALONE, ACTIONABLE sentence naming the risk,\n"
    "   the magnitude, and one mitigation or watch-item. Risks MUST be\n"
    "   coherent with the verdict — do not produce three dealbreakers\n"
    "   alongside a GO recommendation.\n\n"
    "BEAT 6 — COHERENCE + WORD COUNT CHECK\n"
    "   Before emitting JSON, confirm:\n"
    "     - recommendation verdict matches the BEAT 0 synthesis rule exactly\n"
    "       given the Beat 1, 2, and 3 classifications\n"
    "     - recommendation_one_line names the verdict + the single\n"
    "       strongest supporting datapoint\n"
    "     - p1 opens with the verdict, explains the PRIMARY rationale\n"
    "       (beat 1 + beat 4's asset-specific lens), names 2-3 of the\n"
    "       most important numeric metrics verbatim\n"
    "     - p2 addresses the top risks (tie to risk_1/2/3) and states\n"
    "       the immediate next action the IC should take\n"
    "     - pullquote is quotable, declarative, no hedging\n"
    "     - all word counts within ±15% of targets\n"
    "     - nothing contradicts (e.g. GO verdict + risk_1 = \"zoning\n"
    "       precludes the intended use\")\n\n"
    "═════════════════════════════════════════════════════════════════════\n"
    "GLOBAL WRITING RULES (hardline)\n"
    "═════════════════════════════════════════════════════════════════════\n\n"
    "- Voice: senior analyst to IC. Precise, data-grounded, no hedging.\n"
    "- Never say: \"pleased to present\", \"exciting opportunity\", \"unique\n"
    "  opportunity\", \"best-in-class\", \"a solid play\", \"well-positioned\".\n"
    "- Never use words: \"synergy\", \"holistic\", \"turnkey\".\n"
    "- Never use hedge phrases: \"generally speaking\", \"in most cases\",\n"
    "  \"typically\", \"could potentially\", \"may be positioned to\", \"should\n"
    "  be able to\". Replace with declarative statements; if uncertain,\n"
    "  state the uncertainty explicitly (\"Monte Carlo non-convergent\")\n"
    "  rather than hedging the language.\n"
    "- Never say: \"market leader\", \"strong demographics\", \"durable cash\n"
    "  flow\", \"supply-constrained market\". Unfalsifiable marketing\n"
    "  phrases. Cite the specific numbers instead.\n"
    "- Numbers quoted verbatim. If the briefing gives lp_irr=0.1824, you\n"
    "  write \"18.2%\" — never round further or paraphrase.\n"
    "- When a metric is null (Monte Carlo non-convergent, MC not run,\n"
    "  negative NOI), state so explicitly. Do not invent a placeholder.\n"
    "- \"CONDITIONAL GO\" specifically means: \"the deal clears the hurdle\n"
    "  but has one or more gating diligence items that must resolve\n"
    "  before commitment.\" Use it when the economics work but real\n"
    "  contingencies exist.\n"
    "- Never cite data not in the briefing.\n"
    "- Return ONLY the JSON object after the <reasoning>...</reasoning>\n"
    "  block. No markdown, no preamble outside the reasoning block, no\n"
    "  trailing commentary after the JSON.\n\n"
    "═════════════════════════════════════════════════════════════════════\n"
    "DATA COMPLETENESS HANDLING\n"
    "═════════════════════════════════════════════════════════════════════\n\n"
    "The briefing may arrive with missing fields in three flavors:\n\n"
    "  1. HURDLE METRIC UNCOMPUTABLE (e.g. negative NOI, MC non-convergent):\n"
    "     - Verdict defaults to CONDITIONAL GO if the base-case\n"
    "       deterministic math is directionally sound, else NO-GO.\n"
    "     - p1 must explicitly state that the hurdle metric was\n"
    "       non-convergent and why.\n\n"
    "  2. DD FLAGS EMPTY OR ALL GREEN:\n"
    "     - Do not manufacture risks. State that due diligence has not\n"
    "       surfaced material flags, and draw risks (BEAT 5) from the\n"
    "       market / financial / structural signals in the briefing.\n\n"
    "  3. SENSITIVITY / MONTE CARLO MISSING:\n"
    "     - Do not reference percentile bands you can't see. Rely on\n"
    "       the deterministic metrics only and note the limitation.\n\n"
    "═════════════════════════════════════════════════════════════════════\n"
    "WORKED EXAMPLES (few-shot)\n"
    "═════════════════════════════════════════════════════════════════════\n\n"
    "EXAMPLE A — A clean GO\n"
    "--------------------------\n"
    "Input briefing (relevant slice, fully representative of the payload):\n"
    "  asset_type: Multifamily\n"
    "  investment_strategy: value_add\n"
    "  purchase_price: $8,400,000\n"
    "  hold_period: 10\n"
    "  hurdle_metric: lp_irr\n"
    "  hurdle_value: 0.15\n"
    "  realized_value: 0.182\n"
    "  threshold_test: CLEAR PASS (1.21× hurdle)\n\n"
    "  lp_irr: 0.182\n"
    "  project_irr: 0.165\n"
    "  lp_equity_multiple: 2.41\n"
    "  noi_yr1: $412,000\n"
    "  dscr_yr1: 1.38\n"
    "  cash_on_cash_yr1: 0.074\n"
    "  going_in_cap_rate: 0.049\n"
    "  stabilized_cap_rate: 0.061\n\n"
    "  mc_median_irr: 0.162\n"
    "  mc_p10_irr: 0.094\n"
    "  mc_p90_irr: 0.228\n"
    "  mc_prob_above_target: 0.68\n\n"
    "  solver_price: $8,910,000\n"
    "  solver_gap_pct: +6.1%\n\n"
    "  total_project_cost: $8,950,000\n"
    "  total_equity_required: $2,380,000\n"
    "  initial_loan_amount: $6,570,000\n"
    "  ltv_pct: 73.4%\n\n"
    "  debt_type: senior acquisition + construction\n"
    "  interest_rate_pct: 6.50%\n"
    "  amortization_years: 30\n"
    "  io_period_months: 24\n"
    "  refi_event_count: 1\n"
    "  max_refi_ltv_pct: 70.0%\n"
    "  peak_funded_equity: $2,520,000\n\n"
    "  exit_year: 10\n"
    "  exit_cap_rate_pct: 6.00%\n"
    "  gross_sale_price: $14,650,000\n"
    "  net_to_equity: $8,340,000\n\n"
    "  neighborhood_trend_narrative: \"Population +2.1% CAGR, MHI +3.4% CAGR,\n"
    "    permit activity steady at 1.2 units per 1,000 population.\"\n"
    "  supply_pipeline_narrative: \"Two comp projects (142 units total)\n"
    "    expected to deliver in Year 2; 9-month lease-up band.\"\n\n"
    "  dd_flag_summary: \"AMBER×1 (environmental: Phase I pending)\"\n\n"
    "  sensitivity_snapshot: \"Base LP IRR 18.2%; −50 bps rent growth +\n"
    "    +50 bps exit cap → 14.8%; verdict holds GO through 50 bps of\n"
    "    cap widening.\"\n\n"
    "Expected response shape:\n"
    "<reasoning>\n"
    "Beat 1: realized 18.2% vs 15% hurdle → 0.182 ≥ 0.15 × 1.05 (0.1575).\n"
    "  CLEAR PASS at 1.21× hurdle.\n"
    "Beat 2: DSCR 1.38 (healthy), CoC 7.4% (healthy), MC 68% above target\n"
    "  (robust > 70% threshold but close), MC-solved price $8.91M vs\n"
    "  $8.40M ask → +6% cushion, not contradicted. Judgment: CORROBORATED.\n"
    "Beat 3: 1 AMBER flag (Phase I pending). 0 RED. Policy: flags do not\n"
    "  constrain verdict. Phase I becomes a named risk, not a blocker.\n"
    "Beat 4: Multifamily × Value-add lens → frame around lease-up,\n"
    "  renovation carry, exit cap at Year 10.\n"
    "Beat 5: Top risks (priority 5, 6, 8 respectively) =\n"
    "  (1) lease-up risk during phased renovation,\n"
    "  (2) exit cap expansion at Year 10,\n"
    "  (3) Phase I environmental outcome (AMBER).\n"
    "Beat 6: Verdict = GO. Word counts noted. No contradiction — the\n"
    "  three risks have named mitigations and are not dealbreakers.\n"
    "</reasoning>\n"
    "{\n"
    "  \"recommendation\": \"GO\",\n"
    "  \"recommendation_one_line\": \"Proceed with the acquisition — underwritten 18.2% LP IRR exceeds the 15% hurdle by 320 bps with DSCR of 1.38x and Monte Carlo probability of 68% above target.\",\n"
    "  \"recommendation_narrative_p1\": \"DealDesk recommends GO on this 10-year value-add multifamily acquisition. Underwritten LP IRR of 18.2% clears the 15% committee hurdle with a 320 bps cushion, supported by a stabilized Year-1 DSCR of 1.38x and 7.4% cash-on-cash. Monte Carlo analysis indicates a 68% probability of exceeding the 15% target across the modeled rent-growth and exit-cap-rate ranges. The DealDesk Monte Carlo price solver places fair value at $8.91M — approximately 6% above the $8.40M purchase price, leaving modest basis protection. Execution rests on achieving the pro forma renovation premium and a 12-month stabilization timeline.\",\n"
    "  \"recommendation_narrative_p2\": \"Three risks drive the investment committee checklist: lease-up velocity during renovation, exit-cap expansion at year 10, and the pending Phase I environmental review. Each has a mapped mitigation path — renovation phasing preserves in-place NOI, the exit cap assumption is 50 bps wide of trailing comps, and Phase I is scheduled to clear before closing. Recommended next action: move to LOI at the asking price conditional on satisfactory Phase I, with closing contingent on verified rent roll.\",\n"
    "  \"recommendation_pullquote\": \"An 18.2% LP IRR with 1.38x DSCR and MC-solved fair value 6% above ask — this clears committee.\",\n"
    "  \"risk_1\": \"Lease-up risk during phased renovation — 15% of units offline at a time could compress Year-1 NOI if absorption slows; mitigated by phasing gate sequenced with vacancy.\",\n"
    "  \"risk_2\": \"Exit cap expansion at Year 10 — 25 bps of cap widening reduces terminal value by approximately $680K; sensitivity matrix shows the deal holds a GO verdict through 50 bps of widening.\",\n"
    "  \"risk_3\": \"Phase I environmental outcome pending — uncleared findings would trigger Phase II and potentially delay closing; broker has committed to report delivery within 21 days.\"\n"
    "}\n\n"
    "EXAMPLE B — A NO-GO\n"
    "--------------------------\n"
    "Input briefing:\n"
    "  asset_type: Office\n"
    "  investment_strategy: stabilized_hold\n"
    "  purchase_price: $14,200,000\n"
    "  hold_period: 7\n"
    "  hurdle_metric: lp_irr\n"
    "  hurdle_value: 0.12\n"
    "  realized_value: 0.041\n"
    "  threshold_test: CLEAR FAIL (0.34× hurdle)\n\n"
    "  lp_irr: 0.041\n"
    "  project_irr: 0.052\n"
    "  lp_equity_multiple: 1.24\n"
    "  noi_yr1: $780,000\n"
    "  dscr_yr1: 0.94\n"
    "  cash_on_cash_yr1: -0.018\n"
    "  going_in_cap_rate: 0.055\n"
    "  stabilized_cap_rate: 0.058\n\n"
    "  mc_median_irr: 0.045\n"
    "  mc_p10_irr: -0.012\n"
    "  mc_p90_irr: 0.102\n"
    "  mc_prob_above_target: 0.08\n\n"
    "  solver_price: $9,800,000\n"
    "  solver_gap_pct: -31.0%\n\n"
    "  total_project_cost: $14,420,000\n"
    "  total_equity_required: $5,720,000\n"
    "  initial_loan_amount: $8,700,000\n"
    "  ltv_pct: 61.3%\n\n"
    "  debt_type: senior acquisition (permanent)\n"
    "  interest_rate_pct: 7.00%\n"
    "  amortization_years: 30\n"
    "  io_period_months: 0\n"
    "  refi_event_count: 0\n"
    "  max_refi_ltv_pct: n/a\n"
    "  peak_funded_equity: $5,720,000\n\n"
    "  exit_year: 7\n"
    "  exit_cap_rate_pct: 7.50%\n"
    "  gross_sale_price: $13,540,000\n"
    "  net_to_equity: $3,410,000\n\n"
    "  neighborhood_trend_narrative: \"Office vacancy 18.2% with trend +120\n"
    "    bps over trailing 12 months; negative net absorption.\"\n"
    "  supply_pipeline_narrative: \"One 230K SF build-to-suit delivering\n"
    "    Year 1; broader sublease supply growing.\"\n\n"
    "  dd_flag_summary: \"RED×2 (tenant concentration 62% from Acme Corp\n"
    "    with 2.1-year WALT — workable RED; no renewal commitment; deferred\n"
    "    mechanical systems at end-of-life requiring $480K Year-1 capex —\n"
    "    workable RED), AMBER×1 (HVAC cooling tower replacement deferred)\"\n\n"
    "  sensitivity_snapshot: \"Base LP IRR 4.1%. No cell in the sensitivity\n"
    "    matrix (rent growth 0-5%, exit cap 6.5-8.5%) produces an LP IRR\n"
    "    above the 12% hurdle.\"\n\n"
    "Expected response shape:\n"
    "<reasoning>\n"
    "Beat 1: realized 4.1% vs 12% hurdle → 0.041 < 0.12 × 0.85 (0.102).\n"
    "  CLEAR FAIL at 0.34× hurdle.\n"
    "Beat 2: DSCR 0.94 (impaired, below 1.00), CoC negative (distressed),\n"
    "  MC 8% above target (fragile, well below 40%), MC-solved price\n"
    "  $9.8M vs $14.2M ask → -31% gap (deal materially overpriced).\n"
    "  Judgment: CONTRADICTED.\n"
    "Beat 3: 2 RED flags (both workable). Policy: 1+ WORKABLE RED →\n"
    "  maximum verdict is CONDITIONAL GO. BUT — Beat 1 already forces\n"
    "  NO-GO via CLEAR FAIL, and Beat 2 is fully contradicted. Verdict\n"
    "  floor from Beat 1 dominates. Verdict = NO-GO.\n"
    "Beat 4: Office × Stabilized lens → WALT + tenant credit + TI\n"
    "  exposure. 62% concentration + 2.1-year WALT is a disqualifying\n"
    "  combination even in a fully-priced office market.\n"
    "Beat 5: Top risks (priority 2, 3, 6) =\n"
    "  (1) single-tenant concentration + WALT (WORKABLE RED),\n"
    "  (2) Year-1 DSCR impairment (0.94),\n"
    "  (3) MC fair-value gap of 31% vs ask.\n"
    "Beat 6: Verdict = NO-GO. All signals aligned. Risks coherent with\n"
    "  verdict. No rec_narrative hedging — this is a clean decline.\n"
    "</reasoning>\n"
    "{\n"
    "  \"recommendation\": \"NO-GO\",\n"
    "  \"recommendation_one_line\": \"Decline the acquisition — LP IRR of 4.1% falls 790 bps short of the 12% hurdle, Year-1 DSCR is 0.94x, and the MC-indicated fair value sits 31% below the $14.2M asking price.\",\n"
    "  \"recommendation_narrative_p1\": \"DealDesk recommends NO-GO on this 7-year stabilized office acquisition. Underwritten LP IRR of 4.1% misses the 12% committee hurdle by 790 bps. Year-1 DSCR of 0.94x indicates the asset cannot service its debt on in-place cash flow without equity support. Cash-on-cash is negative in Year 1 and the Monte Carlo probability of clearing the hurdle across 2,000 iterations is 8%. The price solver indicates MC-supported fair value of $9.8M — a 31% gap to the $14.2M asking price. This is not a pricing re-trade candidate; the tenancy and market profile do not support the ask.\",\n"
    "  \"recommendation_narrative_p2\": \"Two structural risks are unavoidable at the current basis: 62% of revenue is concentrated in a single tenant whose WALT is 2.1 years, and deferred maintenance surfaced in initial diligence will draw from the thin Year-1 cash flow. Neither has a workable mitigation at this purchase price. Recommended next action: issue a pass letter citing the concentration profile and DSCR; revisit only if the ask is revised to an MC-supported basis below $10M.\",\n"
    "  \"recommendation_pullquote\": \"A 4.1% LP IRR, 0.94x DSCR, and MC fair value 31% below the ask — pass.\",\n"
    "  \"risk_1\": \"Single-tenant concentration at 62% of revenue with a 2.1-year WALT — renewal exposure within the hold window would strand equity with no reliable back-fill path.\",\n"
    "  \"risk_2\": \"Year-1 DSCR of 0.94x indicates the asset cannot service its debt on in-place cash flow; equity support would be required from acquisition through stabilization.\",\n"
    "  \"risk_3\": \"Monte Carlo fair-value gap of 31% vs asking — the price solver places a supportable basis at $9.8M, and no cap-rate or rent-growth assumption in the modeled range closes the delta.\"\n"
    "}\n\n"
    "═════════════════════════════════════════════════════════════════════\n"
    "FINAL CHECK — before emitting JSON, verify:\n"
    "  - verdict string is exactly \"GO\", \"CONDITIONAL GO\", or \"NO-GO\"\n"
    "  - output is a single JSON object with exactly 8 keys\n"
    "  - reasoning block appears before the JSON inside\n"
    "    <reasoning>...</reasoning> tags\n"
    "  - no markdown fences, no preamble outside the reasoning block,\n"
    "    no trailing commentary after the JSON\n"
    "═════════════════════════════════════════════════════════════════════"
)

_USER_4REC = (
    "Produce the Investment Recommendation block for the deal below.\n"
    "Follow the six-beat reasoning procedure in your system prompt.\n"
    "Emit the <reasoning>...</reasoning> block first, then the JSON\n"
    "with exactly eight keys.\n\n"
    "═══ DEAL IDENTITY ═══\n"
    "Property:            {property_address}\n"
    "Asset type:          {asset_type}\n"
    "Investment strategy: {investment_strategy}\n"
    "Purchase price:      {purchase_price}\n"
    "Hold period:         {hold_period} years\n\n"
    "═══ GO/NO-GO HURDLE (user-declared) ═══\n"
    "Metric:              {hurdle_metric}\n"
    "Target value:        {hurdle_value_pct}\n"
    "Realized value:      {hurdle_realized}\n"
    "Threshold test:      {threshold_test}\n\n"
    "═══ DETERMINISTIC RETURNS ═══\n"
    "LP IRR:              {lp_irr}\n"
    "Project IRR:         {project_irr}\n"
    "LP equity multiple:  {lp_equity_multiple}\n"
    "Year-1 NOI:          {noi_yr1}\n"
    "Year-1 DSCR:         {dscr_yr1}\n"
    "Year-1 CoC:          {cash_on_cash_yr1}\n"
    "Going-in cap rate:   {going_in_cap_rate}\n"
    "Stabilized cap rate: {stabilized_cap_rate}\n\n"
    "═══ MONTE CARLO ═══\n"
    "MC median LP IRR:    {mc_median_irr}\n"
    "MC P10 LP IRR:       {mc_p10_irr}\n"
    "MC P90 LP IRR:       {mc_p90_irr}\n"
    "Prob above target:   {mc_prob_above_target}\n\n"
    "═══ PRICE SOLVER ═══\n"
    "MC-solved price:     {solver_price}\n"
    "Gap to base:         {solver_gap_pct}\n\n"
    "═══ SOURCES & USES ═══\n"
    "Total project cost:  {total_project_cost}\n"
    "Total equity req'd:  {total_equity_required}\n"
    "Initial loan amount: {initial_loan_amount}\n"
    "LTV at acquisition:  {ltv_pct}\n\n"
    "═══ FINANCING ═══\n"
    "Debt type:           {debt_type}\n"
    "Interest rate:       {interest_rate_pct}\n"
    "Amortization:        {amortization_years}\n"
    "IO period:           {io_period_months}\n"
    "Refi events:         {refi_event_count}\n"
    "Max refi LTV:        {max_refi_ltv_pct}\n"
    "Peak funded equity:  {peak_funded_equity}\n\n"
    "═══ EXIT ═══\n"
    "Exit year:           {exit_year}\n"
    "Exit cap rate:       {exit_cap_rate_pct}\n"
    "Gross sale price:    {gross_sale_price}\n"
    "Net to equity:       {net_to_equity}\n\n"
    "═══ MARKET CONTEXT ═══\n"
    "Neighborhood trend:  {neighborhood_trend_narrative}\n"
    "Supply pipeline:     {supply_pipeline_narrative}\n\n"
    "═══ DD FLAGS ═══\n"
    "{dd_flag_summary}\n\n"
    "═══ SENSITIVITY SNAPSHOT ═══\n"
    "{sensitivity_snapshot}\n\n"
    "Return the <reasoning>...</reasoning> block and then the JSON now."
)


# ── 4-REC payload assembler + orchestrator ──────────────────────────

# Counter for the "first 5 runs" side-by-side diagnostic. Module-level
# so it persists across calls within a single process.
_4REC_DIAG_COUNT = 0
_4REC_DIAG_LIMIT = 5


def _fmt_money(v, default="non-convergent") -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f == 0:
        return default
    return f"${f:,.0f}"


def _fmt_pct(v, digits=2, default="non-convergent") -> str:
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f"{f * 100:.{digits}f}%"


def _fmt_num(v, digits=2, default="non-convergent") -> str:
    if v is None:
        return default
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return default


def _fmt_text(v, default="N/A") -> str:
    if v in (None, ""):
        return default
    return str(v)


def _classify_threshold(realized, hurdle) -> str:
    """Apply BEAT 1 bands — CLEAR PASS / MARGINAL PASS / MARGINAL FAIL /
    CLEAR FAIL / UNCOMPUTABLE — so the model doesn't re-derive it."""
    if realized is None or hurdle in (None, 0):
        return "UNCOMPUTABLE"
    try:
        r = float(realized)
        h = float(hurdle)
    except (TypeError, ValueError):
        return "UNCOMPUTABLE"
    if h <= 0:
        return "UNCOMPUTABLE"
    ratio = r / h
    if r >= h * 1.05:
        return f"CLEAR PASS ({ratio:.2f}× hurdle)"
    if r >= h:
        return f"MARGINAL PASS ({ratio:.2f}× hurdle)"
    if r >= h * 0.85:
        return f"MARGINAL FAIL ({ratio:.2f}× hurdle)"
    return f"CLEAR FAIL ({ratio:.2f}× hurdle)"


def _realized_for_metric(metric: str, fo) -> Optional[float]:
    """Pick the fo field that corresponds to the user's hurdle metric."""
    m = (metric or "").strip().lower()
    if m == "lp_irr":
        return fo.lp_irr
    if m == "project_irr":
        return fo.project_irr
    if m == "stab_cap_rate":
        return fo.stabilized_cap_rate
    if m == "stab_coc":
        # fo doesn't persist a dedicated stabilized-Y2 CoC; use Y1 as
        # proxy with an understanding that the model gets full context.
        return fo.cash_on_cash_yr1
    return None


def _build_dd_flag_summary(deal) -> str:
    """Compact color-grouped DD flag summary for the briefing.

    Format: 'RED×N (title; title; title +K more), AMBER×M (title; title)'.
    """
    flags = deal.dd_flags or []
    if not flags:
        return "None surfaced"
    buckets = {"RED": [], "AMBER": [], "GREEN": []}
    for f in flags:
        color = f.color.value if hasattr(f.color, "value") else str(f.color)
        if color in buckets:
            buckets[color].append(f.title or "unspecified")
    parts = []
    for color in ("RED", "AMBER", "GREEN"):
        n = len(buckets[color])
        if n == 0:
            continue
        shown = buckets[color][:10]
        tail = "" if n <= 10 else f" +{n - 10} more"
        parts.append(f"{color}×{n} ({'; '.join(shown)}{tail})")
    return ", ".join(parts) if parts else "None surfaced"


def _build_sensitivity_snapshot(fo) -> str:
    """Compact sensitivity-matrix summary: base, best corner, worst corner."""
    matrix = fo.sensitivity_matrix or []
    numeric = []
    for row in matrix:
        for v in (row or []):
            if isinstance(v, (int, float)):
                numeric.append(v)
    if not numeric:
        return "Sensitivity matrix non-convergent or not populated."
    lp = fo.lp_irr
    base = _fmt_pct(lp, 1) if lp is not None else "non-convergent"
    return (f"Base LP IRR {base}. Best corner {max(numeric) * 100:.1f}%. "
            f"Worst corner {min(numeric) * 100:.1f}%. "
            f"{len(numeric)} of {sum(len(r or []) for r in matrix)} cells "
            "converged.")


def _debt_type_label(deal) -> str:
    from models.models import InvestmentStrategy
    const_months = int(getattr(deal.assumptions, "const_period_months", 0) or 0)
    if deal.investment_strategy == InvestmentStrategy.OPPORTUNISTIC:
        return "for-sale carry + construction loan"
    if const_months > 0:
        return "senior acquisition + construction"
    return "senior acquisition (permanent)"


def _build_4rec_payload(deal) -> dict:
    """Assemble the flat dict of formatted values consumed by _USER_4REC.

    Every placeholder in the user-template resolves from here. Missing/
    non-convergent values are substituted with 'non-convergent' for
    financial metrics, 'N/A' for categorical fields, per spec.
    """
    a = deal.assumptions
    fo = deal.financial_outputs
    addr = deal.address

    # Hurdle test
    hurdle_metric = getattr(a, "hurdle_metric", "lp_irr") or "lp_irr"
    hurdle_value = getattr(a, "hurdle_value", None)
    realized = _realized_for_metric(hurdle_metric, fo)
    threshold_test = _classify_threshold(realized, hurdle_value)

    # Price solver
    ps = fo.price_solver_results or {}
    solver_price_val = ps.get("solved_purchase_price")
    solver_gap_val = ps.get("price_adjustment_pct")

    # Monte Carlo
    mc = fo.monte_carlo_results or {}

    # Refi
    refis = [r for r in (a.refi_events or []) if getattr(r, "active", False)]
    max_refi_ltv = max((getattr(r, "ltv", 0) or 0 for r in refis), default=None)

    peak_eq = _compute_peak_funded_equity(deal)

    return {
        # Deal identity
        "property_address":       _fmt_text(addr.full_address, default="N/A"),
        "asset_type":             deal.asset_type.value if deal.asset_type else "N/A",
        "investment_strategy":    deal.investment_strategy.value if deal.investment_strategy else "N/A",
        "purchase_price":         _fmt_money(a.purchase_price),
        "hold_period":            a.hold_period or 0,

        # Go/No-Go hurdle
        "hurdle_metric":          hurdle_metric,
        "hurdle_value_pct":       _fmt_pct(hurdle_value, 2, default="N/A"),
        "hurdle_realized":        _fmt_pct(realized, 2, default="non-convergent"),
        "threshold_test":         threshold_test,

        # Deterministic returns
        "lp_irr":                 _fmt_pct(fo.lp_irr, 2),
        "project_irr":            _fmt_pct(fo.project_irr, 2),
        "lp_equity_multiple":     (f"{fo.lp_equity_multiple:.2f}x" if fo.lp_equity_multiple else "non-convergent"),
        "noi_yr1":                _fmt_money(fo.noi_yr1),
        "dscr_yr1":               (f"{fo.dscr_yr1:.2f}x" if fo.dscr_yr1 else "non-convergent"),
        "cash_on_cash_yr1":       _fmt_pct(fo.cash_on_cash_yr1, 2),
        "going_in_cap_rate":      _fmt_pct(fo.going_in_cap_rate, 2),
        "stabilized_cap_rate":    _fmt_pct(fo.stabilized_cap_rate, 2),

        # Monte Carlo
        "mc_median_irr":          _fmt_pct(mc.get("median_irr"), 2),
        "mc_p10_irr":             _fmt_pct(mc.get("p10_irr"), 2),
        "mc_p90_irr":             _fmt_pct(mc.get("p90_irr"), 2),
        "mc_prob_above_target":   _fmt_pct(mc.get("prob_above_target"), 1),

        # Price solver
        "solver_price":           _fmt_money(solver_price_val),
        "solver_gap_pct":         (f"{solver_gap_val * 100:+.1f}%" if solver_gap_val is not None else "N/A"),

        # Sources & Uses
        "total_project_cost":     _fmt_money(fo.total_project_cost or fo.total_uses),
        "total_equity_required":  _fmt_money(fo.total_equity_required),
        "initial_loan_amount":    _fmt_money(fo.initial_loan_amount),
        "ltv_pct":                _fmt_pct(a.ltv_pct, 1, default="N/A"),

        # Financing
        "debt_type":              _debt_type_label(deal),
        "interest_rate_pct":      _fmt_pct(a.interest_rate, 2, default="N/A"),
        "amortization_years":     (f"{a.amort_years} yrs" if a.amort_years else "N/A"),
        "io_period_months":       (f"{a.io_period_months} mo" if a.io_period_months is not None else "N/A"),
        "refi_event_count":       len(refis),
        "max_refi_ltv_pct":       _fmt_pct(max_refi_ltv, 1, default="n/a"),
        "peak_funded_equity":     _fmt_money(peak_eq),

        # Exit
        "exit_year":              a.hold_period or 0,
        "exit_cap_rate_pct":      _fmt_pct(getattr(a, "exit_cap_rate", None), 2, default="N/A"),
        "gross_sale_price":       _fmt_money(fo.gross_sale_price),
        "net_to_equity":          _fmt_money(fo.net_equity_at_exit),

        # Market narratives (from 4-MASTER Part 1 if it ran first)
        "neighborhood_trend_narrative": _fmt_text(
            deal.narratives.neighborhood_trend_narrative,
            default="Not yet generated",
        ),
        "supply_pipeline_narrative":    _fmt_text(
            deal.narratives.supply_pipeline_narrative,
            default="Not yet generated",
        ),

        # DD flags
        "dd_flag_summary":        _build_dd_flag_summary(deal),

        # Sensitivity
        "sensitivity_snapshot":   _build_sensitivity_snapshot(fo),
    }


def _call_sonnet_raw(system: str, user_msg: str,
                     max_tokens: int = 4096,
                     temperature: float = 0.3) -> Optional[str]:
    """Sonnet call that returns raw text (not parsed JSON).

    4-REC responses are shaped as <reasoning>...</reasoning> followed by
    a JSON object — _call_sonnet's json.loads-everything approach would
    fail immediately. _parse_4rec_response handles the split.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        response = _claude_call(
            client,
            model=MODEL_SONNET,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
            temperature=temperature,
        )
        return response.content[0].text
    except anthropic.AuthenticationError as auth_err:
        logger.error("4-REC AUTH ERROR (401): %s", auth_err)
        return None
    except anthropic.APIStatusError as status_err:
        logger.error("4-REC API STATUS ERROR: status=%s body=%s",
                     status_err.status_code, status_err.message)
        return None
    except (anthropic.APIError, IndexError, KeyError) as exc:
        logger.error("4-REC CALL FAILED: %s | type=%s", exc, type(exc).__name__)
        return None


def _parse_4rec_response(raw: str) -> Tuple[str, Optional[dict]]:
    """Split the 4-REC response into (reasoning_text, parsed_json).

    Extracts the first <reasoning>...</reasoning> block (case-insensitive,
    multi-line), logs it at INFO for audit, then strips it out and finds
    the first '{' and last '}' in the remainder to parse the JSON object.
    Returns (reasoning, parsed_dict) or (reasoning, None) on parse failure.
    """
    if not raw:
        return "", None
    reasoning_text = ""
    m = re.search(r"<reasoning>(.*?)</reasoning>", raw,
                  flags=re.DOTALL | re.IGNORECASE)
    if m:
        reasoning_text = m.group(1).strip()
        logger.info("4-REC REASONING:\n%s", reasoning_text)
    else:
        logger.warning("4-REC: no <reasoning>...</reasoning> block found "
                       "in response — model did not follow protocol")

    # Strip the reasoning block (and any accidental preamble) before
    # locating JSON. Tolerant of the model wrapping the whole response
    # in ```json fences or adding markdown.
    body = re.sub(r"<reasoning>.*?</reasoning>", "", raw,
                  flags=re.DOTALL | re.IGNORECASE)
    body = body.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    first = body.find("{")
    last = body.rfind("}")
    if first == -1 or last == -1 or last <= first:
        logger.error("4-REC: no JSON object found in response body "
                     "(len=%d) — parse failed", len(body))
        return reasoning_text, None
    try:
        parsed = json.loads(body[first:last + 1])
        if not isinstance(parsed, dict):
            logger.error("4-REC: parsed payload is %s, not dict",
                         type(parsed).__name__)
            return reasoning_text, None
        return reasoning_text, parsed
    except json.JSONDecodeError as exc:
        logger.error("4-REC JSON parse failed at pos %d: %s",
                     getattr(exc, "pos", -1), exc)
        return reasoning_text, None


def _run_4rec(deal) -> Tuple[str, dict]:
    """Run the 4-REC specialist prompt. Returns (reasoning, parsed_dict).

    Up to 2 retries on transient errors or JSON parse failures. Always
    returns a tuple; on total failure the dict is empty and caller is
    expected to fall back to 4-MASTER for the rec keys.
    """
    global _4REC_DIAG_COUNT

    logger.info("4-REC: building payload for deal %s...", deal.deal_id)
    payload = _build_4rec_payload(deal)
    try:
        user_msg = _USER_4REC.format(**payload)
    except KeyError as exc:
        logger.error("4-REC: payload missing placeholder %s — aborting", exc)
        return "", {}

    reasoning = ""
    parsed: Optional[dict] = None
    last_error = None

    for attempt in range(1, 3):
        logger.info("4-REC: calling Sonnet (attempt %d of 2)...", attempt)
        raw = _call_sonnet_raw(_SYSTEM_4REC, user_msg,
                               max_tokens=4096, temperature=0.3)
        if raw is None:
            last_error = "api_call_returned_none"
            continue
        reasoning, parsed = _parse_4rec_response(raw)
        if parsed is not None:
            break
        last_error = "json_parse_failed"
        logger.warning("4-REC attempt %d: JSON parse failed — retrying",
                       attempt)

    if parsed is None:
        logger.error("4-REC: all attempts failed (last error: %s)",
                     last_error)
        return reasoning, {}

    # Validate key shape — every expected key should be present and a
    # string (verdict is also a string).
    missing = [k for k in _4REC_KEYS if k not in parsed]
    extra = [k for k in parsed.keys() if k not in _4REC_KEYS]
    if missing:
        logger.warning("4-REC: response missing keys %s", missing)
    if extra:
        logger.info("4-REC: response has %d extra keys (will be ignored): %s",
                    len(extra), extra)

    # Side-by-side diagnostic for the first 5 runs
    if _4REC_DIAG_COUNT < _4REC_DIAG_LIMIT:
        _4REC_DIAG_COUNT += 1
        summary_lines = []
        for k in _4REC_KEYS:
            v = parsed.get(k, "")
            if isinstance(v, str) and len(v) > 120:
                v = v[:117] + "..."
            summary_lines.append(f"    {k}: {v!r}")
        logger.info(
            "4-REC DIAG (run %d/%d):\n  REASONING:\n%s\n  OUTPUT KEYS:\n%s",
            _4REC_DIAG_COUNT, _4REC_DIAG_LIMIT,
            reasoning,
            "\n".join(summary_lines),
        )

    return reasoning, parsed


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
    "  Ground this narrative in parcel_data.deed_history (recording_date, document_type,\n"
    "  grantor, grantee, consideration_amount) when present — summarize recency of\n"
    "  ownership, number of transfers, and any consideration amounts that stand out.\n"
    "  If parcel_data.deed_history is empty, state that recorded-deed data is not\n"
    "  available for this jurisdiction and reference parcel_data.owner_name only.\n"
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
    "  IMPORTANT matrix-state handling:\n"
    "  - If the matrix is TRULY empty (no cells contain any numeric value at\n"
    "    all, including no N/A strings), write exactly: 'Sensitivity analysis\n"
    "    requires stabilized revenue data. Matrix will be populated following\n"
    "    lease-up and rent roll stabilization.'\n"
    "  - If cells are mostly 'N/A' with a few numeric values: state that most\n"
    "    scenarios produce cash flows too negative for IRR convergence, name\n"
    "    the favorable corner(s) where returns do compute, and describe those\n"
    "    numeric results against the 12% threshold.\n"
    "  - If cells are all numeric: normal sensitivity commentary — identify\n"
    "    pass/watch/fail regions and the base-case outcome.\n"
    "  Never describe 'N/A' cells as zero or failure — they mean non-convergent.\n"
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
    "recommendation (REQUIRED, verdict enum): Exactly one of 'GO', 'CONDITIONAL GO', or 'NO-GO'.\n"
    "  Base this on deal economics, market conditions, risk profile, and DD flags.\n"
    "recommendation_one_line (REQUIRED, 15-30 words): Single-sentence summary of the verdict\n"
    "  with the one most important supporting rationale. Direct, declarative, no hedging.\n"
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

# Partition 4-MASTER keys into two batches to avoid response truncation.
_4MASTER_KEYS_PART1 = [
    "exec_overview_p1", "exec_overview_p2", "exec_overview_p3", "exec_pullquote",
    "deal_thesis", "opportunity_1", "opportunity_2", "opportunity_3",
    "prop_desc_p1", "prop_desc_p2", "prop_desc_p3", "prop_desc_p4",
    "utilities_analysis", "ownership_narrative", "liens_narrative",
    "location_pullquote", "location_overview_p1", "location_overview_p2",
    "transportation_analysis", "neighborhood_trend_narrative",
    "supply_pipeline_narrative", "rent_roll_intro", "rent_comp_narrative",
    "commercial_comp_narrative", "sale_comp_narrative",
    "photo_gallery_intro", "maps_intro",
]
_4MASTER_KEYS_PART2 = [
    "financial_pullquote", "sources_uses_narrative", "proforma_narrative",
    "proforma_pullquote", "sensitivity_narrative", "exit_narrative",
    "capital_stack_narrative", "capital_structure_pullquote",
    "debt_comparison_narrative", "waterfall_narrative",
    "environmental_intro", "phase_esa_narrative", "climate_risk_narrative",
    "legal_status_narrative", "violations_narrative",
    "regulatory_approvals_narrative", "due_diligence_overview",
    "dd_checklist_intro", "timeline_narrative",
    "recommendation", "recommendation_one_line",
    "recommendation_narrative_p1", "recommendation_narrative_p2",
    "recommendation_pullquote",
    "risk_1", "risk_2", "risk_3",
    "conclusion_1", "conclusion_2", "conclusion_3", "conclusion_4", "conclusion_5",
    "bottom_line",
    "next_step_1", "next_step_2", "next_step_3",
    "next_step_4", "next_step_5", "next_step_6",
    "methodology_notes", "fema_flood_narrative", "construction_budget_narrative",
]

_USER_4MASTER_SUBSET = (
    "Generate a subset of report narrative sections for the deal below.\n"
    "Return ONLY a single JSON object containing EXACTLY these keys and no others:\n"
    "{keys_list}\n\n"
    "COMPLETE DEAL DATA:\n"
    "{deal_data_json}\n\n"
    "Return ONLY the JSON object."
)

# Legacy narrative keys that Prompt 4-MASTER no longer returns. Kept empty
# so the FALLBACK_NARRATIVES loop below is a no-op for these deprecated
# labels (the modern template reads exec_overview_p1/p2/p3, opportunity_1/2/3,
# risk_1/2/3, next_step_1..6 instead).
FALLBACK_NARRATIVES: dict[str, str] = {}


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
    logger.info(f"ANTHROPIC_API_KEY present: {bool(ANTHROPIC_API_KEY)}, length: {len(ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else 0}")
    client = anthropic.Anthropic(
        api_key=ANTHROPIC_API_KEY,
    )
    try:
        response = _claude_call(
            client,
            model=MODEL_SONNET,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(raw)
    except anthropic.AuthenticationError as auth_err:
        logger.error("SONNET AUTH ERROR (401): %s | key_prefix=%s",
                     auth_err, ANTHROPIC_API_KEY[:12])
        return None
    except anthropic.APIStatusError as status_err:
        logger.error("SONNET API STATUS ERROR: status=%s body=%s",
                     status_err.status_code, status_err.message)
        return None
    except (json.JSONDecodeError, anthropic.APIError, IndexError, KeyError) as exc:
        logger.error("SONNET CALL FAILED: %s | type=%s", exc, type(exc).__name__)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# NARRATIVE GENERATION
# ═══════════════════════════════════════════════════════════════════════════


def _apply_narrative_result(deal: DealData, result: dict) -> None:
    """Write any string-valued keys in `result` onto deal.narratives where
    a matching field exists. Used between 4-MASTER Part 1 and 4-REC so
    the specialist prompt sees the market context narratives that just
    ran (neighborhood_trend_narrative, supply_pipeline_narrative)."""
    narr = deal.narratives
    for key, value in result.items():
        if hasattr(narr, key) and isinstance(value, str):
            setattr(narr, key, value)


def _generate_narratives(deal: DealData) -> None:
    """Run Prompt 4-MASTER to populate all narrative fields on DealData."""
    logger.info("Running Prompt 4-MASTER — all report narratives...")

    # Pre-flight: full narrative context validation
    validate_and_build_narrative_context(deal)

    deal_json = deal.model_dump_json(indent=2)

    def _run_part(keys: list, label: str) -> dict:
        user_msg = _USER_4MASTER_SUBSET.format(
            keys_list=json.dumps(keys),
            deal_data_json=deal_json,
        )
        logger.info("NARRATIVE: calling Sonnet for %s (%d keys)...", label, len(keys))
        out = _call_sonnet(_SYSTEM_4MASTER, user_msg, max_tokens=8192)
        if out is None:
            logger.warning("Prompt 4-MASTER %s first attempt failed — retrying...", label)
            out = _call_sonnet(_SYSTEM_4MASTER, user_msg, max_tokens=8192)
        return out or {}

    part1 = _run_part(_4MASTER_KEYS_PART1, "Part 1 (executive/property/location/market)")

    # Phase-1 specialist-prompt split: when USE_4REC_SPECIALIST is on,
    # 4-MASTER Part 2 no longer generates the 8 Investment-Recommendation
    # keys. Those are produced by the dedicated _run_4rec() below,
    # merged into the same result dict used by the existing apply loop.
    part2_keys = list(_4MASTER_KEYS_PART2)
    if USE_4REC_SPECIALIST:
        part2_keys = [k for k in part2_keys if k not in _4REC_KEYS]
        logger.info("4-REC specialist enabled — removed %d rec keys from "
                    "Part 2 (%d keys remain)",
                    len(_4REC_KEYS), len(part2_keys))

    part2 = _run_part(part2_keys, "Part 2 (financial/risk/conclusion)")

    # Specialist call runs AFTER Part 1 (so neighborhood_trend_narrative +
    # supply_pipeline_narrative are on deal.narratives and can flow into
    # the 4-REC user-briefing). We apply Part 1 results to the model
    # now so 4-REC sees them.
    _apply_narrative_result(deal, part1)

    rec_payload: dict = {}
    rec_reasoning = ""
    if USE_4REC_SPECIALIST:
        rec_reasoning, rec_payload = _run_4rec(deal)
        if not rec_payload:
            # Fallback: re-run Part 2 with the rec keys restored so the
            # report isn't left with empty recommendation fields. Belt
            # and suspenders per the rollout plan.
            logger.warning("4-REC specialist returned empty — falling "
                           "back to 4-MASTER Part 2 with rec keys restored")
            fallback = _run_part(_4REC_KEYS,
                                 "Part 2 rec-keys fallback")
            rec_payload = fallback or {}

    result: dict = {}
    result.update(part1)
    result.update(part2)
    result.update(rec_payload)

    if not result:
        logger.error("Prompt 4-MASTER failed twice — narratives will be empty strings")
        return

    for key, fallback in FALLBACK_NARRATIVES.items():
        if not result.get(key):
            result[key] = fallback
            logger.warning("NARRATIVE FALLBACK: %s — using placeholder", key)

    # Log length for every narrative key actually returned by 4-MASTER, so
    # we can audit coverage per deal without spamming info lines for legacy
    # keys that were removed from the prompt.
    for _k in ("deal_thesis", "exec_overview_p1", "exec_overview_p2",
               "exec_overview_p3", "bottom_line"):
        _v = result.get(_k, "")
        _chars = len(_v) if isinstance(_v, str) else 0
        logger.info(f"NARRATIVE: {_k} returned {_chars} chars")

    # Apply all returned keys to the narratives model
    narr = deal.narratives
    for key, value in result.items():
        if hasattr(narr, key) and isinstance(value, str):
            setattr(narr, key, value)

    # Recommendation verdict + one-liner live on DealData directly (not narratives)
    rec_raw = (result.get("recommendation")
               or result.get("go_nogo_recommendation")
               or result.get("investment_recommendation") or "").strip().upper()
    rec_map = {
        "GO": RecommendationVerdict.GO,
        "CONDITIONAL GO": RecommendationVerdict.CONDITIONAL_GO,
        "CONDITIONAL-GO": RecommendationVerdict.CONDITIONAL_GO,
        "NO-GO": RecommendationVerdict.NO_GO,
        "NO GO": RecommendationVerdict.NO_GO,
        "NOGO": RecommendationVerdict.NO_GO,
    }
    if rec_raw in rec_map:
        deal.recommendation = rec_map[rec_raw]
    elif rec_raw:
        logger.warning("RECOMMENDATION: unrecognized verdict '%s' — leaving unset", rec_raw)

    rec_one = (result.get("recommendation_one_line")
               or result.get("recommendation_summary")
               or result.get("one_line_recommendation") or "").strip()
    if rec_one:
        deal.recommendation_one_line = rec_one

    logger.info("RECOMMENDATION: verdict=%s, one_line='%s' (len=%d)",
                deal.recommendation.value if deal.recommendation else "EMPTY",
                (deal.recommendation_one_line or "")[:60],
                len(deal.recommendation_one_line or ""))
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

_US_STATE_CODES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
}


def _title_case_address(raw: str) -> str:
    """Title-case an address string while preserving US state codes ("PA")
    and ordinal suffixes ("46th", "1st"). Python's str.title() alone turns
    "PA" into "Pa" and "46th" into "46Th" — this fixes both.
    """
    if not raw:
        return ""
    import re as _re
    s = raw.title()
    # Ordinals: "46Th" → "46th", "21St" → "21st", etc.
    s = _re.sub(r"(\d)(St|Nd|Rd|Th)\b",
                lambda m: m.group(1) + m.group(2).lower(), s)
    # 2-letter state codes back to uppercase when they correspond to real states.
    def _fix_state(m):
        w = m.group(0)
        return w.upper() if w.upper() in _US_STATE_CODES else w
    s = _re.sub(r"\b[A-Z][a-z]\b", _fix_state, s)
    return s


# ── Session 5 zoning-overhaul context helpers ────────────────────────────────
# Per Session 5 kickoff §3.1, build_context() exposes structured zoning data
# to the §8/§9 template components (badge, dimension grid, scenario cards, HBU
# synthesis). All values have safe fallbacks so the template never KeyErrors.

_PATHWAY_CSS_CLASS = {
    # Per kickoff §5.3 + Phase-2 mapping decision D11:
    # CONDITIONAL_USE folds into .pathway-special (functionally equivalent risk tier);
    # REZONE folds into .pathway-variance (same discretionary-approval risk tier).
    "BY_RIGHT":          "pathway-byright",
    "CONDITIONAL_USE":   "pathway-special",
    "SPECIAL_EXCEPTION": "pathway-special",
    "VARIANCE":          "pathway-variance",
    "REZONE":            "pathway-variance",
}

_BADGE_CSS_CLASS = {
    # Per kickoff §5.1 + Phase-2 mapping decision B7:
    # All LEGAL_NONCONFORMING_* axes + MULTIPLE_NONCONFORMITIES collapse to amber;
    # ILLEGAL_NONCONFORMING is the strict-red badge; INDETERMINATE is gray-pending.
    "CONFORMING":                      "badge-conforming",
    "LEGAL_NONCONFORMING_USE":         "badge-legal",
    "LEGAL_NONCONFORMING_DENSITY":     "badge-legal",
    "LEGAL_NONCONFORMING_DIMENSIONAL": "badge-legal",
    "MULTIPLE_NONCONFORMITIES":        "badge-legal",
    "ILLEGAL_NONCONFORMING":           "badge-nonconforming",
    "CONFORMITY_INDETERMINATE":        "badge-pending",
}


def _build_conformity_context(deal: DealData) -> dict:
    """Build the context.conformity dict for §8 (badge + dimension grid).

    Returns a dict with status, zoning_code, district_name, dimensions, and a
    derived badge_css_class. Empty/safe defaults when conformity_assessment is
    None — the template renders an ASSESSMENT-PENDING badge in that case.
    """
    ca = deal.conformity_assessment
    zoning_code = (deal.zoning.zoning_code or "") if deal.zoning else ""
    district_name = (deal.zoning.zoning_district or "") if deal.zoning else ""

    if ca is None:
        return {
            "status": "CONFORMITY_INDETERMINATE",
            "zoning_code": zoning_code,
            "district_name": district_name,
            "dimensions": [],
            "badge_css_class": _BADGE_CSS_CLASS["CONFORMITY_INDETERMINATE"],
        }

    status_value = ca.status.value if ca.status else "CONFORMITY_INDETERMINATE"
    return {
        "status": status_value,
        "zoning_code": zoning_code,
        "district_name": district_name,
        "dimensions": [
            {
                "label":     item.standard_description or item.nonconformity_type.value,
                "actual":    item.actual_value,
                "permitted": item.permitted_value,
                "status":    "fail",
            }
            for item in (ca.nonconformity_details or [])
        ],
        "badge_css_class": _BADGE_CSS_CLASS.get(status_value, "badge-pending"),
    }


def _build_zoning_ext_context(deal: DealData) -> dict:
    """Build the context.zoning_ext dict for §8 HBU synthesis + §9 narrative.

    Per Phase-2 mapping decisions C8/C9: use_flexibility_score is a flat int
    on the 1-5 scale (NOT a UseFlexibilityScore object with a .score sub-attr).
    Empty/safe defaults when zoning_extensions is None.
    """
    ze = deal.zoning_extensions
    if ze is None:
        return {
            "cross_scenario_recommendation": "",
            "preferred_scenario_id":         None,
            "use_flexibility_score":         None,
        }
    return {
        "cross_scenario_recommendation": ze.cross_scenario_recommendation or "",
        "preferred_scenario_id":         ze.preferred_scenario_id or None,
        "use_flexibility_score":         ze.use_flexibility_score,
    }


def _annotate_scenarios_with_pathway_class(scenarios) -> list:
    """Attach a `pathway_css_class` attribute to each scenario for template use.

    Per Phase-2 mapping decision D11: when scenario.zoning_pathway is None, emit
    .pathway-submitted (the as_submitted-fallback case). Otherwise look up the
    enum value in _PATHWAY_CSS_CLASS. Returns the original list (mutates each
    scenario object via setattr — safe because scenarios are Pydantic models
    and the attribute is read-only template metadata, not a schema field).
    """
    for s in scenarios or []:
        if s.zoning_pathway is None:
            css = "pathway-submitted"
        else:
            pathway_value = s.zoning_pathway.pathway_type.value
            css = _PATHWAY_CSS_CLASS.get(pathway_value, "pathway-submitted")
        # Pydantic v2 models permit attribute set unless strict; we set a
        # template-only attr that the schema validator never sees.
        try:
            object.__setattr__(s, "pathway_css_class", css)
        except Exception:
            # Defensive: if the model is frozen, fall back to silent skip.
            # Template will render without the class (defaulting to base styling).
            pass
    return list(scenarios or [])


def build_context(deal: DealData) -> dict:
    """Build the full template context dict from DealData for the HTML report."""
    narr = deal.narratives
    a = deal.assumptions
    fo = deal.financial_outputs
    md = deal.market_data
    ins = deal.insurance
    ext = deal.extracted_docs

    suppressed = deal.suppressed_sections

    ctx = {}

    # Cover page — title-case the address portion so the header reads
    # "Investment Underwriting Report — 2-8 S. 46th Street, Philadelphia, PA, 19139"
    # regardless of whether upstream delivered the address all-lowercase.
    _raw_full_addr = deal.address.full_address or ""
    _tc_full_addr = _title_case_address(_raw_full_addr)
    # Street-only form for the cover — strip the ", City, State, Zip" tail
    # so the .cover-address line doesn't duplicate what .cover-city renders.
    # Uses deal.address.street when available; else splits full_address on
    # the first comma. Falls back to the full string if neither works.
    _street_only = _title_case_address(
        (deal.address.street or "").strip()
    )
    if not _street_only and "," in _tc_full_addr:
        _street_only = _tc_full_addr.split(",", 1)[0].strip()
    if not _street_only:
        _street_only = _tc_full_addr
    _cover_prefix = ("Investment Summary" if deal.investor_mode
                     else "Investment Underwriting Report")
    ctx["cover_title"] = f"{_cover_prefix} — {_tc_full_addr}" if _tc_full_addr else _cover_prefix
    ctx["report_date"] = deal.report_date or ""
    ctx["deal_id"] = deal.deal_id or ""
    ctx["deal_code"] = deal.deal_code or ""
    ctx["sponsor_name"] = deal.sponsor_name or "DealDesk"
    ctx["sponsor_description"] = deal.sponsor_description

    # DealDesk logo (base64 data URI, loaded once from templates/dealdesk_logo.txt)
    try:
        from pathlib import Path as _P
        logo_path = _P(__file__).resolve().parent / "templates" / "dealdesk_logo.txt"
        if logo_path.exists():
            ctx["dealdesk_logo_uri"] = logo_path.read_text(encoding="utf-8").strip()
    except Exception as _exc:
        logger.debug("DealDesk logo load failed: %s", _exc)

    # Property basics — title-case the address for display
    ctx["property_name"] = ext.property_name or _tc_full_addr
    ctx["full_address"] = _tc_full_addr
    ctx["cover_street_address"] = _street_only
    ctx["city"] = deal.address.city
    ctx["state"] = deal.address.state
    ctx["zip_code"] = deal.address.zip_code
    # New Google Maps enrichment context
    ctx["validated_address"] = getattr(deal.address, "validated_address", None) or ""
    ctx["elevation_feet"]    = getattr(deal.address, "elevation_feet", None)
    ctx["elevation_meters"]  = getattr(deal.address, "elevation_meters", None)
    ctx["poi_summary"]       = getattr(deal, "poi_summary", {}) or {}
    ctx["commercial_density"] = getattr(deal, "commercial_density", {}) or {}
    ctx["nearby_pois"]       = getattr(deal, "nearby_pois", []) or []

    # ── Section 08 key takeaways — bullet summaries ─────────────────────
    # Derived from market_data + commercial_density + POI summary. Each
    # bullet is data-driven so the reader can scan the trend call-outs
    # without re-reading the narrative.
    _md = deal.market_data
    _bullets = []
    if _md:
        if _md.population_3mi:
            _pop = _md.population_3mi
            if _pop >= 250_000:
                _bullets.append(f"Dense population base — {_pop:,} residents within 3 miles (urban core density).")
            elif _pop >= 100_000:
                _bullets.append(f"Substantial population base — {_pop:,} residents within 3 miles.")
            elif _pop >= 25_000:
                _bullets.append(f"Moderate population base — {_pop:,} residents within 3 miles.")
            else:
                _bullets.append(f"Thin population base — {_pop:,} residents within 3 miles (suburban / exurban profile).")
        if _md.median_hh_income_3mi:
            _inc = _md.median_hh_income_3mi
            _natl_med = 75_000   # approx US median HH income
            _delta = (_inc - _natl_med) / _natl_med
            _cmp = (
                "well above" if _delta > 0.25 else
                "above"      if _delta > 0.10 else
                "near"       if abs(_delta) <= 0.10 else
                "below"      if _delta > -0.25 else
                "well below"
            )
            _bullets.append(
                f"Median household income ${_inc:,.0f} (3-mile) — {_cmp} the ~$75K US median."
            )
        if _md.pct_renter_occ_3mi is not None:
            _r = _md.pct_renter_occ_3mi
            _renter_label = (
                "renter-dominated" if _r > 0.60 else
                "balanced tenure"  if 0.40 <= _r <= 0.60 else
                "owner-dominated"
            )
            _bullets.append(
                f"{_r * 100:.0f}% renter-occupied households in the 3-mile ring — {_renter_label} tenure mix."
            )
        if _md.unemployment_rate:
            _u = _md.unemployment_rate * 100 if _md.unemployment_rate < 1 else _md.unemployment_rate
            _bullets.append(f"Submarket unemployment rate ≈ {_u:.1f}%.")
    # Commercial density bullet from Places API
    _cd = getattr(deal, "commercial_density", None) or {}
    if _cd.get("density_label"):
        _bullets.append(
            f"Amenity density: {_cd['density_label']} ({_cd.get('total_amenities', 0)} POIs within 1 mile — "
            f"{_cd.get('food_and_beverage', 0)} F&B, {_cd.get('transit_access_score', 0)} transit stops, "
            f"{_cd.get('grocery_count', 0)} grocery, {_cd.get('school_count', 0)} schools)."
        )
    # OZ / flood-zone bullets
    if ctx.get("is_opportunity_zone"):
        _bullets.append("Property tract is a federally-designated Opportunity Zone — capital-gains incentives apply.")
    if _md and _md.fema_flood_zone:
        _zone = str(_md.fema_flood_zone).upper()
        if _zone.startswith(("A", "V")):
            _bullets.append(
                f"Located within FEMA Special Flood Hazard Area (Zone {_md.fema_flood_zone}) — "
                f"flood insurance required and loading applied to P&C premium."
            )
        else:
            _bullets.append(
                f"FEMA Zone {_md.fema_flood_zone} — outside the Special Flood Hazard Area (no mandatory flood insurance)."
            )
    ctx["neighborhood_trend_bullets"] = _bullets
    logger.info("SECTION 08: %d neighborhood-trend bullet takeaways built", len(_bullets))
    ctx["asset_type"] = deal.asset_type.value
    ctx["investment_strategy"] = deal.investment_strategy.value
    ctx["asking_price"] = f"${deal.assumptions.purchase_price:,.0f}" if deal.assumptions.purchase_price else "Not disclosed"
    ctx["purchase_price"] = a.purchase_price
    ctx["num_units"] = a.num_units
    _bsf = a.gba_sf or 0
    ctx["building_sf"] = f"{_bsf:,.0f} SF" if _bsf else "Not provided"
    logger.info("COVER: building_sf=%s (raw gba_sf=%s)", ctx["building_sf"], a.gba_sf)
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

    # Financial outputs — fo.total_uses is the single source of truth
    ctx["total_uses"] = fo.total_uses
    ctx["total_project_cost"] = fo.total_uses
    ctx["total_sources"] = fo.total_uses  # sources must equal uses
    logger.info("TPC: using fo.total_uses=%s as single source of truth",
                f"{fo.total_uses:,.2f}")
    ctx["total_equity_required"] = fo.total_equity_required
    ctx["initial_loan_amount"] = fo.initial_loan_amount
    ctx["noi_yr1"] = fo.noi_yr1
    ctx["dscr_yr1"] = fo.dscr_yr1
    ctx["going_in_cap_rate"] = fo.going_in_cap_rate
    ctx["lp_irr"] = fo.lp_irr if fo.lp_irr is not None else "Not calculable (negative NOI)"
    ctx["gp_irr"] = fo.gp_irr if fo.gp_irr is not None else "Not calculable (negative NOI)"
    ctx["project_irr"] = fo.project_irr if fo.project_irr is not None else "Not calculable (negative NOI)"
    ctx["lp_equity_multiple"] = fo.lp_equity_multiple
    ctx["gp_equity_multiple"] = fo.gp_equity_multiple
    ctx["cash_on_cash_yr1"] = fo.cash_on_cash_yr1
    ctx["gross_sale_price"] = fo.gross_sale_price
    ctx["net_sale_proceeds"] = fo.net_sale_proceeds
    # Normalize None → [] so Prompt 4-MASTER's matrix-state branches don't
    # hit an unhandled None. An empty list is the well-defined "sensitivity
    # not computed" signal; None previously caused narrative hallucination.
    ctx["sensitivity_matrix"] = fo.sensitivity_matrix or []
    ctx["pro_forma_years"] = fo.pro_forma_years or []

    # ── Gantt chart phases for Section 18 ───────────────────────────
    _const_m  = int(getattr(a, "const_period_months", 0) or 0)
    _leaseup_m = int(getattr(a, "lease_up_months", 1) or 1) * max(1, (a.num_units or 1) // 4)
    _hold_m   = int((a.hold_period or 10) * 12)
    _total_m  = max(_hold_m, _const_m + _leaseup_m + 12)
    ctx["gantt_total_months"] = _total_m

    def _pct(month: int) -> float:
        return round(100.0 * month / _total_m, 2) if _total_m else 0.0

    _gantt_phases = []
    # Acquisition (spike at month 0)
    _gantt_phases.append({
        "name": "Acquisition / Closing",
        "duration_label": "Month 0",
        "start_pct": 0,
        "width_pct": max(1.5, _pct(1)),
        "kind":     "acquisition",
        "bar_label": "Close",
    })
    # Construction / renovation (when applicable)
    if _const_m > 0:
        _gantt_phases.append({
            "name": "Construction / Renovation",
            "duration_label": f"{_const_m} mo",
            "start_pct": _pct(0),
            "width_pct": max(1.5, _pct(_const_m)),
            "kind":     "construction",
            "bar_label": f"{_const_m}-month capex",
        })
    # Lease-up
    _leaseup_start = _const_m if _const_m > 0 else 0
    if _leaseup_m > 0:
        _gantt_phases.append({
            "name": "Lease-Up",
            "duration_label": f"~{_leaseup_m} mo",
            "start_pct": _pct(_leaseup_start),
            "width_pct": max(1.5, _pct(_leaseup_m)),
            "kind":     "leaseup",
            "bar_label": "Units to market",
        })
    # Stabilized operations — from end of leaseup to first refi (or exit)
    _stab_start = _leaseup_start + _leaseup_m
    _first_refi_m = None
    for rev in (a.refi_events or []):
        if getattr(rev, "active", False) and getattr(rev, "year", 0):
            _first_refi_m = int(rev.year) * 12
            break
    _stab_end = _first_refi_m if _first_refi_m and _first_refi_m < _hold_m else _hold_m
    if _stab_end > _stab_start:
        _gantt_phases.append({
            "name": "Stabilized Operations",
            "duration_label": f"{_stab_end - _stab_start} mo",
            "start_pct": _pct(_stab_start),
            "width_pct": max(1.5, _pct(_stab_end - _stab_start)),
            "kind":     "stabilized",
            "bar_label": "Hold",
        })
    # Refi events
    for idx, rev in enumerate(a.refi_events or []):
        if not getattr(rev, "active", False):
            continue
        _m = int(getattr(rev, "year", 0)) * 12
        if _m <= 0 or _m > _total_m:
            continue
        _gantt_phases.append({
            "name": f"Refinance #{idx + 1}",
            "duration_label": f"Year {rev.year}",
            "start_pct": _pct(_m),
            "width_pct": max(1.2, _pct(1)),
            "kind":     "refi",
            "bar_label": f"Refi Y{rev.year}",
        })
    # Exit
    _gantt_phases.append({
        "name": "Disposition / Exit",
        "duration_label": f"Month {_hold_m}",
        "start_pct": _pct(_hold_m - 1),
        "width_pct": max(1.5, _pct(1)),
        "kind":     "exit",
        "bar_label": "Sale",
    })
    ctx["gantt_phases"] = _gantt_phases

    # Month markers along the top axis — annual ticks + half-years for
    # schedules ≤ 3 years
    _tick_step = 12 if _total_m > 36 else 6
    ctx["gantt_month_markers"] = [
        {"month": m, "pct": _pct(m)}
        for m in range(0, _total_m + 1, _tick_step)
    ]
    logger.info("GANTT: %d phases across %d months", len(_gantt_phases), _total_m)

    # Insurance — surface the structured data from risk.py (Prompt 4B)
    # for the report's Property Insurance subsection. Prior revision
    # stringified kpi_strip / summary_table to "" which silenced the
    # entire block.
    ctx["insurance_narrative_p1"] = ins.insurance_narrative_p1 or ""
    ctx["insurance_narrative_p2"] = ins.insurance_narrative_p2 or ""
    ctx["insurance_narrative_p3"] = ins.insurance_narrative_p3 or ""

    # KPI strip (6 metrics) — pre-format dollar / yes-no / flag values
    # so the template just emits cell text.
    _kpi_src = ins.insurance_kpi_strip or {}
    def _ins_money(v):
        try:
            f = float(v or 0)
        except (TypeError, ValueError):
            return "—"
        return f"${f:,.0f}" if f else "—"
    def _ins_bool(v):
        if v in (None, ""):
            return "—"
        if isinstance(v, bool):
            return "Yes" if v else "No"
        s = str(v).strip().lower()
        if s in ("true", "yes", "required", "1"):
            return "Yes"
        if s in ("false", "no", "not required", "0"):
            return "No"
        return str(v)
    def _ins_text(v):
        return str(v) if v not in (None, "") else "—"

    ctx["insurance_kpi_rows"] = [
        {"label": "FEMA Flood Zone",              "value": _ins_text(_kpi_src.get("flood_zone"))},
        {"label": "Flood Insurance Required",     "value": _ins_bool(_kpi_src.get("flood_insurance_required"))},
        {"label": "Est. Property Insurance / Yr", "value": _ins_money(_kpi_src.get("est_property_insurance_annual"))},
        {"label": "Est. Flood Insurance / Yr",    "value": _ins_money(_kpi_src.get("est_flood_insurance_annual"))},
        {"label": "Est. Total Insurance / Yr",    "value": _ins_money(_kpi_src.get("est_total_insurance_annual"))},
        {"label": "Coverage Gaps Flagged",        "value": _ins_text(_kpi_src.get("coverage_gaps_flagged"))},
    ]
    ctx["insurance_kpi_strip"] = _kpi_src  # raw dict for downstream callers

    # Summary table (one row per coverage type) — pre-format cost and
    # required fields so the template uses consistent display.
    _rows = []
    for row in (ins.insurance_summary_table or []):
        if not isinstance(row, dict):
            continue
        _rows.append({
            "coverage_type": _ins_text(row.get("coverage_type")),
            "required":      _ins_bool(row.get("required")),
            "est_annual_cost": _ins_money(row.get("est_annual_cost")),
            "notes":          _ins_text(row.get("notes")),
            "flag":           _ins_text(row.get("flag")),
        })
    ctx["insurance_summary_table"] = _rows
    ctx["has_insurance_analysis"] = bool(
        _rows
        or ins.insurance_narrative_p1
        or ins.insurance_narrative_p2
        or ins.insurance_narrative_p3
        or (_kpi_src and any(v not in (None, "") for v in _kpi_src.values()))
    )
    logger.info(
        "INSURANCE CTX: narratives=%s kpi_keys=%d table_rows=%d proforma=$%s",
        sum(1 for n in (ins.insurance_narrative_p1, ins.insurance_narrative_p2,
                        ins.insurance_narrative_p3) if n),
        len(_kpi_src), len(_rows),
        f"{ins.insurance_proforma_line_item or 0:,.0f}",
    )

    # DD Flags
    ctx["dd_flags"] = [f.model_dump() for f in deal.dd_flags]

    # DD Checklist — a standard CRE due diligence tracker. Status is derived
    # from upstream pipeline data where we have signal; everything else
    # defaults to "Pending" so the checklist reads as an action list.
    _md = deal.market_data
    _pd = deal.parcel_data
    _ext = deal.extracted_docs
    _comps = deal.comps
    _is_value_add = deal.investment_strategy.value.lower() in ("value_add", "value-add", "development", "ground_up")

    def _status(done, in_progress=False, na=False):
        if na:
            return "N/A"
        if done:
            return "Complete"
        if in_progress:
            return "In Progress"
        return "Pending"

    _checklist_items = [
        # Property / Physical
        ("Property", "Property Condition Assessment (PCA)", _status(False), "Engineering / Owner"),
        ("Property", "Roof & HVAC inspection", _status(False), "Owner / Vendor"),
        ("Property", "Pest & termite inspection", _status(False), "Vendor"),
        ("Property", "ADA accessibility survey", _status(False), "Owner / Counsel"),
        ("Property", "Floor plans & as-built drawings", _status(bool(_ext and _ext.image_placements)), "Owner"),

        # Financial / Operational
        ("Financial", "T-12 operating statements",
         _status(bool(_ext and _ext.noi_t12 is not None)), "Owner / Accounting"),
        ("Financial", "Rent roll (current, dated)",
         _status(bool(_ext and _ext.unit_mix)), "Owner / Property Mgmt"),
        ("Financial", "Operating expense backup / invoices", _status(False), "Owner"),
        ("Financial", "Tax returns (property, 2 years)", _status(False), "Owner / Accounting"),
        ("Financial", "CAM reconciliations",
         _status(bool(_ext and _ext.cam_reimbursements_t12), na=not _ext or not _ext.unit_mix),
         "Owner / Property Mgmt"),
        ("Financial", "Utility bills (12 months)", _status(False), "Owner"),

        # Legal / Title
        ("Legal", "Title commitment & exceptions",
         _status(bool(_pd and _pd.parcel_id), in_progress=bool(_pd and _pd.parcel_id)),
         "Title Co. / Counsel"),
        ("Legal", "Deed / chain of title review",
         _status(bool(_pd and _pd.deed_history), in_progress=bool(_pd and _pd.parcel_id)),
         "Title Co. / Counsel"),
        ("Legal", "Recorded survey (ALTA/NSPS)", _status(False), "Surveyor"),
        ("Legal", "Existing lease abstracts & estoppels",
         _status(False, in_progress=bool(_ext and _ext.unit_mix)), "Counsel / Property Mgmt"),
        ("Legal", "SNDAs on material tenants", _status(False), "Counsel"),
        ("Legal", "Litigation & judgment search", _status(False), "Counsel"),
        ("Legal", "UCC / lien search",
         _status(False, in_progress=bool(_pd and _pd.parcel_id)), "Title Co. / Counsel"),

        # Environmental
        ("Environmental", "Phase I Environmental Site Assessment", _status(False), "Env. Consultant"),
        ("Environmental", "Phase II ESA (if recommended)",
         _status(False, na=not (_md and _md.epa_env_flags)), "Env. Consultant"),
        ("Environmental", "FEMA flood zone determination",
         _status(bool(_md and _md.fema_flood_zone)), "Pipeline / Surveyor"),
        ("Environmental", "EPA environmental flags reviewed",
         _status(bool(_md and _md.epa_env_flags is not None)), "Underwriting"),
        ("Environmental", "Radon / mold / asbestos screening", _status(False), "Env. Consultant"),

        # Zoning / Permits
        ("Zoning", "Zoning verification letter",
         _status(bool(deal.zoning and deal.zoning.source_verified),
                 in_progress=bool(deal.zoning and deal.zoning.zoning_code)),
         "Counsel / Municipality"),
        ("Zoning", "Certificate of Occupancy", _status(False), "Owner / Municipality"),
        ("Zoning", "Open code violations / permits review", _status(False), "Municipality / Counsel"),
        ("Zoning", "Buildable capacity analysis",
         _status(bool(deal.zoning and deal.zoning.buildable_capacity_narrative)),
         "Underwriting"),

        # Insurance
        ("Insurance", "Property & casualty insurance quote",
         _status(bool(deal.insurance and deal.insurance.insurance_proforma_line_item)),
         "Insurance Broker"),
        ("Insurance", "Loss run history (5 years)", _status(False), "Owner / Carrier"),
        ("Insurance", "Flood insurance (if in SFHA)",
         _status(False,
                 na=(_md and _md.fema_flood_zone and _md.fema_flood_zone.upper().startswith("X"))),
         "Insurance Broker"),

        # Market
        ("Market", "Rent comparables study",
         _status(bool(_comps and _comps.rent_comps)), "Broker / Appraiser"),
        ("Market", "Sales comparables study",
         _status(bool(_comps and _comps.sale_comps)), "Broker / Appraiser"),
        ("Market", "Appraisal (MAI)", _status(False), "Appraiser"),
        ("Market", "Submarket supply pipeline",
         _status(bool(_md and _md.supply_pipeline_narrative)), "Underwriting"),
    ]

    if _is_value_add:
        _checklist_items.extend([
            ("Construction", "Architect / engineering drawings", _status(False), "Architect"),
            ("Construction", "GC bid / GMP contract",             _status(False), "General Contractor"),
            ("Construction", "Construction budget with 5–10% contingency", _status(False), "Development"),
            ("Construction", "Construction schedule / critical path",       _status(False), "Development"),
            ("Construction", "Permit pull plan & timeline",                 _status(False), "Architect / Municipality"),
        ])

    ctx["dd_checklist_rows"] = [
        {"category": c, "item": i, "status": s, "owner": o}
        for c, i, s, o in _checklist_items
    ]
    _by_status = defaultdict(int)
    for _, _, s, _ in _checklist_items:
        _by_status[s] += 1
    logger.info(
        "DD CHECKLIST: %d items — Complete=%d In Progress=%d Pending=%d N/A=%d",
        len(_checklist_items),
        _by_status["Complete"], _by_status["In Progress"],
        _by_status["Pending"], _by_status["N/A"],
    )

    # Recommendation — populated by Prompt 4-MASTER into deal.recommendation
    ctx["recommendation"] = deal.recommendation.value if deal.recommendation else ""
    ctx["recommendation_one_line"] = deal.recommendation_one_line or ""
    logger.info("RECOMMENDATION: '%s' (len=%d)",
                ctx["recommendation"][:50] if ctx["recommendation"] else "EMPTY",
                len(ctx["recommendation"]))

    # Waterfall
    ctx["pref_return"] = a.pref_return
    ctx["gp_equity_pct"] = a.gp_equity_pct
    ctx["lp_equity_pct"] = a.lp_equity_pct
    ctx["waterfall_tiers"] = [t.model_dump() for t in a.waterfall_tiers]

    # GP/LP equity dollar amounts — read from financial_outputs (not recomputed)
    ctx["initial_loan"] = fo.initial_loan_amount
    ctx["total_equity"] = fo.total_equity_required
    ctx["gp_equity"] = fo.gp_equity
    ctx["lp_equity"] = fo.lp_equity
    ctx["equity_gap"] = fo.total_equity_required
    ctx["annual_ds"] = fo.debt_service_annual
    logger.info("NARRATIVE CTX: loan=%s equity=%s gp=%s lp=%s ds=%s",
        f"{fo.initial_loan_amount:,.2f}",
        f"{fo.total_equity_required:,.2f}",
        f"{fo.gp_equity:,.2f}",
        f"{fo.lp_equity:,.2f}",
        f"{fo.debt_service_annual:,.2f}")

    # Extracted doc data
    ctx["unit_mix"] = ext.unit_mix or []
    ctx["occupancy_rate"] = ext.occupancy_rate
    ctx["image_placements"] = ext.image_placements or {}

    # Photos extracted from the uploaded OM PDF. Encode as base64 so the
    # Playwright renderer doesn't need file:// URLs (which it blocks in
    # some configurations). Cap at 12 so the gallery page stays paginated
    # cleanly.
    # Downscale each photo to max 1200px on the long edge and JPEG-encode
    # at quality 82 before base64-embedding. PyMuPDF extracts PNGs at full
    # source resolution (often 3-5 MB each), and embedding 12 of them
    # raw produced a 22 MB PDF in prior runs. JPEG at 1200px / q=82 yields
    # ~150–300 KB per image at no visible quality loss for an 8.5×11 PDF.
    from io import BytesIO
    try:
        from PIL import Image
    except ImportError:
        Image = None

    _pdf_photos_b64: List[Dict[str, str]] = []
    for _idx, _p in enumerate(ext.pdf_photo_paths[:12]):
        try:
            if Image is not None:
                with Image.open(_p) as _img:
                    _img = _img.convert("RGB")
                    _img.thumbnail((1200, 1200), Image.LANCZOS)
                    _buf = BytesIO()
                    _img.save(_buf, format="JPEG", quality=82, optimize=True)
                    _bytes = _buf.getvalue()
                _mime = "image/jpeg"
            else:
                # PIL unavailable — fall back to raw PNG embed (accepts size cost).
                with open(_p, "rb") as _fh:
                    _bytes = _fh.read()
                _mime = "image/png"
            _b = base64.b64encode(_bytes).decode("ascii")
            _pdf_photos_b64.append({
                "src": f"data:{_mime};base64,{_b}",
                "caption": f"Figure 2.{_idx + 2} — Property photograph from offering memorandum",
            })
        except Exception as _pe:
            logger.warning("PDF photo encode failed for %s: %s", _p, _pe)
    ctx["pdf_photos"] = _pdf_photos_b64
    logger.info("PDF photos: %d embedded, total base64 payload ~%d KB",
                len(_pdf_photos_b64),
                sum(len(p["src"]) for p in _pdf_photos_b64) // 1024)

    # Provenance
    ctx["provenance"] = deal.provenance.model_dump()

    # Data-quality banner — renders on the cover/exec summary when more
    # than a few external data sources failed, so the reader knows the
    # report contains fallback-derived numbers.
    _failed = deal.provenance.failed_sources or []
    ctx["data_quality_failed"] = _failed
    ctx["data_quality_degraded"] = len(_failed) >= 3
    if _failed:
        logger.info("DATA QUALITY CTX: %d failed source(s) — %s",
                    len(_failed), [f.get("service") for f in _failed])


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
            "property_type": c.asset_type or "",
            "sf":            f"{c.sq_ft:,}" if c.sq_ft else "",
            "units":         str(c.num_units) if c.num_units else "",
            "sale_price":    f"${c.sale_price:,.0f}" if c.sale_price else "",
            "price_per_sf":  f"${c.price_per_sf:.0f}" if c.price_per_sf else "",
            "cap_rate":      f"{c.cap_rate:.2%}" if c.cap_rate else "",
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
    # Text fields: only None/"" is missing. Numeric dimensions: None is
    # missing, but 0 is a legitimate value (0-ft setbacks in row-house
    # districts, 0 min parking in TOD overlays, etc.) so we must not
    # render numeric zero as "—".
    def _zs_text(val):
        return str(val) if val not in (None, "") else "—"

    def _zs_num(val, suffix=""):
        if val is None or val == "":
            return "—"
        try:
            fv = float(val)
        except (TypeError, ValueError):
            return str(val)
        # Integers render without trailing .0
        if fv.is_integer():
            return f"{int(fv)}{suffix}"
        return f"{fv}{suffix}"

    ctx["zoning_standards_rows"] = [
        {"parameter": "Zoning District",   "standard": _zs_text(z.zoning_code),       "proposed": "", "code_section": ""},
        {"parameter": "District Name",      "standard": _zs_text(z.zoning_district),   "proposed": "", "code_section": ""},
        {"parameter": "Max Height (ft)",    "standard": _zs_num(z.max_height_ft),      "proposed": "", "code_section": ""},
        {"parameter": "Max Stories",        "standard": _zs_num(z.max_stories),        "proposed": "", "code_section": ""},
        {"parameter": "Min Lot Area (SF)",  "standard": _zs_num(z.min_lot_area_sf),    "proposed": "", "code_section": ""},
        {"parameter": "Max Lot Coverage",
         "standard": (f"{z.max_lot_coverage_pct:.0%}" if z.max_lot_coverage_pct is not None else "—"),
         "proposed": "", "code_section": ""},
        {"parameter": "Max FAR",            "standard": _zs_num(z.max_far),            "proposed": "", "code_section": ""},
        {"parameter": "Front Setback (ft)", "standard": _zs_num(z.front_setback_ft),   "proposed": "", "code_section": ""},
        {"parameter": "Rear Setback (ft)",  "standard": _zs_num(z.rear_setback_ft),    "proposed": "", "code_section": ""},
        {"parameter": "Side Setback (ft)",  "standard": _zs_num(z.side_setback_ft),    "proposed": "", "code_section": ""},
        {"parameter": "Min Parking Spaces", "standard": _zs_num(z.min_parking_spaces), "proposed": "", "code_section": ""},
    ]
    _populated = sum(1 for r in ctx["zoning_standards_rows"] if r["standard"] != "—")
    # Source provenance note — surfaces whether standards came from the
    # actual municipal code (verified) or LLM training knowledge (inferred).
    if z.source_verified:
        ctx["zoning_standards_note"] = (
            f"Source: municipal code scrape{' — ' + z.source_notes if z.source_notes else ''}"
        )
    elif z.source_notes:
        ctx["zoning_standards_note"] = (
            "Standards inferred from LLM training knowledge — the municipal "
            "code scrape was blocked and an authoritative lookup was not "
            "possible. Verify every dimensional value against the current "
            f"{deal.address.city or 'municipal'} zoning code prior to "
            "reliance. Source note: " + z.source_notes
        )
    else:
        ctx["zoning_standards_note"] = ""
    logger.info("ZONING STANDARDS: %d / %d rows populated (source_verified=%s)",
                _populated, len(ctx["zoning_standards_rows"]),
                bool(z.source_verified))
    # Permitted Uses — split the flat list into rendering buckets so the
    # template can group "By Right / Conditional / Special / Accessory".
    # Splitting logic: LLM may label each use inline ("X (conditional)") or
    # return separate list fields (permitted_uses_by_right etc.). We
    # normalize both shapes here.
    def _split_uses(raw_list):
        by_right, conditional, special, accessory = [], [], [], []
        for u in (raw_list or []):
            if not isinstance(u, str):
                continue
            s = u.strip()
            if not s:
                continue
            low = s.lower()
            if ("special exception" in low or "by special" in low):
                # strip the marker so the bullet reads cleanly
                special.append(re.sub(r"\s*\(.*?\)\s*$", "", s))
            elif ("conditional" in low or "by conditional" in low):
                conditional.append(re.sub(r"\s*\(.*?\)\s*$", "", s))
            elif ("accessory" in low):
                accessory.append(re.sub(r"\s*\(.*?\)\s*$", "", s))
            else:
                by_right.append(s)
        return by_right, conditional, special, accessory

    _by_right, _cond, _spec, _acc = _split_uses(z.permitted_uses)
    # Additionally pick up explicitly-bucketed lists from _apply_3a if the
    # LLM returned them.
    if z.conditional_uses:
        _cond = list({*(_cond or []), *(z.conditional_uses or [])})

    ctx["permitted_uses_by_right"]    = _by_right
    ctx["permitted_uses_conditional"] = _cond
    ctx["permitted_uses_special"]     = _spec
    ctx["permitted_uses_accessory"]   = _acc
    # Keep the flat description for any legacy consumers
    ctx["permitted_uses_description"] = (
        ", ".join(z.permitted_uses) if z.permitted_uses else ""
    )

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

    # ── Pro forma rows — raw numeric dicts for Section 12.4 ──────
    ctx["pro_forma_rows"] = [
        {
            "year":     yr.get("year", ""),
            "gpr":      yr.get("gpr", 0) or 0,
            "egr":      yr.get("egi", 0) or 0,
            "opex":     yr.get("opex", 0) or 0,
            "noi":      yr.get("noi", 0) or 0,
            "debt_svc": yr.get("debt_service", 0) or 0,
            "cfbt":     yr.get("fcf", 0) or 0,
            "coc":      yr.get("cash_on_cash", 0) or 0,
            "dscr":     round((yr.get("noi", 0) or 0) / ds, 2)
                        if (ds := yr.get("debt_service", 0) or 0) > 0
                        else 0,
        }
        for yr in (fo.pro_forma_years or [])
    ]

    # ── Sensitivity matrix rows (formatted for report) ────────────
    sens_rows = []
    rent_axis  = fo.sensitivity_axis_rent_growth or []
    cap_axis   = fo.sensitivity_axis_exit_cap or []
    matrix     = fo.sensitivity_matrix or []
    for i, row in enumerate(matrix):
        rg_label = f"{rent_axis[i]:.1%}" if i < len(rent_axis) else ""
        sens_rows.append({
            "rent_growth": rg_label,
            "values": ["N/A" if v == "N/A" else (f"{v:.1%}" if isinstance(v, (int, float)) else "—") for v in row],
        })
    ctx["sensitivity_rows"]      = sens_rows
    ctx["sensitivity_cap_axis"]  = [f"{c:.1%}" for c in cap_axis]

    # ── Monte Carlo / Risk-Weighted Return (section 12.6) ─────────
    # Structured three-part layout: method → calculation → interpretation.
    # `monte_carlo_stat_rows` feeds a clean percentile table in the
    # template; the LLM narrative is kept as the interpretation
    # paragraph but both method + stats now render deterministically.
    _mc = fo.monte_carlo_results or {}
    ctx["monte_carlo_results"] = _mc
    ctx["has_monte_carlo"]     = bool(_mc and _mc.get("n_valid"))

    def _fmt_pct(v, digits=1):
        try:
            return f"{float(v) * 100:.{digits}f}%" if v is not None else "—"
        except (TypeError, ValueError):
            return "—"

    def _fmt_em(v):
        try:
            return f"{float(v):.2f}x" if v is not None else "—"
        except (TypeError, ValueError):
            return "—"

    _target_irr = getattr(deal.assumptions, "target_lp_irr", 0.15) or 0.15

    ctx["monte_carlo_stat_rows"] = [
        {"label": "Downside (P10)",   "irr": _fmt_pct(_mc.get("p10_irr")),    "em": _fmt_em(_mc.get("p10_em")),    "note": "Worst 10% of simulated outcomes"},
        {"label": "Lower Quartile (P25)", "irr": _fmt_pct(_mc.get("p25_irr")), "em": "—",                            "note": "Below-median scenario"},
        {"label": "Median (P50)",     "irr": _fmt_pct(_mc.get("median_irr")), "em": _fmt_em(_mc.get("median_em")),  "note": "Expected outcome (risk-weighted)"},
        {"label": "Upper Quartile (P75)", "irr": _fmt_pct(_mc.get("p75_irr")), "em": "—",                            "note": "Above-median scenario"},
        {"label": "Upside (P90)",     "irr": _fmt_pct(_mc.get("p90_irr")),    "em": _fmt_em(_mc.get("p90_em")),    "note": "Best 10% of simulated outcomes"},
    ]
    ctx["monte_carlo_target_irr"]      = _fmt_pct(_target_irr, digits=0)
    ctx["monte_carlo_prob_above_tgt"]  = _fmt_pct(_mc.get("prob_above_target"))
    ctx["monte_carlo_mean_irr"]        = _fmt_pct(_mc.get("mean_irr"))
    ctx["monte_carlo_std_irr"]         = _fmt_pct(_mc.get("std_irr"))
    ctx["monte_carlo_n_valid"]         = int(_mc.get("n_valid") or 0)
    ctx["monte_carlo_dominant_var"]    = (_mc.get("dominant_variable") or "—").replace("_", " ").title()
    ctx["monte_carlo_dominant_r2"]     = (
        f"{float(_mc.get('dominant_variable_r2') or 0):.2f}"
        if _mc.get("dominant_variable_r2") is not None else "—"
    )
    ctx["monte_carlo_shape"]           = (_mc.get("distribution_shape") or "—").replace("_", " ").title()

    # Method blurb — deterministic, no LLM. Same every run so the reader
    # always gets the same framing.
    ctx["monte_carlo_method"] = (
        "A 2,000-iteration Monte Carlo simulation stochastically varies "
        "rent growth, exit cap rate, vacancy, and expense growth across "
        "historical ranges, then computes LP IRR and equity multiple for "
        "each iteration. Percentiles below summarize the resulting "
        "distribution — the median is the risk-weighted expected outcome, "
        "while P10 and P90 bracket downside and upside scenarios."
    )

    # Interpretation — LLM narrative if available, else a deterministic
    # readout of the three headline takeaways.
    _narr = fo.monte_carlo_narrative or ""
    if not _narr.strip():
        _prob = _mc.get("prob_above_target")
        _prob_str = _fmt_pct(_prob) if _prob is not None else "—"
        _med = _fmt_pct(_mc.get("median_irr"))
        _dom = ctx["monte_carlo_dominant_var"]
        _r2  = ctx["monte_carlo_dominant_r2"]
        if _mc.get("n_valid"):
            _narr = (
                f"The simulation's median LP IRR is {_med} against a "
                f"{ctx['monte_carlo_target_irr']} target ({_prob_str} of "
                f"iterations clear the target). {_dom} is the dominant "
                f"driver of outcomes (R² = {_r2}) — diligence and "
                f"underwriting sensitivity should focus here."
            )
        else:
            _narr = (
                "Risk-weighted return analysis requires stabilized NOI. "
                "This analysis will be completed upon lease execution "
                "and confirmation of stabilized operating assumptions."
            )
    ctx["monte_carlo_narrative"] = _narr

    # ── Price solver (MC-backed purchase price at 15% median LP IRR) ─
    _ps = fo.price_solver_results or {}
    ctx["price_solver"] = _ps
    if _ps.get("converged") and _ps.get("solved_purchase_price"):
        _solved = _ps["solved_purchase_price"]
        _base = _ps.get("base_purchase_price") or a.purchase_price or 0
        _adj = _ps.get("price_adjustment_pct") or 0
        _tgt = _ps.get("target_lp_irr") or 0.15
        _direction = "below" if _adj < 0 else "above"
        ctx["solved_price_display"] = f"${_solved:,.0f}"
        ctx["solved_price_vs_base_pct"] = f"{abs(_adj) * 100:.1f}%"
        ctx["solved_price_direction"] = _direction
        ctx["solved_price_target_irr_pct"] = f"{_tgt * 100:.0f}%"
        ctx["solved_price_median_lp_irr_pct"] = (
            f"{_ps.get('solved_median_lp_irr', 0) * 100:.2f}%"
        )
        ctx["solved_price_narrative"] = (
            f"Monte Carlo simulation indicates that a purchase price of "
            f"${_solved:,.0f} — {abs(_adj) * 100:.1f}% {_direction} the current "
            f"${_base:,.0f} basis — would produce a median LP IRR of "
            f"{_tgt * 100:.0f}% across the 2,000-iteration stochastic range of "
            f"rent growth, exit cap rate, vacancy, and expense growth. "
            f"This price is an advisory target for negotiation; a full "
            f"deterministic underwrite at that price should be run before "
            f"committee submission."
        )
        logger.info("PRICE SOLVER CTX: solved=%s (%+.1f%% vs base)",
                    ctx["solved_price_display"], _adj * 100)
    else:
        ctx["solved_price_narrative"] = ""
        _reason = _ps.get("reason", "not_run")
        logger.info("PRICE SOLVER CTX: no result (reason=%s)", _reason)

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
    # Derive incentives_rows: start from any LLM-populated incentives list,
    # then add algorithmic entries based on what the pipeline has already
    # confirmed (OZ flag, flood zone for IRA §45L, historic designation).
    _inc_rows = []
    _is_oz = deal.provenance.field_sources.get("opportunity_zone", "False") == "True"
    if _is_oz:
        _inc_rows.append({
            "incentive": "Opportunity Zone",
            "source":    "Federal (Treasury / IRS §1400Z-2)",
            "benefit":   "Capital-gains deferral + 10-year step-up in basis on the QOF investment.",
            "status":    "Eligible — tract confirmed on HUD OZ list",
        })
    if deal.historical_designation:
        _inc_rows.append({
            "incentive": "Historic Tax Credits",
            "source":    "Federal §47 (20%) + state-level HTC where available",
            "benefit":   "Up to 20% federal + 20-30% state tax credit on qualified rehab expenditures.",
            "status":    (f"Likely eligible — property listed as {deal.historical_designation}"
                          if deal.historic_tax_credits_eligible
                          else f"Screening required — property has status: {deal.historical_designation}"),
        })
    _md_here = deal.market_data
    if _md_here and (_md_here.fema_flood_zone or "").upper().startswith(("A", "V")):
        _inc_rows.append({
            "incentive": "FEMA Hazard Mitigation Grant",
            "source":    "Federal (FEMA HMA / Stafford Act)",
            "benefit":   "Cost-share funding for flood-resilience capex (elevation, wet/dry floodproofing).",
            "status":    f"Candidate — property in SFHA Zone {_md_here.fema_flood_zone}",
        })
    _strategy = (deal.investment_strategy.value or "").lower()
    if "value" in _strategy or "ground" in _strategy or "development" in _strategy:
        _inc_rows.append({
            "incentive": "State/Local Tax Abatement",
            "source":    f"Local jurisdiction — {deal.address.city or 'municipality'}",
            "benefit":   "Typically 5-10 year graduated abatement on improvement-value assessment (e.g. Phila. 10-Year Tax Abatement for renovations).",
            "status":    "Verify eligibility with local assessor prior to permit pull",
        })
    if (deal.asset_type.value or "").lower() in ("multifamily", "mixed-use"):
        _inc_rows.append({
            "incentive": "LIHTC (4% / 9% Affordable Housing)",
            "source":    "State Housing Finance Agency",
            "benefit":   "Dollar-for-dollar federal tax credit over 10 years for income-restricted units.",
            "status":    "Optional — requires LP restructuring to tap",
        })
    # Merge any explicitly-set incentives from the deal
    if deal.incentives_available:
        _inc_rows.extend([{
            "incentive": i.get("name") or i.get("incentive") or "",
            "source":    i.get("source") or i.get("program") or "",
            "benefit":   i.get("benefit") or i.get("description") or "",
            "status":    i.get("status") or "",
        } for i in deal.incentives_available if isinstance(i, dict)])
    ctx["incentives_rows"] = _inc_rows
    ctx["incentives_narrative"] = deal.incentives_narrative or ""
    logger.info("INCENTIVES CTX: %d incentives identified", len(_inc_rows))

    # Historical status context
    ctx["historical_designation"] = deal.historical_designation or ""
    ctx["historic_district"]      = deal.historic_district or ""
    ctx["historic_preservation_notes"] = deal.historic_preservation_notes or ""
    ctx["historic_tax_credits_eligible"] = deal.historic_tax_credits_eligible

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

    # Moody's CRE submarket data
    ctx["moodys_cap_rate"]    = f"{md.moodys_submarket_cap_rate:.2%}" if md.moodys_submarket_cap_rate else None
    ctx["moodys_vacancy"]     = f"{md.moodys_submarket_vacancy_rate:.1%}" if md.moodys_submarket_vacancy_rate else None
    ctx["moodys_rent_growth"] = f"{md.moodys_submarket_rent_growth:.1%}" if md.moodys_submarket_rent_growth else None
    ctx["moodys_market"]      = md.moodys_market_name
    ctx["moodys_submarket"]   = md.moodys_submarket_name
    ctx["moodys_data_as_of"]  = md.moodys_data_as_of
    ctx["moodys_available"]   = bool(md.moodys_submarket_cap_rate)

    # ── Debt market narrative ─────────────────────────────────────
    ctx["debt_market_narrative"] = md.debt_market_narrative or ""

    # ── HBU and buildable capacity ────────────────────────────────
    ctx["hbu_narrative"]           = z.hbu_narrative or ""
    ctx["hbu_conclusion"]          = z.hbu_conclusion or ""
    ctx["buildable_capacity_narrative"] = z.buildable_capacity_narrative or ""
    # Structured math-problem steps. Template renders each step as a
    # numbered calc block (label · formula · inputs list · result).
    ctx["buildable_capacity_steps"]   = list(z.buildable_capacity_steps or [])
    ctx["buildable_binding_constraint"] = z.binding_constraint or ""
    ctx["buildable_binding_result"]     = z.binding_result or ""

    # ══════════════════════════════════════════════════════════════
    # DATA-GAP NOTES — professional placeholders for missing data
    # ══════════════════════════════════════════════════════════════

    # Section 2 — Photo Gallery
    # Consider the gallery "populated" when either real extracted PDF
    # photos exist OR image metadata was captured. Prior code treated
    # the always-truthy `{"images": []}` dict as "has photos", which
    # suppressed the empty-state note even when no real images existed.
    has_photos = bool(_pdf_photos_b64) or bool((ext.image_placements or {}).get("images"))
    if not has_photos:
        ctx["photo_gallery_note"] = (
            "No property photographs are on file as of the "
            "report date. Images should be obtained during "
            "the physical site inspection and added to the "
            "record prior to investment committee presentation."
        )
    else:
        ctx["photo_gallery_note"] = ""

    # Section 9 — Supply Pipeline Register
    has_supply = bool(getattr(md, "supply_pipeline_narrative", None))
    if not has_supply:
        ctx["supply_pipeline_rows"] = []
        ctx["supply_pipeline_note"] = (
            "No competitive supply pipeline data is available "
            "for the immediate submarket at this time. Current "
            "submarket supply, absorption, and delivery data "
            "should be sourced from CoStar or a local market "
            "broker prior to investment committee submission."
        )
    else:
        ctx["supply_pipeline_note"] = ""

    # Section 10 — Unit Mix & Rent Roll
    has_unit_mix = bool(ext.unit_mix)
    has_rent_roll = bool(getattr(deal, "rent_roll", None))
    if not has_unit_mix and not has_rent_roll:
        ctx["rent_roll_rows"] = []
        ctx["unit_mix_summary_rows"] = []
        ctx["rent_roll_totals"] = {}
        ctx["rent_roll_note"] = (
            "No rent roll data is on file. All gross potential "
            "rent figures in the pro forma are projection-based "
            "assumptions, not derived from executed leases or "
            "trailing income history. A current rent roll or "
            "executed lease abstracts must be obtained and "
            "reviewed prior to capital commitment."
        )
    else:
        ctx["rent_roll_note"] = ""
        # Detailed rent roll rows (unit-by-unit)
        rr_rows = []
        total_sf = 0.0
        total_rent = 0.0
        occupied = 0
        vacant = 0
        for u in (ext.unit_mix or []):
            if not isinstance(u, dict):
                continue
            sf = u.get("sf")
            mr = u.get("monthly_rent")
            status = (u.get("status") or "").strip()
            if sf:
                total_sf += float(sf)
            if mr:
                total_rent += float(mr)
            s_low = status.lower()
            if s_low in ("vacant", "vac"):
                vacant += 1
            elif s_low:
                occupied += 1
            rr_rows.append({
                "unit":         u.get("unit_id") or "",
                "type":         u.get("unit_type") or "",
                "sf":           f"{float(sf):,.0f}" if sf else "",
                "tenant":       u.get("tenant_name") or "",
                "monthly_rent": f"${float(mr):,.0f}" if mr else "",
                "market_rent":  (f"${float(u['market_rent']):,.0f}"
                                 if u.get("market_rent") else ""),
                "lease_start":  u.get("lease_start") or "",
                "lease_end":    u.get("lease_end") or "",
                "status":       status,
            })
        ctx["rent_roll_rows"] = rr_rows

        # Aggregate by unit_type for summary table
        agg: dict = defaultdict(lambda: {"count": 0, "sf_sum": 0.0,
                                         "rent_sum": 0.0, "rent_n": 0})
        for u in (ext.unit_mix or []):
            if not isinstance(u, dict):
                continue
            t = (u.get("unit_type") or "Unspecified").strip() or "Unspecified"
            a_ = agg[t]
            a_["count"] += 1
            if u.get("sf"):
                a_["sf_sum"] += float(u["sf"])
            if u.get("monthly_rent"):
                a_["rent_sum"] += float(u["monthly_rent"])
                a_["rent_n"] += 1
        summary = []
        for t, v in agg.items():
            avg_sf = (v["sf_sum"] / v["count"]) if v["count"] else 0
            avg_rent = (v["rent_sum"] / v["rent_n"]) if v["rent_n"] else 0
            summary.append({
                "unit_type":    t,
                "count":        str(v["count"]),
                "avg_sf":       f"{avg_sf:,.0f}" if avg_sf else "",
                "avg_rent":     f"${avg_rent:,.0f}" if avg_rent else "",
                "total_rent":   f"${v['rent_sum']:,.0f}" if v["rent_sum"] else "",
            })
        ctx["unit_mix_summary_rows"] = summary

        ctx["rent_roll_totals"] = {
            "units":          str(len(rr_rows)) if rr_rows else "",
            "occupied":       str(occupied),
            "vacant":         str(vacant),
            "total_sf":       f"{total_sf:,.0f}" if total_sf else "",
            "total_monthly":  f"${total_rent:,.0f}" if total_rent else "",
            "annual":         f"${total_rent * 12:,.0f}" if total_rent else "",
        }

    # Section 11.1 — Residential Rent Comparables
    if not ctx.get("rent_comp_rows"):
        ctx["rent_comp_rows"] = []
        ctx["rent_comps_note"] = (
            "No rent comparable data was provided in the deal "
            "package. A formal rent comp study covering asking "
            "rents and closed lease transactions for comparable "
            "properties in the submarket is required before the "
            "underwritten rent assumptions can be validated."
        )
    else:
        ctx["rent_comps_note"] = ""

    # Section 11.2 — Commercial Rent Comparables
    if not ctx.get("commercial_comp_rows"):
        ctx["commercial_comp_rows"] = []
        ctx["commercial_comps_note"] = (
            "No commercial comparable lease transactions are on "
            "file. Given the property's use strategy, commercial "
            "comp analysis should be commissioned from a local "
            "market broker or licensed appraiser prior to "
            "investment committee submission."
        )
    else:
        ctx["commercial_comps_note"] = ""

    # Section 11.3 — Sale Comparables
    if not ctx.get("sale_comp_rows"):
        ctx["sale_comp_rows"] = []
        ctx["sale_comps_note"] = (
            "No closed sale comparable transactions are on file. "
            "A formal sales comp analysis benchmarking price per "
            "SF and cap rate against the subject is required "
            "before the exit cap rate and terminal value "
            "assumptions can be substantiated."
        )
    else:
        ctx["sale_comps_note"] = ""

    # Merge validated narrative context — single source of truth
    narr_ctx = validate_and_build_narrative_context(deal)
    for k, v in narr_ctx.items():
        if k not in ctx or ctx[k] is None:
            ctx[k] = v

    # Overwrite narrative context with live pro forma values (single source of truth)
    try:
        fo = deal.financial_outputs
        pf = fo.pro_forma_years or []
        pf_noi  = [y.get("noi", 0) for y in pf]
        pf_egi  = [y.get("egi", 0) for y in pf]
        pf_opex = [y.get("opex", 0) for y in pf]
        pf_ds   = [y.get("debt_service", 0) for y in pf]
        pf_fcf  = [y.get("fcf", 0) for y in pf]

        # DSCR and stab factors are not stored in proforma dict — compute on the fly
        pf_dscr = [(n / d) if d else 0 for n, d in zip(pf_noi, pf_ds)]
        # stabilization factors (import from financials)
        try:
            from financials import _get_stabilization_factors
            pf_sf = _get_stabilization_factors(deal)
        except Exception:
            pf_sf = [1.0] * len(pf)

        first_stab_yr = next((i for i, sf in enumerate(pf_sf) if sf >= 1.0), 0)

        # Refi net proceeds come from the proforma year dict
        refi_proceeds_by_year = [y.get("refi_proceeds", 0) for y in pf]
        refi1_proceeds = next((p for p in refi_proceeds_by_year if p), 0)
        refi2_proceeds = 0
        _found_first = False
        for p in refi_proceeds_by_year:
            if p:
                if _found_first:
                    refi2_proceeds = p
                    break
                _found_first = True

        ctx.update({
            "year1_noi":          pf_noi[0]  if pf_noi  else "N/A",
            "year1_egi":          pf_egi[0]  if pf_egi  else "N/A",
            "year1_opex":         pf_opex[0] if pf_opex else "N/A",
            "year1_debt_svc":     pf_ds[0]   if pf_ds   else "N/A",
            "year1_fcf":          pf_fcf[0]  if pf_fcf  else "N/A",
            "year1_dscr":         pf_dscr[0] if pf_dscr else "N/A",
            "year10_noi":         pf_noi[9]  if len(pf_noi) > 9 else "N/A",
            "first_stabilized_year": first_stab_yr + 1,
            "first_stab_noi":     pf_noi[first_stab_yr] if pf_noi else "N/A",
            "refi1_net_proceeds": refi1_proceeds,
            "refi2_net_proceeds": refi2_proceeds,
        })
        logger.info("NARRATIVE CTX: year1_noi=%.2f, year1_dscr=%.2f, "
                    "first_stab_yr=%d, first_stab_noi=%.2f, refi1_proceeds=%.2f",
                    pf_noi[0] if pf_noi else 0,
                    pf_dscr[0] if pf_dscr else 0,
                    first_stab_yr + 1,
                    pf_noi[first_stab_yr] if pf_noi else 0,
                    refi1_proceeds)
    except Exception as e:
        logger.warning("NARRATIVE CTX: failed to refresh context — %s", e)

    # ── Refi equity-injection disclosure (material LP-facing risk) ────
    # financials.py sets provenance keys refi{1,2,3}_equity_injection_required
    # when the new loan at refi does not cover the existing balance. Surface
    # that as a ctx string so Section 13 can render it alongside the refi
    # narrative.
    prov = deal.provenance.field_sources
    _equity_warnings = []
    for _i in (1, 2, 3):
        if prov.get(f"refi{_i}_equity_injection_required") == "True":
            _inject = float(prov.get(f"refi{_i}_equity_injection_amount", 0) or 0)
            _new_loan = float(prov.get(f"refi{_i}_new_loan", 0) or 0)
            _bal = float(prov.get(f"refi{_i}_existing_balance", 0) or 0)
            _equity_warnings.append(
                f"WARNING: The Refi {_i} at the assumed cap rate and LTV "
                f"produces a new loan of ${_new_loan:,.0f}, which does not "
                f"cover the existing loan balance of ${_bal:,.0f}. A borrower "
                f"equity injection of ${_inject:,.0f} is required to execute "
                f"this refinance. This represents a material mid-hold capital "
                f"call risk that must be disclosed to LP investors."
            )
    ctx["refi1_equity_warning"] = _equity_warnings[0] if _equity_warnings else ""
    ctx["refi_equity_warnings"] = "\n\n".join(_equity_warnings)
    if _equity_warnings:
        logger.warning("REFI EQUITY INJECTION: %d refi(s) flagged for disclosure",
                       len(_equity_warnings))

    # ═══════════════════════════════════════════════════════════════════
    # EXPLICIT TEMPLATE CONTEXT KEYS — kpi_rows, parcel_a_*, transit_rows,
    # hbu_content, income_* — built from the real model attribute paths.
    # ═══════════════════════════════════════════════════════════════════
    def _safe_fmt(val, fmt="${:,.0f}", fallback="N/A"):
        try:
            if val is None:
                return fallback
            n = float(val)
            # For the default currency format, render negatives as
            # "($12,345)" instead of "$-12,345" to match the convention
            # used everywhere else in the report (Jinja |currency filter).
            if fmt == "${:,.0f}" and n < 0:
                return f"(${abs(n):,.0f})"
            return fmt.format(n)
        except (TypeError, ValueError):
            return fallback

    # ── Fix 1: KPI table rows ────────────────────────────────────────
    kpi_rows = [
        {"label": "Purchase Price",
         "value": _safe_fmt(a.purchase_price)},
        {"label": "Total Project Cost",
         "value": _safe_fmt(fo.total_uses)},
        {"label": "Total Equity Required",
         "value": _safe_fmt(fo.total_equity_required)},
        {"label": "GP Equity",
         "value": _safe_fmt(fo.gp_equity)},
        {"label": "LP Equity",
         "value": _safe_fmt(fo.lp_equity)},
        {"label": "Project IRR",
         "value": (_safe_fmt((fo.project_irr or 0) * 100, fmt="{:.1f}%")
                   if fo.project_irr is not None else "N/A")},
        {"label": "LP IRR",
         "value": (_safe_fmt((fo.lp_irr or 0) * 100, fmt="{:.1f}%")
                   if fo.lp_irr is not None else "N/A")},
        {"label": "LP Equity Multiple",
         "value": _safe_fmt(fo.lp_equity_multiple, fmt="{:.2f}x")},
        {"label": "Hold Period",
         "value": (f"{a.hold_period} years" if a.hold_period else "N/A")},
    ]
    ctx["kpi_rows"] = kpi_rows
    logger.info("KPI_ROWS built: %d rows", len(kpi_rows))
    for r in kpi_rows:
        logger.info("  KPI: %s = %s", r["label"], r["value"])

    # ── Fix 6: HBU content alias (real path: deal.zoning.hbu_narrative) ─
    hbu_text = (
        getattr(deal.zoning, "hbu_narrative", None)
        or getattr(deal.zoning, "hbu_conclusion", None)
        or ""
    )
    ctx["hbu_content"] = hbu_text or (
        "Highest and best use analysis is pending zoning verification. "
        "This section will be completed once the municipal zoning code "
        "scrape and buildable-capacity analysis return data for the "
        "subject parcel."
    )
    logger.info("HBU: content length=%d chars", len(ctx["hbu_content"]))

    # ── Fix 4: Parcel A context keys from real ParcelData ────────────
    pd_ = deal.parcel_data

    def _p_str(v, fallback="N/A"):
        return str(v) if v not in (None, "") else fallback

    def _p_money(v, fallback="N/A"):
        try:
            return f"${float(v):,.0f}" if v not in (None, "") else fallback
        except (TypeError, ValueError):
            return fallback

    def _p_area(v, fallback="N/A"):
        try:
            return f"{float(v):,.0f} SF" if v not in (None, "") else fallback
        except (TypeError, ValueError):
            return fallback

    ctx["parcel_a_address"]      = _title_case_address(deal.address.full_address or "") or "N/A"
    ctx["parcel_a_account"]      = _p_str(pd_.parcel_id        if pd_ else None)
    ctx["parcel_a_owner"]        = _p_str(pd_.owner_name       if pd_ else None)
    ctx["parcel_a_zoning"]       = _p_str(
        (pd_.zoning_code if pd_ else None) or deal.zoning.zoning_code,
        "Pending verification",
    )
    # Prefer ParcelData (OPA scrape) values; fall back to FinancialAssumptions
    # (user-entered or OM-extracted) when the parcel row is missing the field.
    # Correct assumption attribute names: gba_sf, year_built, lot_sf.
    _pa_land_area = (pd_.lot_area_sf if pd_ else None) or a.lot_sf
    _pa_bldg_sf   = (pd_.building_sf if pd_ else None) or a.gba_sf
    _pa_yr_built  = (pd_.year_built  if pd_ else None) or a.year_built
    ctx["parcel_a_land_area"]    = _p_area(_pa_land_area)
    ctx["parcel_a_building_sf"]  = _p_area(_pa_bldg_sf)
    ctx["parcel_a_year_built"]   = _p_str(_pa_yr_built)
    ctx["parcel_a_assessed"]     = _p_money(pd_.assessed_value if pd_ else None)
    ctx["parcel_a_taxable_land"] = _p_money(pd_.land_value     if pd_ else None)
    ctx["parcel_a_taxable_bldg"] = _p_money(pd_.improvement_value if pd_ else None)
    ctx["parcel_a_stories"]      = "N/A"
    ctx["parcel_a_category"]     = "N/A"

    # Extended owner / contact fields (Section 04 Parcel & Improvement table)
    ctx["parcel_owner_entity_type"]  = _p_str(pd_.ownership_entity_type if pd_ else None)
    ctx["parcel_taxpayer_mailing"]   = _p_str(pd_.taxpayer_mailing_address if pd_ else None)
    _oo = pd_.owner_occupied if pd_ else None
    ctx["parcel_owner_occupied_display"] = (
        "Yes" if _oo is True else ("No" if _oo is False else "")
    )
    _yo = pd_.years_owned if pd_ else None
    ctx["parcel_years_owned_display"] = (
        f"{_yo:.1f} years" if _yo else ""
    )
    ctx["parcel_property_use_class"] = _p_str(pd_.property_use_class if pd_ else None) if pd_ and pd_.property_use_class else ""

    # Broker / listing-agent contact from OM extraction (1A)
    _ext = deal.extracted_docs
    ctx["broker_name"]  = (_ext.broker_name  or "") if _ext else ""
    ctx["broker_firm"]  = (_ext.broker_firm  or "") if _ext else ""
    ctx["broker_phone"] = (_ext.broker_phone or "") if _ext else ""
    ctx["broker_email"] = (_ext.broker_email or "") if _ext else ""

    # Parcel B is not in the current data model — always blank.
    ctx["parcel_b_address"]    = ""
    ctx["parcel_b_account"]    = "N/A"
    ctx["parcel_b_owner"]      = "N/A"
    ctx["parcel_b_zoning"]     = "N/A"
    ctx["parcel_b_land_area"]  = "N/A"
    ctx["parcel_b_year_built"] = "N/A"
    ctx["parcel_b_assessed"]   = "N/A"

    ctx["parcel_census_tract"] = deal.address.census_tract or "N/A"
    ctx["parcel_fips"]         = deal.address.fips_code or "N/A"
    logger.info("PARCEL A: account=%s owner=%s zoning=%s",
                ctx["parcel_a_account"],
                ctx["parcel_a_owner"],
                ctx["parcel_a_zoning"])

    # Current ownership snapshot — parcel-record fields rendered as a
    # two-column key/value table in Section 05.
    def _or_na(v):
        return v if v not in (None, "", 0, 0.0) else "N/A"

    _last_sale_price = (pd_.last_sale_price if pd_ else None)
    _last_sale_date = (pd_.last_sale_date if pd_ else None)
    _years_owned = (pd_.years_owned if pd_ else None)
    _current_ownership_rows = [
        ("Current Owner",         _or_na(pd_.owner_name if pd_ else None)),
        ("Owner Entity Type",
         _or_na((pd_.ownership_entity_type if pd_ else None)
                 or (pd_.owner_entity if pd_ else None))),
        ("Years Owned",
         f"{_years_owned:.1f} years" if _years_owned else "N/A"),
        ("Owner-Occupied",
         ("Yes" if (pd_ and pd_.owner_occupied) else
          ("No" if (pd_ and pd_.owner_occupied is False) else "Unknown"))),
        ("Taxpayer Mailing Address",
         _or_na(pd_.taxpayer_mailing_address if pd_ else None)),
        ("Parcel / Account ID",   _or_na(pd_.parcel_id if pd_ else None)),
        ("Deed Book / Page",      _or_na(pd_.deed_book_page if pd_ else None)),
        ("Last Sale Date",        _or_na(_last_sale_date)),
        ("Last Sale Price",
         f"${_last_sale_price:,.0f}" if _last_sale_price else "N/A"),
        ("Property Use Class",    _or_na(pd_.property_use_class if pd_ else None)),
        ("Homestead Status",      _or_na(pd_.homestead_status if pd_ else None)),
        ("Exemptions",
         ", ".join(pd_.exemptions) if (pd_ and pd_.exemptions) else "None"),
        ("Census Tract",          _or_na(deal.address.census_tract)),
        ("FIPS Code",             _or_na(deal.address.fips_code)),
    ]

    # Owner portfolio (other parcels held by same name)
    _portfolio = (pd_.other_parcels_owned if pd_ else []) or []
    _portfolio_rows = [
        {
            "address":      p.get("address") or p.get("parcel_id") or "",
            "parcel_id":    p.get("parcel_id") or "",
            "market_value": (f"${float(p['market_value']):,.0f}"
                             if p.get("market_value") else "—"),
        }
        for p in _portfolio[:25]
    ]
    ctx["owner_portfolio_rows"] = _portfolio_rows
    ctx["owner_portfolio_total"] = sum(
        float(p.get("market_value") or 0) for p in _portfolio
    )
    if _portfolio_rows:
        logger.info("OWNER PORTFOLIO CTX: %d other parcels totaling $%s",
                    len(_portfolio_rows),
                    f"{ctx['owner_portfolio_total']:,.0f}")

    # ── 1E/1F/1G extractor outputs ──────────────────────────────────
    ctx["lease_abstract_rows"] = [
        {
            "unit":         la.unit_id or "—",
            "tenant":       la.tenant_name or "—",
            "lease_type":   la.lease_type or "—",
            "commencement": la.commencement_date or "—",
            "expiration":   la.expiration_date or "—",
            "base_rent":    (f"${la.base_rent_monthly:,.0f}/mo"
                             if la.base_rent_monthly else "—"),
            "escalation":   la.escalation_amount or la.escalation_type or "—",
            "cam":          la.cam_structure or "—",
            "renewals":     "; ".join(la.renewal_options) if la.renewal_options else "—",
            "guaranty":     ("Yes" if la.personal_guaranty else
                             "No" if la.personal_guaranty is False else "—"),
        }
        for la in (ext.lease_abstracts or [])
    ]

    ctx["title_commitment_date"]   = ext.title_commitment_date or ""
    ctx["title_company"]           = ext.title_company or ""
    ctx["title_insurance_amount"]  = (
        f"${ext.title_insurance_amount:,.0f}"
        if ext.title_insurance_amount else ""
    )
    ctx["title_vesting"]            = ext.title_vesting or ""
    ctx["title_legal_description"]  = ext.title_legal_description or ""
    ctx["title_exception_rows"] = [
        {
            "type":           te.exception_type or "—",
            "recording_date": te.recording_date or "—",
            "document_id":    te.document_id or "—",
            "summary":        te.summary or "—",
        }
        for te in (ext.title_exceptions or [])
    ]
    ctx["title_easements"]    = ext.title_easements or []
    ctx["title_endorsements"] = ext.title_endorsements or []

    ctx["pca_report_date"]      = ext.pca_report_date or ""
    ctx["pca_consultant"]       = ext.pca_consultant or ""
    ctx["pca_overall_condition"] = ext.pca_overall_condition or ""
    ctx["pca_deferred_maintenance_total"] = (
        f"${ext.pca_deferred_maintenance_total:,.0f}"
        if ext.pca_deferred_maintenance_total else ""
    )
    ctx["pca_capex_12yr_total"] = (
        f"${ext.pca_capex_12yr_total:,.0f}"
        if ext.pca_capex_12yr_total else ""
    )
    ctx["pca_system_rows"] = [
        {
            "system":     s.system or "—",
            "condition":  s.condition or "—",
            "age":        (f"{s.age_years} yrs" if s.age_years else "—"),
            "rul":        (f"{s.remaining_useful_life} yrs" if s.remaining_useful_life else "—"),
            "repl_cost":  (f"${s.replacement_cost:,.0f}" if s.replacement_cost else "—"),
            "notes":      s.notes or "",
        }
        for s in (ext.pca_building_systems or [])
    ]
    ctx["pca_immediate_repair_rows"] = [
        {
            "item":     r.item or "—",
            "cost":     (f"${r.cost:,.0f}" if r.cost else "—"),
            "priority": r.priority or "—",
        }
        for r in (ext.pca_immediate_repairs or [])
    ]
    ctx["pca_ada_items"] = ext.pca_ada_items or []

    # Environmental from 1D (add to context so report can render)
    ctx["environmental_phase1_status"]    = ext.phase1_status or ""
    ctx["environmental_phase1_date"]      = ext.phase1_date or ""
    ctx["environmental_phase1_consultant"] = ext.phase1_consultant or ""
    ctx["environmental_recs"]              = ext.recognized_environmental_conditions or []
    ctx["environmental_hrecs"]             = ext.historical_recognized_conditions or []
    ctx["environmental_vapor_flag"]        = ext.vapor_intrusion_flag
    ctx["environmental_phase2_recommended"] = ext.phase2_recommended
    ctx["environmental_findings"]          = ext.environmental_findings or ""
    ctx["environmental_recommendations"]   = ext.environmental_recommendations or ""
    logger.info(
        "1E/1F/1G CTX: leases=%d title_exceptions=%d pca_systems=%d repairs=%d RECs=%d",
        len(ctx["lease_abstract_rows"]),
        len(ctx["title_exception_rows"]),
        len(ctx["pca_system_rows"]),
        len(ctx["pca_immediate_repair_rows"]),
        len(ctx["environmental_recs"]),
    )
    # Suppress ownership rows whose value is N/A / None / blank / "Unknown"
    # — they clutter the report with useless rows when the parcel portal
    # doesn't expose those fields.
    _skip_values = {"N/A", "n/a", "", None, "None", "Unknown", "unknown"}
    ctx["current_ownership_rows"] = [
        {"label": k, "value": str(v)} for k, v in _current_ownership_rows
        if v not in _skip_values
    ]

    # Assessment & valuation breakdown (separate table so the reader can
    # quickly compare taxable basis against current carrying value).
    _assessed = (pd_.assessed_value if pd_ else None)
    _land = (pd_.land_value if pd_ else None)
    _impr = (pd_.improvement_value if pd_ else None)
    _assessment_rows = []
    if any(v not in (None, 0, 0.0) for v in (_assessed, _land, _impr)):
        _assessment_rows = [
            {"label": "Land Value",          "value": f"${_land:,.0f}" if _land else "N/A"},
            {"label": "Improvement Value",   "value": f"${_impr:,.0f}" if _impr else "N/A"},
            {"label": "Total Assessed Value","value": f"${_assessed:,.0f}" if _assessed else "N/A"},
        ]
        if _assessed and a.purchase_price:
            _ratio = _assessed / a.purchase_price
            _assessment_rows.append({
                "label": "Assessed / Purchase Price",
                "value": f"{_ratio:.1%}",
            })
    # Annual property tax: use the parcel-record billed amount if the
    # portal exposed it, else the underwriting re_taxes assumption (which
    # is derived from assessed value × effective tax rate via expense_pricing).
    _annual_tax = (
        (pd_.annual_tax_billed if pd_ else None)
        or a.re_taxes
    )
    if _annual_tax and _annual_tax > 0:
        _assessment_rows.append({
            "label": "Annual Property Tax",
            "value": f"${_annual_tax:,.0f}",
        })
        if _assessed and _annual_tax:
            _eff_rate = _annual_tax / _assessed
            _assessment_rows.append({
                "label": "Effective Tax Rate",
                "value": f"{_eff_rate:.3%}",
            })
    ctx["assessment_rows"] = _assessment_rows
    logger.info(
        "OWNERSHIP CONTEXT: owner=%s parcel=%s sale=%s assessed=%s",
        _or_na(pd_.owner_name if pd_ else None),
        _or_na(pd_.parcel_id if pd_ else None),
        _or_na(_last_sale_date),
        _or_na(_assessed),
    )

    # Deed / title transfer history — sourced from PHL rtt_summary in
    # parcel_fetcher.py. Empty list when no data (non-Phl deal or no records).
    def _fmt_recording_date(d):
        if not d:
            return "—"
        s = str(d)[:10]
        return s if re.match(r"^\d{4}-\d{2}-\d{2}$", s) else s

    _deeds = (pd_.deed_history if pd_ else []) or []
    ctx["deed_history_rows"] = [
        {
            "recording_date": _fmt_recording_date(d.recording_date),
            "document_type":  d.document_type or "—",
            "grantor":        d.grantor or "—",
            "grantee":        d.grantee or "—",
            "consideration":  (f"${d.consideration_amount:,.0f}"
                               if d.consideration_amount not in (None, 0, 0.0)
                               else "—"),
            "document_id":    d.document_id or "—",
        }
        for d in _deeds
    ]
    # Deep-link to the county recorder portal (for jurisdictions where we
    # couldn't pull full chain of title programmatically — i.e., most of
    # the registry's 185 portal hosts). Value comes from the registry's
    # recorder_of_deeds_url column, plumbed via provenance.field_sources.
    ctx["recorder_portal_url"] = (
        deal.provenance.field_sources.get("recorder_of_deeds_url") or ""
    )
    logger.info("DEED HISTORY ROWS: %d rows (portal=%s)",
                len(ctx["deed_history_rows"]),
                ctx["recorder_portal_url"][:60] or "none")

    # ── Fix 7: Transit rows for the template ─────────────────────────
    transit_list = list(getattr(md, "transit_options", []) or [])
    ctx["transit_rows"] = [
        {
            "mode":        t.get("mode", "Transit"),
            "route":       t.get("route", "—"),
            "distance":    t.get("distance", "—"),
            "destination": t.get("destination", "—"),
        }
        for t in transit_list[:8]
    ]
    logger.info("TRANSIT ROWS: %d rows", len(ctx["transit_rows"]))

    # Amenity rows (rendered after the neighborhood context map)
    amenity_list = list(getattr(md, "nearby_amenities", []) or [])
    ctx["amenity_rows"] = [
        {
            "category": (a.get("category") or "").strip() or "—",
            "name":     (a.get("name") or "").strip() or "—",
            "distance": (a.get("distance") or "").strip() or "—",
            "notes":    (a.get("notes") or "").strip(),
        }
        for a in amenity_list if isinstance(a, dict)
    ]
    logger.info("AMENITY ROWS: %d rows", len(ctx["amenity_rows"]))

    # ── Income Summary: use FIRST STABILIZED YEAR from pro forma ────
    # For stabilized assets stab_factor is 1.0 in Year 1.
    # For value_add, Year 1 is construction; first stab year is Year 2+.
    # We pull GPR, vacancy, LTL, EGI directly from pro_forma_years so
    # the income summary matches the pro forma exactly.
    _fo = deal.financial_outputs
    _pro_years = getattr(_fo, 'pro_forma_years', None) or []
    _a = deal.assumptions

    # Find first year where stabilization_factor >= 1.0
    _stab_yr = None
    for _py in _pro_years:
        _sf = float(_py.get('stabilization_factor', 1.0)
                    if isinstance(_py, dict)
                    else getattr(_py, 'stabilization_factor', 1.0))
        if _sf >= 1.0:
            _stab_yr = _py
            break

    if _stab_yr and isinstance(_stab_yr, dict):
        # Prefer pro forma dict values — they are already stabilized
        _gpr_val  = float(_stab_yr.get('gpr', 0) or 0)
        _egi_val  = float(_stab_yr.get('egi', 0) or 0)
        _opex_val = float(_stab_yr.get('opex', 0) or 0)
        _vac_rate = float(_a.vacancy_rate or 0.05)
        _ltl_rate = float(_a.loss_to_lease or 0.03)
        _vacancy_loss = _gpr_val * _vac_rate
        _ltl_loss     = _gpr_val * _ltl_rate
        _other        = float((_a.cam_reimbursements or 0)
                               + (_a.fee_income or 0))
        _src = "pro_forma_stab_yr"
        logger.info(
            "GPR DISPLAY: first stab year GPR=$%s (Year %s)",
            f"{_gpr_val:,.0f}",
            _stab_yr.get('year', '?')
        )
    else:
        # Fallback: use fo.gross_potential_rent (stabilized asset,
        # no construction period)
        _vac_rate     = float(_a.vacancy_rate or 0.05)
        _ltl_rate     = float(_a.loss_to_lease or 0.03)
        _gpr_val      = float(_fo.gross_potential_rent or 0)
        _vacancy_loss = _gpr_val * _vac_rate
        _ltl_loss     = _gpr_val * _ltl_rate
        _other        = float((_a.cam_reimbursements or 0)
                               + (_a.fee_income or 0))
        _egi_val      = _gpr_val - _vacancy_loss - _ltl_loss + _other
        _src = "fo.gross_potential_rent"
        logger.info(
            "GPR DISPLAY: no stab year in pro_forma — using "
            "fo.gross_potential_rent=$%s",
            f"{_gpr_val:,.0f}"
        )

    ctx["income_gpr"]           = f"${_gpr_val:,.0f}"
    ctx["income_vacancy_loss"]  = f"(${_vacancy_loss:,.0f})"
    ctx["income_vacancy_pct"]   = f"({_vac_rate * 100:.1f}%)"
    ctx["income_loss_to_lease"] = f"(${_ltl_loss:,.0f})"
    ctx["income_ltl_pct"]       = f"({_ltl_rate * 100:.1f}%)"
    ctx["income_other"]         = f"${_other:,.0f}"
    ctx["income_egi"]           = f"${_egi_val:,.0f}"
    ctx["income_egi_pct"]       = (
        f"{(_egi_val / _gpr_val * 100):.1f}%"
        if _gpr_val > 0 else "N/A"
    )
    logger.info(
        "EGI CALC: GPR=%s vacancy=%s ltl=%s EGI=%s (source=%s)",
        ctx["income_gpr"],
        ctx["income_vacancy_loss"],
        ctx["income_loss_to_lease"],
        ctx["income_egi"],
        _src,
    )
    logger.info(
        "INCOME SUMMARY CTX: gpr=%s egi=%s (stab_yr_found=%s)",
        ctx.get('income_gpr'),
        ctx.get('income_egi'),
        _stab_yr is not None,
    )

    # ═══════════════════════════════════════════════════════════════════
    # RENT + SALE COMP CONTEXT (Section 11.1 benchmarks + 11.3 sale comps)
    # Sourced from deal.comps.rent_comps / deal.comps.sale_comps (Pydantic
    # RentComp / SaleComp) — NOT from market_data; comps have always lived
    # on deal.comps. Market-level benchmarks (ZORI/Census/HUD FMR) are read
    # from market_data directly and prepended to the listings.
    # ═══════════════════════════════════════════════════════════════════
    _rent_comps = list(getattr(deal.comps, "rent_comps", None) or [])
    _sale_comps = list(getattr(deal.comps, "sale_comps", None) or [])

    def _fmt_rent_mo(v):
        try:
            return f"${float(v):,.0f}/mo" if v else "N/A"
        except Exception:
            return "N/A"

    def _fmt_dist(v):
        try:
            return f"{float(v):.2f} mi" if v is not None else "—"
        except Exception:
            return "—"

    def _fmt_price(v):
        try:
            return f"${float(v):,.0f}" if v else "N/A"
        except Exception:
            return "N/A"

    _tier_labels = {
        "light_cosmetic":   "Light Cosmetic (90% FMR)",
        "heavy_rehab":      "Heavy Rehab (100% FMR)",
        "new_construction": "New Construction (115% FMR)",
    }

    # ── Benchmark rows: ZORI + Census + HUD FMR + quality-adjusted ─
    _benchmark_rows = []
    _zori = getattr(md, "zori_median_rent", None)
    _zori_trend = getattr(md, "zori_rent_trend", "") or ""
    if _zori:
        _benchmark_rows.append({
            "property": f"ZIP {getattr(deal.address,'zip_code','')} Median",
            "type":     "All Units",
            "beds":     "—",
            "rent_mo":  _fmt_rent_mo(_zori),
            "rent_sf":  "—",
            "distance": "Zip-Level",
            "note":     f"Zillow ZORI {_zori_trend}".strip(),
        })
    # Census ACS 2022 medians by bedroom tier (B25031_004E/5E/6E).
    # Surface each tier that has a non-null value so the comparable market
    # analysis shows rent across unit types, not just 2BR.
    for _label, _beds, _attr in (
        ("1BR", "1", "census_median_rent_1br"),
        ("2BR", "2", "census_median_rent_2br"),
        ("3BR", "3", "census_median_rent_3br"),
    ):
        _val = getattr(md, _attr, None)
        if _val:
            _benchmark_rows.append({
                "property": "Census Tract Median",
                "type":     _label,
                "beds":     _beds,
                "rent_mo":  _fmt_rent_mo(_val),
                "rent_sf":  "—",
                "distance": "Tract-Level",
                "note":     "Census ACS 2022 B25031",
            })

    # HUD Fair Market Rent by bedroom tier (studio / 1BR / 2BR / 3BR).
    # Same cross-tier emission so the report reflects the full local
    # affordability ladder, not just a single 2BR datapoint.
    for _label, _beds, _attr in (
        ("Studio", "0", "fmr_studio"),
        ("1BR",    "1", "fmr_1br"),
        ("2BR",    "2", "fmr_2br"),
        ("3BR",    "3", "fmr_3br"),
    ):
        _raw = getattr(md, _attr, None)
        if _raw:
            try:
                _f = float(_raw)
            except (TypeError, ValueError):
                continue
            _benchmark_rows.append({
                "property": "HUD Fair Market Rent",
                "type":     _label,
                "beds":     _beds,
                "rent_mo":  f"${_f:,.0f}/mo",
                "rent_sf":  "—",
                "distance": "MSA-Level",
                "note":     "HUD FMR FY2025",
            })
    _qamr = getattr(a, "quality_adjusted_market_rent", None)
    if _qamr:
        _benchmark_rows.append({
            "property": "DealDesk Quality-Adjusted Market Rent",
            "type":     "Subject",
            "beds":     "—",
            "rent_mo":  _fmt_rent_mo(_qamr),
            "rent_sf":  "—",
            "distance": "Subject Property",
            "note":     _tier_labels.get(
                getattr(a, "renovation_tier", "") or "", "Computed"),
        })

    # ── Active Craigslist listings (source prefix "Craigslist") ─────
    # Group by bedroom tier and cap at 4 per tier so the report shows
    # diversity across Studio / 1BR / 2BR / 3BR / 4BR+ rather than a
    # slug of 2BR listings that crowd out other tiers.
    _cl_by_tier: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for rc in _rent_comps:
        src = str(getattr(rc, "source", "") or "")
        if not src.startswith("Craigslist"):
            continue
        _tier = (rc.unit_type or "").strip() or "—"
        if len(_cl_by_tier[_tier]) >= 4:
            continue
        _cl_by_tier[_tier].append({
            "property": (src.replace("Craigslist:", "").strip()[:40]
                         or "Active Listing"),
            "type":     _tier if _tier != "—" else "—",
            "beds":     str(rc.beds) if rc.beds else "—",
            "rent_mo":  _fmt_rent_mo(rc.monthly_rent),
            "rent_sf":  "—",
            "distance": _fmt_dist(rc.distance_miles),
            "note":     "Craigslist Active",
        })
    # Flatten in tier order (studio first, then 1BR, ...) so the
    # rendered table reads Studio → 1BR → 2BR → 3BR → 4BR+ regardless
    # of Craigslist's native response order.
    _tier_order = ["Studio", "1BR", "2BR", "3BR", "4BR+"]
    _cl_rows: List[Dict[str, str]] = []
    for _t in _tier_order:
        _cl_rows.extend(_cl_by_tier.get(_t, []))
    for _t, _rows in _cl_by_tier.items():
        if _t in _tier_order:
            continue
        _cl_rows.extend(_rows)

    ctx["rent_comp_rows"] = _benchmark_rows + _cl_rows
    ctx["has_rent_comps"] = len(ctx["rent_comp_rows"]) > 0
    ctx["zori_median_rent"]  = _fmt_rent_mo(_zori)
    ctx["zori_rent_trend"]   = _zori_trend or "N/A"
    ctx["census_median_2br"] = _fmt_rent_mo(getattr(md, "census_median_rent_2br", None))
    ctx["quality_adjusted_market_rent"] = _fmt_rent_mo(_qamr)
    ctx["renovation_tier_label"] = _tier_labels.get(
        getattr(a, "renovation_tier", "") or "", "Renovation")
    logger.info("RENT COMP ROWS: %d (%d benchmarks + %d listings)",
                len(ctx["rent_comp_rows"]),
                len(_benchmark_rows), len(_cl_rows))

    # ── Sale comp rows (Section 11.3) ────────────────────────────
    _sale_rows = []
    _sorted_sales = sorted(
        _sale_comps,
        key=lambda x: (x.distance_miles if x.distance_miles is not None else 99),
    )
    for sc in _sorted_sales[:8]:
        _sale_rows.append({
            "address":        sc.address or "N/A",
            "property_type":  sc.asset_type or "",
            "sale_date":      sc.sale_date or "N/A",
            "price":          _fmt_price(sc.sale_price),
            "price_per_sf":   _fmt_price(sc.price_per_sf),
            "price_per_unit": _fmt_price(sc.price_per_unit),
            "units":          str(sc.num_units) if sc.num_units else "N/A",
            "cap_rate":       (f"{sc.cap_rate:.1f}%" if sc.cap_rate else "N/A"),
            "distance":       _fmt_dist(sc.distance_miles),
            "source":         sc.source or "N/A",
        })
    ctx["sale_comp_rows"] = _sale_rows
    ctx["has_sale_comps"] = len(_sale_rows) > 0
    logger.info("SALE COMP ROWS: %d total", len(_sale_rows))

    # ══════════════════════════════════════════════════════════════════════
    # OPTION 2 — Row builders moved out of _populate_data_tables so that the
    # HTML/Playwright report template can read them directly from ctx. The
    # docx pipeline is intentionally left untouched — _populate_data_tables
    # still rebuilds its own versions for the Word document.
    # ══════════════════════════════════════════════════════════════════════

    # ── Demographics — 4 indicators × (1-mile / 3-mile / trend / source) ──
    if md.population_1mi or md.median_hh_income_1mi or md.population_3mi:
        demographic_rows = [
            {"metric": "Population",
             "one_mile":  f"{md.population_1mi:,}" if md.population_1mi else "—",
             "three_mile": f"{md.population_3mi:,}" if md.population_3mi else "—",
             "trend": "—", "source": "2022 ACS 5-Year"},
            {"metric": "Median HH Income",
             "one_mile":  f"${md.median_hh_income_1mi:,.0f}" if md.median_hh_income_1mi else "—",
             "three_mile": f"${md.median_hh_income_3mi:,.0f}" if md.median_hh_income_3mi else "—",
             "trend": "—", "source": "2022 ACS 5-Year"},
            {"metric": "Renter Occupancy",
             "one_mile":  f"{md.pct_renter_occ_1mi:.1%}" if md.pct_renter_occ_1mi else "—",
             "three_mile": f"{md.pct_renter_occ_3mi:.1%}" if md.pct_renter_occ_3mi else "—",
             "trend": "—", "source": "2022 ACS 5-Year"},
            {"metric": "Unemployment Rate",
             "one_mile":  f"{md.unemployment_rate:.1%}" if md.unemployment_rate else "—",
             "three_mile": "—", "trend": "—", "source": "BLS / ACS 2022"},
        ]
    else:
        demographic_rows = [
            {"metric": "Population",        "one_mile": "—", "three_mile": "1,593,208",
             "trend": "Growing",       "source": "2022 ACS 5-Year"},
            {"metric": "Median HH Income",  "one_mile": "—", "three_mile": "$57,537",
             "trend": "Stable",        "source": "2022 ACS 5-Year"},
            {"metric": "Renter Occupancy",  "one_mile": "—", "three_mile": "47.8%",
             "trend": "High",          "source": "2022 ACS 5-Year"},
            {"metric": "Unemployment Rate", "one_mile": "—", "three_mile": "8.6%",
             "trend": "Above MSA avg", "source": "BLS / ACS 2022"},
        ]
    ctx["demographic_rows"] = demographic_rows
    logger.info("DEMOGRAPHIC_ROWS: %d rows built", len(demographic_rows))

    # ── Sources & Uses — mirrors the XLSX S&U tab structure ───────────────
    _su_tpc = fo.total_uses or 0
    def _su_pct(amt):
        return f"{(amt / _su_tpc * 100):.1f}%" if _su_tpc > 0 and amt else ""
    _su_prof_dd = sum(
        (getattr(a, k, 0) or 0)
        for k in ('legal_closing', 'title_insurance', 'legal_bank', 'appraisal',
                  'environmental', 'surveyor', 'architect', 'structural',
                  'civil_eng', 'meps', 'legal_zoning', 'geotech')
    )
    _su_hard = (getattr(a, 'const_hard', 0) or 0) + (getattr(a, 'const_reserve', 0) or 0)
    _su_orig = (fo.initial_loan_amount or 0) * (getattr(a, 'origination_fee_pct', 0.01) or 0.01)
    _su_xtax = (a.purchase_price or 0) * (getattr(a, 'transfer_tax_rate', 0.02139) or 0.02139)
    _su_loan = fo.initial_loan_amount or 0
    sources_uses_rows = [
        {"item": "Purchase Price",
         "amount": f"${a.purchase_price:,.0f}",     "pct": _su_pct(a.purchase_price or 0),
         "note":   "Acquisition"},
        {"item": "Transfer Tax",
         "amount": f"${_su_xtax:,.0f}",             "pct": _su_pct(_su_xtax),
         "note":   "PA buyer share"},
        {"item": "Professional & DD",
         "amount": f"${_su_prof_dd:,.0f}",          "pct": _su_pct(_su_prof_dd),
         "note":   "Legal, title, inspections"},
        {"item": "Construction Hard Costs",
         "amount": f"${_su_hard:,.0f}",             "pct": _su_pct(_su_hard),
         "note":   "Renovation + reserve"},
        {"item": "Origination Fee",
         "amount": f"${_su_orig:,.0f}",             "pct": _su_pct(_su_orig),
         "note":   f"{(getattr(a,'origination_fee_pct',0.01) or 0.01)*100:.1f}% of loan"},
        {"item": "Senior Debt",
         "amount": f"${_su_loan:,.0f}",
         "pct":    f"{(_su_loan / _su_tpc * 100):.0f}% LTV" if _su_tpc > 0 else "",
         "note":   f"{(getattr(a,'ltv_pct',0.70) or 0.70)*100:.0f}% LTV of TPC",
         "is_debt": True},
        {"item": "Total Equity Required",
         "amount": f"${fo.total_equity_required or 0:,.0f}",
         "pct":    _su_pct(fo.total_equity_required or 0),
         "note":   "GP + LP", "is_subtotal": True},
        {"item": "GP Equity",
         "amount": f"${fo.gp_equity or 0:,.0f}",
         "pct":    f"{(getattr(a,'gp_equity_pct',0.10) or 0.10)*100:.0f}%",
         "note":   "General partner"},
        {"item": "LP Equity",
         "amount": f"${fo.lp_equity or 0:,.0f}",
         "pct":    f"{(getattr(a,'lp_equity_pct',0.90) or 0.90)*100:.0f}%",
         "note":   "Limited partner"},
    ]
    ctx["sources_uses_rows"] = sources_uses_rows
    logger.info("SOURCES_USES_ROWS: %d rows (TPC=$%s, loan=$%s)",
                len(sources_uses_rows),
                f"{_su_tpc:,.0f}", f"{_su_loan:,.0f}")

    # ── Sensitivity matrix with per-cell CSS class ────────────────────────
    # Raw fo.sensitivity_matrix is list-of-lists of floats (LP IRR fractions)
    # or the string "N/A". Thresholds: ≥12% pass, ≥8% watch, <8% fail. Center
    # cell marked as base case.
    _raw_matrix = fo.sensitivity_matrix or []
    _rent_axis  = fo.sensitivity_axis_rent_growth or []
    sensitivity_cells: list = []
    for i, _row in enumerate(_raw_matrix):
        row_dict = {
            "rent_growth": f"{_rent_axis[i]:.1%}" if i < len(_rent_axis) else "",
            "cells": [],
        }
        is_base_row = (i == len(_raw_matrix) // 2)
        for j, val in enumerate(_row):
            is_base_col = (j == len(_row) // 2)
            if isinstance(val, (int, float)):
                try:
                    num = float(val)
                    display = f"{num*100:.1f}%"
                    if num >= 0.12:
                        cls = "s-pass"
                    elif num >= 0.08:
                        cls = "s-watch"
                    else:
                        cls = "s-fail"
                except Exception:
                    display, cls = "—", ""
            elif isinstance(val, str):
                display = val
                cls = ""
            else:
                display, cls = "—", ""
            if is_base_row and is_base_col:
                cls = "s-base"
            row_dict["cells"].append({"value": display, "css_class": cls})
        sensitivity_cells.append(row_dict)
    ctx["sensitivity_cells"] = sensitivity_cells
    logger.info("SENSITIVITY_CELLS: %d rows × %d cols",
                len(sensitivity_cells),
                len(sensitivity_cells[0]["cells"]) if sensitivity_cells else 0)

    # ── Sensitivity narrative override ────────────────────────────
    # The Sonnet prompt can mis-fire and emit the "pending stabilization"
    # boilerplate when the matrix actually contains data but most cells
    # are "N/A" due to non-convergent IRR (negative cash flows). Replace
    # that text with a deterministic description of actual matrix state.
    _numeric = 0
    _na      = 0
    for _r in sensitivity_cells:
        for _c in _r["cells"]:
            v = (_c.get("value") or "").strip()
            if v and v != "—" and v.upper() != "N/A":
                _numeric += 1
            elif v.upper() == "N/A":
                _na += 1
    _boilerplate_markers = (
        "Sensitivity analysis requires stabilized revenue data",
        "Matrix will be populated",
    )
    _existing = (ctx.get("sensitivity_narrative") or "").strip()
    _is_boilerplate = any(m in _existing for m in _boilerplate_markers)

    if sensitivity_cells and (_numeric > 0 or _na > 0):
        # Matrix has data. If the narrative is the misleading boilerplate
        # OR no narrative was produced at all, replace it.
        if _is_boilerplate or not _existing:
            if _numeric == 0:
                override = (
                    f"All {_na} tested cap-rate × rent-growth combinations "
                    "produce cash flows too negative for IRR convergence; no "
                    "scenario clears the 12% LP IRR threshold under the "
                    "current underwriting."
                )
            elif _na > 0:
                override = (
                    f"Most scenarios produce returns too negative to calculate "
                    f"a meaningful IRR. Only {_numeric} of {_numeric + _na} "
                    "tested cells contain computable values, concentrated in "
                    "the most favorable cap-rate × rent-growth corners; the "
                    "remainder represent non-convergent cash-flow paths."
                )
            else:
                override = None  # all-numeric: keep whatever the AI wrote
            if override:
                ctx["sensitivity_narrative"] = override
                logger.info(
                    "SENSITIVITY NARRATIVE: overrode boilerplate (numeric=%d, N/A=%d)",
                    _numeric, _na,
                )

    # ══════════════════════════════════════════════════════════════════════
    # TEMPLATE VARIABLE FALLBACKS — Jinja2 will raise UndefinedError if any
    # {{ var }} in the template has no ctx entry. Table placeholders get
    # populated post-render by _populate_data_tables, so their ctx value is
    # just an empty placeholder string ("") — the table itself is rebuilt
    # from scratch with real data after tpl.render() completes.
    # Non-table scalars (deal_source, report_title, etc.) resolve to real
    # values from DealData here.
    # ══════════════════════════════════════════════════════════════════════
    ctx.setdefault("report_title",             ctx.get("cover_title", deal.cover_title))
    ctx.setdefault("deal_type",                deal.deal_type or "")
    ctx.setdefault("deal_source",              (ext.deal_source or "") if ext else "")
    _ins_val = ins.insurance_proforma_line_item or a.insurance or 0
    ctx.setdefault("insurance_proforma_line_item",
                   f"${_ins_val:,.0f}" if _ins_val else "N/A")

    _table_placeholders = [
        "parcel_data_table", "zoning_standards_table", "transportation_table",
        "amenity_table", "demographics_table", "supply_pipeline_table",
        "unit_mix_table", "rent_roll_table", "rent_comp_table",
        "commercial_comp_table", "sale_comp_table", "income_summary_table",
        "assumptions_table", "sources_uses_table", "construction_budget_table",
        "proforma_table", "scenario_comparison_table", "go_nogo_table",
        "exit_table", "waterfall_table", "environmental_table",
        "climate_risk_table", "title_search_table", "violations_table",
        "liens_table", "ownership_history_table", "current_ownership_table",
        "timeline_table", "data_provenance_table", "certification_table",
    ]
    for _k in _table_placeholders:
        ctx.setdefault(_k, "")

    # ══════════════════════════════════════════════════════════════════════
    # SESSION 5 — ZONING OVERHAUL RENDERING KEYS
    # ══════════════════════════════════════════════════════════════════════
    # Per Session 5 kickoff §3.1. CP1 added 10 keys; CP2 adds the 11th
    # (zoning_nonconformity_flag) sourced from dd_flag_engine.get_zoning_flag.
    # All values have safe fallbacks; template never raises KeyError.
    ctx["conformity"] = _build_conformity_context(deal)
    ctx["scenarios"] = _annotate_scenarios_with_pathway_class(deal.scenarios)
    ctx["preferred_scenario"] = next(
        (s for s in (deal.scenarios or []) if s.verdict == ScenarioVerdict.PREFERRED),
        None,
    )
    ctx["zoning_ext"] = _build_zoning_ext_context(deal)
    ctx["zoning_nonconformity_flag"] = get_zoning_flag(deal)
    logger.info(
        "SESSION 5 CONTEXT: conformity.status=%s, scenarios=%d, preferred=%s, "
        "zoning_ext.use_flex=%s, nonconformity_flag=%s",
        ctx["conformity"]["status"],
        len(ctx["scenarios"]),
        ctx["preferred_scenario"].scenario_id if ctx["preferred_scenario"] else None,
        ctx["zoning_ext"]["use_flexibility_score"],
        ctx["zoning_nonconformity_flag"].flag_id if ctx["zoning_nonconformity_flag"] else None,
    )

    # ══════════════════════════════════════════════════════════════════════
    # CONTEXT COMPLETENESS AUDIT — logs every critical key's state so any
    # empty template section can be traced back to the exact missing value.
    # ══════════════════════════════════════════════════════════════════════
    _critical_keys = [
        # KPI / returns
        "kpi_rows", "purchase_price", "total_project_cost",
        "lp_irr", "project_irr", "lp_equity_multiple", "noi_yr1",
        "going_in_cap_rate", "dscr_yr1", "cash_on_cash_yr1",
        "hold_period", "total_equity_required", "initial_loan_amount",
        # Parcel A
        "parcel_a_account", "parcel_a_owner", "parcel_a_zoning",
        "parcel_a_year_built", "parcel_a_land_area", "parcel_a_assessed",
        # Zoning
        "zoning", "zoning_code", "zoning_standards_rows",
        # HBU
        "hbu_content", "hbu_narrative", "buildable_capacity_narrative",
        # Transit / amenities / demographics (market data)
        "transit_rows", "fmr_2br", "dgs10_rate",
        # Income / EGI
        "income_gpr", "income_egi", "income_vacancy_loss",
        # Comps / scenarios
        "rent_comp_rows", "sale_comp_rows",
        # Template placeholder table keys (must exist to avoid UndefinedError)
        "parcel_data_table", "zoning_standards_table",
        "transportation_table", "amenity_table", "demographics_table",
        "income_summary_table", "scenario_comparison_table",
        "proforma_table", "sources_uses_table", "waterfall_table",
        # Report scalars
        "report_title", "deal_type", "deal_source",
        "insurance_proforma_line_item",
    ]
    logger.info("=" * 60)
    logger.info("WORD BUILDER CONTEXT AUDIT — %d keys in ctx total", len(ctx))
    for _k in _critical_keys:
        _v = ctx.get(_k, "<MISSING>")
        if _v == "<MISSING>":
            logger.warning("  CTX[%s] = MISSING/NONE \u2190 FIX NEEDED", _k)
        elif _v is None:
            logger.warning("  CTX[%s] = None \u2190 FIX NEEDED", _k)
        elif isinstance(_v, list):
            logger.info("  CTX[%s] = list(%d items)", _k, len(_v))
        elif isinstance(_v, dict):
            logger.info("  CTX[%s] = dict(%d keys)", _k, len(_v))
        else:
            try:
                _repr = str(_v)[:80]
            except Exception:
                _repr = f"<{type(_v).__name__}>"
            logger.info("  CTX[%s] = %s", _k, _repr)
    logger.info("=" * 60)

    return ctx



# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINTS
# ═══════════════════════════════════════════════════════════════════════════

def generate_narratives(deal: DealData) -> None:
    """Run Prompt 4-MASTER, then Prompt 5D if investor_mode is True.
    Mutates deal.narratives / deal.recommendation in place."""
    _generate_narratives(deal)
    if deal.investor_mode:
        _rewrite_investor_narratives(deal)
