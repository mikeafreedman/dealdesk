"""
main.py — DealDesk CRE Underwriting Pipeline Orchestrator & FastAPI Entry Point
=================================================================================
Runs the full pipeline in sequence:
    extractor → deal_data → market → risk → financials → excel_builder → report_builder

Usage:  python main.py
"""

import base64
import json
import logging
import os
import re
import sys
import socket
import tempfile
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Cookie
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel
from pydantic import BaseModel as PydanticBaseModel
from auth import (
    generate_otp, verify_otp, is_approved, is_trusted,
    send_otp_email, create_session_token,
    get_current_user, SESSION_COOKIE,
)
from auth_config import REMEMBER_ME_DAYS

from models.models import (
    AssetType,
    CompsData,
    CommercialComp,
    DealData,
    FinancialAssumptions,
    InvestmentStrategy,
    PropertyAddress,
    RefiEvent,
    SectionsConfig,
    WaterfallTier,
    WaterfallType,
)
from config import OUTPUTS_DIR

# Pipeline modules — all live at project root
from extractor import extract_documents
from deal_data import assemble_deal
from market import enrich_market_data
from risk import analyze_insurance
from financials import run_financials
from excel_builder import populate_excel
from report_builder import generate_report

logger = logging.getLogger(__name__)
_fmt = "%(asctime)s  %(name)s  %(levelname)s  %(message)s"
_stream_handler = logging.StreamHandler()
_stream_handler.stream = open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
logging.basicConfig(level=logging.INFO, format=_fmt, force=True,
                    handlers=[_stream_handler,
                              logging.FileHandler("server_output.log", mode="w", encoding="utf-8")])

# ── Pipeline stage definitions ────────────────────────────────────────────

STAGES = [
    ("Extracting document data …",   "extractor"),
    ("Assembling deal record …",      "deal_data"),
    ("Enriching market data …",       "market"),
    ("Analyzing insurance & risk …",  "risk"),
    ("Running financial engine …",    "financials"),
    ("Building Excel model …",        "excel_builder"),
    ("Generating PDF report …",       "report_builder"),
]

# ── In-memory cache for Excel downloads ──────────────────────────────────

_excel_cache: Dict[str, str] = {}  # deal_id → xlsx file path

# ── Per-user JSON store (assumptions + deals archive) ────────────────────

USER_DATA_DIR = Path(__file__).resolve().parent / "user_data"


def _user_file(email: str) -> Path:
    safe = re.sub(r"[^a-z0-9]+", "_", email.lower()).strip("_") or "user"
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return USER_DATA_DIR / f"{safe}.json"


def _read_user(email: str) -> Dict[str, Any]:
    p = _user_file(email)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("USER_STORE: failed to read %s: %s", p, exc)
        return {}


def _write_user(email: str, data: Dict[str, Any]) -> None:
    _user_file(email).write_text(
        json.dumps(data, indent=2, default=str), encoding="utf-8"
    )


def _record_deal(email: str, deal: DealData, deal_name: str) -> None:
    """Append a completed-deal summary to the user's archive."""
    try:
        fo = getattr(deal, "financial_outputs", None)
        record = {
            "deal_id":       deal.deal_id,
            "deal_name":     deal_name or deal.address.full_address or "Untitled",
            "address":       deal.address.full_address,
            "asset_type":    deal.asset_type.value if deal.asset_type else "",
            "strategy":      deal.investment_strategy.value if deal.investment_strategy else "",
            "purchase_price": deal.assumptions.purchase_price,
            "hold_period":   deal.assumptions.hold_period,
            "lp_irr":        getattr(fo, "lp_irr", None) if fo else None,
            "project_irr":   getattr(fo, "project_irr", None) if fo else None,
            "analyzed_date": datetime.now(timezone.utc).isoformat(),
            "status":        "complete",
        }
        user = _read_user(email)
        deals = user.setdefault("deals", [])
        # Replace any prior entry for the same deal_id (idempotent re-runs)
        deals = [d for d in deals if d.get("deal_id") != record["deal_id"]]
        deals.insert(0, record)
        user["deals"] = deals
        _write_user(email, user)
    except Exception as exc:
        logger.warning("USER_STORE: failed to record deal for %s: %s", email, exc)


# ── FastAPI app ──────────────────────────────────────────────────────────

app = FastAPI(title="DealDesk CRE Underwriting API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Request / response models ────────────────────────────────────────────

class UploadedFile(BaseModel):
    name: str
    content_base64: str
    type: str  # "om", "rent_roll", "financials"


class OTPRequest(PydanticBaseModel):
    email: str


class OTPVerify(PydanticBaseModel):
    email: str
    code: str
    remember_me: bool = False


class UnderwriteRequest(BaseModel):
    # Deal fields (f_ prefix)
    f_address: str = ""
    f_city: str = ""
    f_state: str = ""
    f_zip: str = ""
    f_asset_type: str = "Multifamily"
    f_strategy: str = "stabilized_hold"
    f_deal_name: str = ""
    f_description: str = ""
    f_purchase_price: float = 0.0
    f_const_period: int = 0
    f_const_loan_rate: float = 0.08
    f_leaseup_period: int = 0
    f_leaseup_vacancy: float = 0.25
    f_leaseup_concessions: float = 0.0
    f_leaseup_marketing: float = 0.0
    f_sale_price: float = 0.0
    f_sale_const_period: int = 0
    f_sale_mkt_period: int = 0
    f_sale_commission_pct: float = 0.05
    f_carry_loan_interest: float = 0.0
    f_carry_re_taxes: float = 0.0
    f_carry_insurance: float = 0.0
    f_carry_utilities: float = 0.0
    f_carry_maintenance: float = 0.0
    f_carry_hoa: float = 0.0
    f_carry_marketing: float = 0.0
    f_carry_staging: float = 0.0
    f_reno_cost: float = 0.0

    # Assumption fields (a_ prefix)
    a_hold_period: int = 10
    a_num_units: Optional[int] = None
    a_gba_sf: Optional[float] = None
    a_lot_sf: Optional[float] = None
    a_year_built: Optional[int] = None
    a_transfer_tax: float = 2.139
    a_closing_costs_fixed: float = 0.0
    a_tenant_buyout: float = 0.0
    a_legal_closing: float = 25000.0
    a_title_insurance: float = 8000.0
    a_legal_bank: float = 5000.0
    a_appraisal: float = 5000.0
    a_environmental: float = 6000.0
    a_surveyor: float = 3500.0
    a_architect: float = 0.0
    a_structural: float = 0.0
    a_civil_eng: float = 0.0
    a_meps: float = 0.0
    a_legal_zoning: float = 0.0
    a_geotech: float = 0.0
    a_acq_fee_fixed: float = 25000.0
    a_mortgage_carry: float = 0.0
    a_mezz_interest: float = 0.0
    a_working_capital: float = 15000.0
    a_marketing: float = 5000.0
    a_re_tax_carry: float = 0.0
    a_prop_ins_carry: float = 0.0
    a_dev_fee: float = 0.0
    a_dev_pref: float = 0.0
    a_permits: float = 0.0
    a_stormwater: float = 0.0
    a_demo: float = 0.0
    a_const_hard: float = 0.0
    a_const_reserve: float = 0.0
    a_gc_overhead: float = 0.0
    a_mezz_debt: float = 0.0
    a_tax_credit_eq: float = 0.0
    a_grants: float = 0.0
    a_ltv_pct: float = 70.0
    a_interest_rate: float = 6.5
    a_amort_years: int = 30
    a_loan_term: int = 10
    a_origination_fee_pct: float = 1.0
    a_io_period: int = 0

    # Refi 1
    a_refi1_on: bool = False
    a_refi1_year: int = 5
    a_refi1_appraised: float = 3200000.0
    a_refi1_ltv: float = 70.0
    a_refi1_rate: float = 6.0
    a_refi1_amort: int = 30
    a_refi1_term: int = 10
    a_refi1_orig_fee: float = 1.0
    a_refi1_prepay: float = 1.0
    a_refi1_closing: float = 0.0
    a_refi1_cap_rate: Optional[float] = None

    # Refi 2
    a_refi2_on: bool = False
    a_refi2_year: int = 8
    a_refi2_appraised: float = 3800000.0
    a_refi2_ltv: float = 65.0
    a_refi2_rate: float = 5.5
    a_refi2_amort: int = 30
    a_refi2_term: int = 10
    a_refi2_orig_fee: float = 1.0
    a_refi2_prepay: float = 1.0
    a_refi2_closing: float = 0.0
    a_refi2_cap_rate: Optional[float] = None

    # Refi 3
    a_refi3_on: bool = False
    a_refi3_year: int = 0
    a_refi3_appraised: float = 0.0
    a_refi3_ltv: float = 65.0
    a_refi3_rate: float = 5.5
    a_refi3_amort: int = 30
    a_refi3_term: int = 10
    a_refi3_orig_fee: float = 1.0
    a_refi3_prepay: float = 0.0
    a_refi3_closing: float = 0.0
    a_refi3_cap_rate: Optional[float] = None

    # Income
    a_vacancy: float = 7.5
    a_rev_growth: float = 3.0
    a_exp_growth: float = 3.0
    a_loss_to_lease: float = 3.0
    a_cam_reimbursements: float = 0.0
    a_fee_income: float = 0.0

    # Fixed expenses — ALL default to 0
    a_re_taxes: float = 0.0
    a_insurance: float = 0.0
    a_gas: float = 0.0
    a_water_sewer: float = 0.0
    a_electric: float = 0.0
    a_license: float = 0.0
    a_trash: float = 0.0

    # Variable expenses — ALL default to 0
    a_mgmt_fee: float = 0.0
    a_salaries: float = 0.0
    a_repairs: float = 0.0
    a_exterminator: float = 0.0
    a_cleaning: float = 0.0
    a_turnover: float = 0.0
    a_advertising: float = 0.0
    a_landscape: float = 0.0
    a_admin: float = 0.0
    a_office: float = 0.0
    a_misc_expense: float = 0.0

    # Below-the-line
    a_cap_reserve: float = 400.0
    a_commissions: float = 0.0
    a_renovations_yr1: float = 0.0

    # Exit
    a_exit_cap_rate: float = 7.0
    a_disp_fee: float = 2.0

    # Partnership / Waterfall
    a_gp_equity_pct: float = 10.0
    a_waterfall_type: int = 0
    a_waterfall_hurdle_type: str = "irr"
    a_pref_return: float = 8.0
    a_simple_lp: float = 80.0
    a_t1_hurdle: float = 12.0
    a_t2_hurdle: float = 15.0
    a_t3_hurdle: float = 18.0
    a_t4_hurdle: float = 24.0
    a_t1_lp: float = 70.0
    a_t2_lp: float = 60.0
    a_t3_lp: float = 30.0
    a_t4_lp: float = 20.0
    a_t5_lp: float = 10.0

    # EM hurdles
    a_em_t1: float = 2.0
    a_em_t2: float = 2.5
    a_em_t3: float = 3.0

    # Sensitivity
    a_sens_rg_low: float = 1.0      # rent growth: -2% from base (base=3%, low=1%)
    a_sens_rg_high: float = 5.0     # rent growth: +2% from base (base=3%, high=5%)
    a_sens_rg_step: float = 1.0
    a_sens_cap_low: float = 5.0     # exit cap: -1% from base (base=6%, low=5%)
    a_sens_cap_high: float = 7.0    # exit cap: +1% from base (base=6%, high=7%)
    a_sens_cap_step: float = 0.5

    # Return thresholds
    a_min_em: float = 1.80
    a_min_irr: float = 0.12
    a_min_coc: float = 0.07
    a_min_dscr: float = 1.25
    a_min_cap: float = 0.055
    a_target_irr: float = 15.0

    # Leasing cost assumptions
    a_ti_new_psf:              Optional[float] = None
    a_ti_renewal_psf:          Optional[float] = None
    a_commission_new_pct:      Optional[float] = None
    a_commission_renewal_pct:  Optional[float] = None
    a_lease_term_years:        Optional[float] = None
    a_construction_months:     Optional[float] = None
    a_draw_start_lag:          Optional[float] = None
    a_leaseup_months:          Optional[float] = None

    # Renovation scope (drives the market-rent engine in market.py)
    a_renovation_tier:         Optional[str]   = None   # light_cosmetic | heavy_rehab | new_construction
    a_lease_up_months:         Optional[int]   = None   # per-unit re-lease delay

    # Rent roll (from frontend form)
    rent_roll: Optional[List[Dict[str, Any]]] = None
    residential_rent_roll: Optional[List[Dict[str, Any]]] = None
    commercial_rent_roll: Optional[List[Dict[str, Any]]] = None

    # Top-level fields
    monthly_gross_rent: float = 0.0
    investor_mode: bool = False
    sections_config: Dict[str, bool] = {}
    uploaded_files: List[UploadedFile] = []
    comps: Optional[Dict[str, Any]] = None


# ── Helper: save base64 file to temp path ────────────────────────────────

def _save_base64_file(name: str, content_base64: str) -> str:
    """Decode a base64 file and write to a temp file, return the path."""
    suffix = Path(name).suffix
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(base64.b64decode(content_base64))
    tmp.close()
    return tmp.name


def _build_deal(req: UnderwriteRequest) -> DealData:
    """Parse the flat request into a DealData with populated FinancialAssumptions."""
    full_address = ", ".join(
        p for p in [req.f_address, req.f_city, req.f_state, req.f_zip] if p
    )

    deal = DealData(
        deal_id=str(uuid.uuid4()),
        asset_type=AssetType(req.f_asset_type),
        investment_strategy=InvestmentStrategy(req.f_strategy),
        deal_description=req.f_description,
        investor_mode=req.investor_mode,
        address=PropertyAddress(
            street=req.f_address,
            city=req.f_city,
            state=req.f_state,
            zip_code=req.f_zip,
            full_address=full_address,
        ),
    )

    # Build refi events
    exit_cap_decimal = req.a_exit_cap_rate / 100.0
    refi_cap_rates = [
        float(req.a_refi1_cap_rate or req.a_exit_cap_rate or 7.0) / 100.0,
        float(req.a_refi2_cap_rate or req.a_exit_cap_rate or 7.0) / 100.0,
        float(req.a_refi3_cap_rate or req.a_exit_cap_rate or 7.0) / 100.0,
    ]
    refi_events = []
    for i, (prefix, defaults) in enumerate([
        ("refi1", (req.a_refi1_on, req.a_refi1_year, req.a_refi1_appraised,
                   req.a_refi1_ltv, req.a_refi1_rate, req.a_refi1_amort,
                   req.a_refi1_term, req.a_refi1_orig_fee, req.a_refi1_prepay,
                   req.a_refi1_closing)),
        ("refi2", (req.a_refi2_on, req.a_refi2_year, req.a_refi2_appraised,
                   req.a_refi2_ltv, req.a_refi2_rate, req.a_refi2_amort,
                   req.a_refi2_term, req.a_refi2_orig_fee, req.a_refi2_prepay,
                   req.a_refi2_closing)),
        ("refi3", (req.a_refi3_on, req.a_refi3_year, req.a_refi3_appraised,
                   req.a_refi3_ltv, req.a_refi3_rate, req.a_refi3_amort,
                   req.a_refi3_term, req.a_refi3_orig_fee, req.a_refi3_prepay,
                   req.a_refi3_closing)),
    ]):
        active, year, appraised, ltv, rate, amort, term, orig_fee, prepay, closing = defaults
        try:
            refi_events.append(RefiEvent(
                active=active,
                year=year,
                appraised_value=appraised,
                cap_rate=refi_cap_rates[i],
                ltv=(ltv or 0) / 100.0,
                rate=(rate or 0) / 100.0,
                amort_years=amort,
                loan_term=term,
                orig_fee_pct=(orig_fee or 0) / 100.0,
                prepay_pct=(prepay or 0) / 100.0,
                closing_costs=closing,
            ))
        except Exception as exc:
            # Pydantic validation errors here previously crashed the whole
            # request. Log + append a disabled stub so the refi index
            # stays aligned across the three slots and downstream code
            # that references refi_events[i] doesn't IndexError.
            logger.warning(
                "REFI %s: construction failed (%s) — disabling this slot. "
                "Inputs: active=%s year=%s ltv=%s rate=%s",
                prefix, exc, active, year, ltv, rate,
            )
            refi_events.append(RefiEvent(active=False, year=year or 5))

    # Build waterfall tiers
    waterfall_tiers = [
        WaterfallTier(tier_number=1, hurdle_type="irr", hurdle_value=req.a_t1_hurdle / 100.0,
                      lp_share=req.a_t1_lp / 100.0, gp_share=1.0 - req.a_t1_lp / 100.0),
        WaterfallTier(tier_number=2, hurdle_type="irr", hurdle_value=req.a_t2_hurdle / 100.0,
                      lp_share=req.a_t2_lp / 100.0, gp_share=1.0 - req.a_t2_lp / 100.0),
        WaterfallTier(tier_number=3, hurdle_type="irr", hurdle_value=req.a_t3_hurdle / 100.0,
                      lp_share=req.a_t3_lp / 100.0, gp_share=1.0 - req.a_t3_lp / 100.0),
        WaterfallTier(tier_number=4, hurdle_type="irr", hurdle_value=req.a_t4_hurdle / 100.0,
                      lp_share=req.a_t4_lp / 100.0, gp_share=1.0 - req.a_t4_lp / 100.0),
    ]

    assumptions = FinancialAssumptions(
        hold_period=req.a_hold_period,
        num_units=req.a_num_units,
        gba_sf=req.a_gba_sf,
        lot_sf=req.a_lot_sf,
        year_built=req.a_year_built,
        purchase_price=req.f_purchase_price,
        transfer_tax_rate=req.a_transfer_tax / 100.0,
        closing_costs_fixed=req.a_closing_costs_fixed,
        tenant_buyout=req.a_tenant_buyout,
        legal_closing=req.a_legal_closing,
        title_insurance=req.a_title_insurance,
        legal_bank=req.a_legal_bank,
        appraisal=req.a_appraisal,
        environmental=req.a_environmental,
        surveyor=req.a_surveyor,
        architect=req.a_architect,
        structural=req.a_structural,
        civil_eng=req.a_civil_eng,
        meps=req.a_meps,
        legal_zoning=req.a_legal_zoning,
        geotech=req.a_geotech,
        acq_fee_fixed=req.a_acq_fee_fixed,
        mortgage_carry=0.0,   # auto-computed by construction interest S-curve model
        mezz_interest=req.a_mezz_interest,
        working_capital=req.a_working_capital,
        marketing=req.a_marketing,
        re_tax_carry=req.a_re_tax_carry,
        prop_ins_carry=req.a_prop_ins_carry,
        dev_fee=req.a_dev_fee,
        dev_pref=req.a_dev_pref,
        permits=req.a_permits,
        stormwater=req.a_stormwater,
        demo=req.a_demo,
        const_hard=req.a_const_hard,
        const_reserve=req.a_const_reserve,
        gc_overhead=req.a_gc_overhead,
        mezz_debt=req.a_mezz_debt,
        tax_credit_equity=req.a_tax_credit_eq,
        grants=req.a_grants,
        ltv_pct=req.a_ltv_pct / 100.0,
        interest_rate=req.a_interest_rate / 100.0,
        amort_years=req.a_amort_years,
        loan_term=req.a_loan_term,
        origination_fee_pct=req.a_origination_fee_pct / 100.0,
        io_period_months=req.a_io_period,
        refi_events=refi_events,
        # Development period — visible assumptions field overrides hidden dev-period card
        const_period_months=int(req.a_construction_months or 0) or req.f_const_period,
        draw_start_lag=int(req.a_draw_start_lag or 1),
        const_loan_rate=req.f_const_loan_rate,
        leaseup_period_months=int(req.a_leaseup_months or 0) or req.f_leaseup_period,
        leaseup_vacancy_rate=req.f_leaseup_vacancy,
        leaseup_concessions=req.f_leaseup_concessions,
        leaseup_marketing=req.f_leaseup_marketing,
        # For Sale fields
        sale_price_arv=req.f_sale_price,
        sale_const_period_months=req.f_sale_const_period,
        sale_marketing_period_months=req.f_sale_mkt_period,
        sale_broker_commission_pct=req.f_sale_commission_pct,
        carry_loan_interest_monthly=req.f_carry_loan_interest,
        carry_re_taxes_monthly=req.f_carry_re_taxes,
        carry_insurance_monthly=req.f_carry_insurance,
        carry_utilities_monthly=req.f_carry_utilities,
        carry_maintenance_monthly=req.f_carry_maintenance,
        carry_hoa_monthly=req.f_carry_hoa,
        carry_marketing_total=req.f_carry_marketing,
        carry_staging_total=req.f_carry_staging,
        # Renovation
        renovations_yr1=req.f_reno_cost,
        # Income
        vacancy_rate=req.a_vacancy / 100.0,
        annual_rent_growth=req.a_rev_growth / 100.0,
        expense_growth_rate=req.a_exp_growth / 100.0,
        loss_to_lease=req.a_loss_to_lease / 100.0,
        cam_reimbursements=req.a_cam_reimbursements,
        fee_income=req.a_fee_income,
        # Fixed expenses
        re_taxes=req.a_re_taxes,
        insurance=req.a_insurance,
        gas=req.a_gas,
        water_sewer=req.a_water_sewer,
        electric=req.a_electric,
        license_inspections=req.a_license,
        trash=req.a_trash,
        # Variable expenses
        mgmt_fee_pct=req.a_mgmt_fee / 100.0,
        salaries=req.a_salaries,
        repairs=req.a_repairs,
        exterminator=req.a_exterminator,
        cleaning=req.a_cleaning,
        turnover=req.a_turnover,
        advertising=req.a_advertising,
        landscape_snow=req.a_landscape,
        admin_legal_acct=req.a_admin,
        office_phone=req.a_office,
        miscellaneous=req.a_misc_expense,
        # Below-the-line
        cap_reserve_per_unit=req.a_cap_reserve,
        commissions_yr1=req.a_commissions,
        # Leasing cost assumptions
        # Use explicit None-check rather than `or`-idiom so user-entered 0
        # is preserved (e.g. 0% commission on a self-managed roll-over).
        ti_new_psf=float(req.a_ti_new_psf if req.a_ti_new_psf is not None else 0.0),
        ti_renewal_psf=float(req.a_ti_renewal_psf if req.a_ti_renewal_psf is not None else 0.0),
        commission_new_pct=float(req.a_commission_new_pct if req.a_commission_new_pct is not None else 5.0) / 100.0,
        commission_renewal_pct=float(req.a_commission_renewal_pct if req.a_commission_renewal_pct is not None else 2.5) / 100.0,
        lease_term_years=float(req.a_lease_term_years if req.a_lease_term_years is not None else 5),
        # Renovation scope — accept the frontend string or fall back to the
        # model default (light_cosmetic). lease_up_months is per-unit re-lease.
        renovation_tier=(req.a_renovation_tier or "light_cosmetic"),
        lease_up_months=int(req.a_lease_up_months if req.a_lease_up_months is not None else 1),
        # Exit
        exit_cap_rate=req.a_exit_cap_rate / 100.0,
        disposition_costs_pct=req.a_disp_fee / 100.0,
        # Partnership / Waterfall
        gp_equity_pct=req.a_gp_equity_pct / 100.0,
        waterfall_type=WaterfallType(req.a_waterfall_type),
        pref_return=req.a_pref_return / 100.0,
        simple_lp_split=req.a_simple_lp / 100.0,
        waterfall_tiers=waterfall_tiers,
        # EM hurdles
        em_hurdle_t1=req.a_em_t1,
        em_hurdle_t2=req.a_em_t2,
        em_hurdle_t3=req.a_em_t3,
        # Sensitivity
        sens_rent_growth_low=req.a_sens_rg_low / 100.0,
        sens_rent_growth_high=req.a_sens_rg_high / 100.0,
        sens_rent_growth_step=req.a_sens_rg_step / 100.0,
        sens_exit_cap_low=req.a_sens_cap_low / 100.0,
        sens_exit_cap_high=req.a_sens_cap_high / 100.0,
        sens_exit_cap_step=req.a_sens_cap_step / 100.0,
        # Return thresholds
        min_equity_multiple=req.a_min_em,
        min_lp_irr=req.a_min_irr,
        min_coc=req.a_min_coc,
        min_dscr=req.a_min_dscr,
        min_cap_rate=req.a_min_cap,
        target_lp_irr=req.a_target_irr / 100.0,
    )

    deal.assumptions = assumptions
    logger.info("DEAL INPUT hard_costs=%s (from form: const_hard=%s, reno=%s)",
                assumptions.const_hard, req.a_const_hard, req.f_reno_cost)
    logger.info("FORM INPUT a_const_hard=%s, f_reno_cost=%s",
                req.a_const_hard, req.f_reno_cost)

    # Sections config
    if req.sections_config:
        deal.sections_config = SectionsConfig(**req.sections_config)

    # Wire comp data from frontend
    if req.comps:
        try:
            from models.models import RentComp, SaleComp
            rc = [RentComp(**c) for c in req.comps.get("rent_comps", []) if c]
            cc = [CommercialComp(**c) for c in req.comps.get("commercial_comps", []) if c]
            sc = [SaleComp(**c) for c in req.comps.get("sale_comps", []) if c]
            deal.comps = CompsData(rent_comps=rc, commercial_comps=cc, sale_comps=sc)
        except Exception as exc:
            logger.warning("Comps wiring failed: %s", exc)

    return deal


# ── Routes ───────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Serve the login page. Redirect to app if already logged in."""
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=302)
    with open("login.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.post("/auth/request-code")
async def request_code(body: OTPRequest):
    """Validate email is approved, generate OTP, send it."""
    email = body.email.strip().lower()
    if not is_approved(email):
        raise HTTPException(
            status_code=403,
            detail="This email address is not authorized for DealDesk access."
        )
    code = generate_otp(email)
    sent = send_otp_email(email, code)
    if not sent:
        raise HTTPException(
            status_code=500,
            detail="Failed to send access code. Please try again."
        )
    return {"message": "Code sent", "trusted": is_trusted(email)}


@app.post("/auth/verify-code")
async def verify_code_route(body: OTPVerify):
    """Verify OTP and set session cookie."""
    email = body.email.strip().lower()
    if not verify_otp(email, body.code):
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired code. Please request a new one."
        )
    token = create_session_token(email)
    response = JSONResponse(content={"message": "Authenticated"})

    use_remember_me = body.remember_me and is_trusted(email)

    if use_remember_me:
        max_age = REMEMBER_ME_DAYS * 24 * 60 * 60
        logger.info(f"AUTH: Setting 30-day persistent cookie for {email}")
    else:
        max_age = None
        logger.info(f"AUTH: Setting session cookie for {email}")

    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=max_age,
    )
    return response


@app.post("/auth/logout")
async def logout():
    """Clear session cookie and redirect to login."""
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/")
async def serve_frontend(request: Request):
    """Serve the static HTML frontend."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    html_path = Path(__file__).resolve().parent / "fp_underwriting_FINAL_v7.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Frontend HTML file not found")
    return FileResponse(
        str(html_path),
        media_type="text/html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


@app.post("/underwrite")
async def underwrite(req: UnderwriteRequest, request: Request):
    """Run the full underwriting pipeline and return the PDF report."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        logger.info("Payload f_purchase_price = %s (type: %s)", req.f_purchase_price, type(req.f_purchase_price).__name__)
        logger.info(f"PAYLOAD DEBUG — f_purchase_price: {req.f_purchase_price}, "
                    f"f_asking_price: {getattr(req, 'f_asking_price', 'NOT FOUND')}, "
                    f"a_purchase_price: {getattr(req, 'a_purchase_price', 'NOT FOUND')}, "
                    f"asset_type: {getattr(req, 'asset_type', 'NOT FOUND')}, "
                    f"f_address: {getattr(req, 'f_address', 'NOT FOUND')}")

        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

        deal = _build_deal(req)

        # Handle uploaded files — base64 decode to temp files
        om_path = None
        rr_path = None
        fin_path = None
        all_uploaded_paths: list[str] = []
        for uf in req.uploaded_files:
            saved_path = _save_base64_file(uf.name, uf.content_base64)
            if saved_path and os.path.exists(saved_path):
                all_uploaded_paths.append(saved_path)
                logger.info("MAIN: queuing file for extraction: '%s' (type=%s)",
                            saved_path, getattr(uf, 'type', 'unknown'))
            if uf.type == "om":
                om_path = saved_path
            elif uf.type == "rent_roll":
                rr_path = saved_path
            elif uf.type == "financials":
                fin_path = saved_path
        logger.info("MAIN: %d file(s) queued for extraction", len(all_uploaded_paths))

        # Merge frontend rent roll rows into extracted_docs.unit_mix. Every
        # numeric parse is wrapped with _safe_num so a stray letter in a SF
        # or rent cell doesn't 500 the whole request — the bad row is
        # dropped with a warning and the rest of the roll loads.
        def _safe_num(v, default=0.0):
            if v is None or v == "":
                return default
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        if req.rent_roll:
            rr_units = []
            dropped = 0
            for row_idx, row in enumerate(req.rent_roll):
                try:
                    if not row.get("unit") and not row.get("sf"):
                        continue
                    status = row.get("status", "Occupied")
                    is_vacant = status == "Vacant"
                    sf = _safe_num(row.get("sf"))
                    rent_mo = _safe_num(row.get("rent_mo"))
                    rent_sf = _safe_num(row.get("rent_sf"))
                    market_mo = _safe_num(row.get("market_mo"))
                    market_sf = _safe_num(row.get("market_sf"))
                    rr_units.append({
                        "unit_id": row.get("unit", ""),
                        "unit_type": row.get("type", ""),
                        "sf": sf,
                        "monthly_rent": (rent_mo if rent_mo > 0
                                          else (rent_sf * sf / 12.0 if rent_sf > 0 else 0)),
                        "market_rent": market_mo if market_mo > 0 else market_sf,
                        "current_rent_sf": rent_sf,
                        "status": status,
                        "is_vacant": is_vacant,
                        "lease_term_years": _safe_num(row.get("lease_term"), 5),
                        "lease_expiry_year": int(_safe_num(row.get("expiry_year"))),
                        "market_rent_sf": market_sf if market_sf > 0 else market_mo,
                        "renewal_probability": _safe_num(row.get("renewal_prob"), 70) / 100.0,
                        "downtime_months": int(_safe_num(row.get("downtime"), 3)),
                    })
                except Exception as exc:
                    dropped += 1
                    logger.warning("RENT ROLL row %d dropped (%s): %s",
                                   row_idx, exc, row)
            if rr_units:
                deal.extracted_docs.unit_mix = rr_units
                logger.info("RENT ROLL: %d units from frontend form "
                            "(%d row(s) dropped due to bad data)",
                            len(rr_units), dropped)

        # ── New structured rent roll from frontend UI ─────────────
        if req.residential_rent_roll or req.commercial_rent_roll:
            new_unit_mix = []
            dropped_new = 0

            for idx, row in enumerate(req.residential_rent_roll or []):
                try:
                    units = _safe_num(row.get('units'))
                    proforma = _safe_num(row.get('proforma_rent'))
                    current = _safe_num(row.get('current_rent'))
                    if units > 0 and proforma > 0:
                        new_unit_mix.append({
                            'unit_type':    row.get('type', 'Residential'),
                            'count':        units,
                            'monthly_rent': proforma,
                            'market_rent':  proforma,
                            'current_rent': current,
                        })
                except Exception as exc:
                    dropped_new += 1
                    logger.warning("RES RENT ROLL row %d dropped (%s): %s",
                                   idx, exc, row)

            for idx, row in enumerate(req.commercial_rent_roll or []):
                try:
                    sf = _safe_num(row.get('sf'))
                    proforma_psf = _safe_num(row.get('proforma_rent_psf'))
                    current_psf = _safe_num(row.get('current_rent_psf'))
                    if sf > 0 and proforma_psf > 0:
                        monthly_equiv = (sf * proforma_psf) / 12
                        new_unit_mix.append({
                            'unit_type':    row.get('lease_type', 'Commercial'),
                            'tenant':       row.get('tenant', ''),
                            'sf':           sf,
                            'monthly_rent': monthly_equiv,
                            'market_rent':  monthly_equiv,
                            'current_rent': (sf * current_psf) / 12,
                            'cam_psf':      _safe_num(row.get('cam_psf')),
                        })
                except Exception as exc:
                    dropped_new += 1
                    logger.warning("COMM RENT ROLL row %d dropped (%s): %s",
                                   idx, exc, row)
            if dropped_new:
                logger.warning("RENT ROLL: %d row(s) dropped due to bad data", dropped_new)

            if new_unit_mix:
                if deal.extracted_docs is None:
                    from models.models import ExtractedDocumentData
                    deal.extracted_docs = ExtractedDocumentData()
                existing = deal.extracted_docs.unit_mix or []
                deal.extracted_docs.unit_mix = new_unit_mix + existing
                logger.info(
                    "RENT ROLL: injected %d rows from frontend UI "
                    "(%d residential, %d commercial)",
                    len(new_unit_mix),
                    len(req.residential_rent_roll or []),
                    len(req.commercial_rent_roll or []),
                )

        # Collect user inputs as flat dict for assemble_deal
        user_inputs = req.model_dump()

        # Run pipeline stages
        for idx, (label, stage_name) in enumerate(STAGES):
            logger.info("Stage %d/%d — %s", idx + 1, len(STAGES), stage_name)

            if stage_name == "extractor":
                deal = extract_documents(
                    deal,
                    om_pdf_path=om_path,
                    rent_roll_pdf_path=rr_path,
                    financials_pdf_path=fin_path,
                    uploaded_files=all_uploaded_paths,
                )

            elif stage_name == "deal_data":
                deal = assemble_deal(deal, user_inputs)
                logger.info(f"[DEAL_DATA] GBA: {getattr(deal.assumptions, 'gba_sf', 'MISSING')} / "
                            f"{getattr(deal.assumptions, 'gross_building_area', 'MISSING')}")
                if req.monthly_gross_rent and float(req.monthly_gross_rent) > 0:
                    deal.extracted_docs.total_monthly_rent = float(req.monthly_gross_rent)

            elif stage_name == "market":
                deal = enrich_market_data(deal)

            elif stage_name == "risk":
                deal = analyze_insurance(deal)

            elif stage_name == "financials":
                logger.info("PIPELINE: Starting financials computation")
                deal = run_financials(deal)
                try:
                    _fo = deal.financial_outputs
                    logger.info("FINANCIALS COMPLETE: fo=%s, noi_yr1=%s",
                                _fo is not None,
                                getattr(_fo, 'noi_yr1', 'MISSING'))
                except Exception as _fe:
                    logger.error("FINANCIALS COMPLETE log failed: %s", _fe)
                # DD flag evaluation runs after financials so the engine can
                # see NOI, DSCR, IRR, and refi outcomes. Failure is non-fatal.
                try:
                    from dd_flag_engine import generate_dd_flags
                    generate_dd_flags(deal)
                except Exception as _de:
                    logger.warning("DD FLAG engine failed (non-fatal): %s", _de)

            elif stage_name == "excel_builder":
                logger.info("PIPELINE: Starting excel_builder")
                _gpr = getattr(getattr(deal, 'financial_outputs', None), 'gross_potential_rent', 'MISSING')
                logger.info(f"[DIAG] GPR Yr1 = ${_gpr:,.0f}" if isinstance(_gpr, (int, float)) else f"[DIAG] GPR Yr1 = {_gpr}")
                logger.info(f"[DIAG] unit_mix count = {len(getattr(getattr(deal, 'extracted_docs', None), 'unit_mix', None) or [])}")
                logger.info(f"[DIAG] assumptions.num_units = {getattr(getattr(deal, 'assumptions', None), 'num_units', 'MISSING')}")
                logger.info(f"[DIAG] assumptions.monthly_rent = {getattr(getattr(deal, 'assumptions', None), 'monthly_rent', 'MISSING')}")
                xlsx_path: Path = populate_excel(deal)
                deal.output_xlsx_path = str(xlsx_path)
                # Diagnostic scaffold: diff 12 KPIs between Python and Excel.
                # Non-fatal; outputs a KPI DIFF / KPI OK log line per metric.
                try:
                    from kpi_validator import validate as _kpi_validate
                    _kpi_validate(deal, xlsx_path)
                except Exception as _ke:
                    logger.warning("KPI VALIDATOR (non-fatal): %s", _ke)

            elif stage_name == "report_builder":
                pdf_path = generate_report(deal)
                if pdf_path and Path(pdf_path).exists():
                    deal.output_pdf_path = str(pdf_path)
                    logger.info("PDF SERVE TARGET: %s", pdf_path)

        # Cache Excel path for download endpoint
        if deal.deal_id and deal.output_xlsx_path:
            _excel_cache[deal.deal_id] = deal.output_xlsx_path

        # Persist deal summary to the signed-in user's archive
        _record_deal(user, deal, req.f_deal_name)

        logger.info("Pipeline finished — PDF: %s | Excel: %s", deal.output_pdf_path, deal.output_xlsx_path)

        # Return PDF as file download
        if deal.output_pdf_path and Path(deal.output_pdf_path).exists():
            _pdf_path = Path(deal.output_pdf_path)
            return FileResponse(
                path=str(_pdf_path),
                media_type="application/pdf",
                filename=_pdf_path.name,
                headers={"X-Deal-Id": deal.deal_id or "",
                         "Access-Control-Expose-Headers": "X-Deal-Id"},
            )
        else:
            raise HTTPException(status_code=500, detail="PDF report was not generated")

    except HTTPException:
        raise
    except Exception:
        logger.error("Pipeline error:\n%s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=traceback.format_exc())


@app.get("/api/me")
async def api_me(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"email": user}


@app.get("/api/assumptions")
async def api_get_assumptions(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return _read_user(user).get("assumptions", {})


@app.post("/api/assumptions")
async def api_save_assumptions(request: Request, body: Dict[str, Any]):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = _read_user(user)
    data["assumptions"] = body
    _write_user(user, data)
    return {"message": "Saved"}


@app.get("/api/deals")
async def api_get_deals(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return _read_user(user).get("deals", [])


@app.delete("/api/deals/{deal_id}")
async def api_delete_deal(deal_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = _read_user(user)
    before = len(data.get("deals", []))
    data["deals"] = [d for d in data.get("deals", []) if d.get("deal_id") != deal_id]
    _write_user(user, data)
    return {"removed": before - len(data["deals"])}


def _resolve_output_file(deal_id: str, suffix_candidates: list[str]) -> Path | None:
    """Find a generated output file for `deal_id`. First checks the in-process
    caches (fast path during the same server lifetime), then falls back to the
    filesystem pattern `outputs/{deal_id}{suffix}` so archived deals still
    download after a server restart. Returns the resolved Path or None.
    """
    # In-process fast path (populated at generation time)
    if suffix_candidates and suffix_candidates[0].endswith(".xlsx"):
        cached = _excel_cache.get(deal_id)
        if cached and Path(cached).exists():
            return Path(cached)
    # Filesystem fallback — try each known suffix for the deal_id
    outputs_dir = Path("outputs")
    for suffix in suffix_candidates:
        candidate = outputs_dir / f"{deal_id}{suffix}"
        if candidate.exists():
            return candidate
    return None


@app.get("/download/excel/{deal_id}")
async def download_excel(deal_id: str, request: Request):
    """Return a previously generated Excel file by deal_id."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    xlsx_path = _resolve_output_file(deal_id, ["_financial_model.xlsx"])
    if not xlsx_path:
        raise HTTPException(status_code=404, detail=f"Excel file not found for deal {deal_id}")
    return FileResponse(
        path=str(xlsx_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=xlsx_path.name,
    )


@app.get("/download/pdf/{deal_id}")
async def download_pdf(deal_id: str, request: Request):
    """Return a previously generated PDF report by deal_id. Prefers the
    Playwright PDF; falls back to the docx-derived PDF when the Playwright
    build was skipped.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    pdf_path = _resolve_output_file(
        deal_id,
        ["_report_playwright.pdf", "_report.pdf"],
    )
    if not pdf_path:
        raise HTTPException(status_code=404, detail=f"PDF report not found for deal {deal_id}")
    # Strip the "_playwright" disambiguator from the user-visible filename.
    display_name = pdf_path.name.replace("_report_playwright.pdf", "_report.pdf")
    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=display_name,
    )


# ── Entry point ──────────────────────────────────────────────────────────

def find_free_port(start=8000):
    for port in range(start, start + 10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    return start + 10


if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 0)) or find_free_port()
    print(f"Starting server on port {PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None)
