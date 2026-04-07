"""
excel_builder.py — DealDesk CRE Underwriting
=============================================
Populates the Assumptions tab of the correct Excel template from DealData.
Zero hardcoded values — every cell value sourced from DealData.

Template routing:
    stabilized / value_add  →  Hold_Template_v3.xlsx
    for_sale                →  Sale_Template_v3.xlsx

Output: {deal_id}_financial_model.xlsx  →  outputs/
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, List, Tuple

import openpyxl

from config import get_excel_template, OUTPUTS_DIR
from models.models import DealData, InvestmentStrategy


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

def populate_excel(deal: DealData) -> Path:
    """
    Copy the correct Excel template and populate the Assumptions tab
    with all values from DealData.  Returns path to the output file.
    """
    template_path = get_excel_template(deal.strategy_key, deal.asset_type_key)

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUTS_DIR / f"{deal.deal_id}_financial_model.xlsx"

    shutil.copy2(template_path, output_path)

    wb = openpyxl.load_workbook(output_path)
    ws = wb["Assumptions"]

    for cell_ref, value in _build_cell_map(deal):
        if value is not None:
            ws[cell_ref] = value

    wb.save(output_path)
    wb.close()

    deal.output_xlsx_path = str(output_path)
    return output_path


# ═══════════════════════════════════════════════════════════════════════════
# CELL MAP BUILDER
# ═══════════════════════════════════════════════════════════════════════════

CellMap = List[Tuple[str, Any]]


def _build_cell_map(deal: DealData) -> CellMap:
    """Assemble every (cell_ref, value) pair for the Assumptions sheet."""
    a = deal.assumptions
    ext = deal.extracted_docs
    fo = deal.financial_outputs
    is_hold = deal.investment_strategy != InvestmentStrategy.FOR_SALE

    cells: CellMap = []

    # ── Section 1: Property Information (rows 5–14) ──────────────
    cells += _section_property_info(deal, a, ext, is_hold)

    # ── Section 2: Acquisition (rows 17–20) ──────────────────────
    cells += _section_acquisition(a)

    # ── Section 3: Sources & Uses (rows 31–67 uses, 83–87 sources)
    cells += _section_uses(a)
    cells += _section_sources(a, fo)

    # ── Section 4: Initial Financing (rows 70–77) ────────────────
    cells += _section_financing(a)

    # ── Sections 5–7: Refinancing (rows 95–131) ─────────────────
    cells += _section_refis(a)

    # ── Sections 8–11A: Operating (Hold template only) ──────────
    if is_hold:
        cells += _section_operating_income(a)
        cells += _section_fixed_expenses(a)
        cells += _section_variable_expenses(a)
        cells += _section_below_the_line(a)
        cells += _section_dev_period(a)

    # ── Sections 12–15: Exit, Waterfall, EM, Sensitivity ────────
    #    Row positions shift between Hold and Sale templates.
    cells += _section_exit(a, is_hold)
    cells += _section_waterfall(a, is_hold)
    cells += _section_em_hurdles(a, is_hold)
    cells += _section_sensitivity(a, is_hold)

    return cells


# ═══════════════════════════════════════════════════════════════════════════
# SECTION BUILDERS
# ═══════════════════════════════════════════════════════════════════════════

def _section_property_info(
    deal: DealData, a, ext, is_hold: bool,
) -> CellMap:
    prop_name = ext.property_name or deal.address.full_address or ""
    num_units = a.num_units or ext.total_units_from_rr
    gba_sf = a.gba_sf or ext.gba_sf_extracted
    lot_sf = a.lot_sf or ext.lot_sf_extracted
    year_built = a.year_built or ext.year_built_extracted
    if year_built is None and deal.parcel_data:
        year_built = deal.parcel_data.year_built

    return [
        ("C5",  prop_name),
        ("C6",  deal.address.full_address),
        ("C7",  deal.asset_type.value),
        ("C8",  "Hold" if is_hold else "Sale"),
        ("C9",  num_units),
        ("C10", gba_sf),
        ("C11", lot_sf),
        ("C12", year_built),
        ("C13", deal.report_date),
        ("C14", a.hold_period),
    ]


def _section_acquisition(a) -> CellMap:
    return [
        ("C17", a.purchase_price),
        ("C18", a.transfer_tax_rate),
        ("C20", a.closing_costs_fixed),
    ]


def _section_uses(a) -> CellMap:
    return [
        # Acquisition
        ("C31", a.tenant_buyout),
        # Professional & Due Diligence
        ("C34", a.legal_closing),
        ("C35", a.title_insurance),
        ("C36", a.legal_bank),
        ("C37", a.appraisal),
        ("C38", a.environmental),
        ("C39", a.architect),
        ("C40", a.structural),
        ("C41", a.geotech),
        ("C42", a.surveyor),
        ("C43", a.civil_eng),
        ("C44", a.meps),
        ("C45", a.legal_zoning),
        # Financing Costs
        ("C48", a.acq_fee_fixed),
        ("C49", a.mortgage_carry),
        # C50 = Mortgage Fees / Origination (formula)
        ("C51", a.mezz_interest),
        # Soft Costs
        ("C54", a.working_capital),
        ("C55", a.marketing),
        ("C56", a.re_tax_carry),
        ("C57", a.prop_ins_carry),
        ("C58", a.dev_fee),
        ("C59", a.dev_pref),
        ("C60", a.permits),
        # Hard Costs
        ("C63", a.stormwater),
        ("C64", a.demo),
        ("C65", a.const_hard),
        ("C66", a.const_reserve),
        ("C67", a.gc_overhead),
    ]


def _section_sources(a, fo) -> CellMap:
    cells: CellMap = [
        # C82 = Senior Debt (formula)
        ("C83", a.mezz_debt),
        ("C86", a.tax_credit_equity),
        ("C87", a.grants),
    ]
    # GP / LP equity dollar amounts — computed from total equity if available
    if fo.total_equity_required is not None:
        cells += [
            ("C84", round(fo.total_equity_required * a.gp_equity_pct, 2)),
            ("C85", round(fo.total_equity_required * a.lp_equity_pct, 2)),
        ]
    return cells


def _section_financing(a) -> CellMap:
    return [
        ("C70", a.ltv_pct),
        # C71 = Initial Loan Amount (formula)
        ("C72", a.interest_rate),
        ("C73", a.amort_years),
        ("C74", a.loan_term),
        ("C75", a.origination_fee_pct),
        # C76 = Origination Fee $ (formula)
        ("C77", a.io_period_months),
        # C78 = Monthly Payment (formula)
        # C79 = Annual Debt Service (formula)
    ]


def _section_refis(a) -> CellMap:
    """Sections 5–7: up to 3 refinancing events."""
    bases = [95, 108, 121]
    cells: CellMap = []
    for i, refi in enumerate(a.refi_events[:3]):
        b = bases[i]
        cells += [
            (f"C{b}",      1 if refi.active else 0),
            (f"C{b + 1}",  refi.year),
            (f"C{b + 2}",  refi.appraised_value),
            (f"C{b + 3}",  refi.ltv),
            # b+4 = New Loan Amount (formula)
            (f"C{b + 5}",  refi.rate),
            (f"C{b + 6}",  refi.amort_years),
            (f"C{b + 7}",  refi.loan_term),
            (f"C{b + 8}",  refi.orig_fee_pct),
            (f"C{b + 9}",  refi.prepay_pct),
            (f"C{b + 10}", refi.closing_costs),
        ]
    return cells


# ── Hold-only operating sections ─────────────────────────────────────────

def _section_operating_income(a) -> CellMap:
    """Section 8: Operating Assumptions — Income (rows 134–139)."""
    return [
        ("C134", a.vacancy_rate),
        ("C135", a.annual_rent_growth),
        ("C136", a.expense_growth_rate),
        ("C137", a.loss_to_lease),
        ("C138", a.cam_reimbursements),
        ("C139", a.fee_income),
    ]


def _section_fixed_expenses(a) -> CellMap:
    """Section 9: Fixed Expenses Year 1 (rows 142–148)."""
    return [
        ("C142", a.re_taxes),
        ("C143", a.insurance),
        ("C144", a.gas),
        ("C145", a.water_sewer),
        ("C146", a.electric),
        ("C147", a.license_inspections),
        ("C148", a.trash),
    ]


def _section_variable_expenses(a) -> CellMap:
    """Section 10: Variable Expenses Year 1 (rows 152–162)."""
    return [
        ("C152", a.mgmt_fee_pct),
        ("C153", a.salaries),
        ("C154", a.repairs),
        ("C155", a.exterminator),
        ("C156", a.cleaning),
        ("C157", a.turnover),
        ("C158", a.advertising),
        ("C159", a.landscape_snow),
        ("C160", a.admin_legal_acct),
        ("C161", a.office_phone),
        ("C162", a.miscellaneous),
    ]


def _section_below_the_line(a) -> CellMap:
    """Section 11: Below-the-Line Items (rows 166–168)."""
    return [
        ("C166", a.commissions_yr1),
        ("C167", a.cap_reserve_per_unit),
        ("C168", a.renovations_yr1),
    ]


def _section_dev_period(a) -> CellMap:
    """Section 11A: Development Period & Carry Costs (rows 172–184)."""
    return [
        ("C172", a.const_period_months),
        ("C173", a.const_loan_rate),
        ("C174", a.const_hard),
        # C175 = Construction Budget Soft Costs (no single model field)
        # C176 = Total Construction Budget (formula)
        # C177 = Monthly Draw Rate (no model field)
        # C178 = Est. Interest Carry (formula)
        ("C181", a.leaseup_period_months),
        ("C182", a.leaseup_vacancy_rate),
        ("C183", a.leaseup_concessions),
        ("C184", a.leaseup_marketing),
        # C187 = Total Carry Costs (formula)
    ]


# ── Sections 12–15: row positions differ by template ─────────────────────

def _section_exit(a, is_hold: bool) -> CellMap:
    """Section 12: Exit Assumptions."""
    r = 192 if is_hold else 170
    return [
        (f"C{r + 1}", a.exit_cap_rate),
        (f"C{r + 2}", a.disposition_costs_pct),
    ]


def _section_waterfall(a, is_hold: bool) -> CellMap:
    """Section 13: Waterfall / Partnership Structure."""
    r = 196 if is_hold else 174
    tiers = a.waterfall_tiers
    return [
        (f"C{r + 1}",  a.gp_equity_pct),
        # r+2 = LP Equity % (formula: 1 − GP%)
        (f"C{r + 3}",  a.waterfall_type.value),
        (f"C{r + 4}",  a.simple_lp_split),
        (f"C{r + 5}",  round(1.0 - a.simple_lp_split, 6)),
        # Full waterfall tier structure
        (f"C{r + 8}",  a.pref_return),                   # Tier 1: Pref Return
        (f"C{r + 9}",  tiers[0].hurdle_value),            # Tier 2: IRR Hurdle
        (f"C{r + 10}", tiers[0].lp_share),                # Tier 2: LP Share
        (f"C{r + 11}", tiers[1].hurdle_value),             # Tier 3: IRR Hurdle
        (f"C{r + 12}", tiers[1].lp_share),                # Tier 3: LP Share
        (f"C{r + 13}", tiers[2].hurdle_value),             # Tier 4: IRR Hurdle
        (f"C{r + 14}", tiers[2].lp_share),                # Tier 4: LP Share
        (f"C{r + 15}", tiers[3].hurdle_value),             # Tier 5: IRR Hurdle
        (f"C{r + 16}", tiers[3].lp_share),                # Tier 5: LP Share
        (f"C{r + 17}", a.residual_tier.lp_share),          # Tier 6: Residual LP
    ]


def _section_em_hurdles(a, is_hold: bool) -> CellMap:
    """Section 14: Equity Multiple Hurdles."""
    r = 215 if is_hold else 193
    return [
        (f"C{r + 1}", a.em_hurdle_t1),
        (f"C{r + 2}", a.em_hurdle_t2),
        (f"C{r + 3}", a.em_hurdle_t3),
    ]


def _section_sensitivity(a, is_hold: bool) -> CellMap:
    """Section 15: Sensitivity Analysis Ranges."""
    r = 220 if is_hold else 198
    return [
        (f"C{r + 2}", a.sens_rent_growth_low),
        (f"C{r + 3}", a.sens_rent_growth_high),
        (f"C{r + 4}", a.sens_rent_growth_step),
        (f"C{r + 5}", a.sens_exit_cap_low),
        (f"C{r + 6}", a.sens_exit_cap_high),
        (f"C{r + 7}", a.sens_exit_cap_step),
    ]
