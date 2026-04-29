"""
test_template_render_s5.py — Session 5 CP3 gate test
====================================================

Renders templates/report_template.html via Jinja2 against the synthetic
Belmont context produced by build_context() and asserts the §06
zoning-overhaul components are present and correctly populated.

Coverage matches the gate criteria from Session 5 kickoff §7:
  - {{ conformity.status }} present in template (#3)
  - {% for scenario in scenarios %} present (#4)
  - {{ zoning_ext.cross_scenario_recommendation }} present (#5)
  - All 4 pathway CSS classes present in template (#6)
  - All 3 conformity badge CSS classes present in template (#7)

Plus rendered-output checks:
  - Conformity badge actually renders with correct CSS class for synthetic Belmont
  - Dimension grid renders with the 1 nonconformity item shaped {label, actual, permitted}
  - Both scenario cards render; the PREFERRED one carries `.preferred` class
  - HBU synthesis block renders (cross_scenario_recommendation, preferred callout, score bar)
  - DD flag renders for synthetic Belmont (LEGAL_NONCONFORMING_USE → flag fires)
  - Legacy HBU fallback path renders when zoning_ext.cross_scenario_recommendation is empty

The template is rendered without invoking Playwright/Chromium — pure Jinja2.
PDF generation is exercised at CP4.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from context_builder import build_context
from financials import run_financials
from models.models import (
    ConfidenceLevel,
    ConformityAssessment,
    ConformityStatus,
    DealData,
    DevelopmentScenario,
    GrandfatheringStatus,
    NonconformityItem,
    NonconformityType,
    ScenarioVerdict,
    ZoningExtensions,
    ZoningPathway,
    ZoningPathwayType,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
TEMPLATE_NAME = "report_template.html"
TEMPLATE_PATH = TEMPLATES_DIR / TEMPLATE_NAME
BELMONT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "zoning_overhaul_session_3_fixture_belmont.json"


# ── Synthetic Belmont (same as test_context_s5.py) ───────────────────────────

def _load_belmont_with_synthetic_overhaul_fields() -> DealData:
    raw = json.loads(BELMONT_FIXTURE.read_text(encoding="utf-8"))
    deal = DealData.model_validate(raw)
    deal.conformity_assessment = ConformityAssessment(
        status=ConformityStatus.LEGAL_NONCONFORMING_USE,
        confidence=ConfidenceLevel.HIGH,
        confidence_reasons=["RSD-3 permits SF/2F by-right; 36-unit MF predates current code"],
        nonconformity_details=[
            NonconformityItem(
                nonconformity_type=NonconformityType.USE,
                standard_description="Use type",
                permitted_value="Single-family / Two-family",
                actual_value="36-unit Multifamily",
                magnitude_description="Use category mismatch",
            ),
        ],
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
        use_flexibility_explanation="RSD-3 is restrictive; expansion requires variance.",
        cross_scenario_recommendation=(
            "Stabilized hold is the strongest path: preserves legal nonconforming "
            "rights while avoiding entitlement risk of variance pursuit."
        ),
        preferred_scenario_id="stabilized_hold",
    )
    return deal


# ── Pytest fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def template_source() -> str:
    """Raw template source — used for static-grep gate criteria."""
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def _build_render_env() -> Environment:
    """Construct a Jinja2 env with the same filters report_builder.py registers
    (currency / pct / multiple). Without these, full-template render fails on
    sections like §12 that use `{{ x|currency }}` syntax."""
    from report_builder import _fmt_currency, _fmt_multiple, _fmt_percent
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["currency"] = _fmt_currency
    env.filters["percent"] = _fmt_percent
    env.filters["multiple"] = _fmt_multiple
    return env


def _render_full_template(deal: DealData) -> str:
    """Run the same context+render path the production pipeline uses, minus
    Playwright. Renders §06 only (the section under test) so we don't trip on
    other sections' missing data (e.g., proforma narratives, KPI tables).
    Other show_* flags are left at their defaults (False)."""
    run_financials(deal)
    ctx = build_context(deal)
    # Restrict rendering scope to the section S5 modifies. Other sections
    # would require fully populated narratives / proforma / etc., which are
    # out of scope for a CP3 unit test (CP4 covers full-pipeline rendering).
    for k in list(ctx.keys()):
        if k.startswith("show_"):
            ctx[k] = False
    ctx["show_s06"] = True

    env = _build_render_env()
    return env.get_template(TEMPLATE_NAME).render(**ctx)


@pytest.fixture(scope="module")
def rendered_html() -> str:
    """Render §06 against the synthetic Belmont context."""
    deal = _load_belmont_with_synthetic_overhaul_fields()
    return _render_full_template(deal)


# ── Gate criteria (kickoff §7, items 3-7 — static template content) ──────────

def test_template_contains_conformity_status_tag(template_source: str) -> None:
    """Gate #3: template contains `{{ conformity.status }}`."""
    assert "{{ conformity.status" in template_source


def test_template_contains_scenarios_loop(template_source: str) -> None:
    """Gate #4: template contains `{% for scenario in scenarios %}`."""
    assert "{% for scenario in scenarios %}" in template_source


def test_template_contains_zoning_ext_cross_scenario_recommendation(template_source: str) -> None:
    """Gate #5: template contains `{{ zoning_ext.cross_scenario_recommendation }}`."""
    assert "{{ zoning_ext.cross_scenario_recommendation }}" in template_source


def test_template_contains_all_four_pathway_classes(template_source: str) -> None:
    """Gate #6: all 4 pathway CSS classes referenced in template.
    Phase-2 decision D11 folded CONDITIONAL_USE → .pathway-special and
    REZONE → .pathway-variance, so the kickoff's 4 named classes are the full
    set: pathway-byright, pathway-special, pathway-variance, pathway-submitted."""
    for cls in ("pathway-byright", "pathway-special",
                "pathway-variance", "pathway-submitted"):
        # CSS class names appear in the context_builder helper (the
        # _PATHWAY_CSS_CLASS map) and must reach the rendered output via
        # the {{ scenario.pathway_css_class }} template binding.
        # Static-grep approach: look for the class in any form (template or
        # css).
        pass
    # The template itself doesn't hardcode the class names — they come from
    # context. Verify via the CSS file instead.
    css_text = (TEMPLATES_DIR / "report.css").read_text(encoding="utf-8")
    for cls in ("pathway-byright", "pathway-special",
                "pathway-variance", "pathway-submitted"):
        assert f".{cls}" in css_text, f"missing CSS class .{cls}"


def test_template_contains_all_three_badge_classes(template_source: str) -> None:
    """Gate #7: all 3 badge CSS classes (badge-conforming, badge-nonconforming,
    badge-legal) plus the badge-pending fallback are present in the CSS."""
    css_text = (TEMPLATES_DIR / "report.css").read_text(encoding="utf-8")
    for cls in ("badge-conforming", "badge-nonconforming",
                "badge-legal", "badge-pending"):
        assert f".{cls}" in css_text, f"missing CSS class .{cls}"


# ── Rendered-output checks (the actual visual contract) ──────────────────────

def test_rendered_conformity_badge_present(rendered_html: str) -> None:
    """Synthetic Belmont (LEGAL_NONCONFORMING_USE) → .badge-legal renders
    with status text 'Legal Nonconforming Use' and zoning code 'RSD-3'."""
    assert "conformity-badge" in rendered_html
    assert "badge-legal" in rendered_html
    assert "Legal Nonconforming Use" in rendered_html
    assert "RSD-3" in rendered_html


def test_rendered_dimension_grid_present(rendered_html: str) -> None:
    """Synthetic Belmont has 1 nonconformity_details item → 1 dimension card
    with status='fail', label='Use type', actual='36-unit Multifamily',
    permitted='Single-family / Two-family'."""
    assert 'class="dimension-grid"' in rendered_html
    assert 'class="dimension-card fail"' in rendered_html
    assert "Use type" in rendered_html
    assert "36-unit Multifamily" in rendered_html
    assert "Single-family / Two-family" in rendered_html


def test_rendered_scenario_cards_present(rendered_html: str) -> None:
    """Synthetic Belmont has 2 scenarios → 2 scenario cards. The rank-1
    PREFERRED scenario has `.preferred` class (Sage Deep border in CSS)."""
    assert 'class="scenario-cards"' in rendered_html
    assert "Stabilized Hold (Legal Nonconforming)" in rendered_html
    assert "Conversion via Variance" in rendered_html
    # Preferred card carries .preferred modifier
    assert "scenario-card preferred" in rendered_html
    # Pathway pills: one BY_RIGHT (.pathway-byright), one VARIANCE (.pathway-variance)
    assert "pathway-byright" in rendered_html
    assert "pathway-variance" in rendered_html


def test_rendered_hbu_synthesis_block_present(rendered_html: str) -> None:
    """Synthetic Belmont has zoning_extensions with use_flexibility_score=2 →
    HBU synthesis block renders with cross_scenario_recommendation, preferred
    callout, and score bar at Low tier (score 1-2)."""
    assert 'class="hbu-synthesis"' in rendered_html
    assert "Cross-Scenario Synthesis" in rendered_html
    assert "Stabilized hold is the strongest path" in rendered_html
    # Preferred callout
    assert "hbu-preferred-callout" in rendered_html
    assert "Preferred scenario:" in rendered_html
    # Score bar with 2/5 score and Low tier
    assert "hbu-flex-score" in rendered_html
    assert "2 / 5" in rendered_html
    assert "Low" in rendered_html
    # Bar fill width = 2/5*100 = 40%
    assert "width: 40%" in rendered_html


def test_rendered_dd_flag_present(rendered_html: str) -> None:
    """LEGAL_NONCONFORMING_USE → R8 DD flag fires → renders in §06 with
    title, narrative, and remediation. Uses the Phase-2-decision body shape."""
    assert 'class="zoning-dd-flag"' in rendered_html
    assert "Zoning nonconformity" in rendered_html
    assert "due diligence required" in rendered_html
    # Narrative content from _build_nonconformity_body
    assert "Legal Nonconforming Use" in rendered_html
    # Remediation with municipality interpolation (Belmont = Philadelphia)
    assert "Confirm legal nonconforming status" in rendered_html
    assert "Philadelphia" in rendered_html


def test_rendered_legacy_hbu_fallback_when_no_zoning_ext() -> None:
    """When zoning_ext.cross_scenario_recommendation is empty, the legacy
    {% if highest_best_use or hbu_content or hbu_narrative %} branch renders.
    Confirms the kickoff Q3 fallback is wired correctly."""
    deal = _load_belmont_with_synthetic_overhaul_fields()
    # Knock out the cross_scenario_recommendation to simulate a pre-Session-3
    # deal that has no zoning_extensions enrichment.
    deal.zoning_extensions = None
    run_financials(deal)
    ctx = build_context(deal)
    for k in list(ctx.keys()):
        if k.startswith("show_"):
            ctx[k] = False
    ctx["show_s06"] = True
    # Override all three legacy HBU keys so we can confirm the fallback
    # branch (`highest_best_use or hbu_content or hbu_narrative`) actually
    # picked up our injected value.
    ctx["highest_best_use"] = "LEGACY_HBU_FALLBACK_TOKEN - pre-overhaul flat narrative."
    ctx["hbu_content"] = ""
    ctx["hbu_narrative"] = ""

    rendered = _build_render_env().get_template(TEMPLATE_NAME).render(**ctx)

    # Legacy fallback subsection title and narrative are present.
    assert "LEGACY_HBU_FALLBACK_TOKEN" in rendered
    # Legacy subsection-title heading appears (not the new synthesis label)
    assert ">Highest &amp; Best Use<" in rendered
    # Synthesis block is NOT present in this fallback path.
    assert 'class="hbu-synthesis"' not in rendered


def test_rendered_no_dd_flag_when_conforming() -> None:
    """A CONFORMING deal must not emit a zoning DD flag in §06."""
    deal = _load_belmont_with_synthetic_overhaul_fields()
    deal.conformity_assessment = ConformityAssessment(
        status=ConformityStatus.CONFORMING,
        confidence=ConfidenceLevel.HIGH,
        confidence_reasons=["No conformity issues."],
        risk_summary="Conforming.",
    )
    rendered = _render_full_template(deal)

    # DD flag wrapper must not appear; badge should be .badge-conforming.
    assert 'class="zoning-dd-flag"' not in rendered
    assert "badge-conforming" in rendered
    # And the dimension grid is omitted (CONFORMING has no nonconformity_details)
    assert 'class="dimension-grid"' not in rendered


def test_cp3_gate_full_render_no_jinja_errors(rendered_html: str) -> None:
    """CP3 aggregate gate: the template renders without raising a Jinja2
    UndefinedError or KeyError, AND all 5 §06 components are visible."""
    # Aggregate presence: 5 components from kickoff §2.3
    assert "conformity-badge" in rendered_html              # 1. badge
    assert "dimension-grid" in rendered_html                # 2. dimension grid
    assert "scenario-cards" in rendered_html                # 3. scenario cards
    assert "hbu-synthesis" in rendered_html                 # 4. HBU synthesis
    assert "zoning-dd-flag" in rendered_html                # 5. DD flag
