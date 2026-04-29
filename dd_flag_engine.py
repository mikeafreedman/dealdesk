"""
dd_flag_engine.py — Due-Diligence Flag Generator
=================================================
Evaluates a fixed set of rules against a fully-populated DealData and
populates `deal.dd_flags`. Runs after financials.py in the main pipeline
so it can see NOI, DSCR, IRR, refi outcomes, and extracted-doc state.

Rules (RED / AMBER / GREEN):
  R1 Building vintage < 1978 → AMBER "Lead paint / asbestos risk"
  R2 No Phase I ESA on file  → AMBER "Phase I ESA required"
  R3 IO term ≤ 24 months     → AMBER "Short IO — refinance execution risk"
  R4 Year 1 DSCR < 1.0       → RED   "Debt not serviceable from operations"
  R5 Refi net proceeds < 0   → RED   "Refi requires equity injection"
  R6 LP IRR < min_lp_irr     → AMBER "LP IRR below threshold"
  R7 Unemployment > 7%       → AMBER "Above-average unemployment"

Public API beyond the rules engine:
  R8 (Session 5) get_zoning_flag(deal) → RED "Zoning nonconformity"
     Public function (NOT in _RULES tuple) — consumed directly by
     context_builder.build_context() to populate the §8 zoning DD flag.
     Returns Optional[DdFlag]; None when conformity_assessment is None
     or status is CONFORMING.

Each rule is a pure function deal → Optional[DdFlag]. No side effects.
Failed reads return None (flag skipped). Results are appended to
deal.dd_flags in rule order; existing flags are NOT cleared so callers
can pre-seed operator-authored flags.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from models.models import (
    ConformityAssessment,
    ConformityStatus,
    DealData,
    DdFlag,
    DdFlagColor,
    Encumbrance,
)

logger = logging.getLogger(__name__)


# ── Individual rules ─────────────────────────────────────────────────────────

def _r1_vintage(deal: DealData) -> Optional[DdFlag]:
    """Pre-1978 construction → federally regulated lead-based paint presumption."""
    yr = getattr(deal.assumptions, "year_built", None)
    if yr and yr < 1978:
        return DdFlag(
            flag_id="R1_VINTAGE_LEAD",
            color=DdFlagColor.AMBER,
            category="Environmental",
            title=f"Pre-1978 vintage — lead-based paint disclosure regime applies (year built {yr})",
            narrative=(
                "The property was built before 1978, so federal lead-based paint "
                "disclosure and inspection rules apply. Until a Phase I ESA and, "
                "where indicated, a lead-based paint survey are in hand, the asset "
                "carries disclosure liability and remediation uncertainty."
            ),
            remediation="Commission Phase I ESA with lead-based paint assessment add-on.",
        )
    return None


def _r2_no_phase_i(deal: DealData) -> Optional[DdFlag]:
    """No Phase I ESA on file. Phase I is always a gating DD item for CRE."""
    ext = deal.extracted_docs
    has_esa = False
    if ext:
        docs = getattr(ext, "documents_uploaded", None) or []
        has_esa = any("phase i" in (d or "").lower() or "esa" in (d or "").lower()
                      for d in docs)
    if not has_esa:
        return DdFlag(
            flag_id="R2_NO_PHASE_I",
            color=DdFlagColor.AMBER,
            category="Environmental",
            title="No Phase I Environmental Site Assessment on file",
            narrative=(
                "A Phase I ESA compliant with ASTM E1527-21 is a standard "
                "pre-closing deliverable. None is on file for this analysis. "
                "Lender approvals and environmental indemnity carveouts cannot "
                "be finalized without it."
            ),
            remediation="Order Phase I ESA; budget $6,000–$8,000; 3–4 week turnaround.",
        )
    return None


def _r3_short_io(deal: DealData) -> Optional[DdFlag]:
    """IO period ≤ 24 months: tight window to reach refi-appraisal NOI."""
    io = getattr(deal.assumptions, "io_period_months", 0) or 0
    if 0 < io <= 24:
        return DdFlag(
            flag_id="R3_SHORT_IO",
            color=DdFlagColor.AMBER,
            category="Financial",
            title=f"Short interest-only term ({io} months) — refinance execution risk",
            narrative=(
                f"The acquisition loan's {io}-month IO term requires the property "
                "to reach stabilized NOI inside a narrow window to support the "
                "refinance underwriting. Any lease-up slippage compresses the "
                "margin between the refi appraisal and the outstanding balance."
            ),
            remediation=(
                "Negotiate 6-month extension options with the acquisition lender; "
                "pre-qualify refi lenders in parallel with lease-up."
            ),
        )
    return None


def _r4_dscr_under_1(deal: DealData) -> Optional[DdFlag]:
    """Year 1 DSCR below 1.0 means operations cannot cover debt service."""
    dscr = getattr(deal.financial_outputs, "dscr_yr1", None)
    if dscr is not None and dscr < 1.0:
        return DdFlag(
            flag_id="R4_DSCR_LT_1",
            color=DdFlagColor.RED,
            category="Financial",
            title=f"Year 1 DSCR below 1.0 ({dscr:.2f}x) — debt not serviceable from operations",
            narrative=(
                f"Projected Year 1 debt service coverage is {dscr:.2f}x, below the "
                "1.0 threshold at which operations fully cover debt. The gap must "
                "be bridged by interest reserves, sponsor support, or preferred "
                "equity until stabilized."
            ),
            remediation=(
                "Budget a debt service interest reserve from acquisition sources, "
                "sized through the first stabilized year."
            ),
        )
    return None


def _r5_refi_equity_injection(deal: DealData) -> Optional[DdFlag]:
    """Refi new_loan < old_balance → borrower equity call at refi."""
    prov = deal.provenance.field_sources or {}
    for idx in (1, 2, 3):
        if prov.get(f"refi{idx}_equity_injection_required") == "True":
            try:
                amt = float(prov.get(f"refi{idx}_equity_injection_amount", 0) or 0)
            except (TypeError, ValueError):
                amt = 0.0
            return DdFlag(
                flag_id=f"R5_REFI{idx}_EQUITY_INJECTION",
                color=DdFlagColor.RED,
                category="Financial",
                title=f"Refi {idx} requires a borrower equity injection of ${amt:,.0f}",
                narrative=(
                    f"At the modeled appraisal and LTV, the Refi {idx} new loan "
                    f"does not cover the outstanding balance. A sponsor/LP capital "
                    f"call of ${amt:,.0f} is required to execute the refinance. "
                    "This is a material mid-hold capital event that must be "
                    "disclosed and pre-committed."
                ),
                remediation=(
                    "Re-size acquisition equity to pre-fund the gap, or renegotiate "
                    "refi terms (lower origination year, higher LTV, preferred equity "
                    "tranche)."
                ),
            )
    return None


def _r6_lp_irr_under_min(deal: DealData) -> Optional[DdFlag]:
    """LP IRR below the minimum return threshold on assumptions."""
    lp_irr = getattr(deal.financial_outputs, "lp_irr", None)
    min_irr = getattr(deal.assumptions, "min_lp_irr", None)
    if lp_irr is not None and min_irr is not None and lp_irr < min_irr:
        return DdFlag(
            flag_id="R6_LP_IRR_BELOW_MIN",
            color=DdFlagColor.AMBER,
            category="Financial",
            title=(
                f"LP IRR {lp_irr:.2%} below minimum threshold ({min_irr:.2%})"
            ),
            narrative=(
                f"Projected LP IRR of {lp_irr:.2%} sits below the underwriting "
                f"minimum of {min_irr:.2%}. Under current assumptions the deal "
                "does not clear the LP return threshold; re-price, re-structure, "
                "or decline."
            ),
            remediation=(
                "Re-negotiate purchase price, adjust capital structure, or "
                "identify offsetting value-creation levers before closing."
            ),
        )
    return None


def _r7_high_unemployment(deal: DealData) -> Optional[DdFlag]:
    """Submarket unemployment > 7% → tenant-credit risk."""
    ue = getattr(deal.market_data, "unemployment_rate", None)
    if ue is not None and ue > 0.07:
        return DdFlag(
            flag_id="R7_HIGH_UNEMPLOYMENT",
            color=DdFlagColor.AMBER,
            category="Market",
            title=f"Above-average unemployment rate ({ue:.1%}) — tenant credit risk",
            narrative=(
                f"The submarket's {ue:.1%} unemployment rate is above the 7% "
                "monitoring threshold. Elevated unemployment pressures rent "
                "collection and raises delinquency risk during hold."
            ),
            remediation=(
                "Stress-test proforma vacancy assumption +200 bps; require "
                "additional leasing-commission reserve."
            ),
        )
    return None


# ── R8 Zoning nonconformity (Session 5 — direct consumption by build_context) ─

def _build_nonconformity_body(
    ca: ConformityAssessment,
    encs: List[Encumbrance],
) -> str:
    """Build the narrative body for the zoning-nonconformity flag.

    Per Session 5 kickoff §4.2 + Phase-2 mapping decisions:
      - Opening sentence: states conformity status (formatted) and zoning code.
      - One bullet per nonconformity_details item: label, actual vs permitted.
      - One bullet per encumbrance: doc id, grantee, type, key terms.
      - Closing instruction lives in `remediation` (Phase-2 decision E14 —
        matches the R1-R7 pattern), NOT here in the body.
    """
    lines: List[str] = []

    # Opening — humanize the enum value (LEGAL_NONCONFORMING_USE → "Legal Nonconforming Use")
    status_str = (
        ca.status.value.replace("_", " ").title()
        if ca.status else "Indeterminate"
    )
    lines.append(
        f"Property is classified as {status_str} under current zoning."
    )

    # Nonconforming dimensions (all entries are failures by schema construction —
    # nonconformity_details only contains items that fail to conform)
    if ca.nonconformity_details:
        lines.append("")
        lines.append("Nonconforming dimensions:")
        for item in ca.nonconformity_details:
            label = (
                item.standard_description
                or item.nonconformity_type.value.replace("_", " ").title()
            )
            lines.append(
                f"  - {label}: actual {item.actual_value}; "
                f"permitted {item.permitted_value}."
            )

    # Encumbrances
    if encs:
        lines.append("")
        lines.append("Recorded encumbrances:")
        for enc in encs:
            terms_parts: List[str] = []
            if enc.term:
                terms_parts.append(str(enc.term))
            if enc.expiration:
                terms_parts.append(f"exp. {enc.expiration}")
            if enc.right_of_first_refusal:
                terms_parts.append("ROFR")
            terms = " | ".join(terms_parts) if terms_parts else "no key terms recorded"
            doc = enc.doc_id or "(no doc id)"
            grantee = enc.grantee or "(unknown grantee)"
            lines.append(
                f"  - Doc {doc} ({grantee}, {enc.type.value}): {terms}."
            )

    return "\n".join(lines)


def get_zoning_flag(deal: DealData) -> Optional[DdFlag]:
    """R8 — Session 5 zoning-nonconformity DD flag.

    Returns a DdFlag when the deal's conformity_assessment indicates a
    nonconforming property; None otherwise. Consumed directly by
    context_builder.build_context() to populate the §8 `zoning_nonconformity_flag`
    context key — NOT registered in the _RULES tuple, so this flag does not
    auto-append to deal.dd_flags via generate_dd_flags(). The §8 rendering
    path is the sole consumer.

    Per Session 5 kickoff §4.1 + Phase-2 mapping decisions E12-E14:
      - DdFlag (camelCase), not DDFlag.
      - color=DdFlagColor.RED (no severity field in DdFlag — RED maps from HIGH).
      - narrative (not body); category="Zoning"; remediation captures the
        closing instruction.
      - flag_id="R8_ZONING_NONCONFORMITY" (next sequential after R7; matches
        R1-R7 naming convention).

    Trigger condition (kickoff §4.1 literal): fires whenever
    conformity_assessment is non-None and status != CONFORMING. That includes
    all LEGAL_NONCONFORMING_* axes, MULTIPLE_NONCONFORMITIES, ILLEGAL_NONCONFORMING,
    and CONFORMITY_INDETERMINATE.
    """
    ca = deal.conformity_assessment
    # Trigger: skip when no assessment, when CONFORMING (no issue), or when
    # CONFORMITY_INDETERMINATE (we don't know — better to render nothing in §8
    # than emit a misleading RED flag for an inconclusive assessment).
    if ca is None or ca.status in (
        ConformityStatus.CONFORMING,
        ConformityStatus.CONFORMITY_INDETERMINATE,
    ):
        return None

    encs = deal.encumbrances or []
    municipality = (
        (deal.address.city or "the municipality")
        if deal.address else "the municipality"
    )

    return DdFlag(
        flag_id="R8_ZONING_NONCONFORMITY",
        color=DdFlagColor.RED,
        category="Zoning",
        title="Zoning nonconformity — due diligence required",
        narrative=_build_nonconformity_body(ca, encs),
        remediation=(
            f"Confirm legal nonconforming status via {municipality} L&I "
            "records before closing."
        ),
    )


_RULES = (
    _r1_vintage,
    _r2_no_phase_i,
    _r3_short_io,
    _r4_dscr_under_1,
    _r5_refi_equity_injection,
    _r6_lp_irr_under_min,
    _r7_high_unemployment,
    get_zoning_flag,  # R8 — Session 5. Also exposed publicly for build_context.
)


# ── Public API ───────────────────────────────────────────────────────────────

def generate_dd_flags(deal: DealData) -> List[DdFlag]:
    """Evaluate all rules against `deal` and APPEND any triggered flags to
    deal.dd_flags. Returns the list of flags that were added (not the full
    list on deal). Safe to call multiple times — checks flag_id uniqueness.
    """
    existing_ids = {f.flag_id for f in deal.dd_flags}
    added: List[DdFlag] = []
    for rule in _RULES:
        try:
            flag = rule(deal)
        except Exception as exc:  # defensive: one rule must never crash others
            logger.warning("DD flag rule %s raised %s — skipping", rule.__name__, exc)
            continue
        if flag is None:
            continue
        if flag.flag_id in existing_ids:
            continue
        deal.dd_flags.append(flag)
        existing_ids.add(flag.flag_id)
        added.append(flag)
        logger.info(
            "DD FLAG: %s %s — %s",
            flag.color.value, flag.flag_id, flag.title,
        )
    logger.info(
        "DD FLAG ENGINE: evaluated %d rules, added %d flags (total on deal: %d)",
        len(_RULES), len(added), len(deal.dd_flags),
    )
    return added
