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

Insurance expense line: priority = (1) assumptions.insurance if > 0,
(2) DealData.insurance.insurance_proforma_line_item if not None, (3) 0.0.

Pipeline position: runs after risk.py (Stage 6), before excel_builder.py.
"""

from __future__ import annotations

import json
import logging
import math
import traceback
from typing import Dict, List, Optional, Tuple

import numpy as np
import numpy_financial as npf

import anthropic
import os

from config import MODEL_SONNET


def _get_anthropic_api_key():
    return os.environ.get("ANTHROPIC_API_KEY", "") or None
from models.models import (
    AssetType, DealData, InvestmentStrategy, WaterfallType,
    RenovationTier, RENOVATION_DOWNTIME_MONTHS,
)

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
    IO months are NOT counted — caller must subtract them first.
    If amort_years == 0 (IO loan), balance stays flat."""
    if principal <= 0 or amort_months_elapsed <= 0:
        return principal
    if amort_years <= 0:
        return principal  # IO loan — no principal paydown
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
    """Annual debt service for one year.

    If the year falls entirely within the IO period, return interest-only.
    If entirely past IO, return full P&I.  If IO expires mid-year, blend.
    If amort_years == 0 (interest-only loan), return IO for all 12 months.
    """
    if principal <= 0:
        return 0.0
    r = annual_rate / 12
    io_pmt = principal * r if r > 0 else 0.0
    # Pure IO loan: always pay interest-only regardless of io_months_remaining
    if amort_years <= 0:
        return io_pmt * 12
    io_this_year = max(0, min(12, io_months_remaining))
    amort_this_year = 12 - io_this_year
    amort_pmt = _monthly_payment(principal, annual_rate, amort_years)
    return io_pmt * io_this_year + amort_pmt * amort_this_year


# ═══════════════════════════════════════════════════════════════════════════
# §1b  CONSTRUCTION LOAN INTEREST (S-CURVE DRAW MODEL)
# ═══════════════════════════════════════════════════════════════════════════

def _compute_construction_interest(
    initial_loan: float,
    annual_rate: float,
    construction_months: int,
    draw_start_lag: int,
    total_project_cost: float,
    const_hard: float,
    const_reserve: float,
) -> Tuple[float, List[dict]]:
    """
    Compute total construction-period interest carry using an S-curve
    (logistic) draw model.

    The loan is split into two portions:
      - Acquisition portion: drawn in full on Month 1 (day of closing).
      - Construction holdback: drawn over construction_months via a
        logistic S-curve, starting after draw_start_lag months.

    The holdback share is driven by hard cost % of total project cost:
      - Light reno (hard costs = 5% of TPC) → holdback ≈ 5% of loan
        → nearly flat interest line from day 1.
      - Heavy rehab (hard costs = 50% of TPC) → holdback ≈ 50% of loan
        → true S-curve, interest builds gradually over construction.

    Returns:
        (total_interest_carry: float, monthly_schedule: List[dict])

    monthly_schedule entries have keys:
        month, monthly_draw, cumulative_draw_pct,
        outstanding_balance, monthly_interest
    """
    if construction_months <= 0 or initial_loan <= 0:
        logger.info("CONSTR INTEREST: skipped (construction_months=%d, loan=%s)",
                    construction_months, f"{initial_loan:,.0f}")
        return 0.0, []

    # Hard cost share — fraction of loan held back as construction draws
    hard_total = const_hard + const_reserve
    if total_project_cost > 0:
        hard_cost_share = min(0.85, max(0.0, hard_total / total_project_cost))
    else:
        hard_cost_share = 0.0

    acq_portion    = initial_loan * (1.0 - hard_cost_share)   # drawn Month 1
    constr_holdback = initial_loan * hard_cost_share            # drawn via S-curve

    monthly_rate = annual_rate / 12.0
    lag          = max(0, int(draw_start_lag))
    N            = int(construction_months)

    # Logistic (sigmoid) S-curve: f(t) = 1 / (1 + exp(-k*(t - mid)))
    # k = 6/N gives ~5% drawn at t=0 and ~95% drawn at t=N (self-scaling).
    k   = 6.0 / N if N > 0 else 6.0
    mid = N / 2.0

    def _logistic(t: float) -> float:
        try:
            return 1.0 / (1.0 + math.exp(-k * (t - mid)))
        except OverflowError:
            return 0.0 if t < mid else 1.0

    # Build normalised incremental draw percentages for each construction month
    span = _logistic(N) - _logistic(0)
    if span <= 0:
        span = 1.0
    increments = [
        (_logistic(i + 1) - _logistic(i)) / span
        for i in range(N)
    ]

    # Build monthly schedule across (lag + N) total months
    total_months       = lag + N
    outstanding        = 0.0
    constr_drawn       = 0.0
    total_interest     = 0.0
    constr_month_idx   = 0
    schedule: List[dict] = []

    for m in range(1, total_months + 1):
        # Month 1: full acquisition portion drawn at closing
        day1_draw = acq_portion if m == 1 else 0.0

        # Construction draws begin after the lag period
        if m > lag and constr_month_idx < N:
            constr_draw = constr_holdback * increments[constr_month_idx]
            constr_month_idx += 1
        else:
            constr_draw = 0.0

        monthly_draw  = day1_draw + constr_draw
        outstanding  += monthly_draw
        constr_drawn += constr_draw

        monthly_interest  = outstanding * monthly_rate
        total_interest   += monthly_interest

        cum_pct = ((acq_portion + constr_drawn) / initial_loan * 100.0
                   if initial_loan > 0 else 0.0)

        schedule.append({
            "month":               m,
            "monthly_draw":        round(monthly_draw, 2),
            "cumulative_draw_pct": round(cum_pct, 2),
            "outstanding_balance": round(outstanding, 2),
            "monthly_interest":    round(monthly_interest, 2),
        })

    logger.info(
        "CONSTR INTEREST: loan=%s, hard_cost_share=%.1f%%, "
        "acq_portion=%s, holdback=%s, months=%d, lag=%d → total_carry=%s",
        f"{initial_loan:,.0f}",
        hard_cost_share * 100,
        f"{acq_portion:,.0f}",
        f"{constr_holdback:,.0f}",
        N, lag,
        f"{total_interest:,.2f}",
    )
    return round(total_interest, 2), schedule


# ═══════════════════════════════════════════════════════════════════════════
# §2  SOURCES & USES
# ═══════════════════════════════════════════════════════════════════════════

def _compute_sources_uses(deal: DealData) -> dict:
    a = deal.assumptions
    is_sale = deal.investment_strategy == InvestmentStrategy.OPPORTUNISTIC

    transfer_tax = a.purchase_price * a.transfer_tax_rate
    professional = (a.legal_closing + a.title_insurance + a.legal_bank +
                    a.appraisal + a.environmental + a.surveyor +
                    a.architect + a.structural + a.civil_eng +
                    a.meps + a.legal_zoning + a.geotech)
    financing_soft = (a.acq_fee_fixed +
                      a.mezz_interest + a.working_capital + a.marketing +
                      a.re_tax_carry + a.prop_ins_carry + a.dev_fee +
                      a.dev_pref + a.permits + a.stormwater)
    hard_costs = (a.demo + a.const_hard + a.const_reserve + a.gc_overhead)

    # Total project cost before origination (used to size the loan)
    total_project_cost = (a.purchase_price + transfer_tax +
                          a.closing_costs_fixed +
                          a.tenant_buyout + professional + financing_soft +
                          hard_costs)
    initial_loan = total_project_cost * a.ltv_pct
    origination_fee = initial_loan * a.origination_fee_pct
    logger.info(
        "ORIGINATION FEE: pct=%.2f%%, loan=$%s, fee=$%s",
        a.origination_fee_pct * 100,
        f"{initial_loan:,.0f}",
        f"{origination_fee:,.0f}",
    )

    # ── Construction loan interest carry (S-curve draw model) ──────
    # Applies to stabilized and value_add only. For-sale uses its own
    # flat monthly carry model and is left untouched.
    if not is_sale:
        constr_interest, _constr_schedule = _compute_construction_interest(
            initial_loan=initial_loan,
            annual_rate=a.interest_rate,
            construction_months=getattr(a, 'const_period_months', 0) or 0,
            draw_start_lag=getattr(a, 'draw_start_lag', 1),
            total_project_cost=total_project_cost,
            const_hard=getattr(a, 'const_hard', 0.0),
            const_reserve=getattr(a, 'const_reserve', 0.0),
        )
    else:
        constr_interest, _constr_schedule = 0.0, []

    total_uses = total_project_cost + origination_fee + constr_interest

    logger.info("TPC: total_project_cost=%s (incl closing_costs_fixed=%s)",
                f"{total_project_cost:,.2f}", f"{a.closing_costs_fixed:,.2f}")
    logger.info(
        "LOAN SIZING: total_project_cost=%s × LTV=%s = loan=%s",
        f"{total_project_cost:,.2f}", f"{a.ltv_pct:.1%}", f"{initial_loan:,.2f}")
    logger.info(
        "S&U DETAIL: purchase=%s, transfer_tax=%s, "
        "tenant_buyout=%s, professional=%s, financing_soft=%s, "
        "hard_costs=%s, origination=%s -> TOTAL=%s",
        a.purchase_price, transfer_tax,
        a.tenant_buyout, professional, financing_soft,
        hard_costs, origination_fee, total_uses
    )

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
    lp_equity = total_equity * a.lp_equity_pct
    gp_equity = total_equity * a.gp_equity_pct
    total_sources = non_equity_sources + total_equity
    logger.info("S&U: total_uses=%.2f, senior_debt=%.2f",
                total_uses, initial_loan)
    logger.info("CTX: equity_gap=%.2f, gp_equity=%.2f, lp_equity=%.2f",
                total_equity, gp_equity, lp_equity)
    _gap = total_sources - total_uses
    logger.info("S&U check: Uses=%s, Sources=%s, Gap=%s",
                f"{total_uses:,.0f}", f"{total_sources:,.0f}",
                f"{_gap:,.0f}")
    # Sanity: S&U must balance. A gap of more than $1 indicates a formula
    # mismatch between the Python engine and the Excel template — surface
    # it as a warning so reviewers know the model is out of balance.
    if abs(_gap) > 1.0:
        logger.warning(
            "S&U BALANCE: sources-uses gap=$%.2f exceeds $1 tolerance — "
            "verify that non_equity_sources + total_equity = total_uses.",
            _gap,
        )

    return {
        "total_uses":                  total_uses,
        "total_sources":               total_sources,
        "initial_loan":                initial_loan,
        "total_equity_required":        total_equity,
        "lp_equity":                   lp_equity,
        "gp_equity":                   gp_equity,
        "construction_interest_carry": constr_interest,
        "construction_interest_schedule": _constr_schedule,
        "total_project_cost":          total_project_cost,
    }


# ═══════════════════════════════════════════════════════════════════════════
# §3  INCOME / EXPENSE / NOI  (single-year helper)
# ═══════════════════════════════════════════════════════════════════════════

def _get_insurance_expense(deal: DealData) -> float:
    # User-set assumption always wins; AI-estimated proforma is fallback only
    if deal.assumptions.insurance and deal.assumptions.insurance > 0:
        return deal.assumptions.insurance
    if deal.insurance.insurance_proforma_line_item is not None:
        return deal.insurance.insurance_proforma_line_item
    return 0.0


def _gpr_yr1(deal: DealData) -> float:
    # Primary source: extracted from uploaded documents
    monthly = deal.extracted_docs.total_monthly_rent if deal.extracted_docs else None
    if monthly and monthly > 0:
        gpr = monthly * 12
        logger.info(f"GPR: from extracted docs = ${gpr:,.0f}/yr")
        return gpr

    # Fallback: compute from assumptions (num_units × avg monthly rent)
    # Try unit_mix (rent roll line items) next
    if deal.extracted_docs and deal.extracted_docs.unit_mix:
        roll_total = 0.0
        for u in deal.extracted_docs.unit_mix:
            rent = u.get("monthly_rent") or u.get("market_rent") or 0
            count = u.get("count") or 1
            roll_total += float(rent) * float(count)
        if roll_total > 0:
            gpr = roll_total * 12
            logger.info(
                f"GPR: from unit_mix sum = ${gpr:,.0f}/yr "
                f"({len(deal.extracted_docs.unit_mix)} rows)"
            )
            return gpr

    # Last fallback: assumptions-based estimate. Use the extracted doc's
    # avg_rent_per_unit when present (it's populated by Prompt 1B from rent
    # rolls and ends up synthesised to the quality_adjusted_market_rent
    # assumption via deal_data._compute_market_rents). `monthly_rent_per_unit`
    # was the old attribute name and was removed from FinancialAssumptions;
    # calling getattr on it always returned None, silently zeroing GPR.
    num_units = deal.assumptions.num_units or 0
    avg_rent = (
        (deal.extracted_docs.avg_rent_per_unit if deal.extracted_docs else None)
        or getattr(deal.assumptions, 'quality_adjusted_market_rent', None)
        or 0
    )
    if num_units > 0 and avg_rent > 0:
        gpr = num_units * avg_rent * 12
        logger.info(f"GPR: from assumptions ({num_units} units × ${avg_rent}/mo) = ${gpr:,.0f}/yr")
        return gpr

    # Fallback 4: use market data rent if available.
    # Tries (a) MarketData PSF attributes — none exist today but kept for
    # forward-compat — then (b) comps median PSF from commercial_comps /
    # rent_comps, which is the real source of market PSF data.
    market_rent_psf = (
        getattr(deal.market_data, 'median_asking_rent_psf', None) or
        getattr(deal.market_data, 'median_rent_psf', None) or
        getattr(deal.market_data, 'hud_fmr_1br', None) or
        None
    )
    if not market_rent_psf:
        comps = getattr(deal, 'comps', None)
        if comps is not None:
            psf_vals = [c.asking_rent_per_sf for c in (comps.commercial_comps or [])
                        if c.asking_rent_per_sf and c.asking_rent_per_sf > 0]
            if not psf_vals:
                psf_vals = [c.rent_per_sf for c in (comps.rent_comps or [])
                            if c.rent_per_sf and c.rent_per_sf > 0]
            if psf_vals:
                psf_vals.sort()
                n = len(psf_vals)
                market_rent_psf = (psf_vals[n // 2] if n % 2
                                   else (psf_vals[n // 2 - 1] + psf_vals[n // 2]) / 2)

    gba = getattr(deal.assumptions, 'gba_sf', 0) or 0

    if market_rent_psf and market_rent_psf > 0 and gba > 0:
        gpr = gba * market_rent_psf
        logger.info(
            f"GPR: from market data (GBA {gba} SF × ${market_rent_psf:.2f}/SF/yr "
            f"from market research) = ${gpr:,.0f}/yr"
        )
        return gpr

    # Fallback 5 (last resort): hardcoded conservative commercial estimate
    if gba > 0:
        rent_psf = 16.67
        gpr = gba * rent_psf
        logger.info(
            f"GPR: final fallback — GBA {gba} SF × ${rent_psf}/SF/yr "
            f"(no market data available) = ${gpr:,.0f}/yr"
        )
        return gpr

    logger.warning("GPR: all fallbacks exhausted — returning 0")
    return 0.0


# ═══════════════════════════════════════════════════════════════════════════
# §3A  RENOVATION-AWARE UNIT CASHFLOW SCHEDULE  (helper — NOT yet wired)
# ═══════════════════════════════════════════════════════════════════════════

def _build_unit_cashflow_schedule(deal: DealData) -> List[Dict]:
    """Month-by-month rent schedule for every unit, aggregated to annual GPR.

    Each unit follows this sequence (renovation path):
        [0 … lease_expiry_month):       current_rent
        [lease_expiry_month … +downtime): 0 (offline)
        [downtime … +lease_up_months):  0 (leasing up)
        [stabilized …):                 market_rent, grown by annual_rent_growth

    For ``RenovationTier.NEW_CONSTRUCTION`` every unit contributes $0 for the
    full construction window (``const_period_months``) and then market rent
    from completion onward, escalating at ``annual_rent_growth``.

    **Not yet plumbed into the main pro-forma.** The existing pro-forma
    builder (_build_proforma, line 726) uses a flat Year-1 GPR escalated by
    ``annual_rent_growth``, combined with a ``stab_factors`` ramp. Wiring
    this schedule into GPR requires coordinated changes in _build_proforma
    (line 791), _run_monte_carlo (uses ``bp['gpr_yr1']`` as base at line
    1489), and the sensitivity matrix builder (also consumes ``gpr_yr1``).
    Until that integration is reviewed end-to-end, this helper is called
    from ``run_financials`` purely to log the alternate annual GPR so the
    delta vs. the flat model is visible in ``server_output.log``.

    Returns one dict per unit with keys:
        unit_id, curr_rent, mkt_rent, annual_gpr (dict yr→$),
        lease_expiry_month, reno_start, reno_end, leaseup_end.
    """
    a = deal.assumptions
    tier_val = getattr(a, "renovation_tier",
                       RenovationTier.LIGHT_COSMETIC.value)
    if isinstance(tier_val, RenovationTier):
        tier_val = tier_val.value
    downtime    = RENOVATION_DOWNTIME_MONTHS.get(tier_val, 2)
    lease_up    = int(getattr(a, "lease_up_months", 1) or 1)
    mkt_default = float(getattr(a, "quality_adjusted_market_rent", 0) or 0)
    rent_growth = float(getattr(a, "annual_rent_growth", 0.03) or 0.03)
    hold_years  = int(getattr(a, "hold_period", 10) or 10)
    hold_months = hold_years * 12
    constr_months = int(getattr(a, "const_period_months", 0) or 0)

    units = (deal.extracted_docs.unit_mix
             if deal.extracted_docs and deal.extracted_docs.unit_mix
             else [])
    is_new_construction = (tier_val == RenovationTier.NEW_CONSTRUCTION.value)

    schedules: List[Dict] = []
    for u in units:
        curr_rent = float(u.get("monthly_rent") or 0)
        mkt       = float(u.get("market_rent") or mkt_default or 0)

        # lease_expiry_year is a hold-year integer (1-based, 0 = unknown).
        # Translate to month-offset; fall back to construction end month for
        # new construction or if the lease is already expired / unknown.
        try:
            expiry_yr = int(u.get("lease_expiry_year") or 0)
        except (TypeError, ValueError):
            expiry_yr = 0
        if expiry_yr > 0:
            lease_expiry_month = expiry_yr * 12
        else:
            lease_expiry_month = max(constr_months, 0)

        reno_start  = lease_expiry_month
        reno_end    = reno_start + downtime
        leaseup_end = reno_end + lease_up

        monthly: List[float] = []
        for m in range(hold_months):
            if is_new_construction:
                if m < constr_months:
                    monthly.append(0.0)
                else:
                    yrs_stable = (m - constr_months) / 12.0
                    monthly.append(mkt * ((1 + rent_growth) ** yrs_stable))
            else:
                if m < reno_start:
                    monthly.append(curr_rent)
                elif m < leaseup_end:
                    monthly.append(0.0)
                else:
                    yrs_stable = (m - leaseup_end) / 12.0
                    monthly.append(mkt * ((1 + rent_growth) ** yrs_stable))

        annual_gpr: Dict[int, float] = {}
        for yr in range(1, hold_years + 1):
            start = (yr - 1) * 12
            end   = yr * 12
            annual_gpr[yr] = sum(monthly[start:end])

        count = int(u.get("count") or 1)
        schedules.append({
            "unit_id":            u.get("unit_id") or u.get("unit_number", ""),
            "count":              count,
            "curr_rent":          curr_rent,
            "mkt_rent":           mkt,
            "annual_gpr":         {k: v * count for k, v in annual_gpr.items()},
            "lease_expiry_month": lease_expiry_month,
            "reno_start":         reno_start,
            "reno_end":           reno_end,
            "leaseup_end":        leaseup_end,
        })

    return schedules


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
    total = fixed + mgmt + var_base * g

    # Diagnostic: set DEBUG_EXPENSES=1 to emit a full line-item breakdown
    # for Year 1 (where reconciliation discrepancies have been reported).
    import os
    if year == 1 and os.environ.get("DEBUG_EXPENSES") == "1":
        logger.info(
            "DEBUG_EXPENSES Year 1 breakdown: "
            "re_taxes=%.0f insurance=%.0f gas=%.0f water_sewer=%.0f "
            "electric=%.0f license=%.0f trash=%.0f | fixed_subtotal=%.0f | "
            "mgmt_pct=%.4f × egi=%.0f = mgmt=%.0f | "
            "salaries=%.0f repairs=%.0f exterminator=%.0f cleaning=%.0f "
            "turnover=%.0f advertising=%.0f landscape=%.0f admin=%.0f "
            "office=%.0f misc=%.0f | var_subtotal=%.0f | "
            "growth_factor=%.4f | TOTAL=%.0f",
            a.re_taxes, insurance, a.gas, a.water_sewer,
            a.electric, a.license_inspections, a.trash, fixed,
            a.mgmt_fee_pct, egi, mgmt,
            a.salaries, a.repairs, a.exterminator, a.cleaning,
            a.turnover, a.advertising, a.landscape_snow, a.admin_legal_acct,
            a.office_phone, a.miscellaneous, var_base,
            g, total,
        )
    return total


def _year_noi(gpr_yr1: float, year: int, a, insurance: float) -> Tuple[float, float, float, float]:
    """Return (GPR, EGI, OpEx, NOI) for *year*."""
    gpr, egi = _year_income(gpr_yr1, year, a)
    opex = _year_expenses(egi, year, a, insurance)
    return gpr, egi, opex, egi - opex


# ═══════════════════════════════════════════════════════════════════════════
# §3B REFI APPRAISED VALUE HELPER
# ═══════════════════════════════════════════════════════════════════════════

def _refi_appraised_value(pro_forma_years: list,
                          refi_year: int,
                          refi_cap_rate: float) -> float:
    """
    Compute appraised value at refinance as:
      NOI in refi year / refi appraisal cap rate

    The refi happens AT THE END of the refi year.
    Use the NOI for that same year (index refi_year - 1).

    Returns 0.0 if refi_year is out of range or
    cap rate is zero/negative.
    """
    if refi_year < 1 or refi_year > len(pro_forma_years):
        logger.warning(
            "REFI APPRAISED VALUE: refi_year %s out of "
            "range (pro_forma has %s years) — returning 0",
            refi_year, len(pro_forma_years))
        return 0.0
    if refi_cap_rate <= 0:
        logger.warning(
            "REFI APPRAISED VALUE: cap rate %.4f invalid "
            "— returning 0", refi_cap_rate)
        return 0.0
    noi = pro_forma_years[refi_year - 1].get('noi', 0.0)
    value = noi / refi_cap_rate
    return value


# ═══════════════════════════════════════════════════════════════════════════
# §3C LEASE EVENT ENGINE
# ═══════════════════════════════════════════════════════════════════════════

def _compute_lease_events(deal) -> dict:
    """
    For each hold year 1–N, compute total below-the-line lease costs from
    commission, TI, and downtime across all tenants.

    Returns {year_int: {commission, ti, downtime_loss, total_lease_cost, detail}}.
    """
    a = deal.assumptions
    hold = a.hold_period
    gba = a.gba_sf or 0
    ext = deal.extracted_docs
    units = (ext.unit_mix or []) if ext else []

    # GPR/SF for fallback market rent
    gpr_yr1 = _gpr_yr1(deal)
    rent_psf_fallback = (gpr_yr1 / gba) if gba > 0 else 0.0

    year_costs: dict = {}
    tenant_count = 0
    total_commission_all = 0.0
    total_ti_all = 0.0
    total_downtime_all = 0.0

    for u in units:
        sf = float(u.get("sf") or 0)
        if sf <= 0:
            continue
        tenant_count += 1

        is_vacant = u.get("is_vacant", False) or u.get("status", "") == "Vacant"
        lease_term = float(u.get("lease_term_years") or 5)
        expiry_yr = int(u.get("lease_expiry_year") or 0)
        renewal_prob = float(u.get("renewal_probability") or 0.70)
        downtime_mo = int(u.get("downtime_months") or 3)
        market_rent = float(u.get("market_rent_sf") or 0)
        current_rent_sf = float(u.get("current_rent_sf") or 0)
        unit_id = u.get("unit_id") or "?"

        # A. Vacant at acquisition — new lease in Year 1
        if is_vacant:
            yr = 1
            mkt = market_rent if market_rent > 0 else rent_psf_fallback
            if mkt <= 0:
                logger.warning(
                    "LEASE EVENT: vacant unit %s — no market rent "
                    "and no GPR fallback available. Commission "
                    "cannot be calculated. Set market rent on the "
                    "rent roll or enter GPR assumptions.",
                    unit_id)
                continue
            glv = mkt * sf * lease_term
            comm = glv * a.commission_new_pct
            ti = a.ti_new_psf * sf
            detail = f"VACANT {unit_id}: new lease Yr1 GLV=${glv:,.0f} comm=${comm:,.0f} TI=${ti:,.0f}"

            if yr not in year_costs:
                year_costs[yr] = {"commission": 0, "ti": 0, "downtime_loss": 0,
                                  "total_lease_cost": 0, "detail": []}
            year_costs[yr]["commission"] += comm
            year_costs[yr]["ti"] += ti
            year_costs[yr]["total_lease_cost"] += comm + ti
            year_costs[yr]["detail"].append(detail)
            total_commission_all += comm
            total_ti_all += ti
            continue

        # B. Occupied tenant with known expiry
        if expiry_yr < 1 or expiry_yr > hold:
            if expiry_yr == 0:
                logger.warning("LEASE EVENT: tenant %s has no expiry year — skipping", unit_id)
            continue

        yr = expiry_yr

        # Renewal scenario (weighted by renewal_prob)
        renewal_rent = (market_rent if market_rent > 0
                        else current_rent_sf * (1 + a.annual_rent_growth) ** expiry_yr)
        renewal_glv = renewal_rent * sf * lease_term
        renewal_comm = renewal_glv * a.commission_renewal_pct * renewal_prob
        renewal_ti = a.ti_renewal_psf * sf * renewal_prob

        # Non-renewal scenario (weighted by 1 - renewal_prob)
        non_renew_prob = 1.0 - renewal_prob
        new_rent = (market_rent if market_rent > 0
                    else current_rent_sf * (1 + a.annual_rent_growth) ** expiry_yr)
        new_glv = new_rent * sf * lease_term
        new_comm = new_glv * a.commission_new_pct * non_renew_prob
        new_ti = a.ti_new_psf * sf * non_renew_prob

        # Downtime loss (non-renewal path only)
        monthly_rent_loss = new_rent * sf / 12.0
        downtime_loss = monthly_rent_loss * downtime_mo * non_renew_prob

        total_comm = renewal_comm + new_comm
        total_ti = renewal_ti + new_ti

        detail = (f"{unit_id}: Yr{yr} comm=${total_comm:,.0f} TI=${total_ti:,.0f} "
                  f"downtime=${downtime_loss:,.0f} (renew={renewal_prob:.0%})")

        if yr not in year_costs:
            year_costs[yr] = {"commission": 0, "ti": 0, "downtime_loss": 0,
                              "total_lease_cost": 0, "detail": []}
        year_costs[yr]["commission"] += total_comm
        year_costs[yr]["ti"] += total_ti
        year_costs[yr]["downtime_loss"] += downtime_loss
        year_costs[yr]["total_lease_cost"] += total_comm + total_ti + downtime_loss
        year_costs[yr]["detail"].append(detail)
        total_commission_all += total_comm
        total_ti_all += total_ti
        total_downtime_all += downtime_loss

    # Vacant asset / no executed leases: commissions & TI are pre-stabilization
    # costs already captured in S&U, NOT recurring Pro Forma items.
    # Leave year_costs empty so Pro Forma writes $0 for commissions/TI until
    # an actual lease event occurs.
    if tenant_count == 0 and gpr_yr1 > 0:
        logger.info("LEASE EVENTS: vacant asset — commissions=$0, TI=$0 in Pro Forma "
                    "(pre-stabilization costs already captured in S&U)")

    event_years = len([y for y in year_costs if year_costs[y]["total_lease_cost"] > 0])
    logger.info(
        "LEASE EVENTS: %d tenants processed | total commissions=$%.0f | "
        "total TI=$%.0f | total downtime=$%.0f across %d event years",
        tenant_count, total_commission_all, total_ti_all,
        total_downtime_all, event_years)
    for yr in sorted(year_costs):
        for d in year_costs[yr]["detail"]:
            logger.info("  %s", d)

    return year_costs


# ═══════════════════════════════════════════════════════════════════════════
# §4  PRO FORMA BUILDER  (hold strategies)
# ═══════════════════════════════════════════════════════════════════════════

def _get_stabilization_factors(deal_data: DealData) -> list[float]:
    """
    Returns a list of stabilization factors, one per hold year (up to 10).

    Logic is driven entirely by user inputs — no hardcoded ramps:

    STABILIZED ASSET (const_period_months=0, leaseup_period_months=0):
        All years = 1.0

    RENOVATION / CONSTRUCTION (const_period_months > 0):
        Factor = 0.0 during the construction period.
        Factor ramps up evenly during the lease-up period.
        Factor = 1.0 once fully stabilized.

    Ramp calculation (month-accurate):
        For year Y (1-indexed), using end-of-year month boundary:
            months_at_end_of_year = Y * 12
            if months_at_end_of_year <= const_months:
                factor = 0.0   (still in construction)
            elif months_at_end_of_year <= const_months + leaseup_months:
                fraction = (months_at_end_of_year - const_months) / leaseup_months
                factor = round(min(fraction, 1.0), 4)
            else:
                factor = 1.0   (stabilized)

    Examples:
        const=0,  leaseup=0   → [1.0, 1.0, 1.0, ...]
        const=12, leaseup=12  → [0.0, 1.0, 1.0, ...]   (fully leased up by end Y2)
        const=12, leaseup=24  → [0.0, 0.5, 1.0, ...]   (ramp over 2 yrs)
        const=18, leaseup=18  → [0.0, 0.17, 1.0, ...]
            (Y1 end=12mo < 18 const → 0; Y2 end=24mo, 6mo into leaseup/18 = 0.33;
             Y3 end=36mo, 18mo into leaseup/18 = 1.0)
        const=24, leaseup=12  → [0.0, 0.0, 1.0, ...]
    """
    a = deal_data.assumptions
    hold = max(a.hold_period or 10, 10)

    const_months   = float(a.const_period_months   or 0)
    leaseup_months = float(a.leaseup_period_months or 0)

    logger.info(
        "STAB INPUTS: strategy=%s, const_months=%.0f, leaseup_months=%.0f",
        deal_data.investment_strategy,
        const_months,
        leaseup_months,
    )

    factors = []
    for yr in range(1, hold + 1):
        end_month = yr * 12  # month count at END of this year

        if const_months == 0 and leaseup_months == 0:
            # Fully stabilized asset — always 100%
            factor = 1.0
        elif end_month <= const_months:
            # Still in construction at end of this year
            factor = 0.0
        elif const_months > 0 and leaseup_months == 0:
            # Construction with no modeled lease-up (immediate stabilization
            # after construction). First year past const = 1.0
            factor = 1.0
        elif end_month <= const_months + leaseup_months:
            # In lease-up: linearly ramp from 0 → 1 over leaseup_months
            months_into_leaseup = end_month - const_months
            factor = round(min(months_into_leaseup / leaseup_months, 1.0), 4)
        else:
            # Past the end of lease-up — fully stabilized
            factor = 1.0

        factors.append(factor)

    # Log a compact summary for the server log
    logger.info(
        "STAB DEBUG: ext=%s, ext.occupancy_rate=%s, deal.current_occupancy_rate=%s, "
        "const_months=%.0f, leaseup_months=%.0f",
        getattr(deal_data, 'extracted_docs', None) is not None,
        getattr(getattr(deal_data, 'extracted_docs', None), 'occupancy_rate', 'MISSING'),
        getattr(deal_data, 'current_occupancy_rate', 'MISSING'),
        const_months,
        leaseup_months,
    )
    logger.info("STAB FACTORS: factors=%s", factors[:10])

    return factors[:10]  # always return exactly 10 elements


def _stabilized_noi_for_appraisal(noi_by_year, stab_factors, refi_year_idx, label="REFI"):
    """Return the NOI to use for a refi appraisal, using the first fully-stabilized
    year (stab_factor >= 1.0) at or after refi_year_idx (0-based)."""
    n = min(len(noi_by_year), len(stab_factors))
    for yr in range(refi_year_idx, n):
        if stab_factors[yr] >= 1.0:
            logger.info("%s APPRAISAL: stab NOI = %.2f from Year %d "
                        "(stab_factor=%.2f, refi timing = Year %d)",
                        label, noi_by_year[yr], yr + 1,
                        stab_factors[yr], refi_year_idx + 1)
            return noi_by_year[yr]
    fallback = noi_by_year[-1] if noi_by_year else 0.0
    logger.warning("%s APPRAISAL: no stabilized year found from Year %d onward — "
                   "using Year %d NOI = %.2f fallback",
                   label, refi_year_idx + 1, len(noi_by_year), fallback)
    return fallback


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
    refi_balances: List[Optional[float]] = [None, None, None]  # up to 3 refis
    refi_executed: List[bool] = [False, False, False]  # track which refis actually funded

    total_equity = sources_uses["total_equity_required"]
    proforma: List[dict] = []

    # Log debt service parameters
    annual_io = loan_principal * loan_rate
    annual_pi = _monthly_payment(loan_principal, loan_rate, loan_amort) * 12
    io_period_years = loan_io_total / 12.0
    logger.info("DEBT SERVICE: IO_annual=%.2f, PI_annual=%.2f, IO_period=%.1f yrs",
                annual_io, annual_pi, io_period_years)

    # Compute lease events (commissions, TI, downtime) for each year
    lease_events = _compute_lease_events(deal)

    # Stabilization ramp: scale revenue during construction / lease-up years
    stab_factors = _get_stabilization_factors(deal)
    _ext = getattr(deal, 'extracted_docs', None)
    _occ_log = (
        (getattr(_ext, 'occupancy_rate', None) if _ext else None)
        or getattr(deal, 'current_occupancy_rate', None)
        or 1.0
    )
    logger.info("STAB FACTORS: occupancy=%.2f, factors=%s", _occ_log, stab_factors)

    # Pre-compute projected NOI timeline (pure function of year) so refi
    # appraisals can look forward to the first stabilized year rather than
    # using the current-year NOI (which may be negative during ramp).
    projected_noi_by_year: List[float] = []
    for _yr in range(1, max(hold, 10) + 1):
        _gpr, _egi, _opex, _noi = _year_noi(gpr1, _yr, a, insurance)
        _stab = stab_factors[min(_yr - 1, len(stab_factors) - 1)]
        if _stab != 1.0:
            _egi = _egi * _stab
            _opex = _year_expenses(_egi, _yr, a, insurance)
            _noi = _egi - _opex
        projected_noi_by_year.append(_noi)
    logger.info("PROJECTED NOI: %s",
                [f"Y{i+1}:{n:,.0f}" for i, n in enumerate(projected_noi_by_year)])

    for yr in range(1, hold + 1):
        gpr, egi, opex, noi = _year_noi(gpr1, yr, a, insurance)
        # Apply stabilization ramp to revenue AND to revenue-driven opex
        # components (management fee). This matches the Excel template's
        # Pro Forma B30 = B17 * mgmt_pct where B17 is already stabilized.
        # Previously opex was held at full value during stab < 1.0 which
        # inflated Year-1 negative NOI by the management fee delta
        # (produced a ~$10K diff vs. the Excel Pro Forma sheet).
        stab = stab_factors[min(yr - 1, len(stab_factors) - 1)]
        if stab != 1.0:
            gpr = gpr * stab
            egi = egi * stab
            opex = _year_expenses(egi, yr, a, insurance)
            noi = egi - opex

        # Debt service
        io_remaining = max(0, loan_io_total - loan_months)
        ds = _year_debt_service(loan_principal, loan_rate, loan_amort, io_remaining)
        loan_months += 12

        # Refi check (after this year's debt service is calculated)
        refi_proceeds = 0.0
        for refi in active_refis:
            if refi.year == yr:
                # Use the scheduled amortization balance at this month,
                # NOT the IO-adjusted elapsed count.  The amortization
                # schedule runs from month 1 regardless of IO structure,
                # so the outstanding balance at refi is the amort-table
                # balance at month (refi_year × 12).
                refi_month = loan_months          # = yr × 12
                old_balance = _loan_balance(loan_principal, loan_rate,
                                            loan_amort, refi_month)

                # Store amortized balance for Excel Refi Analysis tab
                refi_idx = a.refi_events.index(refi)
                if refi_idx < 3:
                    refi_balances[refi_idx] = round(old_balance, 2)

                # Compute appraised value dynamically, using the NOI from
                # the first STABILIZED year at or after refi timing.
                if refi.cap_rate > 0:
                    refi_num = refi_idx + 1
                    stab_noi_for_refi = _stabilized_noi_for_appraisal(
                        projected_noi_by_year, stab_factors, yr - 1,
                        label=f"REFI {refi_num}"
                    )
                    computed_appraised = stab_noi_for_refi / refi.cap_rate
                else:
                    computed_appraised = 0.0
                computed_appraised = max(0.0, computed_appraised)  # floor at 0
                refi.appraised_value = computed_appraised

                if refi.appraised_value <= 0:
                    logger.warning(
                        "REFI GUARD: Refi %d appraised value = %.0f — "
                        "suppressing refi event, setting proceeds to 0",
                        refi_idx + 1, refi.appraised_value)
                    refi_proceeds = 0.0
                    refi.active = False
                    break

                new_loan = refi.appraised_value * refi.ltv
                new_loan = max(0.0, new_loan)  # floor at 0

                logger.info("REFI %d GUARD: appraised=%.2f, ltv=%.2f, "
                            "new_loan=%.2f, prior_balance=%.2f",
                            refi_idx + 1, refi.appraised_value, refi.ltv,
                            new_loan, old_balance)

                # Guard: if loan is 0, refi does not fund
                if new_loan <= 0:
                    logger.warning(
                        "REFI [Year %d]: noi=%.2f, appraised=%.2f, "
                        "new_loan=0.00 (floored, refi skipped)",
                        yr, noi, refi.appraised_value)
                    refi_proceeds = 0.0
                    # Mark refi as not executed — original loan continues
                    refi.active = False
                    break

                prepay = old_balance * refi.prepay_pct
                # Closing costs: 1% origination + $3,500 flat, rounded to nearest $100
                costs = round((new_loan * 0.01) + 3500, -2)
                total_refi_costs = prepay + costs
                # Raw refi math (may be negative when new_loan < old_balance).
                # refi_proceeds is floored at 0 for the cash flow column; the
                # unfloored value is surfaced as an equity-injection disclosure
                # so LP-facing narratives must reveal it.
                raw_refi_net = new_loan - old_balance - total_refi_costs
                refi_proceeds = max(0.0, raw_refi_net)
                if raw_refi_net < 0:
                    equity_inject = -raw_refi_net   # positive magnitude
                    prov = deal.provenance.field_sources
                    prov[f"refi{refi_idx+1}_equity_injection_required"] = "True"
                    prov[f"refi{refi_idx+1}_equity_injection_amount"] = f"{equity_inject:.2f}"
                    prov[f"refi{refi_idx+1}_new_loan"] = f"{new_loan:.2f}"
                    prov[f"refi{refi_idx+1}_existing_balance"] = f"{old_balance:.2f}"
                    logger.warning(
                        "REFI %d EQUITY INJECTION REQUIRED: new_loan=$%.0f is "
                        "below existing balance=$%.0f. Borrower must inject "
                        "$%.0f of equity to execute the refi.",
                        refi_idx + 1, new_loan, old_balance, equity_inject,
                    )
                logger.info(
                    "REFI [Year %d]: appraised=%.2f, new_loan=%.2f, net_proceeds=%.2f",
                    yr, refi.appraised_value, new_loan, refi_proceeds)

                # Capture pre-switch state for the DS SWITCH log
                old_rate = loan_rate

                # Reset loan state
                loan_principal = new_loan
                loan_rate = refi.rate
                loan_amort = refi.amort_years
                loan_io_total = 0
                loan_months = 0
                if refi_idx < 3:
                    refi_executed[refi_idx] = True

                logger.info(
                    "DS SWITCH yr%d: switched from loan=%s@%s%% to refi=%s@%s%%",
                    yr, f"{old_balance:,.0f}", f"{old_rate * 100:.2f}",
                    f"{new_loan:,.0f}", f"{refi.rate * 100:.2f}")
                break

        # Below the line
        capex = a.cap_reserve_per_unit * num_units
        below = capex
        if yr == 1:
            below += a.commissions_yr1 + a.renovations_yr1

        # Lease event costs (commissions, TI, downtime) — only populated for
        # real lease events; $0 for vacant asset / no executed leases
        yr_lease = lease_events.get(yr, {})
        lc_commission = yr_lease.get("commission", 0.0)
        lc_ti = yr_lease.get("ti", 0.0)
        lc_downtime = yr_lease.get("downtime_loss", 0.0)
        below += lc_commission + lc_ti + lc_downtime
        # unit_mix lives on deal.extracted_docs (ExtractedDocumentData), as
        # List[Dict[str, Any]]. There is no "lease_start" field in the model;
        # a unit represents an executed lease iff it's not vacant — matching
        # the logic in _compute_lease_events (financials.py:333).
        unit_data = (deal.extracted_docs.unit_mix
                     if deal.extracted_docs and deal.extracted_docs.unit_mix
                     else [])
        has_leases = any(
            isinstance(u, dict)
            and not (u.get("is_vacant", False) or u.get("status", "") == "Vacant")
            for u in unit_data
        )
        logger.info("LEASE COSTS yr%d: commissions=%s TI=%s (has_leases=%s)",
                    yr, f"{lc_commission:,.0f}", f"{lc_ti:,.0f}", has_leases)

        fcf = noi - ds - below + refi_proceeds
        coc = fcf / total_equity if total_equity > 0 else 0.0

        proforma.append({
            "year": yr,
            # Stabilization ramp factor for this year (0.0 during construction,
            # → 1.0 once the property is fully stabilized). Stored per year so
            # downstream consumers (context_builder income summary) don't need to
            # re-derive it from _get_stabilization_factors.
            "stabilization_factor": round(float(stab), 4),
            "gpr": round(gpr, 2),
            "egi": round(egi, 2),
            "opex": round(opex, 2),
            "noi": round(noi, 2),
            "debt_service": round(ds, 2),
            "capex_reserve": round(capex, 2),
            "leasing_commission": round(lc_commission, 2),
            "tenant_improvements": round(lc_ti, 2),
            "downtime_loss": round(lc_downtime, 2),
            "refi_proceeds": round(refi_proceeds, 2),
            "fcf": round(fcf, 2),
            "cash_on_cash": round(coc, 4),
        })

    # ── Log debt service by year (active loan check for refi-active deals) ──
    ds_by_year = {f"Y{yr['year']}": f"{yr.get('debt_service', 0):,.0f}"
                  for yr in proforma}
    logger.info("DS BY YEAR (active loan check): %s", ds_by_year)

    # ── Log refi appraised values ──────────────────────────────────
    refis = a.refi_events[:3]
    def _refi_noi(r, idx):
        if r.active and 1 <= r.year <= len(proforma):
            return proforma[r.year - 1].get('noi', 0)
        return 0
    logger.info(
        "REFI APPRAISED VALUE: "
        "Refi1 active=%s yr=%s NOI=%.0f cap=%.4f → $%.0f | "
        "Refi2 active=%s yr=%s NOI=%.0f cap=%.4f → $%.0f | "
        "Refi3 active=%s yr=%s NOI=%.0f cap=%.4f → $%.0f",
        refis[0].active, refis[0].year,
        _refi_noi(refis[0], 0), refis[0].cap_rate, refis[0].appraised_value,
        refis[1].active, refis[1].year,
        _refi_noi(refis[1], 1), refis[1].cap_rate, refis[1].appraised_value,
        refis[2].active, refis[2].year,
        _refi_noi(refis[2], 2), refis[2].cap_rate, refis[2].appraised_value,
    )

    # ── Exit ──────────────────────────────────────────────────────
    forward_noi = _year_noi(gpr1, hold + 1, a, insurance)[3]
    if forward_noi is not None and forward_noi <= 0:
        gross_sale = 0.0
        logger.warning(
            "Exit NOI is negative (%.0f) -- exit value set to $0. "
            "Cap rate exit is not viable on this deal.", forward_noi
        )
    else:
        gross_sale = forward_noi / a.exit_cap_rate if a.exit_cap_rate > 0 else 0.0
    disposition = gross_sale * a.disposition_costs_pct
    net_sale = gross_sale - disposition

    exit_balance = _loan_balance(loan_principal, loan_rate, loan_amort, loan_months)
    net_equity_at_exit = net_sale - exit_balance

    exit_info = {
        "gross_sale_price": round(gross_sale, 2),
        "net_sale_proceeds": round(net_sale, 2),
        "exit_loan_balance": round(exit_balance, 2),
        "net_equity_at_exit": round(net_equity_at_exit, 2),
        "refi_balances": refi_balances,
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
            # For-sale / flip strategy has no lease-up — units are never
            # "stabilized" in the hold-and-rent sense. Record 0.0 so the
            # income-summary consumer treats these years as non-stabilized
            # and falls through to the legacy path.
            "stabilization_factor": 0.0,
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
    # Non-convergence is expected behavior for scenarios with all-negative or
    # strongly-front-loaded cash flows (common in sensitivity / MC sweeps for
    # distressed or negative-NOI deals). Downgrade to DEBUG so the report
    # pipeline doesn't spam dozens of identical WARNINGs per run; callers
    # still get a None back which they can log once at the aggregate level.
    try:
        val = npf.irr(cashflows)
        if val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))):
            all_neg = all(cf <= 0 for cf in cashflows)
            logger.debug(
                "IRR solver did not converge — %s (cf[0]=%.0f, sum=%.0f)",
                "all cash flows negative" if all_neg else "no real solution",
                cashflows[0], sum(cashflows))
            return None
        return float(val)
    except Exception as exc:
        logger.debug("IRR solver exception: %s (cf[0]=%.0f, sum=%.0f)",
                     exc, cashflows[0], sum(cashflows))
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
    fo = deal.financial_outputs
    # Equity gap = Total Uses − Senior Debt (not total_equity_required which
    # also subtracts mezz / tax-credit / grants).
    total_uses  = fo.total_uses or abs(project_cfs[0])
    senior_debt = fo.initial_loan_amount or 0.0
    equity_gap  = total_uses - senior_debt
    logger.info("Waterfall equity gap: %.2f  (total_uses=%.2f - senior_debt=%.2f)",
                equity_gap, total_uses, senior_debt)
    lp_equity = equity_gap * a.lp_equity_pct
    gp_equity = equity_gap * a.gp_equity_pct
    n = len(project_cfs) - 1  # number of periods

    pos_cfs = np.array([max(0.0, cf) for cf in project_cfs[1:]], dtype=np.float64)
    total_dist = float(pos_cfs.sum())

    # ── Simple waterfall ─────────────────────────────────────────
    if a.waterfall_type == WaterfallType.SIMPLE:
        lp_total = total_dist * a.simple_lp_split
        gp_total = total_dist * (1 - a.simple_lp_split)
        return _waterfall_result(project_cfs, pos_cfs, total_dist,
                                 lp_equity, gp_equity, lp_total, gp_total)

    # ── Full tiered waterfall (year-by-year, matches Excel) ─────
    if total_dist <= 0:
        return _waterfall_result(project_cfs, pos_cfs, 0.0,
                                 lp_equity, gp_equity, 0.0, 0.0)

    lp_cfs, gp_cfs = _tiered_waterfall_yearly(
        project_cfs, deal, lp_equity, gp_equity)

    lp_total = float(np.sum(lp_cfs[1:]))
    gp_total = float(np.sum(gp_cfs[1:]))

    logger.info("LP IRR input CFs: %s", [round(float(x), 2) for x in lp_cfs])
    lp_irr = _safe_irr(lp_cfs.tolist())
    gp_irr = _safe_irr(gp_cfs.tolist())
    logger.info("LP IRR result: %s", f"{lp_irr:.6f}" if lp_irr is not None else "N/A")

    return {
        "lp_irr": lp_irr,
        "gp_irr": gp_irr,
        "project_irr": _safe_irr(project_cfs),
        "lp_em": _equity_multiple(lp_cfs.tolist()),
        "gp_em": _equity_multiple(gp_cfs.tolist()),
        "project_em": _equity_multiple(project_cfs),
        "lp_total_dist": round(lp_total, 2),
        "gp_total_dist": round(gp_total, 2),
    }


def _waterfall_result(project_cfs, pos_cfs, total_dist,
                      lp_equity, gp_equity,
                      lp_total, gp_total) -> dict:
    """Build LP/GP cash-flow streams and compute IRR/EM.

    Uses year-by-year tier waterfall matching the Excel Cash Waterfall
    tab logic (pref return accrual, sequential tier allocation).
    Falls back to proportional weighting for SIMPLE waterfall.
    """
    n = len(pos_cfs)
    if total_dist > 0:
        weights = pos_cfs / total_dist
    else:
        weights = np.zeros(n)

    # Default proportional streams (used for SIMPLE waterfall)
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


def _tiered_waterfall_yearly(project_cfs: List[float],
                             deal: DealData,
                             lp_equity: float,
                             gp_equity: float) -> Tuple[np.ndarray, np.ndarray]:
    """Year-by-year tiered waterfall matching Excel Cash Waterfall tab.

    Excel layout:
        Col F  = Year 0: equity contributions only, distributions = 0
        Col G+ = Year 1..N: distributable CF from Pro Forma, tier logic

    Returns (lp_cfs, gp_cfs) arrays of length len(project_cfs).
    lp_cfs[0] = -lp_equity, lp_cfs[1..] = LP distributions per year.
    """
    a = deal.assumptions
    n = len(project_cfs) - 1  # operating years (1..N)

    distributable = np.array([max(0.0, cf) for cf in project_cfs[1:]])
    lp_pct = a.lp_equity_pct
    gp_pct = a.gp_equity_pct
    pref_rate = a.pref_return

    # ── Tier 1: Pref return + return of capital ──────────────────
    # Excel: Year 0 ending balance = equity (contribution, no dist)
    # Year 1+: accrue pref on beginning balance, distribute up to
    #          min(beg_bal + accrual, available_cash * equity_pct)
    lp_bal = lp_equity    # Year 0 ending balance
    gp_bal = gp_equity
    lp_dist_t1 = np.zeros(n)
    gp_dist_t1 = np.zeros(n)

    for yr in range(n):
        lp_accrual = lp_bal * pref_rate
        gp_accrual = gp_bal * pref_rate
        lp_avail = distributable[yr] * lp_pct
        gp_avail = distributable[yr] * gp_pct
        lp_d = min(lp_bal + lp_accrual, lp_avail)
        gp_d = min(gp_bal + gp_accrual, gp_avail)
        lp_dist_t1[yr] = lp_d
        gp_dist_t1[yr] = gp_d
        lp_bal = lp_bal + lp_accrual - lp_d
        gp_bal = gp_bal + gp_accrual - gp_d

    # Cash remaining after Tier 1 (total distributions, not per-party)
    total_t1 = lp_dist_t1 + gp_dist_t1
    remaining = distributable - total_t1
    remaining = np.maximum(remaining, 0.0)

    # ── Tiers 2..N: promote tiers ───────────────────────────────
    # Excel logic per tier:
    #   LP Beginning Balance starts at LP_equity (Year 0 = $D$8)
    #   Req'd Return = beg_bal × hurdle_rate
    #   Prior LP Distributions = sum of LP dists from earlier tiers
    #   LP Dist = max(min(beg + reqd - prior_lp, remaining * lp_share), 0)
    #   GP Dist = LP_Dist / lp_share * gp_share
    #   LP Ending Balance = beg + reqd - prior_lp - lp_dist
    all_lp_dists = [lp_dist_t1]
    all_gp_dists = [gp_dist_t1]

    for tier in a.waterfall_tiers:
        hurdle = tier.hurdle_value
        lp_sh = tier.lp_share
        gp_sh = 1.0 - lp_sh

        lp_dist_tier = np.zeros(n)
        gp_dist_tier = np.zeros(n)
        # Excel: F86 (Year 0 beginning) = $D$8 = lp_equity
        lp_bal_t = lp_equity

        for yr in range(n):
            reqd = lp_bal_t * hurdle
            prior_lp = sum(d[yr] for d in all_lp_dists)
            lp_d = max(min(lp_bal_t + reqd - prior_lp,
                           remaining[yr] * lp_sh), 0.0)
            gp_d = (lp_d / lp_sh * gp_sh) if lp_sh > 0 else 0.0
            # Don't exceed remaining cash
            total_tier = lp_d + gp_d
            if total_tier > remaining[yr] + 1e-6:
                scale = remaining[yr] / total_tier if total_tier > 0 else 0.0
                lp_d *= scale
                gp_d *= scale
            lp_dist_tier[yr] = lp_d
            gp_dist_tier[yr] = gp_d
            lp_bal_t = lp_bal_t + reqd - prior_lp - lp_d

        all_lp_dists.append(lp_dist_tier)
        all_gp_dists.append(gp_dist_tier)
        tier_total = lp_dist_tier + gp_dist_tier
        remaining = np.maximum(remaining - tier_total, 0.0)

    # ── Residual tier: split remaining cash ──────────────────────
    lp_residual = remaining * a.residual_tier.lp_share
    gp_residual = remaining * a.residual_tier.gp_share
    all_lp_dists.append(lp_residual)
    all_gp_dists.append(gp_residual)

    total_lp_per_yr = sum(d for d in all_lp_dists)
    total_gp_per_yr = sum(d for d in all_gp_dists)

    lp_cfs = np.concatenate([[-lp_equity], total_lp_per_yr])
    gp_cfs = np.concatenate([[-gp_equity], total_gp_per_yr])

    return lp_cfs, gp_cfs


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
    if forward_noi <= 0:
        gross_sale = 0.0
    else:
        gross_sale = forward_noi / exit_cap if exit_cap > 0 else 0.0
    net_sale = gross_sale * (1 - disp_pct)

    # Loan balance at exit (approximate — no refi)
    bal = _loan_balance(initial_loan, interest_rate, amort_years, hold * 12)
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
                       sources_uses: dict) -> dict:
    """Build all four sensitivity grids.

    Returns dict with keys:
        irr_matrix, em_matrix  — rent_growth × exit_cap
        noi_matrix             — expense_growth × vacancy → Year-1 NOI
        coc_matrix             — ltv × purchase_price → Year-1 CoC
        rent_axis, cap_axis    — axis labels for IRR/EM grids
    """
    a = deal.assumptions
    rent_axis = _inclusive_range(a.sens_rent_growth_low,
                                a.sens_rent_growth_high,
                                a.sens_rent_growth_step)
    cap_axis = _inclusive_range(a.sens_exit_cap_low,
                               a.sens_exit_cap_high,
                               a.sens_exit_cap_step)
    # Sort descending: higher cap rate (worse outcome) first
    cap_axis = sorted(cap_axis, reverse=True)

    base = _quick_params(deal, insurance, sources_uses)
    irr_matrix: List[List] = []
    em_matrix: List[List[float]] = []

    for rg in rent_axis:
        irr_row: List = []
        em_row: List[float] = []
        for ec in cap_axis:
            params = {**base, "rent_growth": rg, "exit_cap": ec}
            irr, em = _quick_project_irr(**params)
            # Debug: verify lower cap → higher exit_value → higher IRR
            if rg == rent_axis[len(rent_axis) // 2] and ec in (0.055, 0.07, 0.085):
                ev = base["gpr_yr1"] * (1 + rg) ** base["hold"] * (1 + rg) / ec
                logger.info("SENS DEBUG  exit_cap=%.3f  exit_value=%.0f  irr=%s  em=%.2f",
                            ec, ev, f"{irr:.4f}" if irr is not None else "N/A", em)
            irr_row.append(round(irr, 4) if irr is not None else "N/A")
            em_row.append(round(em, 2))
        irr_matrix.append(irr_row)
        em_matrix.append(em_row)

    # ── Year-1 NOI grid: expense_growth (rows) × vacancy (cols) ──
    # The prior formula used (1 + eg) ** 0 which is 1 for all eg values,
    # collapsing every row to the same NOI. We apply a single year of
    # expense growth ((1 + eg) ** 1) so rows differentiate on expense
    # growth as well as vacancy. Management fee scales with EGI (unchanged).
    vac_axis = [0.05, 0.075, 0.10, 0.125, 0.15, 0.20]
    exp_axis = [0.01, 0.02, 0.03, 0.04, 0.05]
    gpr1 = base["gpr_yr1"]
    noi_matrix: List[List[float]] = []
    for eg in exp_axis:
        eg_factor = 1.0 + eg
        noi_row: List[float] = []
        for vac in vac_axis:
            egi = gpr1 * (1 - vac - base["loss_to_lease"]) + base["cam"] + base["fee_income"]
            opex = ((base["fixed_base"] + insurance) * eg_factor +
                    egi * base["mgmt_pct"] +
                    base["var_base"] * eg_factor)
            noi_row.append(round(egi - opex, 2))
        noi_matrix.append(noi_row)

    # ── Year-1 CoC grid: LTV (rows) × purchase_price (cols) ─────
    price = a.purchase_price or 0
    ltv_axis = [0.60, 0.65, 0.70, 0.75, 0.80]
    price_mult = [1 - 0.15, 1 - 0.10, 1 - 0.05, 1.0, 1 + 0.05, 1 + 0.10]
    price_axis = [round(price * m, 2) for m in price_mult]
    logger.info(
        "COC SENSITIVITY: base_price=$%s, ltv_axis=%s, price_axis=%s (%d cols)",
        f"{price:,.0f}", ltv_axis,
        [f"${p:,.0f}" for p in price_axis], len(price_axis),
    )
    coc_matrix: List[List[float]] = []
    for ltv in ltv_axis:
        coc_row: List[float] = []
        for px in price_axis:
            loan = px * ltv
            equity = px - loan + (sources_uses["total_uses"] - (a.purchase_price or 0))
            ds = _year_debt_service(loan, a.interest_rate, a.amort_years, a.io_period_months)
            # Yr1 NOI from base scenario
            yr1_noi = base["gpr_yr1"] * (1 - a.vacancy_rate - base["loss_to_lease"]) + base["cam"] + base["fee_income"]
            yr1_opex = ((base["fixed_base"] + insurance) + yr1_noi * base["mgmt_pct"] + base["var_base"])
            yr1_noi_net = yr1_noi - yr1_opex
            fcf = yr1_noi_net - ds - base["capex_annual"]
            coc_row.append(round(fcf / equity, 4) if equity > 0 else 0.0)
        coc_matrix.append(coc_row)

    return {
        "irr_matrix": irr_matrix,
        "em_matrix": em_matrix,
        "noi_matrix": noi_matrix,
        "coc_matrix": coc_matrix,
        "rent_axis": [round(r, 4) for r in rent_axis],
        "cap_axis": [round(c, 4) for c in cap_axis],
    }


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
        logger.warning("Monte Carlo skipped -- no equity or no rent data")
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
    gross_sale = np.where(
        (cap_samples > 0) & (forward_noi > 0),
        forward_noi / cap_samples, 0.0
    )
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
# §9.5  PURCHASE-PRICE SOLVER  (Monte Carlo-backed binary search)
# ═══════════════════════════════════════════════════════════════════════════

def _mc_median_project_irr(deal: DealData, insurance: float,
                           sources_uses: dict, n_sim: int = 2_000) -> Optional[float]:
    """Light Monte Carlo returning only the median project IRR. Same input
    distributions as _run_monte_carlo; smaller sample for faster solver
    iterations."""
    a = deal.assumptions
    rng = np.random.default_rng(_RNG_SEED)
    bp = _quick_params(deal, insurance, sources_uses)
    hold = bp["hold"]
    total_equity = bp["total_equity"]
    if total_equity <= 0 or bp["gpr_yr1"] <= 0:
        return None

    rent_samples = rng.normal(a.annual_rent_growth, 0.015, n_sim)
    cap_samples = rng.normal(a.exit_cap_rate, 0.01, n_sim).clip(0.02, 0.15)
    vac_samples = rng.normal(a.vacancy_rate, 0.025, n_sim).clip(0.0, 0.40)
    exp_samples = rng.normal(a.expense_growth_rate, 0.01, n_sim)

    years = np.arange(1, hold + 1)
    ig = (1 + rent_samples[:, None]) ** (years[None, :] - 1)
    eg = (1 + exp_samples[:, None]) ** (years[None, :] - 1)
    gpr = bp["gpr_yr1"] * ig
    egi = gpr * (1 - vac_samples[:, None] - bp["loss_to_lease"]) + bp["cam"] + bp["fee_income"]
    opex = ((bp["fixed_base"] + insurance) * eg + egi * bp["mgmt_pct"] + bp["var_base"] * eg)
    noi = egi - opex

    ds = np.zeros((n_sim, hold))
    for yr_idx in range(hold):
        io_rem = max(0, bp["io_months"] - yr_idx * 12)
        ds[:, yr_idx] = _year_debt_service(bp["initial_loan"], bp["interest_rate"],
                                           bp["amort_years"], io_rem)
    fcf = noi - ds - bp["capex_annual"]

    forward_noi = noi[:, -1] * (1 + rent_samples)
    gross_sale = np.where((cap_samples > 0) & (forward_noi > 0),
                          forward_noi / cap_samples, 0.0)
    net_sale = gross_sale * (1 - bp["disp_pct"])
    amort_elapsed = max(0, hold * 12 - bp["io_months"])
    bal = _loan_balance(bp["initial_loan"], bp["interest_rate"],
                        bp["amort_years"], amort_elapsed)

    cf_matrix = np.zeros((n_sim, hold + 1))
    cf_matrix[:, 0] = -total_equity
    cf_matrix[:, 1:] = fcf
    cf_matrix[:, -1] += net_sale - bal

    irrs = _vectorized_irr(cf_matrix)
    valid = np.isfinite(irrs) & (irrs > -1.0) & (irrs < 5.0)
    if valid.sum() < 50:
        return None
    return float(np.median(irrs[valid]))


def solve_purchase_price_for_lp_irr(
    deal: DealData,
    insurance: float,
    target_lp_irr: float = 0.15,
    n_sim: int = 2_000,
    tolerance_irr: float = 0.002,
    max_iters: int = 15,
) -> dict:
    """Binary-search the purchase price such that the Monte Carlo median
    LP IRR equals ``target_lp_irr``.

    LP IRR is approximated as project IRR plus the constant offset observed
    in the deterministic pipeline run (fo.lp_irr − fo.project_irr). Waterfall
    parameters do not depend on purchase price, so the offset is stable across
    the search range — accuracy is within ±30 bps for typical deals.
    """
    a = deal.assumptions
    fo = deal.financial_outputs
    base_price = a.purchase_price or 0.0
    if base_price <= 0:
        logger.warning("Price solver skipped — no base purchase price")
        return {"converged": False, "reason": "no_base_price"}

    # LP-IRR offset from the deterministic pass
    if fo.lp_irr is not None and fo.project_irr is not None:
        lp_offset = fo.lp_irr - fo.project_irr
    else:
        lp_offset = 0.0
        logger.info("Price solver: no deterministic LP/project IRR; assuming LP = project")

    # Project IRR target that will produce target_lp_irr after offset.
    target_project_irr = target_lp_irr - lp_offset

    def _median_irr_at(price: float) -> Optional[float]:
        original = a.purchase_price
        try:
            a.purchase_price = price
            su = _compute_sources_uses(deal)
            return _mc_median_project_irr(deal, insurance, su, n_sim=n_sim)
        finally:
            a.purchase_price = original

    # Initial bracket: project IRR moves inversely with price. Widen until
    # the target is bracketed or we hit an outer bound.
    low, high = base_price * 0.25, base_price * 2.00
    irr_low = _median_irr_at(low)
    irr_high = _median_irr_at(high)
    if irr_low is None or irr_high is None:
        logger.warning("Price solver: MC failed at one of the brackets — aborting")
        return {"converged": False, "reason": "bracket_mc_failed",
                "base_purchase_price": base_price}

    # If target is outside the initial bracket, report and clip rather than
    # extrapolating wildly.
    if target_project_irr > irr_low:
        logger.warning(
            "Price solver: target LP IRR %.2f%% unreachable even at 25%% of "
            "base price (max median LP IRR ≈ %.2f%%).",
            target_lp_irr * 100, (irr_low + lp_offset) * 100,
        )
        return {
            "converged": False,
            "reason": "target_too_high",
            "target_lp_irr": target_lp_irr,
            "base_purchase_price": base_price,
            "max_lp_irr_at_floor": irr_low + lp_offset,
        }
    if target_project_irr < irr_high:
        logger.info(
            "Price solver: target LP IRR %.2f%% achievable at any price up to "
            "2x base (median LP IRR at 2x still %.2f%%); returning 2x base.",
            target_lp_irr * 100, (irr_high + lp_offset) * 100,
        )
        return {
            "converged": True,
            "reason": "target_easily_met",
            "target_lp_irr": target_lp_irr,
            "base_purchase_price": base_price,
            "solved_purchase_price": high,
            "solved_median_lp_irr": irr_high + lp_offset,
            "price_adjustment_pct": (high - base_price) / base_price,
            "iterations_used": 0,
        }

    # Binary search
    iters = 0
    mid = base_price
    irr_mid = target_project_irr
    while iters < max_iters:
        iters += 1
        mid = 0.5 * (low + high)
        irr_mid = _median_irr_at(mid)
        if irr_mid is None:
            logger.warning("Price solver: MC failed mid-search at price=$%.0f", mid)
            return {"converged": False, "reason": "mid_mc_failed",
                    "base_purchase_price": base_price}
        if abs(irr_mid - target_project_irr) <= tolerance_irr:
            break
        if irr_mid > target_project_irr:
            low = mid       # IRR too high → raise price
        else:
            high = mid      # IRR too low → lower price

    converged = abs(irr_mid - target_project_irr) <= tolerance_irr
    result = {
        "converged": converged,
        "target_lp_irr": target_lp_irr,
        "base_purchase_price": base_price,
        "solved_purchase_price": mid,
        "solved_median_lp_irr": irr_mid + lp_offset,
        "price_adjustment_pct": (mid - base_price) / base_price,
        "iterations_used": iters,
        "n_sim_per_iter": n_sim,
        "lp_offset_used": lp_offset,
    }
    logger.info(
        "PRICE SOLVER: target LP IRR %.2f%% → solved price $%s "
        "(base $%s, adj %+.1f%%, median LP IRR %.2f%%, %d iters, converged=%s)",
        target_lp_irr * 100,
        f"{mid:,.0f}", f"{base_price:,.0f}",
        result["price_adjustment_pct"] * 100,
        result["solved_median_lp_irr"] * 100,
        iters, converged,
    )
    return result


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
    client = anthropic.Anthropic(api_key=_get_anthropic_api_key())
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
# §10B  ASSET-TYPE EXPENSE DEFAULTS
# ═══════════════════════════════════════════════════════════════════════════

# Per-SF/yr rates for non-residential asset types
_OFFICE_SALARY_PSF     = 0.50   # $/SF/yr — minimal mgmt for small office
_OFFICE_INSURANCE_PSF  = 1.75   # $/SF/yr — typical small urban office
_INDUSTRIAL_SALARY_PSF = 0.25
_INDUSTRIAL_INS_PSF    = 1.25

# Office-specific per-SF rates (calibrated for commercial, not multifamily)
_OFFICE_REPAIRS_PSF      = 1.00   # $/SF/yr — light maintenance, no unit turns
_OFFICE_CLEANING_PSF     = 1.00   # $/SF/yr — janitorial/cleaning
_OFFICE_LANDSCAPE_PSF    = 0.50   # $/SF/yr — surface lot, minimal landscaping


def _scale_expenses_for_asset_type(deal: DealData) -> None:
    """Adjust expense defaults when the user hasn't overridden them and the
    asset type is non-residential.  Modifies ``deal.assumptions`` in place.

    Scaling rules (Industrial / Retail):
        salaries  → per-SF rate × GBA
        insurance → per-SF rate × GBA
        re_taxes  → OPA assessed value × local mill rate, if parcel data exists

    Scaling rules (Office):
        salaries, exterminator, turnover, advertising → $0  (multifamily-only)
        repairs/maintenance → $1.00/SF/yr
        janitorial/cleaning → $1.00/SF/yr
        landscape/snow      → $0.50/SF/yr
        insurance, re_taxes → same as Industrial/Retail (per-SF or OPA)

    Multifamily / Mixed-Use / Single-Family keep the original defaults because
    those were calibrated for residential.
    """
    a = deal.assumptions
    asset = deal.asset_type
    gba = a.gba_sf or (deal.parcel_data.building_sf if deal.parcel_data else None)

    # ── Public-data tax & insurance estimates (all asset types) ─────────
    # Priority: assessed-value × local effective tax rate is always the
    # authoritative number; user-entered re_taxes is preserved as a prior
    # estimate for audit but overridden when the public-data value is
    # available. Same logic for insurance (TIV × rate × catastrophe loading).
    try:
        from expense_pricing import (
            estimate_property_taxes,
            estimate_property_insurance,
        )
        _tax_est = estimate_property_taxes(deal)
        if _tax_est:
            _tax_val, _tax_src = _tax_est
            _prior = a.re_taxes
            a.re_taxes = _tax_val
            if _prior and _prior not in (0.0, _tax_val):
                logger.info(
                    "PUBLIC DATA: re_taxes user=$%s → overridden to $%s (%s)",
                    f"{_prior:,.0f}", f"{_tax_val:,.0f}", _tax_src,
                )
            else:
                logger.info(
                    "PUBLIC DATA: re_taxes ← $%s (%s)",
                    f"{_tax_val:,.0f}", _tax_src,
                )
        else:
            logger.info("PUBLIC DATA: no basis to estimate re_taxes "
                        "(no parcel assessed value or purchase price); "
                        "user-entered value retained")

        _ins_est = estimate_property_insurance(deal)
        if _ins_est:
            _ins_val, _ins_src = _ins_est
            _prior_ins = a.insurance
            a.insurance = _ins_val
            if _prior_ins and _prior_ins not in (0.0, _ins_val):
                logger.info(
                    "PUBLIC DATA: insurance user=$%s → overridden to $%s (%s)",
                    f"{_prior_ins:,.0f}", f"{_ins_val:,.0f}", _ins_src,
                )
            else:
                logger.info(
                    "PUBLIC DATA: insurance ← $%s (%s)",
                    f"{_ins_val:,.0f}", _ins_src,
                )
        else:
            logger.info("PUBLIC DATA: no GBA — cannot estimate insurance; "
                        "user-entered value retained")
    except Exception as exc:
        logger.warning("PUBLIC DATA estimator failed (non-fatal): %s", exc)

    if asset not in (AssetType.OFFICE, AssetType.INDUSTRIAL, AssetType.RETAIL):
        logger.info("Expense defaults: asset_type=%s -- residential-track expenses "
                     "(salaries=$%s, insurance=$%s, re_taxes=$%s)",
                     asset.value, f"{a.salaries:,.0f}", f"{a.insurance:,.0f}", f"{a.re_taxes:,.0f}")
        return

    # ── Salaries ──────────────────────────────────────────────────
    # Office salaries are zeroed out in the Office-specific block below.
    # Only adjust for Industrial / Retail here.
    if asset != AssetType.OFFICE:
        if a.salaries == 0.0 and gba and gba > 0:
            rate = _INDUSTRIAL_SALARY_PSF if asset == AssetType.INDUSTRIAL else _OFFICE_SALARY_PSF
            old = a.salaries
            a.salaries = round(rate * gba, 2)
            logger.info("Expense scaling [%s]: salaries $%s -> $%s "
                         "(%.2f $/SF × %s SF)",
                         asset.value, f"{old:,.0f}", f"{a.salaries:,.0f}", rate, f"{gba:,.0f}")
        else:
            src = "user-set" if a.salaries != 0.0 else "default (no GBA)"
            logger.info("Expense scaling [%s]: salaries=$%s -- %s",
                         asset.value, f"{a.salaries:,.0f}", src)

    # Insurance and real-estate taxes now flow through expense_pricing above
    # (public-data estimator applied to every asset type). Legacy per-SF /
    # Philly-specific blocks removed.

    # ── Office-specific: zero out multifamily lines & recalibrate ─────
    if asset == AssetType.OFFICE:
        # Zero out lines that don't apply to commercial office
        for field, label in [
            ("salaries",    "salaries (no on-site staff)"),
            ("exterminator", "exterminator (N/A office)"),
            ("turnover",    "turnover (multifamily metric)"),
            ("advertising", "advertising (N/A stabilized office)"),
        ]:
            old = getattr(a, field)
            if old != 0.0:
                setattr(a, field, 0.0)
                logger.info("Expense scaling [%s]: %s $%s -> $0 -- zeroed out",
                             asset.value, label, f"{old:,.0f}")
            else:
                logger.info("Expense scaling [%s]: %s already $0 -- no change",
                             asset.value, label)

        # Recalibrate per-SF lines (only if GBA available)
        if gba and gba > 0:
            for field, rate, label in [
                ("repairs",        _OFFICE_REPAIRS_PSF,   "repairs/maintenance"),
                ("cleaning",       _OFFICE_CLEANING_PSF,  "janitorial/cleaning"),
                ("landscape_snow", _OFFICE_LANDSCAPE_PSF, "landscape/snow"),
            ]:
                old = getattr(a, field)
                new_val = round(rate * gba, 2)
                setattr(a, field, new_val)
                logger.info("Expense scaling [%s]: %s $%s -> $%s "
                             "(%.2f $/SF × %s SF)",
                             asset.value, label, f"{old:,.0f}", f"{new_val:,.0f}", rate, f"{gba:,.0f}")


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
    logger.info("run_financials: starting -- strategy=%s, purchase_price=%s, num_units=%s",
                deal.investment_strategy, deal.assumptions.purchase_price, deal.assumptions.num_units)
    try:
        a = deal.assumptions
        fo = deal.financial_outputs
        is_sale = deal.investment_strategy == InvestmentStrategy.OPPORTUNISTIC
        insurance = _get_insurance_expense(deal)

        # ── Asset-type expense scaling (before proforma) ──────────────
        _scale_expenses_for_asset_type(deal)
        # Re-read insurance after scaling may have changed a.insurance
        insurance = _get_insurance_expense(deal)

        # Auto-calculate title insurance if user has not provided a value
        # PA commercial rate: ~$6 per $1,000 of purchase price (blended owner + lender)
        if not a.title_insurance or a.title_insurance == 0:
            a.title_insurance = max(round(a.purchase_price * 0.006, -2), 3000)
            logger.info("TITLE INS: auto-calculated=%.2f (purchase_price=%.2f × 0.6%%)",
                        a.title_insurance, a.purchase_price)
        else:
            logger.info("TITLE INS: user-provided=%.2f", a.title_insurance)

        # Zero fee income if it is still at the system default of $6,000
        # — this value is not meaningful for any asset type unless the
        # user explicitly sets it to a non-default amount.
        FEE_INCOME_DEFAULT = 6000.0
        if a.fee_income == FEE_INCOME_DEFAULT:
            a.fee_income = 0.0
            logger.info(
                "FEE INCOME zeroed: value is system default "
                "$6,000 — not a user-set input. Set to $0."
            )

        # ── Sources & Uses ────────────────────────────────────────────
        su = _compute_sources_uses(deal)
        fo.total_uses = su["total_uses"]
        fo.total_sources = su["total_sources"]
        fo.total_equity_required = su["total_equity_required"]
        fo.initial_loan_amount = su["initial_loan"]
        fo.gp_equity = su["gp_equity"]
        fo.lp_equity = su["lp_equity"]
        fo.construction_interest_carry   = su["construction_interest_carry"]
        fo.construction_interest_schedule = su["construction_interest_schedule"]
        fo.total_project_cost            = su.get("total_project_cost", 0.0)

        # ── Pro Forma ─────────────────────────────────────────────────
        if is_sale:
            proforma, exit_info = _build_proforma_for_sale(deal, su)
        else:
            proforma, exit_info = _build_proforma(deal, insurance, su)

        fo.pro_forma_years = proforma
        fo.lease_events = _compute_lease_events(deal)

        if not is_sale and len(proforma) >= 8:
            logger.info(
                "DS TIMING CHECK yr1=%s yr2=%s yr3=%s yr7=%s yr8=%s",
                proforma[0]['debt_service'],
                proforma[1]['debt_service'],
                proforma[2]['debt_service'],
                proforma[6]['debt_service'],
                proforma[7]['debt_service'],
            )

        # ── GPR — always store regardless of strategy ─────────────────
        fo.gross_potential_rent = _gpr_yr1(deal)
        logger.info(f"FINANCIALS GPR computed: ${fo.gross_potential_rent:,.0f}")

        # ── Renovation-aware unit schedule (helper-only — NOT yet wired) ─
        # See _build_unit_cashflow_schedule docstring. This call is purely
        # diagnostic: it builds the schedule, logs the alternative annual
        # GPR by year, and raises a WARNING so the gap to the flat-escalation
        # pro-forma is visible. The actual replacement site is
        # financials.py:791 (inside _build_proforma where _year_noi is called)
        # plus the corresponding gpr_yr1 consumers in the sensitivity and
        # Monte Carlo builders near line 1489.
        try:
            _unit_schedules = _build_unit_cashflow_schedule(deal)
            if _unit_schedules:
                _hold = a.hold_period or 10
                _alt_gpr_by_yr = {
                    yr: sum(s["annual_gpr"].get(yr, 0) for s in _unit_schedules)
                    for yr in range(1, _hold + 1)
                }
                logger.debug(
                    "UNIT CASHFLOW SCHEDULE built (%d units, tier=%s) — "
                    "diagnostic only; not wired into pro-forma GPR. "
                    "Alt annual GPR: %s",
                    len(_unit_schedules),
                    getattr(a, "renovation_tier", "?"),
                    {k: f"${v:,.0f}" for k, v in _alt_gpr_by_yr.items()},
                )
        except Exception as _unit_exc:
            logger.warning(
                "UNIT CASHFLOW SCHEDULE: helper raised %s — diagnostic skipped",
                _unit_exc,
            )

        # ── Year-1 metrics ────────────────────────────────────────────
        if not is_sale and proforma:
            yr1 = proforma[0]
            gpr1 = _gpr_yr1(deal)
            _, egi1 = _year_income(gpr1, 1, a)

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
        fo.loan_balance_at_refi = exit_info.get("refi_balances")

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
        # Find first stabilized year (NOI > 0) for value-add deals
        _has_equity = su["total_equity_required"] > 0
        stabilized_year = None
        stabilized_noi = 0.0
        pf_nois = [yr.get("noi", 0) for yr in proforma]
        for _i, _noi in enumerate(pf_nois):
            if _noi > 0:
                stabilized_year = _i + 1
                stabilized_noi = _noi
                break
        if stabilized_year is None and pf_nois:
            stabilized_year = pf_nois.index(max(pf_nois)) + 1
            stabilized_noi = max(pf_nois)

        _has_income = _gpr_yr1(deal) > 0 or stabilized_noi > 0

        # Store stabilization metadata
        fo.sensitivity_stabilized_year = stabilized_year
        fo.sensitivity_stabilized_noi = stabilized_noi
        if stabilized_year and stabilized_noi > 0:
            fo.sensitivity_note = (
                f"Based on Year {stabilized_year} stabilized NOI of "
                f"${stabilized_noi:,.0f}. Assumes stabilization is "
                f"achieved as underwritten.")
        else:
            fo.sensitivity_note = (
                f"Property does not achieve positive NOI under current "
                f"assumptions. Matrix reflects returns based on Year "
                f"{stabilized_year or 1} NOI of ${stabilized_noi:,.0f}. "
                f"Sensitivity will improve materially upon lease execution.")

        logger.info("SENSITIVITY: stabilized_year=%s, stabilized_noi=$%.2f",
                    stabilized_year, stabilized_noi)
        logger.info("SENSITIVITY: note='%s'", fo.sensitivity_note)

        if not is_sale and _has_income and _has_equity:
            sens = _build_sensitivity(deal, insurance, su)
            fo.sensitivity_matrix = sens["irr_matrix"]
            fo.sensitivity_em_matrix = sens["em_matrix"]
            fo.sensitivity_noi_matrix = sens["noi_matrix"]
            fo.sensitivity_coc_matrix = sens["coc_matrix"]
            fo.sensitivity_axis_rent_growth = sens["rent_axis"]
            fo.sensitivity_axis_exit_cap = sens["cap_axis"]
            logger.info("Sensitivity matrices built: IRR %dx%d, NOI %dx%d",
                        len(sens["irr_matrix"]),
                        len(sens["irr_matrix"][0]) if sens["irr_matrix"] else 0,
                        len(sens["noi_matrix"]),
                        len(sens["noi_matrix"][0]) if sens["noi_matrix"] else 0)
        elif not is_sale:
            logger.warning(
                "Sensitivity matrix skipped — gpr_yr1=%.0f, equity=%.0f, stabilized_noi=%.0f. "
                "Matrix will be empty; report will suppress Section 12.5.",
                _gpr_yr1(deal), su["total_equity_required"], stabilized_noi
            )

        # ── Monte Carlo ───────────────────────────────────────────────
        if not is_sale and _has_income and _has_equity:
            mc = _run_monte_carlo(deal, insurance, su)
            fo.monte_carlo_results = mc

            # Prompt 5A — narrative
            if mc.get("median_irr") is not None:
                logger.info("Running Prompt 5A -- Monte Carlo Narrative...")
                narrative = _call_5a(deal, mc)
                fo.monte_carlo_narrative = narrative
                deal.narratives.monte_carlo_narrative = narrative
                if narrative:
                    logger.info("Prompt 5A complete -- narrative generated")
                else:
                    logger.warning("Prompt 5A failed -- narrative remains None")
            else:
                logger.warning("Monte Carlo produced no valid results -- skipping Prompt 5A")

            # Purchase-price solver — find the price that achieves a median
            # LP IRR of 15% across the same MC input distributions.
            try:
                fo.price_solver_results = solve_purchase_price_for_lp_irr(
                    deal, insurance, target_lp_irr=0.15
                )
            except Exception as exc:
                logger.warning("Price solver failed (non-fatal): %s", exc)
                fo.price_solver_results = {"converged": False, "reason": "exception"}
        elif not is_sale:
            logger.warning(
                "Monte Carlo skipped — gpr_yr1=%.0f, equity=%.0f. "
                "Simulation requires positive income and equity.",
                _gpr_yr1(deal), su["total_equity_required"]
            )

    except Exception:
        logger.error("run_financials FAILED:\n%s", traceback.format_exc())
        raise

    logger.info("run_financials: complete -- fo_is_none=%s, noi_yr1=%s, pro_forma_years_len=%s",
                deal.financial_outputs is None,
                getattr(deal.financial_outputs, 'noi_yr1', None),
                len(deal.financial_outputs.pro_forma_years) if deal.financial_outputs and deal.financial_outputs.pro_forma_years else 0)
    return deal
