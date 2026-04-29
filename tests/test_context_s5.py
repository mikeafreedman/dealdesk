"""
test_context_s5.py — Session 5 CP1 gate test
============================================

Asserts that build_context() exposes the 10 zoning-overhaul context keys
prescribed in Session 5 kickoff §3.1 (with the 11th key,
zoning_nonconformity_flag, deferred to CP2 per the post-discovery realignment).

Per Phase-2 mapping decisions:
  - conformity is a dict with status / zoning_code / district_name /
    dimensions / badge_css_class (the badge_css_class is a derived helper).
  - dimensions list is built from ConformityAssessment.nonconformity_details
    with shape {label, actual, permitted, status: 'fail'}.
  - zoning_ext is a dict with cross_scenario_recommendation /
    preferred_scenario_id / use_flexibility_score (flat int 1-5, no .score
    sub-attribute — the kickoff's UseFlexibilityScore type was a regression).
  - scenarios is the list[DevelopmentScenario] with each scenario carrying a
    derived pathway_css_class attribute attached by the context builder.
  - preferred_scenario is the rank-1 PREFERRED scenario or None.

Test approach (Phase-2 decision G16, option b): load the Belmont input fixture
and synthetically populate the zoning-overhaul fields with minimal-but-valid
data, then run financials.run_financials(deal) followed by build_context(deal).
Asserts the 10 keys exist and have the expected shapes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from context_builder import build_context
from financials import run_financials
from models.models import (
    ConfidenceLevel,
    ConformityAssessment,
    ConformityStatus,
    DdFlag,
    DdFlagColor,
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
BELMONT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "zoning_overhaul_session_3_fixture_belmont.json"


def _load_belmont_with_synthetic_overhaul_fields() -> DealData:
    """Load the Belmont input fixture and attach minimal-but-valid
    ConformityAssessment / scenarios / ZoningExtensions for CP1 testing.

    Per Phase-2 mapping decision G16: option (b) — synthetic-helper fixture
    pattern matching the Session 3 gate-script approach. Avoids the LLM-cost
    of running the full synthesis chain for a unit test.
    """
    raw = json.loads(BELMONT_FIXTURE.read_text(encoding="utf-8"))
    deal = DealData.model_validate(raw)

    deal.conformity_assessment = ConformityAssessment(
        status=ConformityStatus.LEGAL_NONCONFORMING_USE,
        confidence=ConfidenceLevel.HIGH,
        confidence_reasons=[
            "RSD-3 permits SF/2F by-right; 36-unit MF predates current code",
        ],
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
            loss_triggers=[
                "Substantial improvement >50%",
                "Change of use",
                "Abandonment >12 months",
            ],
        ),
        risk_summary=(
            "Legal nonconforming MF use carries grandfathering verification risk; "
            "any substantial improvement >50% would void protection."
        ),
        diligence_actions_required=["Pull L&I rental license history"],
    )

    deal.scenarios = [
        DevelopmentScenario(
            scenario_id="stabilized_hold",
            rank=1,
            scenario_name="Stabilized Hold (Legal Nonconforming)",
            business_thesis=(
                "Hold at current 36 units; verify grandfathering pre-close; "
                "modest renovation under the 50% threshold preserves protection."
            ),
            verdict=ScenarioVerdict.PREFERRED,
            unit_count=36,
            operating_strategy="stabilized_hold",
            zoning_pathway=ZoningPathway(
                pathway_type=ZoningPathwayType.BY_RIGHT,
                rationale="Continuing existing legal nonconforming use.",
            ),
        ),
        DevelopmentScenario(
            scenario_id="conversion_variance",
            rank=2,
            scenario_name="Conversion via Variance",
            business_thesis=(
                "Pursue use variance to legitimize and expand permitted unit count."
            ),
            verdict=ScenarioVerdict.ALTERNATE,
            unit_count=42,
            operating_strategy="value_add_renovation",
            zoning_pathway=ZoningPathway(
                pathway_type=ZoningPathwayType.VARIANCE,
                rationale="Use variance required to legitimize MF density expansion.",
            ),
        ),
    ]

    deal.zoning_extensions = ZoningExtensions(
        use_flexibility_score=2,
        use_flexibility_explanation=(
            "RSD-3 is restrictive; expansion requires discretionary approval."
        ),
        cross_scenario_recommendation=(
            "Stabilized hold is the strongest path: preserves legal nonconforming "
            "rights while avoiding entitlement risk of variance pursuit."
        ),
        preferred_scenario_id="stabilized_hold",
    )

    return deal


@pytest.fixture(scope="module")
def deal_b_fixture() -> DealData:
    """Belmont (Deal B) loaded with synthetic zoning-overhaul fields,
    then passed through run_financials() so financial_outputs is populated.
    """
    deal = _load_belmont_with_synthetic_overhaul_fields()
    run_financials(deal)
    return deal


@pytest.fixture(scope="module")
def context(deal_b_fixture: DealData) -> dict:
    """Run build_context once and share the result across assertions."""
    return build_context(deal_b_fixture)


# ── CP1 gate: 10 keys present, shaped correctly ──────────────────────────────

def test_conformity_key_present(context: dict) -> None:
    """KEY 1: ctx['conformity'] is a dict (never raw model, never None)."""
    assert "conformity" in context, "ctx must contain 'conformity'"
    assert isinstance(context["conformity"], dict), \
        "ctx['conformity'] must be a dict for safe template access"


def test_conformity_status_accessible(context: dict) -> None:
    """KEY 2: ctx['conformity']['status'] is a string ConformityStatus value."""
    status = context["conformity"]["status"]
    assert isinstance(status, str)
    assert status == "LEGAL_NONCONFORMING_USE", \
        f"expected LEGAL_NONCONFORMING_USE for synthetic Belmont, got {status!r}"


def test_conformity_zoning_code_accessible(context: dict) -> None:
    """KEY 3: ctx['conformity']['zoning_code'] sourced from deal.zoning.zoning_code
    (Phase-2 mapping decision B4 — primary path doesn't exist on
    ConformityAssessment, so we use the fallback as the actual source)."""
    code = context["conformity"]["zoning_code"]
    assert code == "RSD-3", f"expected RSD-3 from Belmont fixture, got {code!r}"


def test_conformity_district_name_accessible(context: dict) -> None:
    """KEY 4: ctx['conformity']['district_name'] sourced from
    deal.zoning.zoning_district (Phase-2 mapping decision B5 — field rename)."""
    name = context["conformity"]["district_name"]
    assert isinstance(name, str)
    assert "Residential" in name, \
        f"expected Belmont's RSD-3 district name to contain 'Residential', got {name!r}"


def test_conformity_dimensions_shape(context: dict) -> None:
    """KEY 5: ctx['conformity']['dimensions'] is a list of dicts derived from
    nonconformity_details (Phase-2 mapping decision B6 — schema has no
    'dimensions' field; we build {label, actual, permitted, status} per item)."""
    dims = context["conformity"]["dimensions"]
    assert isinstance(dims, list)
    assert len(dims) == 1, \
        f"synthetic Belmont has 1 nonconformity_details item, got {len(dims)}"
    d = dims[0]
    assert set(d.keys()) == {"label", "actual", "permitted", "status"}, \
        f"dimension dict shape mismatch: {sorted(d.keys())}"
    assert d["status"] == "fail"
    assert d["label"] == "Use type"
    assert d["actual"] == "36-unit Multifamily"
    assert d["permitted"] == "Single-family / Two-family"


def test_scenarios_present(context: dict) -> None:
    """KEY 6: ctx['scenarios'] is a list of DevelopmentScenario objects."""
    scenarios = context["scenarios"]
    assert isinstance(scenarios, list)
    assert len(scenarios) == 2
    assert all(isinstance(s, DevelopmentScenario) for s in scenarios)


def test_preferred_scenario_present(context: dict) -> None:
    """KEY 7: ctx['preferred_scenario'] is the rank-1 PREFERRED scenario."""
    pref = context["preferred_scenario"]
    assert pref is not None
    assert isinstance(pref, DevelopmentScenario)
    assert pref.verdict == ScenarioVerdict.PREFERRED
    assert pref.scenario_id == "stabilized_hold"
    assert pref.rank == 1


def test_zoning_ext_key_present(context: dict) -> None:
    """KEY 8: ctx['zoning_ext'] is a dict (never raw model, never None)."""
    assert "zoning_ext" in context
    assert isinstance(context["zoning_ext"], dict)


def test_zoning_ext_cross_scenario_recommendation_accessible(context: dict) -> None:
    """KEY 9: ctx['zoning_ext']['cross_scenario_recommendation'] is a string."""
    rec = context["zoning_ext"]["cross_scenario_recommendation"]
    assert isinstance(rec, str)
    assert rec, "cross_scenario_recommendation must not be empty for synthetic Belmont"
    assert "Stabilized hold" in rec


def test_zoning_ext_use_flexibility_score_accessible(context: dict) -> None:
    """KEY 10: ctx['zoning_ext']['use_flexibility_score'] is a flat int (1-5).

    Phase-2 mapping decision C8: kickoff regressed the post-Session-3 fix that
    flattened this from UseFlexibilityScore-with-.score to a bare int. Schema
    is the source of truth; the score is NOT accessed via .score sub-attribute.
    """
    score = context["zoning_ext"]["use_flexibility_score"]
    assert isinstance(score, int), \
        f"use_flexibility_score must be a flat int, got {type(score).__name__}"
    assert 1 <= score <= 5, f"score must be in 1-5 range, got {score}"
    assert score == 2, f"synthetic Belmont set score=2, got {score}"


# ── Phase-2 decision artifacts (sanity checks for derived helpers) ───────────

def test_conformity_badge_css_class_derived(context: dict) -> None:
    """Phase-2 decision B7: LEGAL_NONCONFORMING_USE → .badge-legal."""
    assert context["conformity"]["badge_css_class"] == "badge-legal"


def test_scenarios_pathway_css_class_attached(context: dict) -> None:
    """Phase-2 decision D11: each scenario carries pathway_css_class.
    BY_RIGHT → pathway-byright; VARIANCE → pathway-variance."""
    s1, s2 = context["scenarios"]
    assert getattr(s1, "pathway_css_class", None) == "pathway-byright"
    assert getattr(s2, "pathway_css_class", None) == "pathway-variance"


def test_zoning_ext_preferred_scenario_id_matches(context: dict) -> None:
    """Sanity: preferred_scenario_id in zoning_ext matches preferred_scenario.scenario_id."""
    pref_id_in_ext = context["zoning_ext"]["preferred_scenario_id"]
    pref_obj = context["preferred_scenario"]
    assert pref_id_in_ext == pref_obj.scenario_id == "stabilized_hold"


# ── Final aggregate gate: all 10 keys reachable in one assertion ─────────────

def test_cp1_gate_all_ten_keys_present(context: dict) -> None:
    """CP1 gate: all 10 keys must be present and accessible without KeyError.

    The 11th key (zoning_nonconformity_flag) is deferred to CP2 per the
    post-discovery realignment in the kickoff.
    """
    # 4 sub-keys on conformity + 1 parent presence = 5
    assert "conformity" in context
    for sub in ("status", "zoning_code", "district_name", "dimensions"):
        assert sub in context["conformity"], f"conformity missing sub-key {sub!r}"
    # scenarios + preferred_scenario = 2
    assert "scenarios" in context
    assert "preferred_scenario" in context
    # 3 sub-keys on zoning_ext = 3
    assert "zoning_ext" in context
    for sub in ("cross_scenario_recommendation",
                "preferred_scenario_id",
                "use_flexibility_score"):
        assert sub in context["zoning_ext"], f"zoning_ext missing sub-key {sub!r}"


# ── CP2 extension: 11th key (zoning_nonconformity_flag) ──────────────────────

def test_zoning_nonconformity_flag_present(context: dict) -> None:
    """KEY 11 (CP2): ctx['zoning_nonconformity_flag'] is a DdFlag for the
    synthetic Belmont (status=LEGAL_NONCONFORMING_USE → flag fires)."""
    assert "zoning_nonconformity_flag" in context, (
        "zoning_nonconformity_flag is a CP2 deliverable and must now be present"
    )
    flag = context["zoning_nonconformity_flag"]
    assert flag is not None, (
        "Belmont synth has LEGAL_NONCONFORMING_USE status → flag must fire"
    )
    assert isinstance(flag, DdFlag)
    assert flag.color == DdFlagColor.RED
    assert flag.flag_id == "R8_ZONING_NONCONFORMITY"


def test_cp2_gate_all_eleven_keys_present(context: dict) -> None:
    """CP2 aggregate gate: all 11 keys present (CP1's 10 + the deferred 11th).
    """
    # CP1's 10 keys
    assert "conformity" in context
    for sub in ("status", "zoning_code", "district_name", "dimensions"):
        assert sub in context["conformity"]
    assert "scenarios" in context
    assert "preferred_scenario" in context
    assert "zoning_ext" in context
    for sub in ("cross_scenario_recommendation",
                "preferred_scenario_id",
                "use_flexibility_score"):
        assert sub in context["zoning_ext"]
    # CP2's 11th key (deferred from CP1)
    assert "zoning_nonconformity_flag" in context
