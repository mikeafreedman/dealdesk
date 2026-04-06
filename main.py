"""
main.py — DealDesk CRE Underwriting Pipeline Orchestrator & Streamlit Entry Point
==================================================================================
Runs the full pipeline in sequence:
    extractor → deal_data → market → risk → financials → excel_builder → word_builder

Usage:  streamlit run main.py
"""

import logging
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


# ── Streamlit page ────────────────────────────────────────────────────────

def main() -> None:
    st.set_page_config(page_title="DealDesk CRE Underwriting", layout="wide")
    st.title("DealDesk CRE Underwriting")

    if st.button("Run Pipeline", type="primary"):
        run_pipeline()


if __name__ == "__main__":
    main()
