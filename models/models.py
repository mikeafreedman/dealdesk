"""
models.py — DealDesk CRE Underwriting System
=============================================
Unified DealData schema — the foundational data contract for the entire pipeline.
Every module reads from and writes to this schema. No logic lives here.

Version:  v1.0  (initial approval build — April 6, 2026)
Status:   PENDING APPROVAL
"""

from __future__ import annotations
import re
from enum import Enum
from typing import Optional, List, Dict, Any, Union
from pydantic import BaseModel, Field, field_validator, model_validator


# ═══════════════════════════════════════════════════════════════════════════
# ENUMERATIONS
# ═══════════════════════════════════════════════════════════════════════════

class AssetType(str, Enum):
    """6 canonical asset types. Retail and Office are distinct first-class values."""
    MULTIFAMILY   = "Multifamily"
    MIXED_USE     = "Mixed-Use"
    RETAIL        = "Retail"
    OFFICE        = "Office"
    INDUSTRIAL    = "Industrial"
    SINGLE_FAMILY = "Single-Family"


class InvestmentStrategy(str, Enum):
    """
    3-strategy canonical taxonomy — effective April 7, 2026.
    Replaces the prior 7-strategy system.
      stabilized_hold → Buy & Hold, Stabilized Hold
      value_add       → Value-Add Renovation, Ground-Up Build, KD&R, Adaptive Reuse
      opportunistic   → Flip for Sale, Land Subdivision, Land Development
    """
    STABILIZED_HOLD = "stabilized_hold"
    VALUE_ADD       = "value_add"
    OPPORTUNISTIC   = "opportunistic"


class RenovationTier(str, Enum):
    """Renovation scope classifier used to compute quality-adjusted market rent."""
    LIGHT_COSMETIC   = "light_cosmetic"
    HEAVY_REHAB      = "heavy_rehab"
    NEW_CONSTRUCTION = "new_construction"


# Market-rent multipliers applied to HUD FMR based on renovation scope.
RENOVATION_TIER_MULTIPLIERS: Dict[str, float] = {
    RenovationTier.LIGHT_COSMETIC.value:   0.90,
    RenovationTier.HEAVY_REHAB.value:      1.00,
    RenovationTier.NEW_CONSTRUCTION.value: 1.15,
}

# Months a unit is offline during renovation (new construction bypasses —
# all units come online together at completion).
RENOVATION_DOWNTIME_MONTHS: Dict[str, int] = {
    RenovationTier.LIGHT_COSMETIC.value:   2,
    RenovationTier.HEAVY_REHAB.value:      4,
    RenovationTier.NEW_CONSTRUCTION.value: 0,
}


class WaterfallType(int, Enum):
    FULL   = 1   # Tiered promote (default)
    SIMPLE = 0   # Single LP/GP split


class RecommendationVerdict(str, Enum):
    GO             = "GO"
    CONDITIONAL_GO = "CONDITIONAL GO"
    NO_GO          = "NO-GO"


class DdFlagColor(str, Enum):
    RED   = "RED"
    AMBER = "AMBER"
    GREEN = "GREEN"


# ═══════════════════════════════════════════════════════════════════════════
# SUB-MODELS
# ═══════════════════════════════════════════════════════════════════════════

class PropertyAddress(BaseModel):
    street:       str             = ""
    city:         str             = ""
    state:        str             = ""
    zip_code:     str             = ""
    full_address: str             = ""
    latitude:     Optional[float] = None
    longitude:    Optional[float] = None
    census_tract: Optional[str]   = None
    fips_code:    Optional[str]   = None
    # Populated by Google Address Validation API (USPS CASS)
    validated_address:     Optional[str]   = None
    validation_confidence: Optional[str]   = None   # HIGH / MEDIUM / LOW
    dpv_confirmation:      Optional[str]   = None   # USPS: Y / S / D / N
    # Populated by Google Maps Elevation API
    elevation_meters:      Optional[float] = None
    elevation_feet:        Optional[float] = None


class DeedRecord(BaseModel):
    recording_date:       Optional[str]   = None
    document_type:        Optional[str]   = None
    grantor:              Optional[str]   = None
    grantee:              Optional[str]   = None
    consideration_amount: Optional[float] = None
    document_id:          Optional[str]   = None


class ParcelData(BaseModel):
    parcel_id:          Optional[str]   = None
    owner_name:         Optional[str]   = None
    owner_entity:       Optional[str]   = None
    assessed_value:     Optional[float] = None
    land_value:         Optional[float] = None
    improvement_value:  Optional[float] = None
    last_sale_date:     Optional[str]   = None
    last_sale_price:    Optional[float] = None
    lot_area_sf:        Optional[float] = None
    building_sf:        Optional[float] = None
    year_built:         Optional[int]   = None
    zoning_code:        Optional[str]   = None
    deed_book_page:     Optional[str]   = None
    deed_history:       List[DeedRecord] = Field(default_factory=list)
    # Extended owner / taxpayer fields (populated when the portal exposes them)
    taxpayer_name:              Optional[str]   = None
    taxpayer_mailing_address:   Optional[str]   = None
    owner_occupied:             Optional[bool]  = None   # True when mailing = site
    ownership_entity_type:      Optional[str]   = None   # LLC/Inc/Trust/Individual/etc.
    years_owned:                Optional[float] = None   # derived from last_sale_date
    exemptions:                 List[str]       = Field(default_factory=list)
    annual_tax_billed:          Optional[float] = None   # actual bill, when exposed
    homestead_status:           Optional[str]   = None
    property_use_class:         Optional[str]   = None   # portal classification code
    number_of_stories:          Optional[int]   = None
    number_of_units_recorded:   Optional[int]   = None
    # Portfolio: other parcels recorded to the same owner (if searchable)
    other_parcels_owned:        List[Dict[str, Any]] = Field(default_factory=list)


class LeaseAbstract(BaseModel):
    """Per-lease abstraction from Prompt 1E (lease document)."""
    unit_id:              Optional[str]   = None
    tenant_name:          Optional[str]   = None
    lease_type:           Optional[str]   = None   # Gross / MG / NNN / etc.
    commencement_date:    Optional[str]   = None
    expiration_date:      Optional[str]   = None
    term_months:          Optional[int]   = None
    base_rent_monthly:    Optional[float] = None
    base_rent_psf:        Optional[float] = None
    escalation_type:      Optional[str]   = None   # CPI / fixed / stepped / none
    escalation_amount:    Optional[str]   = None   # free-text (e.g. "3% annually", "CPI capped at 4%")
    cam_structure:        Optional[str]   = None   # base year / expense stop / pro-rata / NNN
    cam_base_year:        Optional[int]   = None
    ti_allowance_psf:     Optional[float] = None
    free_rent_months:     Optional[int]   = None
    renewal_options:      List[str]       = Field(default_factory=list)
    personal_guaranty:    Optional[bool]  = None
    percentage_rent:      Optional[str]   = None
    go_dark_allowed:      Optional[bool]  = None
    kickout_clause:       Optional[str]   = None
    radius_restriction:   Optional[str]   = None
    special_clauses:      List[str]       = Field(default_factory=list)


class TitleException(BaseModel):
    """Single Schedule B title exception / encumbrance."""
    exception_type:   Optional[str] = None   # easement / covenant / lien / restriction / etc.
    recording_date:   Optional[str] = None
    document_id:      Optional[str] = None
    grantor:          Optional[str] = None
    grantee:          Optional[str] = None
    summary:          Optional[str] = None


class PCASystemCondition(BaseModel):
    """Single building-system condition from a PCA / engineering report."""
    system:                  Optional[str]   = None   # roof / HVAC / plumbing / etc.
    age_years:               Optional[int]   = None
    condition:               Optional[str]   = None   # good / fair / poor
    remaining_useful_life:   Optional[int]   = None
    replacement_cost:        Optional[float] = None
    notes:                   Optional[str]   = None


class ImmediateRepairItem(BaseModel):
    item:       Optional[str]   = None
    cost:       Optional[float] = None
    priority:   Optional[str]   = None   # immediate / short-term / long-term


class ZoningData(BaseModel):
    zoning_code:          Optional[str]   = None
    zoning_district:      Optional[str]   = None
    overlay_districts:    List[str]       = Field(default_factory=list)
    permitted_uses:       List[str]       = Field(default_factory=list)
    conditional_uses:     List[str]       = Field(default_factory=list)
    max_height_ft:        Optional[float] = None
    max_stories:          Optional[int]   = None
    min_lot_area_sf:      Optional[float] = None
    max_lot_coverage_pct: Optional[float] = None
    max_far:              Optional[float] = None
    front_setback_ft:     Optional[float] = None
    rear_setback_ft:      Optional[float] = None
    side_setback_ft:      Optional[float] = None
    min_parking_spaces:   Optional[int]   = None
    max_buildable_units:  Optional[int]   = None
    max_buildable_sf:     Optional[float] = None
    buildable_capacity_narrative: Optional[str] = None
    # Structured math-problem layout: each entry carries label, formula,
    # inputs (list of "Name = value" strings), result, and a one-line note.
    # Populated by Prompt 3B via _apply_3b; rendered in the report as a
    # numbered calc layout rather than a prose paragraph.
    buildable_capacity_steps: List[Dict[str, Any]] = Field(default_factory=list)
    binding_constraint:   Optional[str]   = None
    binding_result:       Optional[str]   = None
    hbu_narrative:        Optional[str]   = None
    hbu_conclusion:       Optional[str]   = None
    municipal_code_url:   Optional[str]   = None
    zoning_code_chapter:  Optional[str]   = None
    source_verified:      bool            = False
    source_notes:         Optional[str]   = None


class MarketData(BaseModel):
    population_1mi:       Optional[int]   = None
    population_3mi:       Optional[int]   = None
    median_hh_income_1mi: Optional[float] = None
    median_hh_income_3mi: Optional[float] = None
    pct_renter_occ_1mi:   Optional[float] = None
    pct_renter_occ_3mi:   Optional[float] = None
    unemployment_rate:    Optional[float] = None
    fmr_studio:           Optional[float] = None
    fmr_1br:              Optional[float] = None
    fmr_2br:              Optional[float] = None
    fmr_3br:              Optional[float] = None
    dgs10_rate:           Optional[float] = None
    sofr_rate:            Optional[float] = None
    mortgage30_rate:      Optional[float] = None
    cpi_yoy:              Optional[float] = None
    data_pull_date:       Optional[str]   = None
    fema_flood_zone:      Optional[str]   = None
    fema_panel_number:    Optional[str]   = None
    epa_env_flags:        List[str]       = Field(default_factory=list)
    first_street_flood:   Optional[float] = None
    first_street_fire:    Optional[float] = None
    first_street_heat:    Optional[float] = None
    first_street_wind:    Optional[float] = None
    moodys_submarket_cap_rate:      Optional[float] = None  # e.g. 0.0625 = 6.25%
    moodys_submarket_vacancy_rate:  Optional[float] = None  # e.g. 0.085 = 8.5%
    moodys_submarket_rent_growth:   Optional[float] = None  # e.g. 0.032 = 3.2% annualized
    moodys_market_name:             Optional[str]   = None  # e.g. 'Philadelphia Metro'
    moodys_submarket_name:          Optional[str]   = None  # e.g. 'Center City'
    moodys_data_as_of:              Optional[str]   = None  # e.g. '2026-Q1'
    supply_pipeline_narrative: Optional[str] = None
    debt_market_narrative:     Optional[str] = None   # Prompt 5B output
    transit_options:    List[dict] = Field(default_factory=list)
    nearby_amenities:   List[dict] = Field(default_factory=list)
    # Zillow ZORI (ZIP-level median asking rent) and Census ACS 2022 median
    # contract rent by bedroom count (B25031). Used by the market-rent engine
    # as cross-checks against HUD FMR.
    zori_median_rent:        Optional[float] = None
    zori_rent_trend:         str             = ""
    census_median_rent_1br:  Optional[float] = None
    census_median_rent_2br:  Optional[float] = None
    census_median_rent_3br:  Optional[float] = None


class RentRollUnit(BaseModel):
    """One tenant/unit in the rent roll — used by the lease event engine."""
    unit_id:              str   = ""
    unit_type:            str   = ""
    sf:                   float = 0.0
    monthly_rent:         float = 0.0       # residential $/mo or commercial $/mo
    annual_rent:          float = 0.0       # commercial: rent_sf × sf
    current_rent_sf:      float = 0.0       # commercial: $/SF/yr
    lease_term_years:     float = 5.0
    lease_expiry_year:    int   = 0         # hold year when lease expires (1–10), 0=unknown
    market_rent_sf:       float = 0.0       # market rent at renewal
    renewal_probability:  float = 0.70
    is_vacant:            bool  = False
    downtime_months:      int   = 3
    status:               str   = "Occupied"


class RefiEvent(BaseModel):
    """
    One refinancing event. Use List[RefiEvent] (max 3) instead of 33 flat fields.
    This enables clean iteration in financials.py and amortization schedule generation.
    """
    active:          bool  = False
    year:            int   = 0
    appraised_value: float = 0.0
    cap_rate:        float = 0.07
    ltv:             float = 0.70
    rate:            float = 0.065
    amort_years:     int   = 30
    loan_term:       int   = 10
    orig_fee_pct:    float = 0.01
    prepay_pct:      float = 0.01
    closing_costs:   float = 25000.0

    @property
    def new_loan_amount(self) -> float:
        return self.appraised_value * self.ltv


class WaterfallTier(BaseModel):
    """One promote tier. Tier 1 = first tier above preferred return."""
    tier_number:  int   # 1–4 active tiers; residual captured in ResidualTier
    hurdle_type:  str   = "irr"   # "irr" or "em"
    hurdle_value: float = 0.0
    lp_share:     float = 0.70
    gp_share:     float = 0.30

    @model_validator(mode='after')
    def shares_valid(self) -> 'WaterfallTier':
        if abs(round(self.lp_share + self.gp_share, 6) - 1.0) > 0.001:
            raise ValueError(f"Tier {self.tier_number}: LP + GP shares must equal 1.0")
        return self


class ResidualTier(BaseModel):
    """Above-Tier-4 residual — no upper bound. Captures all returns above Tier 4 hurdle."""
    lp_share: float = 0.10
    gp_share: float = 0.90

    @model_validator(mode='after')
    def shares_valid(self) -> 'ResidualTier':
        if abs(round(self.lp_share + self.gp_share, 6) - 1.0) > 0.001:
            raise ValueError("Residual tier: LP + GP shares must equal 1.0")
        return self


class FinancialAssumptions(BaseModel):
    """All user-configurable financial parameters from the frontend."""

    # §1 Property info
    hold_period:         int            = 10
    num_units:           Optional[int]  = None
    gba_sf:              Optional[float]= None
    lot_sf:              Optional[float]= None
    year_built:          Optional[int]  = None

    # §2 Acquisition
    purchase_price:           float = 0.0
    transfer_tax_rate:        float = 0.02139  # Buyer's share only. Convention: buyer pays ~50% of 4.278% PA rate.
    closing_costs_fixed:      float = 75000.0
    tenant_buyout:            float = 0.0

    # §3 Professional & Due Diligence
    legal_closing:       float = 25000.0
    title_insurance:     float = 8000.0
    legal_bank:          float = 5000.0
    appraisal:           float = 5000.0
    environmental:       float = 6000.0
    surveyor:            float = 3500.0
    architect:           float = 0.0
    structural:          float = 0.0
    civil_eng:           float = 0.0
    meps:                float = 0.0
    legal_zoning:        float = 0.0
    geotech:             float = 0.0

    # §4 Financing costs / Soft costs / Hard costs
    acq_fee_fixed:       float = 25000.0
    mortgage_carry:      float = 0.0
    mortgage_fees:       float = 17500.0
    mezz_interest:       float = 0.0
    working_capital:     float = 15000.0
    marketing:           float = 5000.0
    re_tax_carry:        float = 0.0
    prop_ins_carry:      float = 0.0
    dev_fee:             float = 0.0
    dev_pref:            float = 0.0
    permits:             float = 0.0
    stormwater:          float = 0.0
    demo:                float = 0.0
    const_hard:          float = 0.0
    const_reserve:       float = 0.0
    # Per-SF inputs that the frontend collects; multiplied by gba_sf at
    # request time to populate const_hard / const_reserve. Persisted so
    # the Excel Assumptions tab can surface both the PSF rate and the
    # implied dollar total (as an Excel formula that recomputes if the
    # user later edits GBA in-cell).
    const_hard_psf:      float = 0.0
    const_reserve_psf:   float = 0.0
    gc_overhead:         float = 0.0

    # §3 Sources — additional capital
    mezz_debt:           float = 0.0
    tax_credit_equity:   float = 0.0
    grants:              float = 0.0

    # §11A Development Period & Carry Costs (value_add strategy)
    const_period_months:     int   = 0
    draw_start_lag: int = 1   # months after closing before first construction draw begins
    const_loan_rate:         float = 0.08
    leaseup_period_months:   int   = 0
    leaseup_vacancy_rate:    float = 0.25
    leaseup_concessions:     float = 0.0
    leaseup_marketing:       float = 0.0

    # §11A-R Renovation scope (drives the quality-adjusted market-rent engine).
    # Distinct from leaseup_period_months (which is the project-level lease-up
    # window in months). renovation_tier classifies the scope of the rehab;
    # lease_up_months is the per-unit re-lease delay after a unit comes back
    # online post-renovation; quality_adjusted_market_rent is the HUD-FMR
    # × tier-multiplier value computed by market._compute_market_rents.
    renovation_tier:               str            = RenovationTier.LIGHT_COSMETIC.value
    lease_up_months:               int            = 1
    quality_adjusted_market_rent:  Optional[float] = None

    # For Sale / Disposition inputs (for_sale strategy)
    sale_price_arv:               float = 0.0
    sale_const_period_months:     int   = 0
    sale_marketing_period_months: int   = 0
    sale_broker_commission_pct:   float = 0.05
    carry_loan_interest_monthly:  float = 0.0
    carry_re_taxes_monthly:       float = 0.0
    carry_insurance_monthly:      float = 0.0
    carry_utilities_monthly:      float = 0.0
    carry_maintenance_monthly:    float = 0.0
    carry_hoa_monthly:            float = 0.0
    carry_marketing_total:        float = 0.0
    carry_staging_total:          float = 0.0

    # §5 Initial financing
    ltv_pct:             float = 0.70
    interest_rate:       float = 0.065
    amort_years:         int   = 30
    loan_term:           int   = 10
    origination_fee_pct: float = 0.01
    io_period_months:    int   = 0

    # §6–8 Refinancing events — List[RefiEvent], max 3. Each defaults to
    # inactive with appraised_value=0 so it's clear the slot hasn't been
    # configured. The Excel / financials code skips inactive refis.
    # Appraised values must be set explicitly per deal; the prior
    # hardcoded $3.2M / $3.8M defaults were misleading on deals that
    # accidentally flipped active=True without updating the value.
    refi_events: List[RefiEvent] = Field(default_factory=lambda: [
        RefiEvent(active=False, year=5, appraised_value=0, cap_rate=0.07, ltv=0.70, rate=0.060, amort_years=30, loan_term=10, orig_fee_pct=0.01, prepay_pct=0.01, closing_costs=0),
        RefiEvent(active=False, year=8, appraised_value=0, cap_rate=0.07, ltv=0.65, rate=0.055, amort_years=30, loan_term=10, orig_fee_pct=0.01, prepay_pct=0.01, closing_costs=0),
        RefiEvent(active=False, year=0, appraised_value=0, cap_rate=0.07, ltv=0.65, rate=0.055, amort_years=30, loan_term=10, orig_fee_pct=0.01, prepay_pct=0.00, closing_costs=0),
    ])

    # §8 Income
    vacancy_rate:        float = 0.075
    annual_rent_growth:  float = 0.03
    expense_growth_rate: float = 0.03   # Referenced by Prompt 5B
    loss_to_lease:       float = 0.03
    cam_reimbursements:  float = 0.0
    fee_income:          float = 0.0     # was 6000.0 — default to $0

    # §9 Fixed expenses (Year 1)
    # Taxes + insurance retain historical class defaults — their
    # values come from the public-data pipeline (parcel assessed value
    # × local tax rate; TIV × insurance rate) and should not be
    # overridden by a cold-start default.
    # Other fields default to 0 so deal_data._apply_expense_defaults
    # (Tier 3) can substitute the rule-based value when neither
    # extraction nor user input populated them.
    re_taxes:            float = 45000.0
    insurance:           float = 18000.0
    gas:                 float = 0.0
    water_sewer:         float = 0.0
    electric:            float = 0.0
    license_inspections: float = 0.0
    trash:               float = 0.0

    # §10 Variable expenses (Year 1)
    mgmt_fee_pct:        float = 0.05
    salaries:            float = 0.0
    repairs:             float = 0.0
    exterminator:        float = 0.0
    cleaning:            float = 0.0
    turnover:            float = 0.0
    # Fraction of units that turn over each year (0.30 = 30%). Used
    # with per-unit turnover cost to derive Year-1 turnover expense.
    turnover_rate_pct:   float = 0.30
    advertising:         float = 0.0
    landscape_snow:      float = 0.0
    admin_legal_acct:    float = 0.0
    office_phone:        float = 0.0
    miscellaneous:       float = 0.0

    # §11 Below-the-line
    cap_reserve_per_unit: float = 400.0
    commissions_yr1:     float = 0.0
    renovations_yr1:     float = 0.0

    # §11B Leasing cost assumptions
    ti_new_psf:              float = 0.0    # TI allowance — new lease ($/SF)
    ti_renewal_psf:          float = 0.0    # TI allowance — renewal ($/SF)
    commission_new_pct:      float = 0.05   # Leasing commission — new lease (% of GLV)
    commission_renewal_pct:  float = 0.025  # Leasing commission — renewal (% of GLV)

    # §12 Exit
    exit_cap_rate:         float = 0.07
    disposition_costs_pct: float = 0.02

    # §13 Waterfall / Partnership
    gp_equity_pct:   float = 0.10
    lp_equity_pct:   float = 0.90   # Always = 1 - gp_equity_pct
    waterfall_type:  WaterfallType = WaterfallType.FULL
    pref_return:     float = 0.08
    simple_lp_split: float = 0.80

    waterfall_tiers: List[WaterfallTier] = Field(default_factory=lambda: [
        WaterfallTier(tier_number=1, hurdle_type='irr', hurdle_value=0.12, lp_share=0.70, gp_share=0.30),
        WaterfallTier(tier_number=2, hurdle_type='irr', hurdle_value=0.15, lp_share=0.60, gp_share=0.40),
        WaterfallTier(tier_number=3, hurdle_type='irr', hurdle_value=0.18, lp_share=0.30, gp_share=0.70),
        WaterfallTier(tier_number=4, hurdle_type='irr', hurdle_value=0.24, lp_share=0.20, gp_share=0.80),
    ])
    residual_tier: ResidualTier = Field(default_factory=lambda: ResidualTier(lp_share=0.10, gp_share=0.90))

    # §14 EM hurdles
    em_hurdle_t1: float = 2.0
    em_hurdle_t2: float = 2.5
    em_hurdle_t3: float = 3.0

    # §15 Sensitivity ranges
    sens_rent_growth_low:  float = 0.00
    sens_rent_growth_high: float = 0.05
    sens_rent_growth_step: float = 0.01
    sens_exit_cap_low:     float = 0.055
    sens_exit_cap_high:    float = 0.085
    sens_exit_cap_step:    float = 0.005

    # Return thresholds
    min_equity_multiple: float = 1.80
    min_lp_irr:          float = 0.12
    target_lp_irr:       float = 0.15   # Referenced by Prompts 5A, 5E
    min_coc:             float = 0.07
    min_dscr:            float = 1.25
    min_cap_rate:        float = 0.055
    # Go / No-Go hurdle — single-metric binding the MC price solver +
    # Opinion of Value + recommendation. Metric is one of:
    #   "project_irr" | "lp_irr" | "stab_cap_rate" | "stab_coc"
    # hurdle_value is stored as a DECIMAL (0.15 = 15%), not a percent.
    hurdle_metric:       str   = "lp_irr"
    hurdle_value:        float = 0.15

    @model_validator(mode='after')
    def lp_equals_complement(self) -> 'FinancialAssumptions':
        self.lp_equity_pct = round(1.0 - self.gp_equity_pct, 6)
        return self

    @model_validator(mode='after')
    def max_three_refis(self) -> 'FinancialAssumptions':
        if len(self.refi_events) > 3:
            raise ValueError("Maximum 3 refinancing events allowed.")
        return self

    @model_validator(mode='after')
    def validate_ranges(self) -> 'FinancialAssumptions':
        """Clamp nonsensical values with a warning rather than crashing.
        Preserves the pipeline's ability to process rough user input
        while flagging obviously-wrong numbers."""
        import logging as _lg
        _log = _lg.getLogger("models.assumptions")
        warnings_raised = []
        if self.hold_period < 1 or self.hold_period > 30:
            warnings_raised.append(f"hold_period={self.hold_period}yr (expected 1–30)")
            self.hold_period = max(1, min(self.hold_period or 10, 30))
        if self.ltv_pct < 0 or self.ltv_pct > 1.10:
            warnings_raised.append(f"ltv_pct={self.ltv_pct:.2%} (expected 0–110%)")
            self.ltv_pct = max(0.0, min(self.ltv_pct, 1.10))
        if self.interest_rate < 0 or self.interest_rate > 0.30:
            warnings_raised.append(f"interest_rate={self.interest_rate:.2%} (expected 0–30%)")
            self.interest_rate = max(0.0, min(self.interest_rate, 0.30))
        if self.vacancy_rate < 0 or self.vacancy_rate > 0.50:
            warnings_raised.append(f"vacancy_rate={self.vacancy_rate:.2%} (expected 0–50%)")
            self.vacancy_rate = max(0.0, min(self.vacancy_rate, 0.50))
        if self.exit_cap_rate < 0.02 or self.exit_cap_rate > 0.20:
            warnings_raised.append(f"exit_cap_rate={self.exit_cap_rate:.2%} (expected 2–20%)")
            self.exit_cap_rate = max(0.02, min(self.exit_cap_rate, 0.20))
        if self.purchase_price < 0:
            warnings_raised.append(f"purchase_price={self.purchase_price} (negative)")
            self.purchase_price = 0.0
        if warnings_raised:
            _log.warning("FinancialAssumptions: range validation adjusted %d field(s): %s",
                         len(warnings_raised), "; ".join(warnings_raised))
        return self


class ScenarioResult(BaseModel):
    """One named scenario (Base / Upside / Downside) — results from
    financials._run_scenario_core. Populated by run_financials after
    the base run has completed. The five deltas are echoed back so
    the narrative specialist can cite the assumption pressure that
    produced each metric.
    """
    scenario_name:              str                   # "base" | "upside" | "downside"
    note:                       Optional[str] = None  # surfaces reasons a metric is None, or skip-reason on non-applicable strategies

    # Applied deltas (bps for rates; decimal for pct; int for months)
    rent_growth_delta_bps:      float
    vacancy_delta_bps:          float
    exit_cap_delta_bps:         float
    hard_cost_delta_pct:        float
    delivery_delta_months:      int

    # Resolved inputs after deltas (post-clamp) — audit trail
    annual_rent_growth:         float
    vacancy_rate:               float
    exit_cap_rate:              float
    const_hard:                 float
    const_period_months:        int

    # Seven scenario output metrics
    lp_irr:                     Optional[float] = None
    project_irr:                Optional[float] = None
    lp_equity_multiple:         Optional[float] = None
    peak_funded_equity:         Optional[float] = None
    dscr_yr3:                   Optional[float] = None
    dscr_yr10:                  Optional[float] = None
    stabilized_debt_yield:      Optional[float] = None


class FinancialOutputs(BaseModel):
    """Computed financial results — populated by financials.py."""
    total_uses:              Optional[float] = None
    total_sources:           Optional[float] = None
    total_equity_required:   Optional[float] = None
    initial_loan_amount:     Optional[float] = None
    gp_equity:               Optional[float] = None
    lp_equity:               Optional[float] = None
    construction_interest_carry: float = 0.0
    construction_interest_schedule: List[dict] = []
    total_project_cost:      float = 0.0
    gross_potential_rent:    Optional[float] = None
    effective_gross_income:  Optional[float] = None
    total_operating_expenses: Optional[float]= None
    noi_yr1:                 Optional[float] = None
    debt_service_annual:     Optional[float] = None
    free_cash_flow_yr1:      Optional[float] = None
    dscr_yr1:                Optional[float] = None
    going_in_cap_rate:       Optional[float] = None
    stabilized_cap_rate:     Optional[float] = None
    lp_irr:                  Optional[float] = None
    gp_irr:                  Optional[float] = None
    project_irr:             Optional[float] = None
    lp_equity_multiple:      Optional[float] = None
    gp_equity_multiple:      Optional[float] = None
    project_equity_multiple: Optional[float] = None
    cash_on_cash_yr1:        Optional[float] = None
    gross_sale_price:        Optional[float] = None
    net_sale_proceeds:       Optional[float] = None
    net_equity_at_exit:      Optional[float] = None
    sensitivity_matrix:      Optional[List[List[Union[float, str]]]] = None
    sensitivity_em_matrix:   Optional[List[List[float]]] = None
    sensitivity_noi_matrix:  Optional[List[List[float]]] = None
    sensitivity_coc_matrix:  Optional[List[List[float]]] = None
    sensitivity_axis_rent_growth: Optional[List[float]] = None
    sensitivity_axis_exit_cap:    Optional[List[float]] = None
    monte_carlo_results:     Optional[Dict[str, Any]] = None
    monte_carlo_narrative:   Optional[str] = None   # Prompt 5A output
    price_solver_results:    Optional[Dict[str, Any]] = None   # MC-backed purchase-price solver
    pro_forma_years:         Optional[List[Dict[str, float]]] = None
    loan_balance_at_refi:    Optional[List[Optional[float]]] = None  # amortized balance at each refi event
    lease_events:            Optional[Dict[int, Dict[str, Any]]] = None  # {year: {commission, ti, downtime_loss, ...}}
    sensitivity_stabilized_year: Optional[int]   = None
    sensitivity_stabilized_noi:  Optional[float] = None
    sensitivity_note:            Optional[str]   = None
    scenario_results:            Optional[Dict[str, ScenarioResult]] = None



class RentComp(BaseModel):
    """One residential rent comparable — Section 11.1."""
    address:       Optional[str]   = None
    distance_miles: Optional[float] = None
    unit_type:     Optional[str]   = None   # "Studio" | "1BR" | "2BR" | "3BR" | "4BR+"
    beds:          Optional[int]   = None
    baths:         Optional[float] = None
    sq_ft:         Optional[int]   = None
    monthly_rent:  Optional[float] = None
    rent_per_sf:   Optional[float] = None
    lease_date:    Optional[str]   = None   # ISO YYYY-MM-DD
    source:        Optional[str]   = None   # free text: "CoStar", "Manual entry", etc.


class CommercialComp(BaseModel):
    """One commercial rent comparable — Section 11.2."""
    address:           Optional[str]   = None
    distance_miles:    Optional[float] = None
    use_type:          Optional[str]   = None   # "Office" | "Retail" | "Medical" | "Industrial"
    sq_ft:             Optional[int]   = None
    asking_rent_per_sf: Optional[float] = None
    lease_type:        Optional[str]   = None   # "NNN" | "Gross" | "MG" | "FSG"
    lease_date:        Optional[str]   = None
    tenant_name:       Optional[str]   = None
    source:            Optional[str]   = None


class SaleComp(BaseModel):
    """One sale comparable — Section 11.3."""
    address:        Optional[str]   = None
    distance_miles: Optional[float] = None
    asset_type:     Optional[str]   = None
    sq_ft:          Optional[int]   = None
    num_units:      Optional[int]   = None
    sale_price:     Optional[float] = None
    price_per_sf:   Optional[float] = None
    price_per_unit: Optional[float] = None
    cap_rate:       Optional[float] = None
    sale_date:      Optional[str]   = None
    source:         Optional[str]   = None


class CompsData(BaseModel):
    """Container for all comparable data — max 8 rent, 5 commercial, 5 sale."""
    rent_comps:       List[RentComp]       = Field(default_factory=list)
    commercial_comps: List[CommercialComp] = Field(default_factory=list)
    sale_comps:       List[SaleComp]       = Field(default_factory=list)

    @model_validator(mode='after')
    def enforce_caps(self) -> 'CompsData':
        import logging as _lg
        _log = _lg.getLogger("models.comps")
        if len(self.rent_comps) > 8:
            _log.warning("CompsData: truncating rent_comps from %d to 8",
                         len(self.rent_comps))
            self.rent_comps = self.rent_comps[:8]
        if len(self.commercial_comps) > 5:
            _log.warning("CompsData: truncating commercial_comps from %d to 5",
                         len(self.commercial_comps))
            self.commercial_comps = self.commercial_comps[:5]
        if len(self.sale_comps) > 5:
            _log.warning("CompsData: truncating sale_comps from %d to 5",
                         len(self.sale_comps))
            self.sale_comps = self.sale_comps[:5]
        return self


class ExtractedDocumentData(BaseModel):
    """Structured data from uploaded documents — populated by extractor.py."""
    # From Prompt 1A — OM
    property_name:          Optional[str]   = None
    asking_price:           Optional[float] = None
    deal_source:            Optional[str]   = None
    broker_name:            Optional[str]   = None
    broker_firm:            Optional[str]   = None
    broker_phone:           Optional[str]   = None
    broker_email:           Optional[str]   = None
    num_units_extracted:    Optional[int]   = None
    gba_sf_extracted:       Optional[float] = None
    lot_sf_extracted:       Optional[float] = None
    year_built_extracted:   Optional[int]   = None
    description_extracted:  Optional[str]   = None
    image_placements:       Optional[Dict[str, Any]] = None
    # Actual photo files extracted from uploaded PDF pages (absolute paths).
    # Populated by extractor._extract_pdf_photos() using PyMuPDF.
    pdf_photo_paths:        List[str]       = Field(default_factory=list)
    # From Prompt 1B — Rent Roll
    unit_mix:               Optional[List[Dict[str, Any]]] = None
    total_units_from_rr:    Optional[int]   = None
    total_monthly_rent:     Optional[float] = None
    avg_rent_per_unit:      Optional[float] = None
    occupancy_rate:         Optional[float] = None
    # From Prompt 1C — T-12 / Financial Statements
    gross_potential_rent_t12:   Optional[float] = None
    effective_gross_income_t12: Optional[float] = None
    total_expenses_t12:         Optional[float] = None
    noi_t12:                    Optional[float] = None
    expense_line_items:         Optional[Dict[str, float]] = None
    cam_reimbursements_t12:     Optional[float] = None
    nnn_reconciliation:         Optional[Dict[str, Any]]   = None
    # Comparable data extracted from uploaded OM/broker package
    comps:                      Optional[CompsData]        = None
    # From Prompt 1D — Environmental Report (Phase I / II ESA)
    phase1_status:              Optional[str]       = None   # complete | pending | n/a
    phase1_date:                Optional[str]       = None
    phase1_consultant:          Optional[str]       = None
    recognized_environmental_conditions: List[str]  = Field(default_factory=list)
    historical_recognized_conditions:    List[str]  = Field(default_factory=list)
    vapor_intrusion_flag:       Optional[bool]      = None
    phase2_recommended:         Optional[bool]      = None
    environmental_findings:     Optional[str]       = None   # narrative summary
    environmental_recommendations: Optional[str]    = None
    # Document-type classifications observed per uploaded file
    document_classifications:   List[Dict[str, Any]] = Field(default_factory=list)
    # Floor plan + site plan page references
    floor_plan_pages:           List[int]           = Field(default_factory=list)
    site_plan_pages:            List[int]           = Field(default_factory=list)
    # From Prompt 1E — Lease abstraction
    lease_abstracts:            List[LeaseAbstract] = Field(default_factory=list)
    # From Prompt 1F — Title commitment
    title_commitment_date:      Optional[str]       = None
    title_company:              Optional[str]       = None
    title_insurance_amount:     Optional[float]     = None
    title_vesting:              Optional[str]       = None
    title_legal_description:    Optional[str]       = None
    title_exceptions:           List[TitleException] = Field(default_factory=list)
    title_easements:            List[str]           = Field(default_factory=list)
    title_endorsements:         List[str]           = Field(default_factory=list)
    # From Prompt 1G — PCA / engineering report
    pca_report_date:            Optional[str]       = None
    pca_consultant:             Optional[str]       = None
    pca_overall_condition:      Optional[str]       = None
    pca_deferred_maintenance_total: Optional[float] = None
    pca_building_systems:       List[PCASystemCondition] = Field(default_factory=list)
    pca_immediate_repairs:      List[ImmediateRepairItem] = Field(default_factory=list)
    pca_capex_12yr_total:       Optional[float]     = None
    pca_capex_by_year:          Optional[Dict[str, float]] = None
    pca_ada_items:              List[str]           = Field(default_factory=list)


class InsuranceAnalysis(BaseModel):
    """
    Insurance & risk analysis — populated by risk.py using Prompt 4B.
    Maps to report §16.3 placeholders.
    """
    insurance_narrative_p1:       Optional[str]             = None
    insurance_narrative_p2:       Optional[str]             = None
    insurance_narrative_p3:       Optional[str]             = None
    insurance_kpi_strip:          Optional[Dict[str, Any]]  = None
    insurance_summary_table:      Optional[List[Dict[str, Any]]] = None
    insurance_proforma_line_item: Optional[float]           = None


class DdFlag(BaseModel):
    """One DD flag — report §16 only. Never in Excel. Permanent architectural rule."""
    flag_id:     str
    color:       DdFlagColor
    category:    str
    title:       str
    narrative:   str
    remediation: Optional[str] = None


class ReportNarratives(BaseModel):
    """
    All AI-generated narrative blocks for the PDF report.
    Key names match {{ placeholder }} names in DealDesk_Report_Template_v3.docx exactly.
    Populated by Prompt 4-MASTER (all sections) and optionally Prompt 5D (investor mode).
    """
    # §01
    exec_overview_p1:    Optional[str] = None
    exec_overview_p2:    Optional[str] = None
    exec_overview_p3:    Optional[str] = None
    exec_pullquote:      Optional[str] = None
    deal_thesis:         Optional[str] = None
    opportunity_1:       Optional[str] = None
    opportunity_2:       Optional[str] = None
    opportunity_3:       Optional[str] = None
    # §02
    photo_gallery_intro: Optional[str] = None
    photo_hero_source:   Optional[str] = None
    # §03
    maps_intro:          Optional[str] = None
    fema_flood_narrative:Optional[str] = None
    # §04
    prop_desc_p1:        Optional[str] = None
    prop_desc_p2:        Optional[str] = None
    prop_desc_p3:        Optional[str] = None
    prop_desc_p4:        Optional[str] = None
    utilities_analysis:  Optional[str] = None
    # §05
    ownership_narrative: Optional[str] = None
    liens_narrative:     Optional[str] = None
    # §06
    zoning_overview:     Optional[str] = None
    buildable_capacity:  Optional[str] = None
    highest_best_use:    Optional[str] = None
    # §07
    location_overview_p1:    Optional[str] = None
    location_overview_p2:    Optional[str] = None
    location_pullquote:      Optional[str] = None
    transportation_analysis: Optional[str] = None
    # §08
    neighborhood_trend_narrative: Optional[str] = None
    # §09
    supply_pipeline_narrative: Optional[str] = None
    # §10
    rent_roll_intro:     Optional[str] = None
    # §11
    rent_comp_narrative:      Optional[str] = None
    commercial_comp_narrative:Optional[str] = None
    sale_comp_narrative:      Optional[str] = None
    # §12
    financial_pullquote:       Optional[str] = None
    sources_uses_narrative:    Optional[str] = None
    proforma_narrative:        Optional[str] = None
    proforma_pullquote:        Optional[str] = None
    construction_budget_narrative: Optional[str] = None
    sensitivity_narrative:     Optional[str] = None
    exit_narrative:            Optional[str] = None
    monte_carlo_narrative:     Optional[str] = None   # Prompt 5A
    # §13
    capital_stack_narrative:     Optional[str] = None
    capital_structure_pullquote: Optional[str] = None
    debt_comparison_narrative:   Optional[str] = None
    waterfall_narrative:         Optional[str] = None
    debt_market_narrative:       Optional[str] = None   # Prompt 5B
    # §14
    environmental_intro:   Optional[str] = None
    phase_esa_narrative:   Optional[str] = None
    climate_risk_narrative:Optional[str] = None
    # §15
    legal_status_narrative:          Optional[str] = None
    violations_narrative:            Optional[str] = None
    regulatory_approvals_narrative:  Optional[str] = None
    # §16
    due_diligence_overview: Optional[str] = None
    # §17
    dd_checklist_intro:  Optional[str] = None
    # §18
    timeline_narrative:  Optional[str] = None
    # §19
    recommendation_narrative_p1: Optional[str] = None
    recommendation_narrative_p2: Optional[str] = None
    recommendation_pullquote:    Optional[str] = None
    risk_1:              Optional[str] = None
    risk_2:              Optional[str] = None
    risk_3:              Optional[str] = None
    # §20
    conclusion_1:        Optional[str] = None
    conclusion_2:        Optional[str] = None
    conclusion_3:        Optional[str] = None
    conclusion_4:        Optional[str] = None
    conclusion_5:        Optional[str] = None
    bottom_line:         Optional[str] = None
    next_step_1:         Optional[str] = None
    next_step_2:         Optional[str] = None
    next_step_3:         Optional[str] = None
    next_step_4:         Optional[str] = None
    next_step_5:         Optional[str] = None
    next_step_6:         Optional[str] = None
    # §22
    methodology_notes:   Optional[str] = None


class SectionsConfig(BaseModel):
    """Per-section include/exclude flags. s21 and s22 always True (locked)."""
    s01: bool = True;  s02: bool = True;  s03: bool = True
    s04: bool = True;  s05: bool = True;  s06: bool = True
    s07: bool = True;  s08: bool = True;  s09: bool = True
    s10: bool = True;  s11: bool = True;  s12: bool = True
    s13: bool = True;  s14: bool = True;  s15: bool = True
    s16: bool = True;  s17: bool = True;  s18: bool = True
    s19: bool = True;  s20: bool = True
    s21: bool = True   # Locked — Disclaimer & Certification
    s22: bool = True   # Locked — Appendix


class NotificationConfig(BaseModel):
    """Deal completion alert config — used by notifier.py / Prompt 5G."""
    email_enabled:    bool = False
    email_recipients: str  = ""
    slack_enabled:    bool = False
    slack_webhook:    str  = ""


class ProvenanceLog(BaseModel):
    """Data provenance — populates §22 Appendix data sources table."""
    pipeline_version:   str  = "1.0"
    run_timestamp:      Optional[str] = None
    deal_id:            Optional[str] = None
    extractor_model:    str  = "claude-haiku-4-5-20251001"
    narrative_model:    str  = "claude-sonnet-4-5-20250514"
    census_data_year:   str  = "2022"
    fred_pull_date:     Optional[str] = None
    municipal_registry_version: str = "Phase A"
    documents_uploaded: List[str]   = Field(default_factory=list)
    api_costs_estimated: Optional[float] = None
    field_sources:      Dict[str, str] = Field(default_factory=dict)
    # Pipeline health: external-service failures logged by each fetcher.
    # Each entry: {service, stage, reason}. When the count gets past ~3,
    # the report renders a "data quality degraded" banner.
    failed_sources:     List[Dict[str, Any]] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN MODEL
# ═══════════════════════════════════════════════════════════════════════════

class DealData(BaseModel):
    """
    DealData — single unified data contract for the DealDesk pipeline.

    Pipeline module responsibilities:
        extractor.py    → populates extracted_docs, address fields
        deal_data.py    → assembles and validates; master merge step
        market.py       → populates zoning, market_data
        risk.py         → populates insurance (Prompt 4B)
        financials.py   → populates financial_outputs, monte_carlo
        excel_builder.py → reads assumptions + financial_outputs
        context_builder.py → builds Jinja render context; narratives via Sonnet
        report_builder.py → HTML→PDF via Jinja + Playwright headless Chromium

    Excel template routing:
        strategy=stabilized_hold → Hold_Template_v3.xlsx
        strategy=value_add       → Hold_Template_v3.xlsx
        strategy=opportunistic   → Sale_Template_v3.xlsx

    Investor mode flag:
        investor_mode=False → full internal report (22 sections)
        investor_mode=True  → LP-appropriate report:
                              §16 DD Flags & Risk Assessment — suppressed
                              §17 DD Checklist & Status Tracker — suppressed
                              §22 Appendix methodology notes — suppressed
                              cover = "Investment Summary — {address}"
                              9 narrative blocks rewritten by Prompt 5D
    """

    # Identity
    deal_id:            Optional[str] = None
    deal_code:          Optional[str] = None
    deal_type:          str           = "Acquisition"
    report_date:        Optional[str] = None

    # Classification
    asset_type:          AssetType           = AssetType.MULTIFAMILY
    investment_strategy: InvestmentStrategy  = InvestmentStrategy.STABILIZED_HOLD

    @field_validator("asset_type", mode="before")
    @classmethod
    def _coerce_asset_type(cls, v: Any) -> AssetType:
        if isinstance(v, AssetType):
            return v
        if isinstance(v, str):
            low = v.strip().lower().replace(" ", "-")
            for member in AssetType:
                if low == member.value.lower().replace(" ", "-"):
                    return member
        raise ValueError(
            f"Unknown asset_type {v!r}. "
            f"Valid: {[m.value for m in AssetType]}"
        )

    @field_validator("investment_strategy", mode="before")
    @classmethod
    def _coerce_investment_strategy(cls, v: Any) -> InvestmentStrategy:
        if isinstance(v, InvestmentStrategy):
            return v
        if isinstance(v, str):
            low = v.strip().lower().replace(" ", "_").replace("-", "_")
            for member in InvestmentStrategy:
                if low == member.value.lower():
                    return member
        raise ValueError(
            f"Unknown investment_strategy {v!r}. "
            f"Valid: {[m.value for m in InvestmentStrategy]}"
        )

    # Address
    address: PropertyAddress = Field(default_factory=PropertyAddress)

    # Sponsor — referenced by Prompts 5E, 5F
    sponsor_name: str = "DealDesk"
    sponsor_description: str = (
        "DealDesk is a CRE underwriting platform that automates institutional-"
        "grade investment analysis across multifamily, mixed-use, and commercial "
        "acquisitions."
    )

    # Deal description (required on New Underwrite screen)
    deal_description: str = ""

    # Parcel data
    parcel_data: Optional[ParcelData] = None

    # Zoning
    zoning: ZoningData = Field(default_factory=ZoningData)

    # =========================================================================
    # ZONING OVERHAUL — Session 1 additions (April 2026)
    # =========================================================================
    conformity_assessment: Optional[ConformityAssessment] = None
    # ^^ Populated by 3C-CONF in Session 3. None on deals processed before
    #    the overhaul shipped.

    scenarios: List[DevelopmentScenario] = Field(default_factory=list)
    # ^^ Populated by 3C-SCEN in Session 3. Empty list on legacy deals.
    #    Master plan D2 caps at 3 entries.

    zoning_extensions: Optional[ZoningExtensions] = None
    # ^^ Populated by 3C-HBU in Session 3. None on legacy deals.

    # Market data
    market_data: MarketData = Field(default_factory=MarketData)

    # Financial assumptions (all user-configurable parameters)
    assumptions: FinancialAssumptions = Field(default_factory=FinancialAssumptions)

    # Extracted document data
    extracted_docs: ExtractedDocumentData = Field(default_factory=ExtractedDocumentData)

    # Comparable data — merged from extracted docs + manual frontend entry
    comps: CompsData = Field(default_factory=CompsData)

    # Computed financial outputs
    financial_outputs: FinancialOutputs = Field(default_factory=FinancialOutputs)

    # Insurance analysis (risk.py / Prompt 4B)
    insurance: InsuranceAnalysis = Field(default_factory=InsuranceAnalysis)

    # DD Flags — report §16 only. Never in Excel. Permanent architectural rule.
    dd_flags: List[DdFlag] = Field(default_factory=list)

    # Investment recommendation
    recommendation:          Optional[RecommendationVerdict] = None
    recommendation_one_line: Optional[str] = None

    # AI-generated report narratives
    narratives: ReportNarratives = Field(default_factory=ReportNarratives)

    # Section config
    sections_config: SectionsConfig = Field(default_factory=SectionsConfig)

    # Investor mode
    investor_mode: bool = False

    # Notification config
    notification_config: NotificationConfig = Field(default_factory=NotificationConfig)

    # Provenance log
    provenance: ProvenanceLog = Field(default_factory=ProvenanceLog)

    # Google Maps enrichment (populated by market.py)
    nearby_pois:        Optional[List[Dict[str, Any]]] = None
    poi_summary:        Optional[Dict[str, int]]       = None
    amenity_narrative:  Optional[str]                  = None
    commercial_density: Optional[Dict[str, Any]]       = None

    # Historical / landmark status (Section 04 or dedicated block)
    historical_designation: Optional[str] = None   # "NRHP listed", "local landmark", etc.
    historic_district:      Optional[str] = None   # name of district if applicable
    historic_preservation_notes: Optional[str] = None
    historic_tax_credits_eligible: Optional[bool] = None

    # Available incentives (aggregated from multiple sources)
    incentives_available:   Optional[List[Dict[str, Any]]] = None
    incentives_narrative:   Optional[str] = None

    # Output file paths (set by main.py after generation)
    output_pdf_path:  Optional[str] = None
    output_xlsx_path: Optional[str] = None
    output_html_path: Optional[str] = None

    # ── Computed properties ────────────────────────────────────────────────

    @property
    def strategy_key(self) -> str:
        """
        Return the strategy string key used for template routing in config.py.
        Maps InvestmentStrategy enum → config template routing key.
        """
        mapping = {
            InvestmentStrategy.STABILIZED_HOLD: "stabilized_hold",
            InvestmentStrategy.VALUE_ADD:       "value_add",
            InvestmentStrategy.OPPORTUNISTIC:   "opportunistic",
        }
        return mapping.get(self.investment_strategy, "stabilized_hold")

    @property
    def cover_title(self) -> str:
        """Report cover page title based on investor_mode."""
        prefix = "Investment Summary" if self.investor_mode else "Investment Underwriting Report"
        return f"{prefix} — {self.address.full_address}"

    @property
    def suppressed_sections(self) -> List[str]:
        """
        Sections suppressed in investor mode — identified by section ID (not number)
        so renumbering the report never breaks suppression logic.
        §16 = DD Flags & Risk Assessment
        §17 = DD Checklist & Status Tracker
        §22 = Appendix A: Data Sources & Methodology
        """
        if self.investor_mode:
            return ['s16', 's17', 's22']
        return []

    # =========================================================================
    # ZONING OVERHAUL — Session 1: scenarios constraint validator
    # =========================================================================
    @model_validator(mode="after")
    def validate_scenarios_constraints(self):
        """Enforce master plan constraints on scenarios list."""
        if not self.scenarios:
            return self  # empty is fine — legacy deals or pre-3C-SCEN

        # D2: hard cap of 3 scenarios
        if len(self.scenarios) > 3:
            raise ValueError(
                f"DealData.scenarios cannot exceed 3 entries (got {len(self.scenarios)})"
            )

        # Rank uniqueness within the list
        ranks = [s.rank for s in self.scenarios]
        if len(set(ranks)) != len(ranks):
            raise ValueError(
                f"DealData.scenarios ranks must be unique, got {ranks}"
            )

        # Exactly one scenario must be PREFERRED
        preferred_count = sum(
            1 for s in self.scenarios if s.verdict == ScenarioVerdict.PREFERRED
        )
        if preferred_count != 1:
            raise ValueError(
                f"Exactly one scenario must have verdict=PREFERRED, got {preferred_count}"
            )

        # The PREFERRED scenario must have rank=1
        preferred_scenario = next(
            s for s in self.scenarios if s.verdict == ScenarioVerdict.PREFERRED
        )
        if preferred_scenario.rank != 1:
            raise ValueError(
                "The PREFERRED scenario must have rank=1"
            )

        # zoning_extensions.preferred_scenario_id must match a scenario
        if self.zoning_extensions is not None:
            scenario_ids = {s.scenario_id for s in self.scenarios}
            if self.zoning_extensions.preferred_scenario_id not in scenario_ids:
                raise ValueError(
                    f"zoning_extensions.preferred_scenario_id "
                    f"'{self.zoning_extensions.preferred_scenario_id}' "
                    f"does not match any scenario in scenarios list"
                )

        return self


# ═══════════════════════════════════════════════════════════════════════════
# CONVENIENCE FACTORY
# ═══════════════════════════════════════════════════════════════════════════

def create_deal(
    address_str: str,
    asset_type: AssetType,
    strategy: InvestmentStrategy,
    purchase_price: float,
    description: str = "",
) -> DealData:
    """
    Convenience factory — creates a minimal DealData from the 4 required
    New Underwrite screen inputs. All assumptions use FinancialAssumptions defaults.
    """
    deal = DealData(
        asset_type=asset_type,
        investment_strategy=strategy,
        deal_description=description,
        address=PropertyAddress(full_address=address_str),
    )
    deal.assumptions.purchase_price = purchase_price
    return deal


# =============================================================================
# ZONING OVERHAUL — Session 1 additions (April 2026)
# Master plan: DealDesk_Zoning_Overhaul_Plan.md
# Schema spec: Session_1_Schema_Design.md
# =============================================================================

# ── Enums ────────────────────────────────────────────────────────────────────

class ConfidenceLevel(str, Enum):
    """
    Confidence in the underlying data supporting an assessment.

    Used by the confidence gate (master plan D4) to determine whether
    conformity analysis should run or fall back to INDETERMINATE.
    """
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INDETERMINATE = "INDETERMINATE"


class ConformityStatus(str, Enum):
    """
    The conformity state of the property under current zoning.

    Per master plan D4, this is the headline classification on every Section 06.
    The CONFORMITY_INDETERMINATE value is the mandatory fallback when the
    confidence gate fails (insufficient zoning data).
    """
    CONFORMING = "CONFORMING"
    LEGAL_NONCONFORMING_USE = "LEGAL_NONCONFORMING_USE"
    LEGAL_NONCONFORMING_DENSITY = "LEGAL_NONCONFORMING_DENSITY"
    LEGAL_NONCONFORMING_DIMENSIONAL = "LEGAL_NONCONFORMING_DIMENSIONAL"
    MULTIPLE_NONCONFORMITIES = "MULTIPLE_NONCONFORMITIES"
    ILLEGAL_NONCONFORMING = "ILLEGAL_NONCONFORMING"
    CONFORMITY_INDETERMINATE = "CONFORMITY_INDETERMINATE"


class NonconformityType(str, Enum):
    """
    The specific zoning dimension along which a property fails to conform.

    Multiple instances may apply to one property (captured as a list of
    NonconformityItem records on the ConformityAssessment).
    """
    USE = "USE"
    DENSITY = "DENSITY"
    HEIGHT = "HEIGHT"
    FAR = "FAR"
    SETBACKS = "SETBACKS"
    LOT_COVERAGE = "LOT_COVERAGE"
    PARKING = "PARKING"
    LOT_AREA = "LOT_AREA"


class ScenarioVerdict(str, Enum):
    """
    The recommendation status of a development scenario within a deal.

    Exactly one scenario per deal must have verdict PREFERRED.
    """
    PREFERRED = "PREFERRED"
    ALTERNATE = "ALTERNATE"
    REJECT = "REJECT"


class ZoningPathwayType(str, Enum):
    """
    The regulatory pathway required to execute a development scenario.

    Maps to entitlement timeline, cost, and risk in downstream analysis.
    """
    BY_RIGHT = "BY_RIGHT"
    CONDITIONAL_USE = "CONDITIONAL_USE"
    SPECIAL_EXCEPTION = "SPECIAL_EXCEPTION"
    VARIANCE = "VARIANCE"
    REZONE = "REZONE"


# ── Sub-models (referenced by top-level models) ──────────────────────────────

class NonconformityItem(BaseModel):
    """
    A single instance of nonconformity. A property with multiple nonconformities
    has a list of these on its ConformityAssessment.
    """
    nonconformity_type: NonconformityType
    existing_value: str
    permitted_value: str
    magnitude_description: str
    triggers_loss_of_grandfathering: List[str] = Field(default_factory=list)


class GrandfatheringStatus(BaseModel):
    """
    The grandfathering posture of a nonconforming property.

    Only populated when ConformityStatus is one of the LEGAL_NONCONFORMING_*
    or MULTIPLE_NONCONFORMITIES values. None for CONFORMING and INDETERMINATE.
    """
    is_documented: bool
    documentation_source: Optional[str] = None
    presumption_basis: Optional[str] = None
    confirmation_action_required: str
    risk_if_denied: str


class ZoningPathway(BaseModel):
    """
    The regulatory pathway a scenario must traverse to be approvable.
    Each DevelopmentScenario carries one of these.
    """
    pathway_type: ZoningPathwayType
    approval_body: Optional[str] = None
    estimated_timeline_months: Optional[int] = None
    estimated_soft_cost_usd: Optional[float] = None
    success_probability_pct: Optional[int] = None
    fallback_if_denied: Optional[str] = None


class EntitlementRiskFlag(BaseModel):
    """
    A material entitlement risk flag attached to a scenario.

    Optional — only populated for scenarios where entitlement is a real risk
    (typically anything other than BY_RIGHT). None for clean by-right scenarios.
    """
    severity: str  # "LOW" | "MEDIUM" | "HIGH"
    risk_summary: str
    diligence_required: List[str] = Field(default_factory=list)


class UseAllocation(BaseModel):
    """
    Floor area allocation by use category within a scenario.
    A scenario can have multiple of these (mixed-use deals).
    """
    use_category: str
    square_feet: float
    unit_count: Optional[int] = None
    notes: Optional[str] = None


class OverlayImpact(BaseModel):
    """
    A single overlay district's impact on the property's development envelope.
    A property can have zero or more overlays.
    """
    overlay_name: str
    overlay_type: str
    impact_summary: str
    triggers_review: bool
    additional_diligence: List[str] = Field(default_factory=list)


class DevelopmentUpside(BaseModel):
    """
    Captures how much development capacity remains unused on the parcel.
    Useful for assessing future expansion potential.
    """
    far_remaining_sf: Optional[float] = None
    units_remaining: Optional[int] = None
    height_remaining_ft: Optional[float] = None
    summary: str


# ── Top-level models ─────────────────────────────────────────────────────────

class ConformityAssessment(BaseModel):
    """
    The headline conformity classification for the deal's property.

    Master plan D4: this assessment is rendered as a prominent callout at the
    top of Section 06 on every report. When the confidence gate fails, the
    status is set to CONFORMITY_INDETERMINATE with explicit explanation.
    """
    status: ConformityStatus
    confidence: ConfidenceLevel
    confidence_reasons: List[str] = Field(default_factory=list)

    nonconformity_details: List[NonconformityItem] = Field(default_factory=list)
    grandfathering_status: Optional[GrandfatheringStatus] = None

    risk_summary: str
    diligence_actions_required: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status_consistency(self):
        """Enforce semantic consistency between status and other fields."""
        if self.status == ConformityStatus.CONFORMING:
            if self.nonconformity_details:
                raise ValueError(
                    "CONFORMING status cannot have nonconformity_details"
                )
            if self.grandfathering_status is not None:
                raise ValueError(
                    "CONFORMING status cannot have grandfathering_status"
                )

        if self.status in {
            ConformityStatus.LEGAL_NONCONFORMING_USE,
            ConformityStatus.LEGAL_NONCONFORMING_DENSITY,
            ConformityStatus.LEGAL_NONCONFORMING_DIMENSIONAL,
            ConformityStatus.MULTIPLE_NONCONFORMITIES,
        }:
            if not self.nonconformity_details:
                raise ValueError(
                    f"{self.status.value} requires at least one nonconformity_details entry"
                )
            if self.grandfathering_status is None:
                raise ValueError(
                    f"{self.status.value} requires grandfathering_status to be populated"
                )

        if self.status == ConformityStatus.CONFORMITY_INDETERMINATE:
            if self.confidence != ConfidenceLevel.INDETERMINATE:
                raise ValueError(
                    "CONFORMITY_INDETERMINATE status must have INDETERMINATE confidence"
                )

        return self


class DevelopmentScenario(BaseModel):
    """
    A single development/business-plan scenario for the property.

    Master plan D1 defines what counts as a scenario. Master plan D2 caps
    the count at 3 per deal. Master plan D7 specifies the filename pattern
    that consumes scenario_id and rank.
    """
    # Identity
    scenario_id: str
    rank: int
    scenario_name: str
    business_thesis: str
    verdict: ScenarioVerdict

    # Physical configuration
    unit_count: int = 0
    building_sf: float = 0.0
    use_mix: List[UseAllocation] = Field(default_factory=list)

    # Operating strategy
    operating_strategy: str

    # Zoning pathway
    zoning_pathway: ZoningPathway

    # Per-scenario assumption deltas (applied to baseline assumptions in financials.py)
    construction_budget_delta_usd: Optional[float] = None
    rent_delta_pct: Optional[float] = None
    timeline_delta_months: Optional[int] = None

    # Outputs (populated by financials.py in Session 4 — None until then)
    financial_outputs: Optional["FinancialOutputs"] = None
    excel_filename: Optional[str] = None

    # Risk
    key_risks: List[str] = Field(default_factory=list)
    entitlement_risk_flag: Optional[EntitlementRiskFlag] = None

    @field_validator("scenario_id")
    @classmethod
    def validate_scenario_id_format(cls, v: str) -> str:
        """Enforce snake_case, 30-char cap, no special chars."""
        if not re.match(r"^[a-z][a-z0-9_]*$", v):
            raise ValueError(
                f"scenario_id must be snake_case starting with a letter, got '{v}'"
            )
        if len(v) > 30:
            raise ValueError(
                f"scenario_id max length is 30 chars, got {len(v)}"
            )
        return v

    @field_validator("rank")
    @classmethod
    def validate_rank_range(cls, v: int) -> int:
        """Rank must be 1, 2, or 3."""
        if v not in {1, 2, 3}:
            raise ValueError(f"rank must be 1, 2, or 3 — got {v}")
        return v


class ZoningExtensions(BaseModel):
    """
    Zoning analysis layers that are properties of the parcel as a whole,
    not of any one scenario.
    """
    use_flexibility_score: int
    use_flexibility_explanation: str

    overlay_impact_assessment: List[OverlayImpact] = Field(default_factory=list)
    development_upside: Optional[DevelopmentUpside] = None

    cross_scenario_recommendation: str
    preferred_scenario_id: str

    @field_validator("use_flexibility_score")
    @classmethod
    def validate_flexibility_score(cls, v: int) -> int:
        if v < 1 or v > 5:
            raise ValueError(f"use_flexibility_score must be 1-5, got {v}")
        return v


# ── Utility function ─────────────────────────────────────────────────────────

def mirror_preferred_to_legacy(deal: DealData) -> None:
    """
    Mirror the preferred scenario's financial_outputs to deal.financial_outputs.

    Per master plan D6: deal.financial_outputs is a derived field on the new
    multi-scenario path. This function is the only sanctioned write path.
    Direct writes to deal.financial_outputs should be avoided in new code.

    Args:
        deal: The DealData object to update. Mutates in place.

    Raises:
        ValueError: If deal has no scenarios, or if no scenario is PREFERRED,
                    or if the preferred scenario has no financial_outputs yet.
    """
    if not deal.scenarios:
        raise ValueError(
            "Cannot mirror: deal.scenarios is empty. "
            "This function should only be called after scenarios are populated."
        )

    preferred = next(
        (s for s in deal.scenarios if s.verdict == ScenarioVerdict.PREFERRED),
        None,
    )
    if preferred is None:
        raise ValueError(
            "Cannot mirror: no scenario has verdict=PREFERRED. "
            "DealData validators should have caught this."
        )

    if preferred.financial_outputs is None:
        raise ValueError(
            f"Cannot mirror: preferred scenario '{preferred.scenario_id}' "
            f"has no financial_outputs populated yet. "
            f"Call this AFTER financials.py fan-out completes."
        )

    deal.financial_outputs = preferred.financial_outputs


# ── Forward reference resolution ─────────────────────────────────────────────
# Resolve forward references introduced in Session 1
DevelopmentScenario.model_rebuild()
ConformityAssessment.model_rebuild()
ZoningExtensions.model_rebuild()
DealData.model_rebuild()
