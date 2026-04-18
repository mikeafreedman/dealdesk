"""
models.py — DealDesk CRE Underwriting System
=============================================
Unified DealData schema — the foundational data contract for the entire pipeline.
Every module reads from and writes to this schema. No logic lives here.

Version:  v1.0  (initial approval build — April 6, 2026)
Status:   PENDING APPROVAL
"""

from __future__ import annotations
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

    # §6–8 Refinancing events — List[RefiEvent], max 3
    refi_events: List[RefiEvent] = Field(default_factory=lambda: [
        RefiEvent(active=False, year=5,  appraised_value=3200000, cap_rate=0.07, ltv=0.70, rate=0.060, amort_years=30, loan_term=10, orig_fee_pct=0.01, prepay_pct=0.01, closing_costs=0),
        RefiEvent(active=False, year=8,  appraised_value=3800000, cap_rate=0.07, ltv=0.65, rate=0.055, amort_years=30, loan_term=10, orig_fee_pct=0.01, prepay_pct=0.01, closing_costs=0),
        RefiEvent(active=False, year=0,  appraised_value=0,       cap_rate=0.07, ltv=0.65, rate=0.055, amort_years=30, loan_term=10, orig_fee_pct=0.01, prepay_pct=0.00, closing_costs=0),
    ])

    # §8 Income
    vacancy_rate:        float = 0.075
    annual_rent_growth:  float = 0.03
    expense_growth_rate: float = 0.03   # Referenced by Prompt 5B
    loss_to_lease:       float = 0.03
    cam_reimbursements:  float = 0.0
    fee_income:          float = 0.0     # was 6000.0 — default to $0

    # §9 Fixed expenses (Year 1)
    re_taxes:            float = 45000.0
    insurance:           float = 18000.0
    gas:                 float = 12000.0
    water_sewer:         float = 14000.0
    electric:            float = 10000.0
    license_inspections: float = 2500.0
    trash:               float = 8000.0

    # §10 Variable expenses (Year 1)
    mgmt_fee_pct:        float = 0.06
    salaries:            float = 24000.0
    repairs:             float = 8000.0
    exterminator:        float = 3600.0
    cleaning:            float = 6000.0
    turnover:            float = 5000.0
    advertising:         float = 4000.0
    landscape_snow:      float = 6000.0
    admin_legal_acct:    float = 5000.0
    office_phone:        float = 3000.0
    miscellaneous:       float = 2000.0

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

    @model_validator(mode='after')
    def lp_equals_complement(self) -> 'FinancialAssumptions':
        self.lp_equity_pct = round(1.0 - self.gp_equity_pct, 6)
        return self

    @model_validator(mode='after')
    def max_three_refis(self) -> 'FinancialAssumptions':
        if len(self.refi_events) > 3:
            raise ValueError("Maximum 3 refinancing events allowed.")
        return self


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
    pro_forma_years:         Optional[List[Dict[str, float]]] = None
    loan_balance_at_refi:    Optional[List[Optional[float]]] = None  # amortized balance at each refi event
    lease_events:            Optional[Dict[int, Dict[str, Any]]] = None  # {year: {commission, ti, downtime_loss, ...}}
    sensitivity_stabilized_year: Optional[int]   = None
    sensitivity_stabilized_noi:  Optional[float] = None
    sensitivity_note:            Optional[str]   = None



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
        self.rent_comps       = self.rent_comps[:8]
        self.commercial_comps = self.commercial_comps[:5]
        self.sale_comps       = self.sale_comps[:5]
        return self


class ExtractedDocumentData(BaseModel):
    """Structured data from uploaded documents — populated by extractor.py."""
    # From Prompt 1A — OM
    property_name:          Optional[str]   = None
    asking_price:           Optional[float] = None
    deal_source:            Optional[str]   = None
    broker_name:            Optional[str]   = None
    num_units_extracted:    Optional[int]   = None
    gba_sf_extracted:       Optional[float] = None
    lot_sf_extracted:       Optional[float] = None
    year_built_extracted:   Optional[int]   = None
    description_extracted:  Optional[str]   = None
    image_placements:       Optional[Dict[str, Any]] = None
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
        word_builder.py  → reads all fields; generates PDF report

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
