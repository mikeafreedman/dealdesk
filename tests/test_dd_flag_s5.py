"""
test_dd_flag_s5.py — Session 5 CP2 gate test for the zoning DD flag
====================================================================

Asserts that dd_flag_engine.get_zoning_flag(deal) implements the R8
zoning-nonconformity rule per Session 5 kickoff §4 + Phase-2 mapping
decisions E12-E14.

Coverage:
  - CONFORMING deal → returns None
  - LEGAL_NONCONFORMING_USE deal (Belmont synth) → returns DdFlag, color RED
  - LEGAL_NONCONFORMING_DIMENSIONAL deal → returns DdFlag, color RED
  - ILLEGAL_NONCONFORMING deal → returns DdFlag, color RED
  - CONFORMITY_INDETERMINATE deal → returns DdFlag (per kickoff §4.1 literal:
    fires when status != CONFORMING)
  - conformity_assessment is None → returns None
  - Encumbrances appear in narrative body (Indian Queen synth)
  - Closing instruction lives in remediation, NOT narrative (Phase-2 E14)
  - Body lists actual + permitted per nonconformity_details item
  - flag_id == "R8_ZONING_NONCONFORMITY" (next sequential after R7)
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from dd_flag_engine import _build_nonconformity_body, get_zoning_flag
from models.models import (
    ConfidenceLevel,
    ConformityAssessment,
    ConformityStatus,
    DdFlagColor,
    DealData,
    Encumbrance,
    EncumbranceType,
    GrandfatheringStatus,
    NonconformityItem,
    NonconformityType,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BELMONT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "zoning_overhaul_session_3_fixture_belmont.json"
INDIAN_QUEEN_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "zoning_overhaul_session_3_fixture_indian_queen.json"


# ── Fixture builders ─────────────────────────────────────────────────────────

def _load_belmont() -> DealData:
    return DealData.model_validate(json.loads(BELMONT_FIXTURE.read_text(encoding="utf-8")))


def _load_indian_queen() -> DealData:
    return DealData.model_validate(json.loads(INDIAN_QUEEN_FIXTURE.read_text(encoding="utf-8")))


def _conforming_assessment() -> ConformityAssessment:
    """A CONFORMING assessment has no nonconformity_details and no grandfathering."""
    return ConformityAssessment(
        status=ConformityStatus.CONFORMING,
        confidence=ConfidenceLevel.HIGH,
        confidence_reasons=["Use, density, and dimensional standards all match RSD-3."],
        risk_summary="No conformity issues identified.",
    )


def _legal_nonconforming_use_assessment() -> ConformityAssessment:
    """LEGAL_NONCONFORMING_USE — Belmont's actual conformity state."""
    return ConformityAssessment(
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
            loss_triggers=["Substantial improvement >50%", "Change of use"],
        ),
        risk_summary="Legal nonconforming MF use carries grandfathering verification risk.",
    )


def _legal_nonconforming_dimensional_assessment() -> ConformityAssessment:
    return ConformityAssessment(
        status=ConformityStatus.LEGAL_NONCONFORMING_DIMENSIONAL,
        confidence=ConfidenceLevel.HIGH,
        confidence_reasons=["Building exceeds height standard."],
        nonconformity_details=[
            NonconformityItem(
                nonconformity_type=NonconformityType.HEIGHT,
                standard_description="Maximum building height",
                permitted_value="35 ft",
                actual_value="42 ft",
                magnitude_description="7 ft over standard",
            ),
        ],
        grandfathering_status=GrandfatheringStatus(
            is_presumed_grandfathered=True,
            basis="Built pre-1978",
        ),
        risk_summary="Dimensional nonconformity at building height.",
    )


def _illegal_nonconforming_assessment() -> ConformityAssessment:
    """ILLEGAL_NONCONFORMING does NOT require grandfathering_status per validator."""
    return ConformityAssessment(
        status=ConformityStatus.ILLEGAL_NONCONFORMING,
        confidence=ConfidenceLevel.HIGH,
        confidence_reasons=["Use installed without permit; no grandfathering basis."],
        nonconformity_details=[
            NonconformityItem(
                nonconformity_type=NonconformityType.USE,
                standard_description="Use type",
                permitted_value="Residential",
                actual_value="Industrial",
                magnitude_description="Use category mismatch — no legal basis",
            ),
        ],
        risk_summary="Illegal nonconforming use; abatement risk.",
    )


def _indeterminate_assessment() -> ConformityAssessment:
    """CONFORMITY_INDETERMINATE — confidence gate failed; insufficient zoning data."""
    return ConformityAssessment(
        status=ConformityStatus.CONFORMITY_INDETERMINATE,
        confidence=ConfidenceLevel.INDETERMINATE,
        confidence_reasons=["Zoning code not provided in extracted documents."],
        risk_summary="Conformity could not be assessed; manual review required.",
    )


# ── CP2 gate tests ───────────────────────────────────────────────────────────

def test_conforming_returns_none() -> None:
    """get_zoning_flag returns None for CONFORMING deals (kickoff §4.1)."""
    deal = _load_belmont()
    deal.conformity_assessment = _conforming_assessment()
    flag = get_zoning_flag(deal)
    assert flag is None


def test_no_assessment_returns_none() -> None:
    """get_zoning_flag returns None when conformity_assessment is None
    (kickoff §4.1: `if ca is None or ...`)."""
    deal = _load_belmont()
    assert deal.conformity_assessment is None  # Belmont fixture has no assessment
    assert get_zoning_flag(deal) is None


def test_legal_nonconforming_use_returns_red_flag() -> None:
    """LEGAL_NONCONFORMING_USE → DdFlag with color=RED (Phase-2 E13:
    severity=HIGH maps to color=RED in the actual schema)."""
    deal = _load_belmont()
    deal.conformity_assessment = _legal_nonconforming_use_assessment()
    flag = get_zoning_flag(deal)
    assert flag is not None
    assert flag.color == DdFlagColor.RED
    assert flag.flag_id == "R8_ZONING_NONCONFORMITY"
    assert flag.category == "Zoning"
    assert flag.title == "Zoning nonconformity — due diligence required"


def test_legal_nonconforming_dimensional_returns_red_flag() -> None:
    deal = _load_belmont()
    deal.conformity_assessment = _legal_nonconforming_dimensional_assessment()
    flag = get_zoning_flag(deal)
    assert flag is not None
    assert flag.color == DdFlagColor.RED
    assert "Maximum building height" in flag.narrative
    assert "42 ft" in flag.narrative
    assert "35 ft" in flag.narrative


def test_illegal_nonconforming_returns_red_flag() -> None:
    """ILLEGAL_NONCONFORMING fires the same way LEGAL_NONCONFORMING_* do —
    kickoff §4.1 literal: any status != CONFORMING triggers."""
    deal = _load_belmont()
    deal.conformity_assessment = _illegal_nonconforming_assessment()
    flag = get_zoning_flag(deal)
    assert flag is not None
    assert flag.color == DdFlagColor.RED


def test_indeterminate_returns_none() -> None:
    """CONFORMITY_INDETERMINATE → None. Skip the flag when the assessment is
    inconclusive (confidence-gate failure / insufficient zoning data). Better
    to render nothing in §8 than emit a misleading RED flag for a state that
    means 'we don't know,' not 'definitely nonconforming.'

    Diverges from the kickoff §4.1 literal (`if status == CONFORMING: return
    None`) per the post-CP2 trigger correction."""
    deal = _load_belmont()
    deal.conformity_assessment = _indeterminate_assessment()
    assert get_zoning_flag(deal) is None


def test_remediation_carries_closing_instruction() -> None:
    """Phase-2 decision E14: closing instruction lives in `remediation`,
    NOT in `narrative` (matches R1-R7 pattern)."""
    deal = _load_belmont()
    deal.conformity_assessment = _legal_nonconforming_use_assessment()
    flag = get_zoning_flag(deal)
    assert flag is not None
    assert flag.remediation is not None
    assert "Confirm legal nonconforming status" in flag.remediation
    # The municipality string interpolation pulls from deal.address.city.
    # Belmont's city is Philadelphia.
    assert "Philadelphia" in flag.remediation
    # And critically: the closing instruction MUST NOT be duplicated in narrative.
    assert "Confirm legal nonconforming status" not in flag.narrative


def test_narrative_lists_failing_dimensions_with_actual_and_permitted() -> None:
    """Per kickoff §4.2: per failing/warn dimension, body has 'one line
    describing the dimension label, actual value, and permitted value'."""
    deal = _load_belmont()
    deal.conformity_assessment = _legal_nonconforming_use_assessment()
    flag = get_zoning_flag(deal)
    assert flag is not None
    assert "Use type" in flag.narrative
    assert "36-unit Multifamily" in flag.narrative
    assert "Single-family / Two-family" in flag.narrative
    assert "Nonconforming dimensions:" in flag.narrative


def test_narrative_lists_encumbrances_when_present() -> None:
    """Per kickoff §4.2: per encumbrance, one line with doc id, grantee, type,
    and any key terms (perpetual, ROFR, expiration). Tests Indian Queen which
    has 2 recorded encumbrances in its fixture."""
    deal = _load_indian_queen()
    deal.conformity_assessment = ConformityAssessment(
        status=ConformityStatus.LEGAL_NONCONFORMING_USE,
        confidence=ConfidenceLevel.HIGH,
        confidence_reasons=["Industrial use in residential zone, predating current code."],
        nonconformity_details=[
            NonconformityItem(
                nonconformity_type=NonconformityType.USE,
                standard_description="Use type",
                permitted_value="Residential",
                actual_value="Industrial",
                magnitude_description="Use category mismatch",
            ),
        ],
        grandfathering_status=GrandfatheringStatus(
            is_presumed_grandfathered=True,
            basis="Industrial use predates current zoning.",
        ),
        risk_summary="Industrial use in RSA-1/RSA-5 split-zoned parcel.",
    )

    # Sanity: the fixture should already have 2 encumbrances loaded.
    assert len(deal.encumbrances) == 2

    flag = get_zoning_flag(deal)
    assert flag is not None
    assert "Recorded encumbrances:" in flag.narrative
    # Both grantees should appear:
    assert "American Tower" in flag.narrative
    assert "SBC Tower Holdings" in flag.narrative
    # ROFR key term should appear (one of the encumbrances has it):
    assert "ROFR" in flag.narrative


def test_municipality_fallback_when_address_missing_city() -> None:
    """Defensive: if deal.address.city is empty, remediation uses the
    'the municipality' fallback rather than emitting an empty bracket."""
    deal = _load_belmont()
    deal.conformity_assessment = _legal_nonconforming_use_assessment()
    deal.address.city = ""  # simulate missing city
    flag = get_zoning_flag(deal)
    assert flag is not None
    assert "the municipality" in flag.remediation
    assert "[" not in flag.remediation  # no unfilled placeholder


def test_no_encumbrances_section_when_list_empty() -> None:
    """When deal.encumbrances is empty, the body must not emit an empty
    'Recorded encumbrances:' header."""
    deal = _load_belmont()
    assert len(deal.encumbrances) == 0  # Belmont fixture has no encumbrances
    deal.conformity_assessment = _legal_nonconforming_use_assessment()
    flag = get_zoning_flag(deal)
    assert flag is not None
    assert "Recorded encumbrances:" not in flag.narrative


def test_helper_callable_directly() -> None:
    """_build_nonconformity_body is a pure function — directly callable
    with a ConformityAssessment + List[Encumbrance]. No deal coupling."""
    ca = _legal_nonconforming_use_assessment()
    enc = Encumbrance(
        type=EncumbranceType.EASEMENT,
        doc_id="DOC-001",
        grantee="Test Grantee",
        term="perpetual",
    )
    body = _build_nonconformity_body(ca, [enc])
    assert "Legal Nonconforming Use" in body
    assert "DOC-001" in body
    assert "Test Grantee" in body
    assert "perpetual" in body


def test_cp2_gate_full_flag_shape() -> None:
    """CP2 aggregate gate: full-shape assertion on a single representative
    flag invocation. All fields populated correctly."""
    deal = _load_belmont()
    deal.conformity_assessment = _legal_nonconforming_use_assessment()
    flag = get_zoning_flag(deal)
    assert flag is not None
    # Every required DdFlag field populated:
    assert flag.flag_id == "R8_ZONING_NONCONFORMITY"
    assert flag.color == DdFlagColor.RED
    assert flag.category == "Zoning"
    assert flag.title == "Zoning nonconformity — due diligence required"
    assert flag.narrative  # non-empty
    assert flag.remediation  # non-empty
    # Type checks:
    assert isinstance(flag.flag_id, str)
    assert isinstance(flag.narrative, str)
    assert isinstance(flag.remediation, str)
