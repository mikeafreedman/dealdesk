"""
financials.py — Financial Calculations Module
==============================================
Deterministic financial engine for the DealDesk CRE Underwriting pipeline.

Computes:
    1. Sources & Uses / equity required
    2. NOI and 10-year (hold_period) pro forma cash flows
    3. Debt service with IO period + up to 3 refinancing events
    4. LP/GP waterfall (4 active tiers + residual, or simple split)
    5. IRR, equity multiple, cash-on-cash by year
    6. 5×7 sensitivity matrix (rent growth × exit cap → project IRR)
    7. 10,000-iteration Monte Carlo simulation
    8. Prompt 5A — Monte Carlo narrative (only AI call in this module)

Insurance expense line: uses DealData.insurance.insurance_proforma_line_item
if not None, otherwise falls back to DealData.assumptions.insurance.

Pipeline position: runs after risk.py (Stage 6), before excel_builder.py.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Dict, List, Optional, Tuple

import numpy as np
import numpy_financial as npf

import anthropic
import streamlit as st

from config import ANTHROPIC_SECRET_KEY, MODEL_SONNET
from models.models import DealData, InvestmentStrategy, WaterfallType

logger = logging.getLogger(__name__)

# Reproducible Monte Carlo
_RNG_SEED = 42


# ═══════════════════════════════════════════════════════════════════════════
# §1  LOAN MATH
# ═══════════════════════════════════════════════════════════════════════════

def _monthly_payment(principal: float, annual_rate: float,
                     amort_years: int) -> float:
    """Fully-amortizing monthly payment (P&I)."""
    if principal <= 0 or amort_years <= 0:
        return 0.0
    if annual_rate <= 0:
        return principal / (amort_years * 12)
    r = annual_rate / 12
    n = amort_years * 12
    return principal * r * (1 + r) ** n / ((1 + r) ** n - 1)


def _loan_balance(principal: float, annual_rate: float,
                  amort_years: int, amort_months_elapsed: int) -> float:
    """Outstanding balance after *amort_months_elapsed* amortizing payments.
    IO months are NOT counted — caller must subtract them first."""
    if principal <= 0 or amort_years <= 0 or amort_months_elapsed <= 0:
        return principal
    r = annual_rate / 12
    if r <= 0:
        return max(0.0, principal - principal / (amort_years * 12) * amort_months_elapsed)
    n = amort_years * 12
    if amort_months_elapsed >= n:
        return 0.0
    return principal * ((1 + r) ** n - (1 + r) ** amort_months_elapsed) / \
           ((1 + r) ** n - 1)


def _year_debt_service(principal: float, annual_rate: float,
                       amort_years: int, io_months_remaining: int) -> float:
    """Annual debt service for one year, mixing IO and amortizing months."""
    if principal <= 0:
        return 0.0
    r = annual_rate / 12
    io_this_year = max(0, min(12, io_months_remaining))
    amort_this_year = 12 - io_this_year
    io_pmt = principal * r if r > 0 else 0.0
    amort_pmt = _monthly_payment(principal, annual_rate, amort_years)
    return io_pmt * io_this_year + amort_pmt * amort_this_year


# ═══════════════════════════════════════════════════════════════════════════
# §2  SOURCES & USES
# ═══════════════════════════════════════════════════════════════════════════

def _compute_sources_uses(deal: DealData) -> dict:
    a = deal.assumptions
    is_sale = deal.investment_strategy == InvestmentStrategy.FOR_SALE

    transfer_tax = a.purchase_price * a.transfer_tax_rate
    professional = (a.legal_closing + a.title_insurance + a.legal_bank +
                    a.appraisal + a.environmental + a.surveyor +
                    a.architect + a.structural + a.civil_eng +
                    a.meps + a.legal_zoning + a.geotech)
    financing_soft = (a.acq_fee_fixed + a.mortgage_carry + a.mortgage_fees +
                      a.mezz_interest + a.working_capital + a.marketing +
                      a.re_tax_carry + a.prop_ins_carry + a.dev_fee +
                      a.dev_pref + a.permits + a.stormwater)
    hard_costs = a.demo + a.const_hard + a.const_reserve + a.gc_overhead
    initial_loan = a.purchase_price * a.ltv_pct
    origination_fee = initial_loan * a.origination_fee_pct

    total_uses = (a.purchase_price + transfer_tax + a.closing_costs_fixed +
                  a.tenant_buyout + professional + financing_soft +
                  hard_costs + origination_fee)

    # For-sale: add carry costs to uses
    if is_sale:
        total_months = a.sale_const_period_months + a.sale_marketing_period_months
        monthly_carry = (a.carry_loan_interest_monthly + a.carry_re_taxes_monthly +
                         a.carry_insurance_monthly + a.carry_utilities_monthly +
                         a.carry_maintenance_monthly + a.carry_hoa_monthly)
        total_uses += (monthly_carry * total_months +
                       a.carry_marketing_total + a.carry_staging_total)

    non_equity_sources = initial_loan + a.mezz_debt + a.tax_credit_equity + a.grants
    total_equity = max(0.0, total_uses - non_equity_sources)

    return {
        "total_uses": total_uses,
        "total_sources": total_uses,          # sources ≡ uses
        "initial_loan": initial_loan,
        "total_equity_required": total_equity,
    }


# ═══════════════════════════════════════════════════════════════════════════
# §3  INCOME / EXPENSE / NOI  (single-year helper)
# ═══════════════════════════════════════════════════════════════════════════

def _get_insurance_expense(deal: DealData) -> float:
    if deal.insurance.insurance_proforma_line_item is not None:
        return deal.insurance.insurance_proforma_line_item
    return deal.assumptions.insurance


def _gpr_yr1(deal: DealData) -> float:
    monthly = deal.extracted_docs.total_monthly_rent
    if monthly and monthly > 0:
        return monthly * 12
    return 0.0


def _year_income(gpr_yr1: float, year: int, a) -> Tuple[float, float]:
    """Return (GPR, EGI) for *year* (1-indexed)."""
    gpr = gpr_yr1 * (1 + a.annual_rent_growth) ** (year - 1)
    vacancy = gpr * a.vacancy_rate
    ltl = gpr * a.loss_to_lease
    egi = gpr - vacancy - ltl + a.cam_reimbursements + a.fee_income
    return gpr, egi


def _year_expenses(egi: float, year: int, a, insurance: float) -> float:
    """Total operating expenses for *year*."""
    g = (1 + a.expense_growth_rate) ** (year - 1)
    fixed = (a.re_taxes + insurance + a.gas + a.water_sewer +
             a.electric + a.license_inspections + a.trash) * g
    mgmt = egi * a.mgmt_fee_pct
    var_base = (a.salaries + a.repairs + a.exterminator +
                a.cleaning + a.turnover + a.advertising +
                a.landscape_snow + a.admin_legal_acct +
                a.office_phone + a.miscellaneous)
    return fixed + mgmt + var_base * g


def _year_noi(gpr_yr1: float, year: int, a, insurance: float) -> Tuple[float, float, float, float]:
    """Return (GPR, EGI, OpEx, NOI) for *year*."""
    gpr, egi = _year_income(gpr_yr1, year, a)
    opex = _year_expenses(egi, year, a, insurance)
    return gpr, egi, opex, egi - opex


# ═══════════════════════════════════════════════════════════════════════════
# §4  PRO FORMA BUILDER  (hold strategies)
# ═══════════════════════════════════════════════════════════════════════════

def _build_proforma(deal: DealData, insurance: float,
                    sources_uses: dict) -> Tuple[List[dict], dict]:
    """Build year-by-year pro forma for stabilized / value_add strategies.

    Returns:
        proforma  – list of dicts, one per year (1 … hold_period)
        exit_info – dict with exit-year numbers
    """
    a = deal.assumptions
    hold = a.hold_period
    gpr1 = _gpr_yr1(deal)
    num_units = a.num_units or 0

    # ── Loan state ────────────────────────────────────────────────
    loan_principal = sources_uses["initial_loan"]
    loan_rate = a.interest_rate
    loan_amort = a.amort_years
    loan_io_total = a.io_period_months   # total IO months for this loan
    loan_months = 0                      # months elapsed on current loan

    active_refis = sorted(
        [r for r in a.refi_events if r.active],
        key=lambda r: r.year,
    )

    total_equity = sources_uses["total_equity_required"]
    proforma: List[dict] = []

    for yr in range(1, hold + 1):
        gpr, egi, opex, noi = _year_noi(gpr1, yr, a, insurance)

        # Debt service
        io_remaining = max(0, loan_io_total - loan_months)
        ds = _year_debt_service(loan_principal, loan_rate, loan_amort, io_remaining)
        loan_months += 12

        # Refi check (after this year's debt service is calculated)
        refi_proceeds = 0.0
        for refi in active_refis:
            if refi.year == yr:
                amort_elapsed = max(0, loan_months - loan_io_total)
                old_balance = _loan_balance(loan_principal, loan_rate,
                                            loan_amort, amort_elapsed)
                new_loan = refi.new_loan_amount
                prepay = old_balance * refi.prepay_pct
                costs = refi.closing_costs + new_loan * refi.orig_fee_pct
                refi_proceeds = new_loan - old_balance - prepay - costs

                # Reset loan state
                loan_principal = new_loan
                loan_rate = refi.rate
                loan_amort = refi.amort_years
                loan_io_total = 0
                loan_months = 0
                break

        # Below the line
        capex = a.cap_reserve_per_unit * num_units
        below = capex
        if yr == 1:
            below += a.commissions_yr1 + a.renovations_yr1

        fcf = noi - ds - below + refi_proceeds
        coc = fcf / total_equity if total_equity > 0 else 0.0

        proforma.append({
            "year": yr,
            "gpr": round(gpr, 2),
            "egi": round(egi, 2),
            "opex": round(opex, 2),
            "noi": round(noi, 2),
            "debt_service": round(ds, 2),
            "capex_reserve": round(capex, 2),
            "refi_proceeds": round(refi_proceeds, 2),
            "fcf": round(fcf, 2),
            "cash_on_cash": round(coc, 4),
        })

    # ── Exit ──────────────────────────────────────────────────────
    forward_noi = _year_noi(gpr1, hold + 1, a, insurance)[3]
    gross_sale = forward_noi / a.exit_cap_rate if a.exit_cap_rate > 0 else 0.0
    disposition = gross_sale * a.disposition_costs_pct
    net_sale = gross_sale - disposition

    amort_elapsed = max(0, loan_months - loan_io_total)
    exit_balance = _loan_balance(loan_principal, loan_rate, loan_amort, amort_elapsed)
    net_equity_at_exit = net_sale - exit_balance

    exit_info = {
        "gross_sale_price": round(gross_sale, 2),
        "net_sale_proceeds": round(net_sale, 2),
        "exit_loan_balance": round(exit_balance, 2),
        "net_equity_at_exit": round(net_equity_at_exit, 2),
    }
    return proforma, exit_info


# ═══════════════════════════════════════════════════════════════════════════
# §4b  PRO FORMA — FOR-SALE STRATEGY
# ═══════════════════════════════════════════════════════════════════════════

def _build_proforma_for_sale(deal: DealData,
                             sources_uses: dict) -> Tuple[List[dict], dict]:
    """Simplified pro forma for flip / for-sale deals."""
    a = deal.assumptions
    total_months = a.sale_const_period_months + a.sale_marketing_period_months
    hold_years = max(1, math.ceil(total_months / 12))
    total_equity = sources_uses["total_equity_required"]

    monthly_carry = (a.carry_loan_interest_monthly + a.carry_re_taxes_monthly +
                     a.carry_insurance_monthly + a.carry_utilities_monthly +
                     a.carry_maintenance_monthly + a.carry_hoa_monthly)

    proforma: List[dict] = []
    for yr in range(1, hold_years + 1):
        months_this_yr = min(12, total_months - (yr - 1) * 12)
        carry = monthly_carry * max(0, months_this_yr)
        proforma.append({
            "year": yr,
            "gpr": 0.0, "egi": 0.0, "opex": 0.0, "noi": 0.0,
            "debt_service": 0.0,
            "capex_reserve": 0.0,
            "refi_proceeds": 0.0,
            "fcf": round(-carry, 2),
            "cash_on_cash": round(-carry / total_equity, 4) if total_equity > 0 else 0.0,
        })

    gross_sale = a.sale_price_arv
    broker = gross_sale * a.sale_broker_commission_pct
    disposition = gross_sale * a.disposition_costs_pct
    net_sale = gross_sale - broker - disposition
    loan_balance = sources_uses["initial_loan"]  # balloon at exit
    net_equity = net_sale - loan_balance

    exit_info = {
        "gross_sale_price": round(gross_sale, 2),
        "net_sale_proceeds": round(net_sale, 2),
        "exit_loan_balance": round(loan_balance, 2),
        "net_equity_at_exit": round(net_equity, 2),
    }
    return proforma, exit_info


# ═══════════════════════════════════════════════════════════════════════════
# §5  PROJECT CASH FLOWS & METRICS
# ═══════════════════════════════════════════════════════════════════════════

def _project_cashflows(proforma: List[dict], exit_info: dict,
                       total_equity: float) -> List[float]:
    """Equity cash-flow stream: [−equity, FCF₁, …, FCFₙ + net_equity_at_exit]."""
    cfs = [-total_equity]
    for i, yr in enumerate(proforma):
        cf = yr["fcf"]
        if i == len(proforma) - 1:
            cf += exit_info["net_equity_at_exit"]
        cfs.append(cf)
    return cfs


def _safe_irr(cashflows) -> Optional[float]:
    try:
        val = npf.irr(cashflows)
        if val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))):
            return None
        return float(val)
    except Exception:
        return None


def _equity_multiple(cashflows) -> float:
    """EM = total distributions / total invested."""
    invested = abs(cashflows[0]) if cashflows[0] < 0 else 0.0
    if invested <= 0:
        return 0.0
    return sum(cashflows[1:]) / invested


# ═══════════════════════════════════════════════════════════════════════════
# §6  LP / GP WATERFALL
# ═══════════════════════════════════════════════════════════════════════════

def _compute_waterfall(project_cfs: List[float],
                       deal: DealData) -> dict:
    """European-style look-back waterfall.

    Returns dict with lp_irr, gp_irr, lp_em, gp_em, lp_cfs, gp_cfs.
    """
    a = deal.assumptions
    total_equity = abs(project_cfs[0])
    lp_equity = total_equity * a.lp_equity_pct
    gp_equity = total_equity * a.gp_equity_pct
    n = len(project_cfs) - 1  # number of periods

    pos_cfs = np.array([max(0.0, cf) for cf in project_cfs[1:]], dtype=np.float64)
    total_dist = float(pos_cfs.sum())

    # ── Simple waterfall ─────────────────────────────────────────
    if a.waterfall_type == WaterfallType.SIMPLE:
        lp_total = total_dist * a.simple_lp_split
        gp_total = total_dist * (1 - a.simple_lp_split)
        return _waterfall_result(project_cfs, pos_cfs, total_dist,
                                 lp_equity, gp_equity, lp_total, gp_total)

    # ── Full tiered waterfall ────────────────────────────────────
    if total_dist <= 0:
        return _waterfall_result(project_cfs, pos_cfs, 0.0,
                                 lp_equity, gp_equity, 0.0, 0.0)

    # Hurdle boundaries: [pref, tier1, tier2, tier3, tier4]
    hurdle_rates = [a.pref_return]
    lp_shares = [1.0]       # below pref → 100 % to LP
    for t in a.waterfall_tiers:
        hurdle_rates.append(t.hurdle_value)
        lp_shares.append(t.lp_share)

    # For each hurdle rate, compute total LP dist needed for LP to hit that IRR
    # assuming LP distributions are proportional to project positive CFs.
    # k(r) = lp_equity / NPV(pos_cfs, r)   →   LP total = k(r) × total_dist
    lp_at_hurdle: List[float] = []
    for r in hurdle_rates:
        if r <= 0:
            lp_at_hurdle.append(lp_equity)
            continue
        npv = sum(float(pos_cfs[t]) / (1 + r) ** (t + 1) for t in range(n))
        if npv <= 1e-10:
            lp_at_hurdle.append(total_dist)  # hurdle unachievable
        else:
            lp_at_hurdle.append(min(lp_equity / npv * total_dist, total_dist))

    # Walk through tiers, allocating marginal distributions
    lp_total = 0.0
    gp_total = 0.0
    allocated = 0.0
    prev_lp = 0.0

    for i, lp_target in enumerate(lp_at_hurdle):
        lp_marginal = max(0.0, lp_target - prev_lp)
        lp_sh = lp_shares[i]
        if lp_sh > 0:
            tier_total = lp_marginal / lp_sh
        else:
            tier_total = 0.0
        tier_total = min(tier_total, total_dist - allocated)
        if tier_total <= 0:
            break
        lp_total += tier_total * lp_sh
        gp_total += tier_total * (1 - lp_sh)
        allocated += tier_total
        prev_lp = lp_target

    # Residual tier
    remaining = total_dist - allocated
    if remaining > 0:
        lp_total += remaining * a.residual_tier.lp_share
        gp_total += remaining * a.residual_tier.gp_share

    return _waterfall_result(project_cfs, pos_cfs, total_dist,
                             lp_equity, gp_equity, lp_total, gp_total)


def _waterfall_result(project_cfs, pos_cfs, total_dist,
                      lp_equity, gp_equity,
                      lp_total, gp_total) -> dict:
    """Build LP/GP cash-flow streams and compute IRR/EM."""
    n = len(pos_cfs)
    if total_dist > 0:
        weights = pos_cfs / total_dist
    else:
        weights = np.zeros(n)

    lp_cfs = np.concatenate([[-lp_equity], weights * lp_total])
    gp_cfs = np.concatenate([[-gp_equity], weights * gp_total])

    return {
        "lp_irr": _safe_irr(lp_cfs.tolist()),
        "gp_irr": _safe_irr(gp_cfs.tolist()),
        "project_irr": _safe_irr(project_cfs),
        "lp_em": _equity_multiple(lp_cfs.tolist()),
        "gp_em": _equity_multiple(gp_cfs.tolist()),
        "project_em": _equity_multiple(project_cfs),
        "lp_total_dist": round(lp_total, 2),
        "gp_total_dist": round(gp_total, 2),
    }


# ═══════════════════════════════════════════════════════════════════════════
# §7  QUICK METRICS  (for sensitivity & Monte Carlo)
# ═══════════════════════════════════════════════════════════════════════════

def _quick_project_irr(
    gpr_yr1: float, total_equity: float, initial_loan: float,
    interest_rate: float, amort_years: int, io_months: int,
    hold: int, rent_growth: float, expense_growth: float,
    exit_cap: float, vacancy: float, loss_to_lease: float,
    cam: float, fee_income: float, mgmt_pct: float,
    insurance: float, fixed_base: float, var_base: float,
    capex_annual: float, disp_pct: float,
) -> Tuple[Optional[float], float]:
    """Fast project IRR + EM for a single scenario (no refis)."""
    # Annual debt service (constant — no refi in quick mode)
    ds = _year_debt_service(initial_loan, interest_rate, amort_years, io_months)

    cfs = [-total_equity]
    last_noi = 0.0
    for yr in range(1, hold + 1):
        ig = (1 + rent_growth) ** (yr - 1)
        eg = (1 + expense_growth) ** (yr - 1)
        gpr = gpr_yr1 * ig
        egi = gpr * (1 - vacancy - loss_to_lease) + cam + fee_income
        opex = (fixed_base + insurance) * eg + egi * mgmt_pct + var_base * eg
        # Re-add insurance that was double-counted if insurance is in fixed_base
        # (insurance is NOT in fixed_base — see caller)
        noi = egi - opex
        last_noi = noi

        # IO adjustment per year
        io_rem = max(0, io_months - (yr - 1) * 12)
        yr_ds = _year_debt_service(initial_loan, interest_rate, amort_years, io_rem)

        fcf = noi - yr_ds - capex_annual
        cfs.append(fcf)

    # Exit
    forward_noi = last_noi * (1 + rent_growth)
    gross_sale = forward_noi / exit_cap if exit_cap > 0 else 0.0
    net_sale = gross_sale * (1 - disp_pct)

    # Loan balance at exit (approximate — no refi)
    amort_elapsed = max(0, hold * 12 - io_months)
    bal = _loan_balance(initial_loan, interest_rate, amort_years, amort_elapsed)
    cfs[-1] += net_sale - bal

    return _safe_irr(cfs), _equity_multiple(cfs)


def _quick_params(deal: DealData, insurance: float,
                  sources_uses: dict) -> dict:
    """Extract parameters for _quick_project_irr from deal."""
    a = deal.assumptions
    num_units = a.num_units or 0
    fixed_base = (a.re_taxes + a.gas + a.water_sewer +
                  a.electric + a.license_inspections + a.trash)
    var_base = (a.salaries + a.repairs + a.exterminator +
                a.cleaning + a.turnover + a.advertising +
                a.landscape_snow + a.admin_legal_acct +
                a.office_phone + a.miscellaneous)
    return dict(
        gpr_yr1=_gpr_yr1(deal),
        total_equity=sources_uses["total_equity_required"],
        initial_loan=sources_uses["initial_loan"],
        interest_rate=a.interest_rate,
        amort_years=a.amort_years,
        io_months=a.io_period_months,
        hold=a.hold_period,
        rent_growth=a.annual_rent_growth,
        expense_growth=a.expense_growth_rate,
        exit_cap=a.exit_cap_rate,
        vacancy=a.vacancy_rate,
        loss_to_lease=a.loss_to_lease,
        cam=a.cam_reimbursements,
        fee_income=a.fee_income,
        mgmt_pct=a.mgmt_fee_pct,
        insurance=insurance,
        fixed_base=fixed_base,
        var_base=var_base,
        capex_annual=a.cap_reserve_per_unit * num_units,
        disp_pct=a.disposition_costs_pct,
    )


# ═══════════════════════════════════════════════════════════════════════════
# §8  SENSITIVITY MATRIX
# ═══════════════════════════════════════════════════════════════════════════

def _build_sensitivity(deal: DealData, insurance: float,
                       sources_uses: dict) -> Tuple[List[List[float]],
                                                     List[float],
                                                     List[float]]:
    """5×7 (rent_growth × exit_cap) sensitivity matrix of project IRR."""
    a = deal.assumptions
    rent_axis = _inclusive_range(a.sens_rent_growth_low,
                                a.sens_rent_growth_high,
                                a.sens_rent_growth_step)
    cap_axis = _inclusive_range(a.sens_exit_cap_low,
                               a.sens_exit_cap_high,
                               a.sens_exit_cap_step)

    base = _quick_params(deal, insurance, sources_uses)
    matrix: List[List[float]] = []

    for rg in rent_axis:
        row: List[float] = []
        for ec in cap_axis:
            params = {**base, "rent_growth": rg, "exit_cap": ec}
            irr, _ = _quick_project_irr(**params)
            row.append(round(irr, 4) if irr is not None else 0.0)
        matrix.append(row)

    return matrix, [round(r, 4) for r in rent_axis], [round(c, 4) for c in cap_axis]


def _inclusive_range(low: float, high: float, step: float) -> List[float]:
    """np.arange inclusive of the upper bound."""
    if step <= 0:
        return [low]
    vals = np.arange(low, high + step * 0.5, step)
    return [round(float(v), 6) for v in vals]


# ═══════════════════════════════════════════════════════════════════════════
# §9  MONTE CARLO SIMULATION  (10 000 iterations, vectorized)
# ═══════════════════════════════════════════════════════════════════════════

def _run_monte_carlo(deal: DealData, insurance: float,
                     sources_uses: dict) -> dict:
    """10,000-iteration Monte Carlo over rent_growth, exit_cap, vacancy,
    expense_growth. Returns summary statistics for Prompt 5A."""
    N_SIM = 10_000
    a = deal.assumptions
    rng = np.random.default_rng(_RNG_SEED)

    # Base parameters
    bp = _quick_params(deal, insurance, sources_uses)
    hold = bp["hold"]
    total_equity = bp["total_equity"]

    if total_equity <= 0 or bp["gpr_yr1"] <= 0:
        logger.warning("Monte Carlo skipped — no equity or no rent data")
        return _empty_mc()

    # ── Sample random inputs ──────────────────────────────────────
    rent_samples = rng.normal(a.annual_rent_growth, 0.015, N_SIM)
    cap_samples = rng.normal(a.exit_cap_rate, 0.01, N_SIM).clip(0.02, 0.15)
    vac_samples = rng.normal(a.vacancy_rate, 0.025, N_SIM).clip(0.0, 0.40)
    exp_samples = rng.normal(a.expense_growth_rate, 0.01, N_SIM)

    # ── Vectorized cash-flow computation ──────────────────────────
    years = np.arange(1, hold + 1)  # (hold,)

    # Income growth: (N_SIM, hold)
    ig = (1 + rent_samples[:, None]) ** (years[None, :] - 1)
    eg = (1 + exp_samples[:, None]) ** (years[None, :] - 1)

    gpr = bp["gpr_yr1"] * ig
    egi = gpr * (1 - vac_samples[:, None] - bp["loss_to_lease"]) + bp["cam"] + bp["fee_income"]

    opex = ((bp["fixed_base"] + insurance) * eg +
            egi * bp["mgmt_pct"] +
            bp["var_base"] * eg)
    noi = egi - opex

    # Debt service — compute per-year accounting for IO runoff
    ds = np.zeros((N_SIM, hold))
    for yr_idx in range(hold):
        io_rem = max(0, bp["io_months"] - yr_idx * 12)
        yr_ds = _year_debt_service(bp["initial_loan"], bp["interest_rate"],
                                   bp["amort_years"], io_rem)
        ds[:, yr_idx] = yr_ds

    fcf = noi - ds - bp["capex_annual"]

    # Exit at final year
    forward_noi = noi[:, -1] * (1 + rent_samples)
    gross_sale = np.where(cap_samples > 0, forward_noi / cap_samples, 0.0)
    net_sale = gross_sale * (1 - bp["disp_pct"])
    amort_elapsed = max(0, hold * 12 - bp["io_months"])
    bal = _loan_balance(bp["initial_loan"], bp["interest_rate"],
                        bp["amort_years"], amort_elapsed)

    # Cash flow matrix: (N_SIM, hold+1)
    cf_matrix = np.zeros((N_SIM, hold + 1))
    cf_matrix[:, 0] = -total_equity
    cf_matrix[:, 1:] = fcf
    cf_matrix[:, -1] += net_sale - bal

    # ── Vectorized IRR (Newton) ───────────────────────────────────
    irrs = _vectorized_irr(cf_matrix)
    ems = cf_matrix[:, 1:].sum(axis=1) / total_equity

    # ── Filter non-converged ──────────────────────────────────────
    valid = np.isfinite(irrs) & (irrs > -1.0) & (irrs < 5.0)
    irrs_v = irrs[valid]
    ems_v = ems[valid]
    if len(irrs_v) < 100:
        logger.warning("Monte Carlo: fewer than 100 valid iterations")
        return _empty_mc()

    # ── Summary statistics ────────────────────────────────────────
    target = a.target_lp_irr

    # Distribution shape
    mean_irr = float(np.mean(irrs_v))
    std_irr = float(np.std(irrs_v))
    if std_irr > 1e-8:
        skew = float(np.mean(((irrs_v - mean_irr) / std_irr) ** 3))
        kurt = float(np.mean(((irrs_v - mean_irr) / std_irr) ** 4) - 3)
    else:
        skew, kurt = 0.0, 0.0

    if kurt > 3.0:
        shape = "fat_tailed"
    elif abs(skew) < 0.5:
        shape = "normal"
    elif skew > 0.5:
        shape = "right_skewed"
    else:
        shape = "left_skewed"

    # Dominant variable (R² via correlation)
    labels = ["rent_growth", "exit_cap_rate", "vacancy_rate", "expense_growth"]
    samples = [rent_samples[valid], cap_samples[valid],
               vac_samples[valid], exp_samples[valid]]
    r2s = {}
    for lbl, s in zip(labels, samples):
        corr = np.corrcoef(s, irrs_v)[0, 1]
        r2s[lbl] = float(corr ** 2) if np.isfinite(corr) else 0.0
    dominant = max(r2s, key=r2s.get)

    # Bear-case (bottom-10 % average inputs)
    p10_threshold = float(np.percentile(irrs_v, 10))
    bear_mask = irrs_v <= p10_threshold
    # Use original indices for bear case
    valid_indices = np.where(valid)[0]
    bear_idx = valid_indices[bear_mask]
    bear_rg = float(np.mean(rent_samples[bear_idx]))
    bear_ec = float(np.mean(cap_samples[bear_idx]))
    bear_vac = float(np.mean(vac_samples[bear_idx]))

    bear_text = (f"Rent growth {bear_rg:.1%}, exit cap {bear_ec:.1%}, "
                 f"vacancy {bear_vac:.1%}")

    return {
        "median_irr": round(float(np.median(irrs_v)), 4),
        "mean_irr": round(mean_irr, 4),
        "std_irr": round(std_irr, 4),
        "p10_irr": round(float(np.percentile(irrs_v, 10)), 4),
        "p25_irr": round(float(np.percentile(irrs_v, 25)), 4),
        "p75_irr": round(float(np.percentile(irrs_v, 75)), 4),
        "p90_irr": round(float(np.percentile(irrs_v, 90)), 4),
        "prob_above_target": round(float(np.mean(irrs_v > target)), 4),
        "median_em": round(float(np.median(ems_v)), 2),
        "p10_em": round(float(np.percentile(ems_v, 10)), 2),
        "p90_em": round(float(np.percentile(ems_v, 90)), 2),
        "dominant_variable": dominant,
        "dominant_variable_r2": round(r2s[dominant], 3),
        "distribution_shape": shape,
        "bear_case_scenario": bear_text,
        "n_valid": int(len(irrs_v)),
    }


def _vectorized_irr(cf_matrix: np.ndarray,
                    max_iter: int = 50, tol: float = 1e-8) -> np.ndarray:
    """Newton's method IRR for every row of cf_matrix (n_sim × n_periods)."""
    n_sim, n_per = cf_matrix.shape
    t = np.arange(n_per, dtype=np.float64)
    rates = np.full(n_sim, 0.10)

    for _ in range(max_iter):
        disc = (1 + rates[:, None]) ** t[None, :]
        npv = (cf_matrix / disc).sum(axis=1)
        dnpv = (-t[None, :] * cf_matrix / ((1 + rates[:, None]) ** (t[None, :] + 1))).sum(axis=1)
        mask = np.abs(dnpv) > 1e-12
        delta = np.zeros(n_sim)
        delta[mask] = npv[mask] / dnpv[mask]
        new_rates = rates - delta
        new_rates = np.clip(new_rates, -0.99, 10.0)
        if np.max(np.abs(new_rates - rates)) < tol:
            rates = new_rates
            break
        rates = new_rates

    return rates


def _empty_mc() -> dict:
    return {
        "median_irr": None, "mean_irr": None, "std_irr": None,
        "p10_irr": None, "p25_irr": None, "p75_irr": None, "p90_irr": None,
        "prob_above_target": None,
        "median_em": None, "p10_em": None, "p90_em": None,
        "dominant_variable": None, "dominant_variable_r2": None,
        "distribution_shape": None, "bear_case_scenario": None,
        "n_valid": 0,
    }


# ═══════════════════════════════════════════════════════════════════════════
# §10  PROMPT 5A — MONTE CARLO NARRATIVE  (only AI call)
# ═══════════════════════════════════════════════════════════════════════════

_SYSTEM_5A = (
    "You are a senior CRE analyst writing for an institutional investment committee.\n"
    "Interpret 10,000-iteration Monte Carlo simulation results and write the\n"
    "risk-weighted return narrative for the Financial Analysis section.\n\n"
    "RULES:\n"
    "- Exactly two paragraphs. No headers, no bullets, no tables.\n"
    "- P1: Central tendency and distribution shape. Median IRR and EM. P10-P90 spread.\n"
    "  Probability of exceeding target LP IRR. If bimodal: identify two clusters.\n"
    "- P2: Dominant input variable and its R-squared. Bear case scenario in plain English.\n"
    "  Whether the return profile is appropriately compensated for the risk level.\n"
    "- Tone: Precise, confident, analytical. No hedging language.\n"
    "- Do not define Monte Carlo. Do not repeat numbers in the adjacent table. Interpret.\n"
    "- Length: 120–180 words per paragraph. Output plain text only."
)

_USER_5A = (
    "Property: {property_address} | Asset: {asset_type} | Strategy: {investment_strategy}\n"
    "Target LP IRR: {target_lp_irr}% | Hold: {hold_period} years\n\n"
    "Monte Carlo results (10,000 iterations):\n"
    "{monte_carlo_results_json}\n\n"
    "Write the two-paragraph narrative now. Output plain text only."
)


def _call_5a(deal: DealData, mc_results: dict) -> Optional[str]:
    """Call Sonnet for Prompt 5A Monte Carlo narrative. Returns plain text or None."""
    client = anthropic.Anthropic(
        api_key=st.secrets[ANTHROPIC_SECRET_KEY]["api_key"],
    )
    user_msg = _USER_5A.format(
        property_address=deal.address.full_address,
        asset_type=deal.asset_type.value,
        investment_strategy=deal.investment_strategy.value,
        target_lp_irr=round(deal.assumptions.target_lp_irr * 100, 1),
        hold_period=deal.assumptions.hold_period,
        monte_carlo_results_json=json.dumps(mc_results, indent=2),
    )
    try:
        response = client.messages.create(
            model=MODEL_SONNET,
            max_tokens=1024,
            system=_SYSTEM_5A,
            messages=[{"role": "user", "content": user_msg}],
        )
        return response.content[0].text.strip()
    except (anthropic.APIError, IndexError, KeyError) as exc:
        logger.warning("Prompt 5A Sonnet call failed: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# §11  PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

def run_financials(deal: DealData) -> DealData:
    """Run the full financial engine and populate DealData.financial_outputs.

    Pipeline position: after risk.py, before excel_builder.py.

    Args:
        deal: DealData with assumptions, extracted_docs, and insurance populated.

    Returns:
        The same DealData object with financial_outputs fully populated.
    """
    logger.info("Running financials module...")
    a = deal.assumptions
    fo = deal.financial_outputs
    is_sale = deal.investment_strategy == InvestmentStrategy.FOR_SALE
    insurance = _get_insurance_expense(deal)

    # ── Sources & Uses ────────────────────────────────────────────
    su = _compute_sources_uses(deal)
    fo.total_uses = su["total_uses"]
    fo.total_sources = su["total_sources"]
    fo.total_equity_required = su["total_equity_required"]
    fo.initial_loan_amount = su["initial_loan"]

    # ── Pro Forma ─────────────────────────────────────────────────
    if is_sale:
        proforma, exit_info = _build_proforma_for_sale(deal, su)
    else:
        proforma, exit_info = _build_proforma(deal, insurance, su)

    fo.pro_forma_years = proforma

    # ── Year-1 metrics ────────────────────────────────────────────
    if not is_sale and proforma:
        yr1 = proforma[0]
        gpr1 = _gpr_yr1(deal)
        _, egi1 = _year_income(gpr1, 1, a)

        fo.gross_potential_rent = yr1["gpr"]
        fo.effective_gross_income = yr1["egi"]
        fo.total_operating_expenses = yr1["opex"]
        fo.noi_yr1 = yr1["noi"]
        fo.debt_service_annual = yr1["debt_service"]
        fo.free_cash_flow_yr1 = yr1["fcf"]
        fo.dscr_yr1 = (yr1["noi"] / yr1["debt_service"]
                       if yr1["debt_service"] > 0 else None)
        fo.going_in_cap_rate = (yr1["noi"] / a.purchase_price
                                if a.purchase_price > 0 else None)
        fo.cash_on_cash_yr1 = yr1["cash_on_cash"]

    # ── Exit ──────────────────────────────────────────────────────
    fo.gross_sale_price = exit_info["gross_sale_price"]
    fo.net_sale_proceeds = exit_info["net_sale_proceeds"]
    fo.net_equity_at_exit = exit_info["net_equity_at_exit"]

    # ── Project cash flows & IRR / EM ─────────────────────────────
    total_equity = su["total_equity_required"]
    project_cfs = _project_cashflows(proforma, exit_info, total_equity)
    fo.project_irr = _safe_irr(project_cfs)
    fo.project_equity_multiple = round(_equity_multiple(project_cfs), 2)

    # ── Waterfall (LP / GP) ───────────────────────────────────────
    wf = _compute_waterfall(project_cfs, deal)
    fo.lp_irr = wf["lp_irr"]
    fo.gp_irr = wf["gp_irr"]
    fo.lp_equity_multiple = round(wf["lp_em"], 2)
    fo.gp_equity_multiple = round(wf["gp_em"], 2)

    # ── Sensitivity Matrix ────────────────────────────────────────
    if not is_sale:
        matrix, rent_axis, cap_axis = _build_sensitivity(deal, insurance, su)
        fo.sensitivity_matrix = matrix
        fo.sensitivity_axis_rent_growth = rent_axis
        fo.sensitivity_axis_exit_cap = cap_axis

    # ── Monte Carlo ───────────────────────────────────────────────
    if not is_sale:
        mc = _run_monte_carlo(deal, insurance, su)
        fo.monte_carlo_results = mc

        # Prompt 5A — narrative
        if mc.get("median_irr") is not None:
            logger.info("Running Prompt 5A — Monte Carlo Narrative...")
            narrative = _call_5a(deal, mc)
            fo.monte_carlo_narrative = narrative
            deal.narratives.monte_carlo_narrative = narrative
            if narrative:
                logger.info("Prompt 5A complete — narrative generated")
            else:
                logger.warning("Prompt 5A failed — narrative remains None")
        else:
            logger.warning("Monte Carlo produced no valid results — skipping Prompt 5A")

    logger.info("Financials module complete")
    return deal
