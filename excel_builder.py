"""
excel_builder.py — DealDesk CRE Underwriting
=============================================
Populates the Assumptions tab of the correct Excel template from DealData.
Zero hardcoded values — every cell value sourced from DealData.

Template routing (driven by InvestmentStrategy, not asset_type):
    stabilized_hold / value_add  →  Hold_Template_v3.xlsx
    opportunistic                →  Sale_Template_v3.xlsx

Output: {deal_id}_financial_model.xlsx  →  outputs/
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, List, Tuple

import openpyxl
from openpyxl.styles import PatternFill, Font

from config import get_excel_template, OUTPUTS_DIR
from models.models import AssetType, DealData, InvestmentStrategy

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

def populate_excel(deal: DealData) -> Path:
    """
    Copy the correct Excel template and populate the Assumptions tab
    with all values from DealData.  Returns path to the output file.
    """
    template_path = get_excel_template(deal.investment_strategy)

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUTS_DIR / f"{deal.deal_id}_financial_model.xlsx"

    shutil.copy2(template_path, output_path)

    wb = openpyxl.load_workbook(output_path)
    ws = wb["Assumptions"]

    for cell_ref, value in _build_cell_map(deal):
        if value is not None:
            ws[cell_ref] = value

    # ── Initial Loan, Origination Fee, Mortgage Fees — plain floats ──
    # These used to be a circular formula chain:
    #   C50 (mortgage fees) → C76 (orig fee) → C71 (loan) → C89 (total uses,
    #   which sums C50). Compute all three in Python from the authoritative
    #   financial_outputs + assumptions and write as plain floats so the S&U
    #   tab has no circular dependency.
    a = deal.assumptions
    fo = deal.financial_outputs
    initial_loan = round(fo.initial_loan_amount or 0.0, 2)
    origination_fee = round(initial_loan * (a.origination_fee_pct or 0.0), 2)

    logger.info(f"EXCEL [Assumptions]: writing origination_fee = {origination_fee:.2f}")
    logger.info(f"EXCEL [Assumptions]: writing initial_loan = {initial_loan:.2f}")

    ws["C71"] = initial_loan
    logger.info("ASSUMPTIONS [C71]: wrote initial_loan=%s (plain float, loan × LTV)",
                f"{initial_loan:,.2f}")

    ws["C76"] = origination_fee
    logger.info("ASSUMPTIONS [C76]: wrote origination_fee=%s (plain float, loan × %.4f)",
                f"{origination_fee:,.2f}", a.origination_fee_pct or 0.0)

    ws["C50"] = origination_fee
    logger.info("ASSUMPTIONS [C50]: wrote mortgage_fees=%s (plain float, = origination fee)",
                f"{origination_fee:,.2f}")

    # ── Refi guard: override Active? flags for skipped refis ────
    refi_active_cells = ["C95", "C108", "C121"]  # Refi 1, 2, 3 Active? cells
    for i, refi in enumerate(a.refi_events[:3]):
        if not refi.active:
            ws[refi_active_cells[i]] = 0
            logger.info("REFI GUARD: wrote Active=0 to %s (refi %d skipped)",
                        refi_active_cells[i], i + 1)

    # ── Style refi cap rate (blue input) and appraised value (computed) ──
    _style_refi_cap_rate_cells(ws)

    # ── Rent Roll tab ────────────────────────────────────────────
    if "Rent Roll" in wb.sheetnames:
        _populate_rent_roll(wb["Rent Roll"], deal)

    # ── Pro Forma tab — override GPR Year 1 from financial_outputs ─
    if "Pro Forma" in wb.sheetnames:
        _populate_pro_forma_gpr(wb["Pro Forma"], deal)

    # ── Sensitivity tab ──────────────────────────────────────────
    if "Sensitivity" in wb.sheetnames:
        _populate_sensitivity(wb["Sensitivity"], deal)

    # ── Refi Analysis tab — write amortized loan balances ────────
    if "Refi Analysis" in wb.sheetnames:
        _populate_refi_balances(wb["Refi Analysis"], deal)

    # ── Amort - Initial tab — override for IO loans ─────────────
    if "Amort - Initial" in wb.sheetnames:
        _populate_amort_initial_io(wb["Amort - Initial"], deal)

    # ── Constr Interest tab ──────────────────────────────────────
    if "Constr Interest" in wb.sheetnames:
        _populate_constr_interest_tab(wb["Constr Interest"], deal)

    # ── Exit tab — write exit year NOI and guard Gross Sale Price ──
    if "Exit" in wb.sheetnames:
        ws_exit = wb["Exit"]
        # Dynamic formula: NOI row 49 in Pro Forma, hold period in Assumptions C14
        ws_exit["B5"] = "=INDEX('Pro Forma'!$B$49:$K$49,Assumptions!$C$14)"
        logger.info("EXIT: wrote INDEX formula for exit year NOI (row 49, hold=Assumptions!C14)")

        gross_sale = max(0.0, fo.gross_sale_price) if fo.gross_sale_price is not None else 0.0
        if gross_sale <= 0:
            ws_exit["B7"] = 0.0
            ws_exit["C7"] = "Exit not viable \u2014 NOI \u2264 $0"
            logger.warning("Exit tab: wrote $0 Gross Sale Price (negative exit NOI)")
        else:
            ws_exit["B7"] = gross_sale

    wb.save(output_path)
    wb.close()

    recalculate_xlsx(str(output_path))

    deal.output_xlsx_path = str(output_path)
    return output_path


def _find_libreoffice() -> str:
    """Return the LibreOffice executable path for the current platform."""
    import sys
    if sys.platform == "win32":
        for candidate in [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]:
            if os.path.isfile(candidate):
                return candidate
    # Linux / macOS — 'libreoffice' or 'soffice' should be on PATH
    return "libreoffice"


def recalculate_xlsx(xlsx_path: str) -> str:
    """Use LibreOffice headless to force-recalculate all formulas."""
    import tempfile
    soffice = _find_libreoffice()
    basename = os.path.basename(xlsx_path)

    with tempfile.TemporaryDirectory() as tmp_dir:
        result = subprocess.run([
            soffice, "--headless", "--calc",
            "--convert-to", "xlsx:Calc MS Excel 2007 XML",
            "--outdir", tmp_dir,
            xlsx_path
        ], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"LibreOffice recalc failed: {result.stderr}")
        recalced = os.path.join(tmp_dir, basename)
        if not os.path.isfile(recalced):
            raise RuntimeError(f"LibreOffice produced no output at {recalced}")
        shutil.move(recalced, xlsx_path)
    return xlsx_path


# ═══════════════════════════════════════════════════════════════════════════
# RENT ROLL POPULATION
# ═══════════════════════════════════════════════════════════════════════════

# Template layout:
#   Residential rows 6–25 (B=Unit#, C=Type, D=SF, E=Rent/Mo, H=Status, I=LeaseExp, J=MarketRent)
#   Commercial  rows 35–39 (B=Tenant, C=Suite, D=SF, E=Rent/SF/Yr, G=LeaseType, H=Start, I=Expiry, J=TI/SF)
_RES_START, _RES_END = 6, 25
_COM_START, _COM_END = 35, 39

_COMMERCIAL_ASSET_TYPES = {AssetType.OFFICE, AssetType.RETAIL}


def _is_commercial_asset(deal: DealData) -> bool:
    return deal.asset_type in _COMMERCIAL_ASSET_TYPES


def _clear_rows(ws, start: int, end: int, cols: str) -> None:
    """Set all cells in the given row range + columns to None."""
    for row in range(start, end + 1):
        for col in cols:
            ws[f"{col}{row}"] = None


def _populate_rent_roll(ws, deal: DealData) -> None:
    """Write unit-level or tenant-level data to the Rent Roll sheet."""
    ext = deal.extracted_docs
    units = (ext.unit_mix or []) if ext else []

    if _is_commercial_asset(deal):
        _populate_rent_roll_commercial(ws, units, deal)
    else:
        _populate_rent_roll_residential(ws, units, deal)


def _populate_rent_roll_residential(ws, units: list, deal: DealData | None = None) -> None:
    """Write residential unit data to rows 6–25."""
    # ── If no unit-level data, try to write a summary row from assumptions ──
    if not units and deal is not None:
        a = deal.assumptions
        fo = deal.financial_outputs
        monthly_rent = getattr(fo, "gross_potential_rent", None) or 0
        # gross_potential_rent is annual; we need the monthly total for display
        num_units = a.num_units or 0
        gba_sf = a.gba_sf or 0
        gpr_yr1 = monthly_rent  # this is already annual from financial_outputs

        if gpr_yr1 > 0 and num_units > 0:
            row = _RES_START
            avg_sf = round(gba_sf / num_units, 0) if gba_sf and num_units else None
            rent_per_mo = round(gpr_yr1 / 12 / num_units, 2) if num_units else 0
            annual_rent = round(gpr_yr1, 2)

            ws[f"B{row}"] = "All Units (Summary)"
            ws[f"C{row}"] = deal.asset_type.value
            ws[f"D{row}"] = avg_sf
            ws[f"E{row}"] = rent_per_mo
            # G = Annual Rent — write directly since formula won't compute
            ws[f"G{row}"] = annual_rent
            ws[f"H{row}"] = "Occupied"

            logger.info(
                "EXCEL Rent Roll: wrote summary row — monthly_rent=%s, "
                "num_units=%s, gpr_yr1=%s",
                round(gpr_yr1 / 12, 2), num_units, gpr_yr1,
            )

            # Clear remaining rows
            for r in range(_RES_START + 1, _RES_END + 1):
                for col in "BCDEHIJ":
                    ws[f"{col}{r}"] = None
            return

    # Leasing column headers for residential
    ws[f"K{_RES_START - 1}"] = "Market ($/mo)"
    ws[f"L{_RES_START - 1}"] = "Lease Term (Yrs)"
    ws[f"M{_RES_START - 1}"] = "Expiry (Hold Yr)"
    ws[f"N{_RES_START - 1}"] = "Renewal Prob (%)"

    for i, row in enumerate(range(_RES_START, _RES_END + 1)):
        if i < len(units):
            u = units[i]
            ws[f"B{row}"] = u.get("unit_id") or f"{i+1:03d}"
            ws[f"C{row}"] = u.get("unit_type")
            ws[f"D{row}"] = u.get("sf")
            ws[f"E{row}"] = u.get("monthly_rent")
            # F (Rent/SF/Mo) and G (Annual Rent) are formulas — leave intact
            ws[f"H{row}"] = _normalise_status(u.get("status"))
            ws[f"I{row}"] = u.get("lease_end")
            ws[f"J{row}"] = u.get("market_rent")
            # Leasing fields
            ws[f"K{row}"] = u.get("market_rent_sf") or 0
            ws[f"L{row}"] = u.get("lease_term_years") or 1
            ws[f"M{row}"] = u.get("lease_expiry_year") or 0
            ws[f"N{row}"] = round((u.get("renewal_probability") or 0.70) * 100, 0)
        else:
            # Clear unused rows to avoid template defaults
            for col in "BCDEHIJ":
                ws[f"{col}{row}"] = None


def _populate_rent_roll_commercial(ws, units: list, deal: DealData) -> None:
    """
    For Office/Retail deals: zero out residential rows and populate
    the commercial tenant section (rows 35–39).
    """
    # ── Clear residential rows — set values to 0/blank/Vacant ───
    for row in range(_RES_START, _RES_END + 1):
        ws[f"B{row}"] = None
        ws[f"C{row}"] = None
        ws[f"D{row}"] = 0
        ws[f"E{row}"] = 0
        # F and G are formulas — leave intact (they'll compute to 0)
        ws[f"H{row}"] = "Vacant"
        ws[f"I{row}"] = None
        ws[f"J{row}"] = 0

    # ── Filter for commercial units ─────────────────────────────
    commercial_units = [
        u for u in units
        if (u.get("unit_type") or "").lower() in ("commercial", "office", "retail", "other")
        or u.get("annual_rent_per_sf") is not None
        or u.get("lease_type") is not None
    ]
    # If no units explicitly tagged as commercial, treat all units as tenants
    if not commercial_units:
        commercial_units = units

    # ── Check if all commercial tenant rows are empty ───────────
    all_empty = all(
        not t.get("tenant_name") and not t.get("sf")
        for t in commercial_units
    ) if commercial_units else True

    gpr = getattr(deal.financial_outputs, "gross_potential_rent", None) or 0

    # ── GPR fallback: if no tenant data but GPR exists, write it ─
    if all_empty and gpr > 0:
        gba = deal.assumptions.gba_sf
        if not gba or gba <= 0:
            gba = 18000
        rent_per_sf = round(gpr / gba, 2)
        row = _COM_START
        logger.info(f"[EXCEL] Writing GPR fallback to commercial rent row: ${gpr:,.0f}")
        ws[f"B{row}"] = "All Tenants (GPR)"
        ws[f"C{row}"] = None
        ws[f"D{row}"] = gba
        ws[f"E{row}"] = rent_per_sf
        ws[f"G{row}"] = None
        ws[f"H{row}"] = None
        ws[f"I{row}"] = None
        ws[f"J{row}"] = 0
        # Clear remaining rows
        for row2 in range(_COM_START + 1, _COM_END + 1):
            for col in "BCDEGHI":
                ws[f"{col}{row2}"] = None
            ws[f"D{row2}"] = 0
            ws[f"E{row2}"] = 0
            ws[f"J{row2}"] = 0
        return

    # ── Write commercial tenants to rows 35–39 ──────────────────
    # Add leasing column headers (K–O)
    ws["K34"] = "Market ($/SF/yr)"
    ws["L34"] = "Lease Term (Yrs)"
    ws["M34"] = "Expiry (Hold Yr)"
    ws["N34"] = "Renewal Prob (%)"
    ws["O34"] = "Downtime (Mo)"

    for i, row in enumerate(range(_COM_START, _COM_END + 1)):
        if i < len(commercial_units):
            t = commercial_units[i]
            ws[f"B{row}"] = t.get("tenant_name")
            ws[f"C{row}"] = t.get("unit_id") or t.get("suite")
            ws[f"D{row}"] = t.get("sf") or 0
            # Derive annual rent/SF: prefer explicit, else compute from monthly_rent
            rent_per_sf = t.get("annual_rent_per_sf") or t.get("rent_per_sf_yr")
            if rent_per_sf is None:
                monthly = t.get("monthly_rent")
                sf = t.get("sf")
                if monthly and sf and sf > 0:
                    rent_per_sf = round((monthly * 12) / sf, 2)
            ws[f"E{row}"] = rent_per_sf or 0
            # F (Annual Rent) is a formula — leave intact
            ws[f"G{row}"] = t.get("lease_type")
            ws[f"H{row}"] = t.get("lease_start")
            ws[f"I{row}"] = t.get("lease_end")
            ws[f"J{row}"] = t.get("ti_per_sf") or 0
            # Leasing fields
            ws[f"K{row}"] = t.get("market_rent_sf") or 0
            ws[f"L{row}"] = t.get("lease_term_years") or 5
            ws[f"M{row}"] = t.get("lease_expiry_year") or 0
            ws[f"N{row}"] = round((t.get("renewal_probability") or 0.70) * 100, 0)
            ws[f"O{row}"] = t.get("downtime_months") or 3
        else:
            # Clear unused commercial rows
            for col in "BCDEGHI":
                ws[f"{col}{row}"] = None
            ws[f"D{row}"] = 0
            ws[f"E{row}"] = 0
            ws[f"J{row}"] = 0


def _normalise_status(raw: str | None) -> str:
    """Map extracted lease status strings to the template's expected values."""
    if not raw:
        return "Vacant"
    lower = raw.lower().strip()
    if lower in ("occupied", "current", "leased"):
        return "Occupied"
    if lower in ("vacant", "available"):
        return "Vacant"
    return raw.title()


# ═══════════════════════════════════════════════════════════════════════════
# SENSITIVITY TAB POPULATION
# ═══════════════════════════════════════════════════════════════════════════

# Template layout (hold_template.xlsx → "Sensitivity" sheet):
#   Grid 1 — LEVERED IRR:       rows 7–13, cols C–H  (rent_growth × exit_cap)
#   Grid 2 — EQUITY MULTIPLE:   rows 7–13, cols L–Q  (same axes)
#   Grid 3 — YEAR 1 NOI:        rows 21–25, cols C–H (exp_growth × vacancy)
#   Grid 4 — YEAR 1 CoC:        rows 21–25, cols L–Q (ltv × purchase_price)

def _populate_sensitivity(ws, deal: DealData) -> None:
    """Write computed sensitivity grids into the Sensitivity tab."""
    fo = deal.financial_outputs

    _write_grid(ws, fo.sensitivity_matrix, row_start=7, col_start=3,
                max_rows=7, max_cols=6, fmt_fn=_fmt_pct)
    _write_grid(ws, fo.sensitivity_em_matrix, row_start=7, col_start=12,
                max_rows=7, max_cols=6, fmt_fn=_fmt_em)
    _write_grid(ws, fo.sensitivity_noi_matrix, row_start=21, col_start=3,
                max_rows=5, max_cols=6, fmt_fn=_fmt_dollar)
    _write_grid(ws, fo.sensitivity_coc_matrix, row_start=21, col_start=12,
                max_rows=5, max_cols=6, fmt_fn=_fmt_pct)


def _write_grid(
    ws, matrix: list | None,
    row_start: int, col_start: int,
    max_rows: int, max_cols: int,
    fmt_fn,
) -> None:
    """Write a 2-D matrix into a rectangular cell region.

    If matrix is None or empty, leave existing cell values (template "—") intact.
    """
    if not matrix:
        return
    for r_idx, row_data in enumerate(matrix[:max_rows]):
        for c_idx, val in enumerate(row_data[:max_cols]):
            ws.cell(
                row=row_start + r_idx,
                column=col_start + c_idx,
                value=fmt_fn(val),
            )


def _fmt_pct(v) -> float | str:
    """Return value as-is (already a decimal like 0.0823); Excel cell is
    formatted as percentage by the template. Pass through 'N/A' strings."""
    if isinstance(v, str):
        return v
    return v


def _fmt_em(v: float) -> float:
    """Equity multiple — raw number like 1.85."""
    return v


def _fmt_dollar(v: float) -> float:
    """Dollar amount — raw number; Excel cell has $ formatting."""
    return round(v, 0)


# ═══════════════════════════════════════════════════════════════════════════
# CELL MAP BUILDER
# ═══════════════════════════════════════════════════════════════════════════

CellMap = List[Tuple[str, Any]]


def _build_cell_map(deal: DealData) -> CellMap:
    """Assemble every (cell_ref, value) pair for the Assumptions sheet."""
    a = deal.assumptions
    ext = deal.extracted_docs
    fo = deal.financial_outputs
    is_hold = deal.investment_strategy != InvestmentStrategy.OPPORTUNISTIC

    cells: CellMap = []

    # ── Section 1: Property Information (rows 5–14) ──────────────
    cells += _section_property_info(deal, a, ext, is_hold)

    # ── Section 2: Acquisition (rows 17–20) ──────────────────────
    cells += _section_acquisition(a)

    # ── Section 3: Sources & Uses (rows 31–67 uses, 83–87 sources)
    cells += _section_uses(a, fo)
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


def _section_uses(a, fo=None) -> CellMap:
    # Log the sum of values Python writes, to compare with financials.py total_uses
    excel_total_uses = (
        a.purchase_price +                              # C17 (acq section)
        a.purchase_price * a.transfer_tax_rate +        # C18→C19 (transfer tax)
        a.closing_costs_fixed +                         # C20 (acquisition loan closing costs)
        a.tenant_buyout +                               # C31
        # Professional
        a.legal_closing + a.title_insurance + a.legal_bank +
        a.appraisal + a.environmental + a.architect +
        a.structural + a.geotech + a.surveyor + a.civil_eng +
        a.meps + a.legal_zoning +
        # Financing
        a.acq_fee_fixed +
        # (origination fee is a formula in Excel: C50)
        # Soft
        a.working_capital + a.marketing + a.re_tax_carry +
        a.prop_ins_carry + a.dev_fee + a.dev_pref + a.permits +
        # Hard
        a.stormwater + a.demo + a.const_hard +
        a.const_reserve + a.gc_overhead
    )
    logger.info("EXCEL S&U total written (excl origination, closing_costs, mortgage_carry): %s", excel_total_uses)
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
        ("C49", getattr(fo, 'construction_interest_carry', 0.0) or 0.0),
        # C50 = Mortgage Fees / Origination (formula)
        ("C51", 0.0),  # mezzanine interest — removed from standard model
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
        ("C83", 0.0),  # mezzanine debt — removed from standard model
        ("C86", a.tax_credit_equity),
        ("C87", a.grants),
    ]
    # GP / LP equity — derive from the template's own equity calc (C91)
    # so that Total Sources always equals Total Uses.
    #   C91 = C89 (Total Uses) − C82 (Senior Debt) − C83 (Mezz)
    #   C84 = C91 × GP%   (C197 = gp_equity_pct)
    #   C85 = C91 × LP%   (C198 = 1 − GP%)
    # Writing formulas instead of Python-computed values avoids a
    # mismatch between Python's total_uses and Excel's C89 sum.
    cells += [
        ("C84", "=C91*C197"),
        ("C85", "=C91*C198"),
    ]
    logger.info("GP/LP equity: written as formulas =C91*C197, =C91*C198 "
                "(C91=Total Equity Required, C197=GP%%, C198=LP%%)")
    return cells


def _style_refi_cap_rate_cells(ws) -> None:
    """Style refi cap rate cells as blue inputs, appraised value as computed."""
    blue_fill = PatternFill(start_color="FFFFF0", end_color="FFFFF0", fill_type="solid")
    blue_font = Font(color="0000FF", bold=True)
    computed_fill = PatternFill(start_color="F5EFE4", end_color="F5EFE4", fill_type="solid")
    computed_font = Font(color="4A6E50", italic=True)
    cap_rate_fmt = "0.00%"
    dollar_fmt = r'\$#,##0;"($"#,##0\);\-'

    bases = [95, 108, 121]
    cap_rows = [106, 119, 132]
    for b, cr in zip(bases, cap_rows):
        # Cap rate cell — blue editable input
        for col in ["B", "C"]:
            cell = ws[f"{col}{cr}"]
            cell.fill = blue_fill
            cell.font = blue_font
        ws[f"C{cr}"].number_format = cap_rate_fmt
        # Appraised value cell — computed (formula)
        cell = ws[f"C{b + 2}"]
        cell.fill = computed_fill
        cell.font = computed_font
        cell.number_format = dollar_fmt


def _populate_refi_balances(ws, deal: DealData) -> None:
    """Write amortized loan balances to Refi Analysis B17/C17/D17.

    Overwrites the template formula (which uses the original loan amount)
    with the actual amortized balance at the refi year, computed by
    financials.py from the Amort - Initial schedule.

    Also writes guarded (skipped) refi values: when refi.active was set
    to False by the NOI guard, override Refi Analysis cells to show $0.
    """
    a = deal.assumptions
    fo = deal.financial_outputs
    balances = fo.loan_balance_at_refi or [None, None, None]
    cols = ["B", "C", "D"]  # Refi 1, 2, 3

    for i, refi in enumerate(a.refi_events[:3]):
        col = cols[i]

        if not refi.active:
            # Refi was guarded/skipped — write zeros and a note
            ws[f"{col}6"] = 0.0    # Appraised Value
            ws[f"{col}8"] = 0.0    # New Loan Amount
            ws[f"{col}22"] = 0.0   # Net Refi Proceeds
            logger.info("Refi Analysis %s: refi skipped (NOI ≤ $0) — wrote $0 values", col)
            continue

        bal = balances[i] if i < len(balances) else None
        if bal is not None:
            cell = f"{col}17"
            ws[cell] = bal
            logger.info("Refi Analysis %s: wrote amortized balance $%s "
                        "(overriding original loan amount formula)", cell, f"{bal:,.0f}")


def _populate_constr_interest_tab(ws, deal: DealData) -> None:
    """
    Populate the 'Constr Interest' sheet with the monthly S-curve draw
    schedule and summary stats computed by financials.py.
    """
    fo = deal.financial_outputs
    a  = deal.assumptions

    schedule = getattr(fo, 'construction_interest_schedule', []) or []
    carry    = getattr(fo, 'construction_interest_carry', 0.0) or 0.0

    # ── Summary header block (rows 2–11) ────────────────────────────
    ws["B2"] = "CONSTRUCTION LOAN INTEREST SCHEDULE"
    ws["B3"] = "S-curve draw model — interest accrues on drawn balance only"

    ws["B5"] = "Acquisition Loan Amount"
    ws["C5"] = getattr(fo, 'initial_loan_amount', 0.0) or 0.0

    ws["B6"] = "Construction Period (Months)"
    ws["C6"] = getattr(a, 'const_period_months', 0) or 0

    ws["B7"] = "Permit / Mobilization Lag (Months)"
    ws["C7"] = getattr(a, 'draw_start_lag', 1)

    ws["B8"] = "Annual Interest Rate"
    ws["C8"] = getattr(a, 'interest_rate', 0.0) or 0.0

    hard_total = (getattr(a, 'const_hard', 0.0) or 0.0) + \
                 (getattr(a, 'const_reserve', 0.0) or 0.0)
    tpc = getattr(fo, 'total_project_cost', 0.0) or 0.0
    ws["B9"]  = "Hard Cost Share (% of Total Project Cost)"
    ws["C9"]  = round(hard_total / tpc, 4) if tpc > 0 else 0.0

    ws["B10"] = "Total Construction Interest Carry"
    ws["C10"] = carry

    # ── Column headers (row 13) ──────────────────────────────────────
    HDR_ROW = 13
    headers = [
        "Month",
        "Monthly Draw ($)",
        "Cumulative Draw %",
        "Outstanding Balance ($)",
        "Monthly Interest ($)",
    ]
    for col_offset, h in enumerate(headers):
        ws.cell(row=HDR_ROW, column=2 + col_offset, value=h)

    # ── Data rows ────────────────────────────────────────────────────
    if not schedule:
        ws.cell(row=HDR_ROW + 1, column=2,
                value="No construction period — interest carry = $0")
    else:
        for i, entry in enumerate(schedule):
            r = HDR_ROW + 1 + i
            ws.cell(row=r, column=2, value=entry.get("month"))
            ws.cell(row=r, column=3, value=entry.get("monthly_draw"))
            ws.cell(row=r, column=4, value=entry.get("cumulative_draw_pct"))
            ws.cell(row=r, column=5, value=entry.get("outstanding_balance"))
            ws.cell(row=r, column=6, value=entry.get("monthly_interest"))

        # Totals row
        total_row = HDR_ROW + 1 + len(schedule)
        ws.cell(row=total_row, column=2, value="TOTAL")
        ws.cell(row=total_row, column=6, value=carry)

    logger.info(
        "CONSTR INTEREST TAB: wrote %d schedule rows, total_carry=%s",
        len(schedule), f"{carry:,.2f}"
    )


def _populate_amort_initial_io(ws, deal: DealData) -> None:
    """Override Amort - Initial tab for IO loans (amort_years == 0).

    The template formula PMT(rate/12, 0, -loan) errors when amort=0.
    For IO loans: payment = interest-only, principal = 0, balance = flat.
    """
    a = deal.assumptions
    if a.amort_years and a.amort_years > 0:
        return  # amortizing loan — let template formulas handle it

    fo = deal.financial_outputs
    loan_amount = fo.initial_loan_amount or 0.0
    annual_rate = a.interest_rate or 0.0
    monthly_payment = loan_amount * (annual_rate / 12) if annual_rate > 0 else 0.0

    # Override the summary cells
    ws["B3"] = loan_amount      # Loan Amount (override formula =Assumptions!C71)
    ws["B6"] = monthly_payment  # Monthly Payment (template formula errors for IO)

    # Override each month row (rows 9–368 = 360 months max)
    max_months = min(a.loan_term * 12 if a.loan_term else 120, 360)
    for month in range(1, max_months + 1):
        row = 8 + month  # row 9 = month 1
        ws[f"A{row}"] = month
        ws[f"B{row}"] = monthly_payment   # Payment
        ws[f"C{row}"] = 0.0               # Principal
        ws[f"D{row}"] = monthly_payment   # Interest
        ws[f"E{row}"] = loan_amount       # Balance (flat)

    logger.info("AMORT INITIAL: IO loan — flat balance at %s, monthly payment=%s",
                f"{loan_amount:,.2f}", f"{monthly_payment:,.2f}")


def _populate_pro_forma_gpr(ws, deal: DealData) -> None:
    """Override Pro Forma rows that the template formulas get wrong.

    1) GPR row 6: template formula ='Rent Roll'!E45 evaluates to 0 when
       Rent Roll has no unit data.  Write financial_outputs.gross_potential_rent
       directly, compounded by rent_growth.

    2) Debt Service row 60: template formula switches to refi payment in the
       refi year itself (year >= refi_year).  Correct convention: debt service
       in year N uses the loan active at the START of year N, so the new
       payment starts the year AFTER the refi.  Override with Python-computed
       values from pro_forma_years which already follow this convention.
    """
    fo = deal.financial_outputs
    hold_period = deal.assumptions.hold_period or 10
    cols = "BCDEFGHIJK"  # Year 1–10
    num_years = min(hold_period, 10)

    # ── Stabilization Factor row 4 ──────────────────────────────
    # Override the template's formula-driven stab factors with the
    # Python-computed values so Excel matches the financial model exactly.
    from financials import _get_stabilization_factors
    stab_factors = _get_stabilization_factors(deal)
    for n in range(num_years):
        ws[f"{cols[n]}4"] = round(stab_factors[n], 4)
    logger.info(
        "EXCEL Pro Forma: wrote Stabilization Factor row 4 — %s",
        [f"Y{i+1}={v:.2f}" for i, v in enumerate(stab_factors[:num_years])]
    )

    # ── GPR row 6 ────────────────────────────────────────────────
    gpr_yr1 = fo.gross_potential_rent if fo else None
    if gpr_yr1 and gpr_yr1 > 0:
        rent_growth = deal.assumptions.annual_rent_growth or 0.03
        for n in range(num_years):
            value = gpr_yr1 * (1 + rent_growth) ** n
            ws[f"{cols[n]}6"] = round(value, 2)
        logger.info(
            "EXCEL Pro Forma: wrote GPR row — gpr_yr1=%s, rent_growth=%s, years=%s",
            gpr_yr1, rent_growth, num_years,
        )

    proforma = fo.pro_forma_years if fo else None

    # ── Debt Service row 60 — formula-driven from Amort sheets ───
    #
    # Each Amort sheet has month rows starting at row 9 (Month 1).
    # Annual DS for Year Y = SUM of column B, rows (Y-1)*12+9 to Y*12+8:
    #   Year 1 → SUM(B9:B20)
    #   Year 2 → SUM(B21:B32)
    #   ...
    #   Year Y → SUM(B{(Y-1)*12+9}:B{Y*12+8})
    #
    # Logic: use the most recently executed refi at or before year Y.
    # Refi timing cells in Assumptions:
    #   Refi 1: Active=C95, Timing=C96
    #   Refi 2: Active=C108, Timing=C109
    #   Refi 3: Active=C121, Timing=C122
    #
    # Formula structure (nested IF, outermost = highest refi):
    #   =IF(AND(Assumptions!C121=1, Assumptions!C122<=Y),
    #        SUM('Amort - Refi 3'!B{s}:B{e}),
    #    IF(AND(Assumptions!C108=1, Assumptions!C109<=Y),
    #        SUM('Amort - Refi 2'!B{s}:B{e}),
    #    IF(AND(Assumptions!C95=1, Assumptions!C96<=Y),
    #        SUM('Amort - Refi 1'!B{s}:B{e}),
    #        SUM('Amort - Initial'!B{s}:B{e})
    #    )))

    ds_formulas = []
    for n in range(num_years):
        yr = n + 1
        # Row range in amort sheets for this year
        r_start = (yr - 1) * 12 + 9   # e.g. Year 1 → 9
        r_end   = yr * 12 + 8          # e.g. Year 1 → 20

        # Inner-most = Initial loan
        initial_sum  = f"SUM('Amort - Initial'!B{r_start}:B{r_end})"
        refi1_sum    = f"SUM('Amort - Refi 1'!B{r_start}:B{r_end})"
        refi2_sum    = f"SUM('Amort - Refi 2'!B{r_start}:B{r_end})"
        refi3_sum    = f"SUM('Amort - Refi 3'!B{r_start}:B{r_end})"

        # Build the nested IF from innermost out
        # Refi 1 check: active (C95=1) AND timing (C96) <= this year
        f_r1 = (
            f"IF(AND(Assumptions!$C$95=1,Assumptions!$C$96<={yr}),"
            f"{refi1_sum},{initial_sum})"
        )
        # Refi 2 check: if refi2 active AND timing <= year, use refi2;
        # otherwise fall through to refi1 check
        f_r2 = (
            f"IF(AND(Assumptions!$C$108=1,Assumptions!$C$109<={yr}),"
            f"{refi2_sum},{f_r1})"
        )
        # Refi 3 check: outermost
        formula = (
            f"=IF(AND(Assumptions!$C$121=1,Assumptions!$C$122<={yr}),"
            f"{refi3_sum},{f_r2})"
        )

        ws[f"{cols[n]}60"] = formula
        ds_formulas.append(f"Y{yr}=formula")

    logger.info("PRO FORMA DS: wrote formula-driven debt service row 60 "
                "(IF cascade: Refi3→Refi2→Refi1→Initial, SUM of monthly "
                "payment rows from each Amort sheet) for %d years", num_years)

    # ── Commissions row 53 — year-specific from lease events ──────
    lease_events = fo.lease_events or {}
    if proforma:
        comm_list = []
        for n in range(num_years):
            yr = n + 1
            comm = 0.0
            if n < len(proforma):
                comm = proforma[n].get("leasing_commission", 0.0)
            ws[f"{cols[n]}53"] = round(comm, 2)
            comm_list.append(round(comm, 0))
        # Also write Year 1 commission to Assumptions C166 for reference
        yr1_comm = comm_list[0] if comm_list else 0
        ws_assumptions = ws.parent["Assumptions"] if "Assumptions" in ws.parent.sheetnames else None
        if ws_assumptions:
            ws_assumptions["C166"] = yr1_comm
        logger.info("PRO FORMA COMMISSIONS: %s",
                    [f"Y{i+1}={v:,.0f}" for i, v in enumerate(comm_list)])

    # ── Leasing Costs row 56 (TI + downtime, excl commissions) ──
    if proforma:
        ws["A56"] = "  Leasing Costs (TI + Downtime)"
        lc_list = []
        has_any = False
        for n in range(num_years):
            if n < len(proforma):
                lc = (proforma[n].get("tenant_improvements", 0.0)
                      + proforma[n].get("downtime_loss", 0.0))
            else:
                lc = 0.0
            ws[f"{cols[n]}56"] = round(lc, 2)
            lc_list.append(round(lc, 0))
            if lc > 0:
                has_any = True
        if has_any:
            # Override NOCF row 57 to include leasing costs
            for n in range(num_years):
                c = cols[n]
                ws[f"{c}57"] = f"={c}49-{c}53-{c}54-{c}55-{c}56"
            logger.info("LEASING COSTS (TI+downtime): %s", lc_list)

    # ── Cash-on-Cash Return row 64 ──────────────────────────────
    # CoC = Free Cash Flow / LP Equity (Assumptions C85)
    ws["A64"] = "Cash-on-Cash Return"
    for n in range(num_years):
        c = cols[n]
        ws[f"{c}64"] = f"=IF(Assumptions!$C$85=0,0,{c}63/Assumptions!$C$85)"
    # Format as percentage
    for n in range(num_years):
        ws[f"{cols[n]}64"].number_format = "0.00%"
    logger.info("EXCEL Pro Forma: wrote Cash-on-Cash Return row 64 (FCF/LP equity)")


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
    """Sections 5–7: up to 3 refinancing events.

    Gap rows (106, 119, 132) are used for per-refi appraisal cap rates.
    Appraised Value cells (C97, C110, C123) are written as formulas:
        =IF(active=0, 0, INDEX('Pro Forma' NOI row, refi year) / cap_rate)
    """
    bases = [95, 108, 121]
    cap_rate_rows = [106, 119, 132]  # gap rows repurposed for cap rate
    cells: CellMap = []
    for i, refi in enumerate(a.refi_events[:3]):
        b = bases[i]
        cr = cap_rate_rows[i]
        # Appraised Value formula: NOI in refi year / cap rate
        # Pro Forma NOI is row 49, Years 1-10 are columns B-K
        appraised_formula = (
            f"=IF(C{b}=0,0,INDEX('Pro Forma'!$B$49:$K$49,"
            f"MATCH(C{b + 1},{{1,2,3,4,5,6,7,8,9,10}},0))/C{cr})"
        )
        cells += [
            (f"C{b}",      1 if refi.active else 0),
            (f"C{b + 1}",  refi.year),
            (f"C{b + 2}",  appraised_formula),             # formula, not value
            (f"C{b + 3}",  refi.ltv),
            # b+4 = New Loan Amount (formula)
            (f"C{b + 5}",  refi.rate),
            (f"C{b + 6}",  refi.amort_years),
            (f"C{b + 7}",  refi.loan_term),
            (f"C{b + 8}",  refi.orig_fee_pct),
            (f"C{b + 9}",  refi.prepay_pct),
            (f"C{b + 10}", refi.closing_costs),
            # Cap rate in gap row — blue editable input
            (f"B{cr}",     f"Refi {i + 1} — Appraisal Cap Rate"),
            (f"C{cr}",     refi.cap_rate),
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
        ("C139", 0.0 if a.fee_income == 6000.0 else a.fee_income),
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
    logger.info(
        "EXCEL [Pro Forma]: writing salaries=%s exterminator=%s turnover=%s "
        "advertising=%s repairs=%s cleaning=%s",
        a.salaries, a.exterminator, a.turnover,
        a.advertising, a.repairs, a.cleaning)
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
    """Section 11: Below-the-Line Items (rows 166–168) + 11B Leasing Costs (189–193)."""
    return [
        ("C166", a.commissions_yr1),
        ("C167", a.cap_reserve_per_unit),
        ("C168", a.renovations_yr1),
        # §11B Leasing cost assumptions — rows 188–191
        ("B188", "  11B. LEASING COST ASSUMPTIONS"),
        ("B189", "TI — New Lease ($/SF)"),
        ("C189", a.ti_new_psf),
        ("B190", "TI — Renewal ($/SF)"),
        ("C190", a.ti_renewal_psf),
        ("B191", "Commission — New (% of GLV) / Renewal (% of GLV)"),
        ("C191", a.commission_new_pct),
        ("D191", a.commission_renewal_pct),
    ]


def _section_dev_period(a) -> CellMap:
    """Section 11A: Development Period & Carry Costs (rows 172–184)."""
    return [
        ("C172", a.const_period_months),
        ("C173", a.const_loan_rate),
        ("C174", a.const_hard),
        # draw_start_lag exposed on the 'Constr Interest' tab (no row in template)
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
