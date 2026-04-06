"""
main.py — DealDesk CRE Underwriting Pipeline Orchestrator & Streamlit Entry Point
==================================================================================
Runs the full pipeline in sequence:
    extractor → deal_data → market → risk → financials → excel_builder → word_builder

Usage:  streamlit run main.py
"""

import logging
import tempfile
import traceback
from pathlib import Path

import streamlit as st

from models.models import (
    AssetType,
    DealData,
    InvestmentStrategy,
    PropertyAddress,
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


def _collect_user_inputs() -> dict:
    """Gather all user-editable fields from st.session_state into a plain dict."""
    ss = st.session_state
    inputs: dict = {}

    # Mirror every assumption key present in session state
    for key in list(ss.keys()):
        if key.startswith("_"):
            continue
        inputs[key] = ss[key]

    return inputs


def _build_initial_deal() -> DealData:
    """Create the seed DealData from the required Streamlit inputs."""
    ss = st.session_state

    deal = DealData(
        asset_type=AssetType(ss.get("asset_type", AssetType.MULTIFAMILY)),
        investment_strategy=InvestmentStrategy(ss.get("investment_strategy", InvestmentStrategy.STABILIZED)),
        deal_description=ss.get("deal_description", ""),
        address=PropertyAddress(full_address=ss.get("address", "")),
    )
    deal.assumptions.purchase_price = float(ss.get("purchase_price", 0))
    return deal


def run_pipeline() -> None:
    """Execute the full 7-stage pipeline with a Streamlit progress bar."""
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    deal = _build_initial_deal()
    user_inputs = _collect_user_inputs()

    progress = st.progress(0, text="Starting pipeline …")
    total = len(STAGES)

    try:
        for idx, (label, stage_name) in enumerate(STAGES):
            progress.progress(idx / total, text=label)
            logger.info("Stage %d/%d — %s", idx + 1, total, stage_name)

            try:
                if stage_name == "extractor":
                    deal = extract_documents(
                        deal,
                        om_pdf_path=st.session_state.get("om_pdf_path"),
                        rent_roll_pdf_path=st.session_state.get("rent_roll_pdf_path"),
                        financials_pdf_path=st.session_state.get("financials_pdf_path"),
                    )

                elif stage_name == "deal_data":
                    deal = assemble_deal(deal, user_inputs)
                    # Set monthly gross rent from form input after assembly
                    monthly_rent = st.session_state.get("monthly_gross_rent", 0)
                    if monthly_rent:
                        deal.extracted_docs.total_monthly_rent = float(monthly_rent)

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

            except Exception:
                logger.error("Pipeline failed at stage '%s':\n%s", stage_name, traceback.format_exc())
                st.error(f"Pipeline error in **{stage_name}**. Check logs for details.")
                return

        progress.progress(1.0, text="Pipeline complete ✓")
        logger.info("Pipeline finished — PDF: %s | Excel: %s", deal.output_pdf_path, deal.output_xlsx_path)

        # ── Download buttons ──────────────────────────────────────────
        col1, col2 = st.columns(2)

        if deal.output_pdf_path and Path(deal.output_pdf_path).exists():
            pdf_bytes = Path(deal.output_pdf_path).read_bytes()
            col1.download_button(
                label="Download PDF Report",
                data=pdf_bytes,
                file_name=Path(deal.output_pdf_path).name,
                mime="application/pdf",
            )
        else:
            col1.warning("PDF report was not generated.")

        if deal.output_xlsx_path and Path(deal.output_xlsx_path).exists():
            xlsx_bytes = Path(deal.output_xlsx_path).read_bytes()
            col2.download_button(
                label="Download Excel Model",
                data=xlsx_bytes,
                file_name=Path(deal.output_xlsx_path).name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        else:
            col2.warning("Excel model was not generated.")

    except Exception:
        logger.error("Unexpected pipeline error:\n%s", traceback.format_exc())
        st.error("An unexpected error occurred. Check logs for details.")


# ── Helper: save uploaded file to temp path ───────────────────────────────

def _save_upload(uploaded_file) -> str | None:
    """Write a Streamlit UploadedFile to a temp file, return the path."""
    if uploaded_file is None:
        return None
    suffix = Path(uploaded_file.name).suffix
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(uploaded_file.getvalue())
    tmp.close()
    return tmp.name


# ── Screen 1: New Underwrite ──────────────────────────────────────────────

def _render_screen_new_underwrite() -> None:
    st.header("New Underwrite")

    st.text_input("Property Address", key="address", placeholder="123 Main St, Philadelphia, PA 19103")

    col1, col2 = st.columns(2)
    col1.selectbox(
        "Asset Type",
        options=[e.value for e in AssetType],
        key="asset_type",
    )
    col2.selectbox(
        "Investment Strategy",
        options=[e.value for e in InvestmentStrategy],
        key="investment_strategy",
    )

    st.text_area("Deal Description (optional)", key="deal_description", height=100)

    st.subheader("Document Uploads")
    om_file = st.file_uploader("Offering Memorandum (PDF)", type=["pdf"], key="_om_upload")
    rr_file = st.file_uploader("Rent Roll (PDF)", type=["pdf"], key="_rr_upload")
    fin_file = st.file_uploader("Financial Statements (PDF)", type=["pdf"], key="_fin_upload")

    # Persist temp paths in session_state when files are uploaded
    if om_file is not None:
        st.session_state["om_pdf_path"] = _save_upload(om_file)
    if rr_file is not None:
        st.session_state["rent_roll_pdf_path"] = _save_upload(rr_file)
    if fin_file is not None:
        st.session_state["financials_pdf_path"] = _save_upload(fin_file)


# ── Screen 2: Assumptions ────────────────────────────────────────────────

def _render_screen_assumptions() -> None:
    st.header("Assumptions")

    # ── Property & Acquisition ────────────────────────────────────
    st.subheader("Property & Acquisition")
    c1, c2 = st.columns(2)
    c1.number_input("Purchase Price ($)", min_value=0.0, step=10000.0, format="%.2f", key="purchase_price")
    c2.number_input("Number of Units", min_value=0, step=1, key="num_units")

    c3, c4 = st.columns(2)
    c3.number_input("Gross Building Area (SF)", min_value=0.0, step=100.0, format="%.0f", key="gba_sf")
    c4.number_input("Year Built", min_value=1800, max_value=2030, step=1, key="year_built", value=1960)

    # ── Hold & Financing ──────────────────────────────────────────
    st.subheader("Hold Period & Financing")
    c5, c6, c7 = st.columns(3)
    c5.number_input("Hold Period (years)", min_value=1, max_value=30, step=1, key="hold_period", value=10)
    c6.number_input("LTV %", min_value=0.0, max_value=1.0, step=0.01, format="%.2f", key="ltv_pct", value=0.70)
    c7.number_input("Interest Rate %", min_value=0.0, max_value=1.0, step=0.001, format="%.4f", key="interest_rate", value=0.065)

    c8, c9 = st.columns(2)
    c8.number_input("Amortization (years)", min_value=0, max_value=40, step=1, key="amort_years", value=30)
    c9.number_input("Target LP IRR %", min_value=0.0, max_value=1.0, step=0.01, format="%.2f", key="target_lp_irr", value=0.15)

    # ── Income & Growth ───────────────────────────────────────────
    st.subheader("Income & Growth")
    c10, c11 = st.columns(2)
    c10.number_input("Monthly Gross Rent — Year 1 ($)", min_value=0.0, step=500.0, format="%.2f", key="monthly_gross_rent")
    c11.number_input("Vacancy Rate %", min_value=0.0, max_value=1.0, step=0.005, format="%.3f", key="vacancy_rate", value=0.075)

    c12, c13 = st.columns(2)
    c12.number_input("Annual Rent Growth %", min_value=0.0, max_value=1.0, step=0.005, format="%.3f", key="annual_rent_growth", value=0.03)
    c13.number_input("Expense Growth %", min_value=0.0, max_value=1.0, step=0.005, format="%.3f", key="expense_growth_rate", value=0.03)

    # ── Expenses ──────────────────────────────────────────────────
    st.subheader("Operating Expenses")
    c14, c15, c16 = st.columns(3)
    c14.number_input("Management Fee %", min_value=0.0, max_value=1.0, step=0.01, format="%.2f", key="mgmt_fee_pct", value=0.06)
    c15.number_input("RE Taxes (annual $)", min_value=0.0, step=1000.0, format="%.2f", key="re_taxes", value=45000.0)
    c16.number_input("Insurance (annual $)", min_value=0.0, step=500.0, format="%.2f", key="insurance", value=18000.0)

    # ── Exit ──────────────────────────────────────────────────────
    st.subheader("Exit")
    st.number_input("Exit Cap Rate %", min_value=0.0, max_value=1.0, step=0.005, format="%.3f", key="exit_cap_rate", value=0.07)


# ── Session state defaults ───────────────────────────────────────────────

def _init_session_state() -> None:
    """Initialize all session-state keys with default values if absent."""
    defaults = {
        "address": "",
        "asset_type": "Multifamily",
        "investment_strategy": "stabilized",
        "deal_description": "",
        "purchase_price": 0.0,
        "num_units": 0,
        "gba_sf": 0.0,
        "year_built": 1960,
        "hold_period": 10,
        "ltv_pct": 0.70,
        "interest_rate": 0.065,
        "amort_years": 30,
        "target_lp_irr": 0.15,
        "monthly_gross_rent": 0.0,
        "vacancy_rate": 0.075,
        "annual_rent_growth": 0.03,
        "expense_growth_rate": 0.03,
        "exit_cap_rate": 0.07,
        "mgmt_fee_pct": 0.06,
        "re_taxes": 45000.0,
        "insurance": 18000.0,
        "om_pdf_path": None,
        "rent_roll_pdf_path": None,
        "financials_pdf_path": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


# ── Streamlit page ────────────────────────────────────────────────────────

def main() -> None:
    st.set_page_config(page_title="DealDesk CRE Underwriting", layout="wide")
    st.title("DealDesk CRE Underwriting")

    _init_session_state()

    # Sidebar navigation
    screen = st.sidebar.radio(
        "Navigation",
        options=["1 — New Underwrite", "2 — Assumptions", "3 — Run Pipeline"],
        index=0,
    )

    if screen == "1 — New Underwrite":
        _render_screen_new_underwrite()

    elif screen == "2 — Assumptions":
        _render_screen_assumptions()

    elif screen == "3 — Run Pipeline":
        st.header("Run Pipeline")

        # Show a summary of key inputs before running
        ss = st.session_state
        addr = ss.get("address", "")
        price = ss.get("purchase_price", 0)
        strategy = ss.get("investment_strategy", "stabilized")

        if addr:
            st.markdown(f"**Property:** {addr}")
            st.markdown(f"**Strategy:** {strategy} &nbsp;|&nbsp; **Purchase Price:** ${price:,.0f}")
        else:
            st.warning("Go to Screen 1 to enter a property address before running.")

        if st.button("Run Pipeline", type="primary", disabled=not addr):
            run_pipeline()


if __name__ == "__main__":
    main()
