"""
main.py — DealDesk CRE Underwriting Pipeline Orchestrator & FastAPI Entry Point
=================================================================================
Runs the full pipeline in sequence:
    extractor → deal_data → market → risk → financials → excel_builder → word_builder

Usage:  python main.py
"""

import base64
import logging
import tempfile
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

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
from word_builder import generate_report

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(name)s  %(levelname)s  %(message)s")

# ── Pipeline stage definitions ────────────────────────────────────────────

STAGES = [
    ("Extracting document data …",   "extractor"),
    ("Assembling deal record …",      "deal_data"),
    ("Enriching market data …",       "market"),
    ("Analyzing insurance & risk …",  "risk"),
    ("Running financial engine …",    "financials"),
    ("Building Excel model …",        "excel_builder"),
    ("Generating PDF report …",       "word_builder"),
]

# ── In-memory cache for Excel downloads ──────────────────────────────────

_excel_cache: Dict[str, str] = {}  # deal_id → xlsx file path

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


class UnderwriteRequest(BaseModel):
    # Deal fields (f_ prefix)
    f_address: str = ""
    f_city: str = ""
    f_state: str = ""
    f_zip: str = ""
    f_asset_type: str = "Multifamily"
    f_strategy: str = "stabilized"
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
    a_closing_costs_fixed: float = 75000.0
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
    a_mortgage_fees: float = 17500.0
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
    a_origination_fee: float = 1.0
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
    a_refi1_closing: float = 25000.0

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
    a_refi2_closing: float = 25000.0

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

    # Income
    a_vacancy: float = 7.5
    a_rev_growth: float = 3.0
    a_exp_growth: float = 3.0
    a_loss_to_lease: float = 3.0
    a_cam_reimbursements: float = 0.0
    a_fee_income: float = 6000.0

    # Fixed expenses
    a_re_taxes: float = 45000.0
    a_insurance: float = 18000.0
    a_gas: float = 12000.0
    a_water_sewer: float = 14000.0
    a_electric: float = 10000.0
    a_license: float = 2500.0
    a_trash: float = 8000.0

    # Variable expenses
    a_mgmt_fee: float = 6.0
    a_salaries: float = 24000.0
    a_repairs: float = 8000.0
    a_exterminator: float = 3600.0
    a_cleaning: float = 6000.0
    a_turnover: float = 5000.0
    a_advertising: float = 4000.0
    a_landscape: float = 6000.0
    a_admin: float = 5000.0
    a_office: float = 3000.0
    a_misc_expense: float = 2000.0

    # Below-the-line
    a_cap_reserve: float = 400.0
    a_commissions: float = 0.0
    a_renovations_yr1: float = 0.0

    # Exit
    a_exit_cap_rate: float = 7.0
    a_disp_fee: float = 2.0

    # Partnership / Waterfall
    a_gp_equity_pct: float = 10.0
    a_waterfall_type: int = 1
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
    a_sens_rg_low: float = 0.0
    a_sens_rg_high: float = 5.0
    a_sens_rg_step: float = 1.0
    a_sens_cap_low: float = 5.5
    a_sens_cap_high: float = 8.5
    a_sens_cap_step: float = 0.5

    # Return thresholds
    a_min_em: float = 1.80
    a_min_irr: float = 0.12
    a_min_coc: float = 0.07
    a_min_dscr: float = 1.25
    a_min_cap: float = 0.055
    a_target_irr: float = 15.0

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
    refi_events = []
    for prefix, defaults in [
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
    ]:
        active, year, appraised, ltv, rate, amort, term, orig_fee, prepay, closing = defaults
        refi_events.append(RefiEvent(
            active=active,
            year=year,
            appraised_value=appraised,
            ltv=ltv / 100.0,
            rate=rate / 100.0,
            amort_years=amort,
            loan_term=term,
            orig_fee_pct=orig_fee / 100.0,
            prepay_pct=prepay / 100.0,
            closing_costs=closing,
        ))

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
        mortgage_carry=req.a_mortgage_carry,
        mortgage_fees=req.a_mortgage_fees,
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
        origination_fee_pct=req.a_origination_fee / 100.0,
        io_period_months=req.a_io_period,
        refi_events=refi_events,
        # Development period (value_add)
        const_period_months=req.f_const_period,
        const_loan_rate=req.f_const_loan_rate,
        leaseup_period_months=req.f_leaseup_period,
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

@app.get("/")
async def serve_frontend():
    """Serve the static HTML frontend."""
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
async def underwrite(req: UnderwriteRequest):
    """Run the full underwriting pipeline and return the PDF report."""
    try:
        logger.info("Payload f_purchase_price = %s (type: %s)", req.f_purchase_price, type(req.f_purchase_price).__name__)

        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

        deal = _build_deal(req)

        # Handle uploaded files — base64 decode to temp files
        om_path = None
        rr_path = None
        fin_path = None
        for uf in req.uploaded_files:
            saved_path = _save_base64_file(uf.name, uf.content_base64)
            if uf.type == "om":
                om_path = saved_path
            elif uf.type == "rent_roll":
                rr_path = saved_path
            elif uf.type == "financials":
                fin_path = saved_path

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
                )

            elif stage_name == "deal_data":
                deal = assemble_deal(deal, user_inputs)
                if req.monthly_gross_rent:
                    deal.extracted_docs.total_monthly_rent = float(req.monthly_gross_rent)

            elif stage_name == "market":
                deal = enrich_market_data(deal)

            elif stage_name == "risk":
                deal = analyze_insurance(deal)

            elif stage_name == "financials":
                deal = run_financials(deal)

            elif stage_name == "excel_builder":
                xlsx_path: Path = populate_excel(deal)
                deal.output_xlsx_path = str(xlsx_path)

            elif stage_name == "word_builder":
                deal = generate_report(deal)

        # Cache Excel path for download endpoint
        if deal.deal_id and deal.output_xlsx_path:
            _excel_cache[deal.deal_id] = deal.output_xlsx_path

        logger.info("Pipeline finished — PDF: %s | Excel: %s", deal.output_pdf_path, deal.output_xlsx_path)

        # Return PDF as file download
        if deal.output_pdf_path and Path(deal.output_pdf_path).exists():
            return FileResponse(
                path=deal.output_pdf_path,
                media_type="application/pdf",
                filename=Path(deal.output_pdf_path).name,
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


@app.get("/download/excel/{deal_id}")
async def download_excel(deal_id: str):
    """Return a previously generated Excel file by deal_id."""
    xlsx_path = _excel_cache.get(deal_id)
    if not xlsx_path or not Path(xlsx_path).exists():
        raise HTTPException(status_code=404, detail=f"Excel file not found for deal {deal_id}")
    return FileResponse(
        path=xlsx_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=Path(xlsx_path).name,
    )


# ── Entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
