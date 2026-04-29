"""
smoke_test_s5.py — Session 5 CP4 end-to-end smoke test
======================================================

Renders Belmont (Deal B) and Indian-Queen-style (Deal C) PDFs end-to-end
through the production pipeline (financials → context_builder → Jinja2 →
Playwright), bypassing only the Sonnet narrative-generation step
(generate_narratives) which would require live API calls. Uses synthetic
zoning-overhaul fields per Phase-2 decision G16-b (matches the CP1/CP2/CP3
test pattern).

Asserts (gate criteria from kickoff §7, items 10-13 — corrected from §8 to §06
per the CP3 section-numbering fix):
  - #10  Belmont PDF generates without error (file size > 100KB)
  - #11  Indian Queen PDF generates without error (file size > 100KB)
  - #12  Belmont PDF §06 contains the text "Nonconforming"
  - #13  Indian Queen PDF §06 contains at least 3 scenario card blocks
  - Page count within ±3 of pre-S5 baselines (Belmont=26, Indian Queen=27)

The pre-S5 baselines were established by .cp4_baseline.py (one-shot
measurement script, gitignored): Belmont 26 pages, Indian Queen 27 pages.
Post-S5 page counts at the time of CP4: Belmont 27, Indian Queen 28
(+1 each — well within the ±3 tolerance).

Indian Queen note: the upstream fixture has minimal financial data (no
extracted_docs, no rent assumptions). Pre-existing limitation, not S5.
Smoke test substrate: load Belmont, overlay Indian Queen's deal-identity
fields (address, zoning, encumbrances, deal_id) plus 3 synthetic Indian
Queen scenarios. The PDF visually reads as Indian Queen for §06 rendering
purposes; financial numbers are Belmont's. This is documented synthetic
padding consistent with the CP1-CP3 test pattern.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import fitz  # PyMuPDF
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
TEMPLATE_NAME = "report_template.html"
CSS_NAME = "report.css"
BELMONT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "zoning_overhaul_session_3_fixture_belmont.json"
INDIAN_QUEEN_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "zoning_overhaul_session_3_fixture_indian_queen.json"
SMOKE_OUTPUTS = REPO_ROOT / "outputs" / "_s5_smoke"

# Pre-S5 baselines captured by .cp4_baseline.py (committed values used by
# the ±3 gate). If the §06 layout changes substantially, regenerate via
# the baseline script and update these constants.
BASELINE_BELMONT_PAGES = 26
BASELINE_INDIAN_QUEEN_PAGES = 27
PAGE_TOLERANCE = 3


# ── Synthetic-deal builders (Phase-2 decision G16-b pattern) ────────────────

def _load_belmont_with_synth():
    """Belmont with synthetic LEGAL_NONCONFORMING_USE conformity, 2 scenarios,
    use_flexibility_score=2 zoning_extensions. Same shape as
    test_context_s5.py and test_template_render_s5.py."""
    from models.models import (
        ConfidenceLevel, ConformityAssessment, ConformityStatus, DealData,
        DevelopmentScenario, GrandfatheringStatus, NonconformityItem,
        NonconformityType, ScenarioVerdict, ZoningExtensions, ZoningPathway,
        ZoningPathwayType,
    )
    deal = DealData.model_validate(json.loads(BELMONT_FIXTURE.read_text(encoding="utf-8")))
    deal.conformity_assessment = ConformityAssessment(
        status=ConformityStatus.LEGAL_NONCONFORMING_USE,
        confidence=ConfidenceLevel.HIGH,
        confidence_reasons=["RSD-3 permits SF/2F by-right; 36-unit MF predates current code"],
        nonconformity_details=[NonconformityItem(
            nonconformity_type=NonconformityType.USE,
            standard_description="Use type",
            permitted_value="Single-family / Two-family",
            actual_value="36-unit Multifamily",
            magnitude_description="Use category mismatch",
        )],
        grandfathering_status=GrandfatheringStatus(
            is_presumed_grandfathered=True,
            basis="Built pre-1978; continuous MF use presumed",
        ),
        risk_summary="Legal nonconforming MF use carries grandfathering verification risk.",
    )
    deal.scenarios = [
        DevelopmentScenario(
            scenario_id="stabilized_hold", rank=1,
            scenario_name="Stabilized Hold (Legal Nonconforming)",
            business_thesis="Hold at current 36 units; verify grandfathering pre-close.",
            verdict=ScenarioVerdict.PREFERRED,
            unit_count=36, operating_strategy="stabilized_hold",
            zoning_pathway=ZoningPathway(pathway_type=ZoningPathwayType.BY_RIGHT),
        ),
        DevelopmentScenario(
            scenario_id="conversion_variance", rank=2,
            scenario_name="Conversion via Variance",
            business_thesis="Pursue use variance to legitimize MF density expansion.",
            verdict=ScenarioVerdict.ALTERNATE,
            unit_count=42, operating_strategy="value_add_renovation",
            zoning_pathway=ZoningPathway(pathway_type=ZoningPathwayType.VARIANCE),
        ),
    ]
    deal.zoning_extensions = ZoningExtensions(
        use_flexibility_score=2,
        use_flexibility_explanation="RSD-3 is restrictive.",
        cross_scenario_recommendation="Stabilized hold preserves legal nonconforming rights.",
        preferred_scenario_id="stabilized_hold",
    )
    return deal


def _load_indian_queen_with_synth():
    """Belmont substrate + Indian Queen identity overlay (address, zoning,
    encumbrances, deal_id) + 3 synthetic Indian Queen scenarios. See module
    docstring for rationale on the Belmont substrate.
    """
    from models.models import (
        ConfidenceLevel, ConformityAssessment, ConformityStatus, DealData,
        DevelopmentScenario, GrandfatheringStatus, NonconformityItem,
        NonconformityType, ScenarioVerdict, ZoningExtensions, ZoningPathway,
        ZoningPathwayType,
    )
    iq_raw = json.loads(INDIAN_QUEEN_FIXTURE.read_text(encoding="utf-8"))
    belmont_raw = json.loads(BELMONT_FIXTURE.read_text(encoding="utf-8"))
    raw = dict(belmont_raw)
    raw["deal_id"] = iq_raw.get("deal_id", "session_3_test_fixture_indian_queen")
    raw["address"] = iq_raw["address"]
    raw["zoning"] = iq_raw["zoning"]
    raw["encumbrances"] = iq_raw.get("encumbrances", [])
    raw["deal_description"] = iq_raw.get("deal_description", raw.get("deal_description", ""))
    deal = DealData.model_validate(raw)

    deal.conformity_assessment = ConformityAssessment(
        status=ConformityStatus.LEGAL_NONCONFORMING_USE,
        confidence=ConfidenceLevel.HIGH,
        confidence_reasons=["Industrial use in residential RSA-1/RSA-5 split zoning."],
        nonconformity_details=[
            NonconformityItem(
                nonconformity_type=NonconformityType.USE,
                standard_description="Use type",
                permitted_value="Residential",
                actual_value="Industrial",
                magnitude_description="Use category mismatch",
            ),
            NonconformityItem(
                nonconformity_type=NonconformityType.LOT_AREA,
                standard_description="Minimum lot area",
                permitted_value="68,389 SF (combined RSA-1/RSA-5)",
                actual_value="68,389 SF",
                magnitude_description="Split-zoned parcel",
            ),
        ],
        grandfathering_status=GrandfatheringStatus(
            is_presumed_grandfathered=True,
            basis="Industrial use predates current zoning.",
        ),
        risk_summary="Industrial use in split RSA-1/RSA-5 with two recorded encumbrances.",
    )
    deal.scenarios = [
        DevelopmentScenario(
            scenario_id="scheme_c_95u", rank=1,
            scenario_name="Scheme C — 95 Units",
            business_thesis="95-unit multifamily residential build; preferred density.",
            verdict=ScenarioVerdict.PREFERRED,
            unit_count=95, operating_strategy="ground_up_development",
            zoning_pathway=ZoningPathway(pathway_type=ZoningPathwayType.VARIANCE),
        ),
        DevelopmentScenario(
            scenario_id="scheme_a_88u", rank=2,
            scenario_name="Scheme A — 88 Units",
            business_thesis="88-unit residential with reduced density.",
            verdict=ScenarioVerdict.ALTERNATE,
            unit_count=88, operating_strategy="ground_up_development",
            zoning_pathway=ZoningPathway(pathway_type=ZoningPathwayType.VARIANCE),
        ),
        DevelopmentScenario(
            scenario_id="scheme_b_84u", rank=3,
            scenario_name="Scheme B — 84 Units Mixed-Use",
            business_thesis="84-unit residential with ground-floor commercial.",
            verdict=ScenarioVerdict.ALTERNATE,
            unit_count=84, operating_strategy="ground_up_development",
            zoning_pathway=ZoningPathway(pathway_type=ZoningPathwayType.SPECIAL_EXCEPTION),
        ),
    ]
    deal.zoning_extensions = ZoningExtensions(
        use_flexibility_score=3,
        use_flexibility_explanation="Split-zoned parcel offers some development flexibility.",
        cross_scenario_recommendation="Scheme C 95-unit pursuit aligns with neighborhood density trajectory.",
        preferred_scenario_id="scheme_c_95u",
    )
    return deal


# ── Render path (no-LLM fork of report_builder.generate_report) ─────────────

def _render_pdf_no_llm(deal, pdf_path: Path) -> Path:
    """Mirror report_builder.generate_report() but skip the
    Sonnet-narrative-generation step. Fast, deterministic, no API."""
    from financials import run_financials
    run_financials(deal)

    from context_builder import build_context
    from report_builder import (
        _build_image_context, _fmt_currency, _fmt_multiple, _fmt_percent,
    )
    from config import WORD_TEMPLATES_DIR
    ctx = build_context(deal)
    ctx.update(_build_image_context(deal))

    from jinja2 import Environment, FileSystemLoader, select_autoescape
    env = Environment(
        loader=FileSystemLoader(str(WORD_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["currency"] = _fmt_currency
    env.filters["percent"] = _fmt_percent
    env.filters["multiple"] = _fmt_multiple
    template = env.get_template(TEMPLATE_NAME)
    html_content = template.render(**ctx)

    css_path = WORD_TEMPLATES_DIR / CSS_NAME
    css_text = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    html_content = re.sub(
        r'<link\s+rel="stylesheet"\s+href="report\.css"\s*/?>',
        f"<style>{css_text}</style>",
        html_content,
        count=1,
    )

    from playwright.sync_api import sync_playwright
    SMOKE_OUTPUTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html_content, wait_until="networkidle")
            page.pdf(
                path=str(pdf_path),
                format="Letter",
                print_background=True,
            )
        finally:
            browser.close()
    return pdf_path


# ── PyMuPDF helpers ─────────────────────────────────────────────────────────

def _pdf_page_count(pdf_path: Path) -> int:
    with fitz.open(str(pdf_path)) as doc:
        return doc.page_count


def _pdf_full_text(pdf_path: Path) -> str:
    with fitz.open(str(pdf_path)) as doc:
        return "\n".join(page.get_text() for page in doc)


# ── Module-scoped fixtures (render once, assert many) ────────────────────────

@pytest.fixture(scope="module")
def belmont_pdf() -> Path:
    deal = _load_belmont_with_synth()
    pdf_path = SMOKE_OUTPUTS / "belmont_smoke.pdf"
    return _render_pdf_no_llm(deal, pdf_path)


@pytest.fixture(scope="module")
def indian_queen_pdf() -> Path:
    deal = _load_indian_queen_with_synth()
    pdf_path = SMOKE_OUTPUTS / "indian_queen_smoke.pdf"
    return _render_pdf_no_llm(deal, pdf_path)


# ── Gate criterion #10 — Belmont PDF generates without error ─────────────────

def test_belmont_pdf_generates(belmont_pdf: Path) -> None:
    assert belmont_pdf.exists(), "Belmont PDF not produced"
    assert belmont_pdf.stat().st_size > 100_000, (
        f"Belmont PDF size {belmont_pdf.stat().st_size:,} bytes is below the 100KB threshold"
    )


# ── Gate criterion #11 — Indian Queen PDF generates without error ───────────

def test_indian_queen_pdf_generates(indian_queen_pdf: Path) -> None:
    assert indian_queen_pdf.exists(), "Indian Queen PDF not produced"
    assert indian_queen_pdf.stat().st_size > 100_000, (
        f"Indian Queen PDF size {indian_queen_pdf.stat().st_size:,} bytes is below the 100KB threshold"
    )


# ── Gate criterion #12 — Belmont PDF §06 contains "Nonconforming" ────────────

def test_belmont_pdf_contains_nonconforming(belmont_pdf: Path) -> None:
    """Per kickoff §7 #12, corrected to §06 (the actual zoning section).
    The synthetic Belmont has status=LEGAL_NONCONFORMING_USE → badge text
    should read 'Legal Nonconforming Use'. Lowercase substring search to
    catch any title-case / sentence-case variant."""
    text = _pdf_full_text(belmont_pdf)
    assert "Nonconforming" in text or "nonconforming" in text, (
        "Belmont PDF §06 must contain 'Nonconforming' from the badge text"
    )
    # Tighter assertion: the specific badge text from our synthetic setup.
    assert "Legal Nonconforming Use" in text, (
        "Belmont PDF should contain the formatted status text 'Legal Nonconforming Use'"
    )


# ── Gate criterion #13 — Indian Queen PDF contains ≥3 scenario card blocks ──

def test_indian_queen_pdf_has_three_scenario_cards(indian_queen_pdf: Path) -> None:
    """Per kickoff §7 #13. The synthetic Indian Queen has 3 scenarios:
    Scheme C (rank 1, PREFERRED), Scheme A (rank 2), Scheme B (rank 3).
    All 3 scenario_name strings must appear in extracted PDF text.

    PyMuPDF text extraction sometimes breaks scenario-name strings across
    multiple text fragments due to flex layout (e.g., 'Scheme B — 84 Units
    Mixed-Use' may extract as separate fragments 'Scheme', 'B', '—', '84',
    'Units', 'Mixed-Use'). Use unique multi-word fragments per scenario
    instead of the full scenario_name string.
    """
    text = _pdf_full_text(indian_queen_pdf)
    # Scheme A — 88 Units: extract via the unique unit count
    assert "88 Units" in text or "88-unit" in text, (
        "Scheme A (88 Units) scenario card content missing from PDF text"
    )
    # Scheme B — 84 Units Mixed-Use: extract via the unique 'Mixed-Use' marker
    assert "Mixed-Use" in text or "Mixed-\nUse" in text, (
        "Scheme B (Mixed-Use) scenario card content missing from PDF text"
    )
    # Scheme C — 95 Units (PREFERRED): extract via 95-unit marker
    assert "95-unit" in text or "95 Units" in text, (
        "Scheme C (95 Units) scenario card content missing from PDF text"
    )
    # Plus: 3 rank badges (1, 2, 3) and 3 pathway pills must all be present
    # The pathway pills uppercase to 'VARIANCE' and 'SPECIAL EXCEPTION'.
    assert text.count("VARIANCE") >= 2, "expected ≥2 VARIANCE pathway pills (Schemes A, C)"
    assert "SPECIAL EXCEPTION" in text, "expected SPECIAL EXCEPTION pill (Scheme B)"
    # 'PREFERRED' tab on the rank-1 card
    assert "PREFERRED" in text, "expected PREFERRED tab on rank-1 scenario card"


# ── Page count within ±3 of pre-S5 baseline ─────────────────────────────────

def test_belmont_page_count_within_tolerance(belmont_pdf: Path) -> None:
    pages = _pdf_page_count(belmont_pdf)
    delta = pages - BASELINE_BELMONT_PAGES
    assert abs(delta) <= PAGE_TOLERANCE, (
        f"Belmont page count {pages} drifts by {delta:+d} from "
        f"pre-S5 baseline {BASELINE_BELMONT_PAGES} (tolerance ±{PAGE_TOLERANCE})"
    )


def test_indian_queen_page_count_within_tolerance(indian_queen_pdf: Path) -> None:
    pages = _pdf_page_count(indian_queen_pdf)
    delta = pages - BASELINE_INDIAN_QUEEN_PAGES
    assert abs(delta) <= PAGE_TOLERANCE, (
        f"Indian Queen page count {pages} drifts by {delta:+d} from "
        f"pre-S5 baseline {BASELINE_INDIAN_QUEEN_PAGES} (tolerance ±{PAGE_TOLERANCE})"
    )


# ── Aggregate gate: all CP4 criteria pass + DD flag content visible ─────────

def test_cp4_gate_full_summary(belmont_pdf: Path, indian_queen_pdf: Path,
                                 capsys) -> None:
    """CP4 aggregate gate: all 5 §06 components visible in both PDFs, page
    counts within tolerance, file sizes acceptable. Prints a summary that
    pytest captures and surfaces in the gate output."""
    bel_text = _pdf_full_text(belmont_pdf)
    iq_text = _pdf_full_text(indian_queen_pdf)
    bel_pages = _pdf_page_count(belmont_pdf)
    iq_pages = _pdf_page_count(indian_queen_pdf)

    # Belmont expectations
    assert "Legal Nonconforming Use" in bel_text         # badge
    assert "RSD-3" in bel_text                            # zoning code in badge meta
    assert "Use type" in bel_text                         # dimension grid card label
    assert "36-unit Multifamily" in bel_text              # dimension card actual value
    assert "Stabilized Hold" in bel_text                  # preferred scenario name
    assert "Conversion via Variance" in bel_text          # alternate scenario name
    # The HBU synthesis label has CSS text-transform:uppercase so PyMuPDF
    # extracts the rendered (uppercase) form.
    assert "CROSS-SCENARIO SYNTHESIS" in bel_text or "Cross-Scenario Synthesis" in bel_text
    assert "2 / 5" in bel_text                            # score bar value
    assert "Zoning nonconformity" in bel_text             # DD flag title
    assert "Philadelphia" in bel_text                     # remediation municipality

    # Indian Queen expectations
    # Scenario detection uses unique fragments because flex layout fragments
    # extract awkwardly via PyMuPDF.
    assert "95-unit" in iq_text or "95 Units" in iq_text  # Scheme C (PREFERRED)
    assert "88-unit" in iq_text or "88 Units" in iq_text  # Scheme A
    assert "Mixed-Use" in iq_text or "Mixed-\nUse" in iq_text  # Scheme B
    assert "American Tower" in iq_text                    # encumbrance #1 grantee
    assert "SBC Tower Holdings" in iq_text                # encumbrance #2 grantee
    # Indian Queen substrate is Belmont, so the address/conformity work, but
    # the zoning code came from the IQ overlay → split-zone code is RSA-5.
    assert "RSA-5" in iq_text or "RSA-1" in iq_text       # split-zoning code

    # Page count within tolerance for both
    bel_delta = bel_pages - BASELINE_BELMONT_PAGES
    iq_delta = iq_pages - BASELINE_INDIAN_QUEEN_PAGES
    assert abs(bel_delta) <= PAGE_TOLERANCE
    assert abs(iq_delta) <= PAGE_TOLERANCE

    # Surface the page-count summary so the gate output shows the numbers
    print()
    print(f"  Belmont      : {bel_pages} pages (baseline {BASELINE_BELMONT_PAGES}, delta {bel_delta:+d}), "
          f"{belmont_pdf.stat().st_size:,} bytes")
    print(f"  Indian Queen : {iq_pages} pages (baseline {BASELINE_INDIAN_QUEEN_PAGES}, delta {iq_delta:+d}), "
          f"{indian_queen_pdf.stat().st_size:,} bytes")
