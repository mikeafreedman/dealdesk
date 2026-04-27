# Session 1 — Schema Design Specification

**Document version:** 1.0
**Created:** April 27, 2026
**Owner:** Mike Freedman, Freedman Properties
**Status:** Approved — ready for Claude Code implementation
**Target file:** `C:\Users\mikea\dealdesk\models.py`
**Build session:** 1 of 5
**Master plan reference:** `DealDesk_Zoning_Overhaul_Plan.md`

---

## How to use this document

This is the complete specification for Session 1 — the schema foundation. The Claude Code instance executing this session must:

1. First read `DealDesk_Zoning_Overhaul_Plan.md` for context, locked decisions, and gate criteria
2. Then read this document in full before writing any code
3. Implement every model, enum, and validator below in `models.py`
4. Run the gate criteria at the end of master plan Session 1 section before declaring done
5. Commit with the message format from master plan: `Session 1: Schema (models.py) — Add ConformityAssessment, DevelopmentScenario, ZoningExtensions`

---

## Design overview

The Session 1 deliverable adds **three top-level new models** to the `DealData` schema, supported by **eight new sub-models** and **five new enums**. Plus one utility function.

### Top-level additions to DealData

```python
class DealData(BaseModel):
    # ... all existing fields preserved ...

    # NEW in Session 1 (master plan D6, D8):
    conformity_assessment: Optional[ConformityAssessment] = None
    scenarios: List[DevelopmentScenario] = Field(default_factory=list)
    zoning_extensions: Optional[ZoningExtensions] = None
```

### File organization

All new models go in `models.py`. Keep existing import order. Add new models in this sequence (forward references resolved by Pydantic at runtime):

1. New enums (top of new section, alphabetical)
2. Sub-models referenced by top-level models
3. Top-level models (`ConformityAssessment`, `DevelopmentScenario`, `ZoningExtensions`)
4. Extensions to `DealData` class
5. Utility function `mirror_preferred_to_legacy(deal)`

Section divider comment in `models.py`:

```python
# =============================================================================
# ZONING OVERHAUL — Session 1 additions (April 2026)
# Master plan: DealDesk_Zoning_Overhaul_Plan.md
# Schema spec: Session_1_Schema_Design.md
# =============================================================================
```

---

## 1. New enums

All enums use `str` base class for clean JSON serialization. All enum values are UPPER_SNAKE_CASE.

### 1.1 ConformityStatus

Captures the relationship between the existing or proposed use/configuration and current zoning. Used in `ConformityAssessment.status`.

```python
class ConformityStatus(str, Enum):
    """
    The conformity state of the property under current zoning.

    Per master plan D4, this is the headline classification on every Section 06.
    The CONFORMITY_INDETERMINATE value is the mandatory fallback when the
    confidence gate fails (insufficient zoning data).
    """
    CONFORMING = "CONFORMING"
    LEGAL_NONCONFORMING_USE = "LEGAL_NONCONFORMING_USE"
    LEGAL_NONCONFORMING_DENSITY = "LEGAL_NONCONFORMING_DENSITY"
    LEGAL_NONCONFORMING_DIMENSIONAL = "LEGAL_NONCONFORMING_DIMENSIONAL"
    MULTIPLE_NONCONFORMITIES = "MULTIPLE_NONCONFORMITIES"
    ILLEGAL_NONCONFORMING = "ILLEGAL_NONCONFORMING"
    CONFORMITY_INDETERMINATE = "CONFORMITY_INDETERMINATE"
```

**Semantic notes for downstream consumers:**

- `CONFORMING` — Existing use, density, and dimensional standards all comply
- `LEGAL_NONCONFORMING_USE` — Use is grandfathered (e.g., commercial in residential district)
- `LEGAL_NONCONFORMING_DENSITY` — Use complies but unit count or FAR exceeds current limits *(this is 967-73 N. 9th St)*
- `LEGAL_NONCONFORMING_DIMENSIONAL` — Use complies but height, setbacks, or coverage exceed current limits
- `MULTIPLE_NONCONFORMITIES` — Two or more of the LEGAL_NONCONFORMING_* states apply
- `ILLEGAL_NONCONFORMING` — No grandfathering documentation; use was added without permits — deal-killer flag
- `CONFORMITY_INDETERMINATE` — Insufficient data to assess; mandatory fallback per D4

### 1.2 ConfidenceLevel

Captures how reliable the data underlying an assessment is. Used in `ConformityAssessment.confidence`.

```python
class ConfidenceLevel(str, Enum):
    """
    Confidence in the underlying data supporting an assessment.

    Used by the confidence gate (master plan D4) to determine whether
    conformity analysis should run or fall back to INDETERMINATE.
    """
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INDETERMINATE = "INDETERMINATE"
```

**Semantic notes:**

- `HIGH` — Zoning code source-verified, 4+ dimensional standards populated, 3+ permitted uses listed
- `MEDIUM` — Zoning code source-verified, but 2-3 dimensional standards or fewer permitted uses
- `LOW` — Zoning code from LLM training fallback (not verified) but enough fields populated to attempt assessment with caveats
- `INDETERMINATE` — Confidence gate failed; conformity assessment must fall back

### 1.3 ScenarioVerdict

Captures the recommendation status of a development scenario. Used in `DevelopmentScenario.verdict`.

```python
class ScenarioVerdict(str, Enum):
    """
    The recommendation status of a development scenario within a deal.

    Exactly one scenario per deal must have verdict PREFERRED.
    """
    PREFERRED = "PREFERRED"
    ALTERNATE = "ALTERNATE"
    REJECT = "REJECT"
```

**Semantic notes:**

- `PREFERRED` — The recommended scenario; exactly one per deal; corresponds to rank 1
- `ALTERNATE` — A real alternative path worth evaluating; corresponds to rank 2 or 3
- `REJECT` — Considered and excluded (not produced as Excel output, but mentioned in cross-scenario synthesis)

### 1.4 ZoningPathwayType

Captures the regulatory route to entitlement. Used in `ZoningPathway.pathway_type`.

```python
class ZoningPathwayType(str, Enum):
    """
    The regulatory pathway required to execute a development scenario.

    Maps to entitlement timeline, cost, and risk in downstream analysis.
    """
    BY_RIGHT = "BY_RIGHT"
    CONDITIONAL_USE = "CONDITIONAL_USE"
    SPECIAL_EXCEPTION = "SPECIAL_EXCEPTION"
    VARIANCE = "VARIANCE"
    REZONE = "REZONE"
```

**Semantic notes:**

- `BY_RIGHT` — No discretionary approval needed; building permit only
- `CONDITIONAL_USE` — Requires planning commission or zoning board approval but follows defined criteria
- `SPECIAL_EXCEPTION` — Requires zoning board approval at hearing; subject to specific use criteria
- `VARIANCE` — Requires zoning board hearing; must demonstrate hardship; lowest probability of approval
- `REZONE` — Requires legislative action by city council; longest timeline, highest political risk

### 1.5 NonconformityType

Captures the specific dimension along which a property is nonconforming. Used in `NonconformityItem.nonconformity_type`.

```python
class NonconformityType(str, Enum):
    """
    The specific zoning dimension along which a property fails to conform.

    Multiple instances may apply to one property (captured as a list of
    NonconformityItem records on the ConformityAssessment).
    """
    USE = "USE"
    DENSITY = "DENSITY"
    HEIGHT = "HEIGHT"
    FAR = "FAR"
    SETBACKS = "SETBACKS"
    LOT_COVERAGE = "LOT_COVERAGE"
    PARKING = "PARKING"
    LOT_AREA = "LOT_AREA"
```

---

## 2. Sub-models (referenced by top-level models)

### 2.1 NonconformityItem

A single nonconformity along a specific dimension.

```python
class NonconformityItem(BaseModel):
    """
    A single instance of nonconformity. A property with multiple nonconformities
    has a list of these on its ConformityAssessment.

    Example for 967-73 N. 9th St:
        nonconformity_type=NonconformityType.DENSITY,
        existing_value="21 units",
        permitted_value="6 units (1,440 SF/unit min × 9,652 SF lot)",
        magnitude_description="Existing density exceeds by-right cap by 250%",
        triggers_loss_of_grandfathering=[
            "Substantial improvement (>50% of replacement cost)",
            "Change of use category",
            "Abandonment of use for more than 12 consecutive months",
        ],
    """
    nonconformity_type: NonconformityType
    existing_value: str  # e.g. "21 units" — free text to capture units of measure
    permitted_value: str  # e.g. "6 units" — what current zoning allows
    magnitude_description: str  # 1 sentence describing the gap, e.g. "exceeds cap by 250%"
    triggers_loss_of_grandfathering: List[str] = Field(default_factory=list)
    # ^^ Each string is a one-liner describing what action would void grandfathering
```

### 2.2 GrandfatheringStatus

Captures the documentation and risk profile of a grandfathered nonconforming use.

```python
class GrandfatheringStatus(BaseModel):
    """
    The grandfathering posture of a nonconforming property.

    Only populated when ConformityStatus is one of the LEGAL_NONCONFORMING_*
    or MULTIPLE_NONCONFORMITIES values. None for CONFORMING and INDETERMINATE.
    """
    is_documented: bool  # True if certificate of nonconforming use exists
    documentation_source: Optional[str] = None  # e.g. "1962 building permit on file"
    presumption_basis: Optional[str] = None  # if not documented, why we presume grandfathering exists
    confirmation_action_required: str  # what diligence is needed to confirm, e.g. "Zoning verification letter from L&I"
    risk_if_denied: str  # plain-English consequence if grandfathering not confirmed
```

### 2.3 ZoningPathway

Captures the regulatory route, timeline, cost, and risk for a development scenario.

```python
class ZoningPathway(BaseModel):
    """
    The regulatory pathway a scenario must traverse to be approvable.
    Each DevelopmentScenario carries one of these.
    """
    pathway_type: ZoningPathwayType
    approval_body: Optional[str] = None  # e.g. "Philadelphia Zoning Board of Adjustment"
    estimated_timeline_months: Optional[int] = None  # 0 for BY_RIGHT
    estimated_soft_cost_usd: Optional[float] = None  # legal, expediting, hearings
    success_probability_pct: Optional[int] = None  # 0-100, AI-estimated
    fallback_if_denied: Optional[str] = None  # what happens if approval doesn't come through
```

### 2.4 EntitlementRiskFlag

Captures whether a scenario is at material entitlement risk and why.

```python
class EntitlementRiskFlag(BaseModel):
    """
    A material entitlement risk flag attached to a scenario.

    Optional — only populated for scenarios where entitlement is a real risk
    (typically anything other than BY_RIGHT). None for clean by-right scenarios.
    """
    severity: str  # "LOW" | "MEDIUM" | "HIGH" — string for prompt flexibility
    risk_summary: str  # one paragraph describing the entitlement risk
    diligence_required: List[str] = Field(default_factory=list)
    # ^^ Each string is a discrete diligence action, e.g. "Pre-meeting with L&I planner"
```

### 2.5 UseAllocation

Captures how floor area in a scenario is allocated across uses.

```python
class UseAllocation(BaseModel):
    """
    Floor area allocation by use category within a scenario.
    A scenario can have multiple of these (mixed-use deals).

    Example: ground floor commercial 4,756 SF + residential 21,668 SF
    """
    use_category: str  # free text; common values: "residential", "office", "retail", "industrial", "restaurant"
    square_feet: float
    unit_count: Optional[int] = None  # only populated for residential
    notes: Optional[str] = None  # e.g. "ground floor only" or "floors 2-4"
```

### 2.6 OverlayImpact

Captures how an overlay district affects development on the parcel.

```python
class OverlayImpact(BaseModel):
    """
    A single overlay district's impact on the property's development envelope.
    A property can have zero or more overlays.

    Common overlay types: historic, environmental, transit-oriented, design review,
    floodplain, special service area.
    """
    overlay_name: str  # e.g. "Historic District Overlay"
    overlay_type: str  # e.g. "historic" | "environmental" | "design"
    impact_summary: str  # 1-2 sentences describing the impact
    triggers_review: bool  # True if the overlay adds an approval step
    additional_diligence: List[str] = Field(default_factory=list)
```

### 2.7 DevelopmentUpside

Captures unused entitlement capacity on the parcel.

```python
class DevelopmentUpside(BaseModel):
    """
    Captures how much development capacity remains unused on the parcel.
    Useful for assessing future expansion potential.
    """
    far_remaining_sf: Optional[float] = None  # FAR cap minus existing built SF
    units_remaining: Optional[int] = None  # density cap minus existing units (negative if over)
    height_remaining_ft: Optional[float] = None  # height cap minus existing height
    summary: str  # 1-2 sentences summarizing upside
```

### 2.8 FinancialOutputsRef (cross-reference helper)

This is **NOT a new model** — it's a note that `DevelopmentScenario.financial_outputs` references the **existing** `FinancialOutputs` model already in `models.py`. No changes needed to `FinancialOutputs` itself in Session 1. The change is just the new field on `DevelopmentScenario`.

---

## 3. Top-level models

### 3.1 ConformityAssessment

The headline conformity classification. One per deal. Always present (even if INDETERMINATE).

```python
class ConformityAssessment(BaseModel):
    """
    The headline conformity classification for the deal's property.

    Master plan D4: this assessment is rendered as a prominent callout at the
    top of Section 06 on every report. When the confidence gate fails, the
    status is set to CONFORMITY_INDETERMINATE with explicit explanation.
    """
    status: ConformityStatus
    confidence: ConfidenceLevel
    confidence_reasons: List[str] = Field(default_factory=list)
    # ^^ Why the confidence is what it is, e.g. ["zoning code source-verified",
    #    "5 of 6 dimensional standards populated"]

    nonconformity_details: List[NonconformityItem] = Field(default_factory=list)
    # ^^ Empty list when status is CONFORMING or INDETERMINATE.
    #    One or more items for any LEGAL_NONCONFORMING_* status.

    grandfathering_status: Optional[GrandfatheringStatus] = None
    # ^^ Required (non-None) when status is LEGAL_NONCONFORMING_* or MULTIPLE_NONCONFORMITIES.
    #    None for CONFORMING, ILLEGAL_NONCONFORMING, INDETERMINATE.

    risk_summary: str
    # ^^ 1-2 paragraph plain-English explanation. ALWAYS populated, even on
    #    CONFORMING (where it explains why the property is clean) and
    #    INDETERMINATE (where it explains what's missing).

    diligence_actions_required: List[str] = Field(default_factory=list)
    # ^^ Concrete next steps. Empty for CONFORMING. Populated for everything else.

    @model_validator(mode="after")
    def validate_status_consistency(self):
        """Enforce semantic consistency between status and other fields."""
        if self.status == ConformityStatus.CONFORMING:
            if self.nonconformity_details:
                raise ValueError(
                    "CONFORMING status cannot have nonconformity_details"
                )
            if self.grandfathering_status is not None:
                raise ValueError(
                    "CONFORMING status cannot have grandfathering_status"
                )

        if self.status in {
            ConformityStatus.LEGAL_NONCONFORMING_USE,
            ConformityStatus.LEGAL_NONCONFORMING_DENSITY,
            ConformityStatus.LEGAL_NONCONFORMING_DIMENSIONAL,
            ConformityStatus.MULTIPLE_NONCONFORMITIES,
        }:
            if not self.nonconformity_details:
                raise ValueError(
                    f"{self.status.value} requires at least one nonconformity_details entry"
                )
            if self.grandfathering_status is None:
                raise ValueError(
                    f"{self.status.value} requires grandfathering_status to be populated"
                )

        if self.status == ConformityStatus.CONFORMITY_INDETERMINATE:
            if self.confidence != ConfidenceLevel.INDETERMINATE:
                raise ValueError(
                    "CONFORMITY_INDETERMINATE status must have INDETERMINATE confidence"
                )

        return self
```

### 3.2 DevelopmentScenario

A single development scenario. 1-3 per deal (master plan D2).

```python
class DevelopmentScenario(BaseModel):
    """
    A single development/business-plan scenario for the property.

    Master plan D1 defines what counts as a scenario. Master plan D2 caps
    the count at 3 per deal. Master plan D7 specifies the filename pattern
    that consumes scenario_id and rank.
    """
    # Identity
    scenario_id: str
    # ^^ Canonical, used in filenames. Pattern: ^[a-z][a-z0-9_]*$ (snake_case)
    #    Examples: "asbuilt_reno_21u", "byright_rebuild_6u", "variance_15u"
    #    Hard cap: 30 chars after the rank prefix is added downstream.

    rank: int  # 1, 2, or 3 — corresponds to "S{rank}" in filename
    scenario_name: str  # human-readable, e.g. "As-Built Renovation (21 Units)"
    business_thesis: str  # 2-3 sentence pitch
    verdict: ScenarioVerdict

    # Physical configuration
    unit_count: int = 0  # 0 for non-residential or vacant land
    building_sf: float = 0.0
    use_mix: List[UseAllocation] = Field(default_factory=list)

    # Operating strategy
    operating_strategy: str  # e.g. "lease residential market-rate, ground floor as artisan industrial"

    # Zoning pathway
    zoning_pathway: ZoningPathway

    # Per-scenario assumption deltas (applied to baseline assumptions in financials.py)
    construction_budget_delta_usd: Optional[float] = None  # +/- vs. baseline
    rent_delta_pct: Optional[float] = None  # +/- vs. baseline rents (e.g., 0.05 for +5%)
    timeline_delta_months: Optional[int] = None  # +/- vs. baseline timeline

    # Outputs (populated by financials.py in Session 4 — None until then)
    financial_outputs: Optional["FinancialOutputs"] = None
    excel_filename: Optional[str] = None
    # ^^ Populated by excel_builder.py in Session 4. Format defined by master plan D7.

    # Risk
    key_risks: List[str] = Field(default_factory=list)
    entitlement_risk_flag: Optional[EntitlementRiskFlag] = None

    @field_validator("scenario_id")
    @classmethod
    def validate_scenario_id_format(cls, v: str) -> str:
        """Enforce snake_case, 30-char cap, no special chars."""
        import re
        if not re.match(r"^[a-z][a-z0-9_]*$", v):
            raise ValueError(
                f"scenario_id must be snake_case starting with a letter, got '{v}'"
            )
        if len(v) > 30:
            raise ValueError(
                f"scenario_id max length is 30 chars, got {len(v)}"
            )
        return v

    @field_validator("rank")
    @classmethod
    def validate_rank_range(cls, v: int) -> int:
        """Rank must be 1, 2, or 3."""
        if v not in {1, 2, 3}:
            raise ValueError(f"rank must be 1, 2, or 3 — got {v}")
        return v
```

### 3.3 ZoningExtensions

Container for parcel-wide zoning analysis layers that don't belong on a single scenario.

```python
class ZoningExtensions(BaseModel):
    """
    Zoning analysis layers that are properties of the parcel as a whole,
    not of any one scenario.
    """
    use_flexibility_score: int  # 1-5 scale; 5 = most flexible
    use_flexibility_explanation: str  # 1-2 sentences explaining the score

    overlay_impact_assessment: List[OverlayImpact] = Field(default_factory=list)
    # ^^ Empty list if no overlays apply to the parcel.

    development_upside: Optional[DevelopmentUpside] = None
    # ^^ None when buildable capacity calc didn't run (data missing).

    cross_scenario_recommendation: str
    # ^^ 2-3 paragraph IC-style synthesis explaining the preferred call.
    #    For single-scenario deals, this is a 1-paragraph rationale.

    preferred_scenario_id: str
    # ^^ Foreign key pointer to one of the scenarios. Validates against
    #    DealData.scenarios at the DealData level (cross-field validator).

    @field_validator("use_flexibility_score")
    @classmethod
    def validate_flexibility_score(cls, v: int) -> int:
        if v < 1 or v > 5:
            raise ValueError(f"use_flexibility_score must be 1-5, got {v}")
        return v
```

---

## 4. DealData extensions

Add three new optional fields to the existing `DealData` class. Do not modify any existing fields.

```python
class DealData(BaseModel):
    # ... ALL existing fields preserved ...

    # =========================================================================
    # ZONING OVERHAUL — Session 1 additions (April 2026)
    # =========================================================================
    conformity_assessment: Optional[ConformityAssessment] = None
    # ^^ Populated by 3C-CONF in Session 3. None on deals processed before
    #    the overhaul shipped.

    scenarios: List[DevelopmentScenario] = Field(default_factory=list)
    # ^^ Populated by 3C-SCEN in Session 3. Empty list on legacy deals.
    #    Master plan D2 caps at 3 entries.

    zoning_extensions: Optional[ZoningExtensions] = None
    # ^^ Populated by 3C-HBU in Session 3. None on legacy deals.

    @model_validator(mode="after")
    def validate_scenarios_constraints(self):
        """Enforce master plan constraints on scenarios list."""
        if not self.scenarios:
            return self  # empty is fine — legacy deals or pre-3C-SCEN

        # D2: hard cap of 3 scenarios
        if len(self.scenarios) > 3:
            raise ValueError(
                f"DealData.scenarios cannot exceed 3 entries (got {len(self.scenarios)})"
            )

        # Rank uniqueness within the list
        ranks = [s.rank for s in self.scenarios]
        if len(set(ranks)) != len(ranks):
            raise ValueError(
                f"DealData.scenarios ranks must be unique, got {ranks}"
            )

        # Exactly one scenario must be PREFERRED
        preferred_count = sum(
            1 for s in self.scenarios if s.verdict == ScenarioVerdict.PREFERRED
        )
        if preferred_count != 1:
            raise ValueError(
                f"Exactly one scenario must have verdict=PREFERRED, got {preferred_count}"
            )

        # The PREFERRED scenario must have rank=1
        preferred_scenario = next(
            s for s in self.scenarios if s.verdict == ScenarioVerdict.PREFERRED
        )
        if preferred_scenario.rank != 1:
            raise ValueError(
                "The PREFERRED scenario must have rank=1"
            )

        # zoning_extensions.preferred_scenario_id must match a scenario
        if self.zoning_extensions is not None:
            scenario_ids = {s.scenario_id for s in self.scenarios}
            if self.zoning_extensions.preferred_scenario_id not in scenario_ids:
                raise ValueError(
                    f"zoning_extensions.preferred_scenario_id "
                    f"'{self.zoning_extensions.preferred_scenario_id}' "
                    f"does not match any scenario in scenarios list"
                )

        return self
```

---

## 5. Utility function

The single sanctioned write path between scenarios and the legacy `deal.financial_outputs` field. Per master plan D6.

```python
def mirror_preferred_to_legacy(deal: DealData) -> None:
    """
    Mirror the preferred scenario's financial_outputs to deal.financial_outputs.

    Per master plan D6: deal.financial_outputs is a derived field on the new
    multi-scenario path. This function is the only sanctioned write path.
    Direct writes to deal.financial_outputs should be avoided in new code.

    Args:
        deal: The DealData object to update. Mutates in place.

    Raises:
        ValueError: If deal has no scenarios, or if no scenario is PREFERRED,
                    or if the preferred scenario has no financial_outputs yet.
    """
    if not deal.scenarios:
        raise ValueError(
            "Cannot mirror: deal.scenarios is empty. "
            "This function should only be called after scenarios are populated."
        )

    preferred = next(
        (s for s in deal.scenarios if s.verdict == ScenarioVerdict.PREFERRED),
        None,
    )
    if preferred is None:
        raise ValueError(
            "Cannot mirror: no scenario has verdict=PREFERRED. "
            "DealData validators should have caught this."
        )

    if preferred.financial_outputs is None:
        raise ValueError(
            f"Cannot mirror: preferred scenario '{preferred.scenario_id}' "
            f"has no financial_outputs populated yet. "
            f"Call this AFTER financials.py fan-out completes."
        )

    deal.financial_outputs = preferred.financial_outputs
```

---

## 6. Imports to add at top of models.py

The new code requires these imports. Add to existing import block in `models.py`:

```python
import re  # for scenario_id regex validation
from enum import Enum  # likely already present; confirm
from typing import List, Optional  # likely already present; confirm
from pydantic import BaseModel, Field, field_validator, model_validator  # confirm field_validator and model_validator
```

If `field_validator` or `model_validator` is not imported (older Pydantic v1 style codebase), the implementation will need adjustment. Confirm Pydantic version in use; the code above assumes Pydantic v2 syntax. Project memory indicates `models.py` is Pydantic v2 — verify before implementing.

---

## 7. Forward reference resolution

The `DevelopmentScenario.financial_outputs: Optional["FinancialOutputs"]` field uses a forward reference because `FinancialOutputs` may be defined elsewhere in `models.py`. After all model classes are defined, add at the bottom of the new section:

```python
# Resolve forward references introduced in Session 1
DevelopmentScenario.model_rebuild()
ConformityAssessment.model_rebuild()
ZoningExtensions.model_rebuild()
DealData.model_rebuild()
```

---

## 8. Backward compatibility verification

Session 1 must not break any existing code. Verify these paths still work:

1. **Existing DealData deserialization** — load any existing deal fixture from disk; it must deserialize cleanly with new optional fields defaulting to None or empty list
2. **Existing report rendering** — `report_builder.py` currently reads `deal.financial_outputs`; this field still exists, so old reports still render
3. **Existing financial pipeline** — `financials.py` still writes to `deal.financial_outputs`; still works (Session 4 will refactor to also write to scenarios)

**Concrete backward compatibility test (gate criterion):**

```python
# In Claude Code session, run this verification:
from models import DealData

# Load an existing deal fixture (from prior runs in /outputs/)
deal = DealData.parse_file("path/to/existing_deal_fixture.json")

# Verify new fields default correctly
assert deal.conformity_assessment is None
assert deal.scenarios == []
assert deal.zoning_extensions is None

# Verify existing fields still readable
assert deal.financial_outputs is not None  # if the fixture had outputs

print("Backward compatibility verified.")
```

---

## 9. Test fixture for gate verification

Create a test fixture file at `tests/fixtures/zoning_overhaul_session_1_fixture.json` that exercises all new models. This is part of Session 1's deliverable and is what subsequent sessions will use for testing.

The fixture represents 967-73 N. 9th St with 3 scenarios. Save the following JSON:

```json
{
  "deal_id": "session_1_test_fixture",
  "address": "967-73 N. 9th Street, Philadelphia, PA 19123",

  "conformity_assessment": {
    "status": "LEGAL_NONCONFORMING_DENSITY",
    "confidence": "HIGH",
    "confidence_reasons": [
      "ICMX zoning code source-verified from Philadelphia Title 14",
      "5 of 6 dimensional standards populated",
      "13 permitted uses listed"
    ],
    "nonconformity_details": [
      {
        "nonconformity_type": "DENSITY",
        "existing_value": "21 units",
        "permitted_value": "6 units (1,440 SF/unit min × 9,652 SF lot)",
        "magnitude_description": "Existing density exceeds by-right cap by 250%",
        "triggers_loss_of_grandfathering": [
          "Substantial improvement (>50% of replacement cost)",
          "Change of use category",
          "Abandonment of use for more than 12 consecutive months"
        ]
      }
    ],
    "grandfathering_status": {
      "is_documented": false,
      "documentation_source": null,
      "presumption_basis": "Building constructed 1930, predates current ICMX density standards",
      "confirmation_action_required": "Zoning verification letter from Philadelphia L&I confirming grandfathered nonconforming residential density",
      "risk_if_denied": "Property reverts to 6-unit by-right cap, collapsing the 21-unit underwriting thesis and reducing stabilized NOI by approximately 71%"
    },
    "risk_summary": "The property's existing 21-unit residential configuration exceeds the ICMX district's by-right density cap of 6 units (calculated from the 1,440 SF minimum lot area per unit standard applied to the 9,652 SF lot). The use is presumptively grandfathered as a legal nonconforming density given the 1930 construction date, which predates current ICMX standards. However, no certificate of nonconforming use is on file. The proposed renovation must remain within Philadelphia's substantial-improvement threshold (typically 50% of replacement cost) to preserve grandfathering. A zoning verification letter from L&I is a non-negotiable pre-closing deliverable.",
    "diligence_actions_required": [
      "Submit formal request to Philadelphia L&I for zoning verification letter",
      "Confirm renovation budget remains under substantial-improvement threshold",
      "Verify no period of abandonment over 12 months in property's recent history"
    ]
  },

  "scenarios": [
    {
      "scenario_id": "asbuilt_reno_21u",
      "rank": 1,
      "scenario_name": "As-Built Renovation (21 Units)",
      "business_thesis": "Renovate the existing 1930 mixed-use building to deliver 21 residential units across floors 2-4 while preserving two ground-floor commercial tenants. Relies on grandfathered nonconforming density status.",
      "verdict": "PREFERRED",
      "unit_count": 21,
      "building_sf": 26424,
      "use_mix": [
        {"use_category": "residential", "square_feet": 21668, "unit_count": 21, "notes": "floors 2-4"},
        {"use_category": "commercial", "square_feet": 4756, "unit_count": null, "notes": "ground floor, two tenants"}
      ],
      "operating_strategy": "Lease residential as market-rate apartments; retain existing commercial tenants on month-to-month leases at $2,000 and $2,200/month",
      "zoning_pathway": {
        "pathway_type": "BY_RIGHT",
        "approval_body": "Philadelphia Department of Licenses and Inspections",
        "estimated_timeline_months": 3,
        "estimated_soft_cost_usd": 25000,
        "success_probability_pct": 85,
        "fallback_if_denied": "If L&I rules grandfathering is lost, fall back to Scenario 2 (by-right rebuild at 6 units)"
      },
      "construction_budget_delta_usd": null,
      "rent_delta_pct": null,
      "timeline_delta_months": null,
      "financial_outputs": null,
      "excel_filename": null,
      "key_risks": [
        "Grandfathering confirmation from L&I is a gating diligence item",
        "Substantial improvement threshold must not be exceeded",
        "Two existing commercial tenants on month-to-month — limited income predictability"
      ],
      "entitlement_risk_flag": {
        "severity": "MEDIUM",
        "risk_summary": "While the pathway is by-right contingent on grandfathered status, the absence of a documented nonconforming use certificate creates moderate risk that L&I could deny the verification letter. If denied, the deal collapses to Scenario 2 economics.",
        "diligence_required": [
          "Pre-meeting with L&I planner before LOI execution",
          "Title search for any prior abandonment-of-use claims",
          "Insurance ordinance-and-law endorsement to address rebuild risk"
        ]
      }
    },
    {
      "scenario_id": "byright_rebuild_6u",
      "rank": 2,
      "scenario_name": "By-Right Rebuild (6 Units)",
      "business_thesis": "Demolish the existing structure and rebuild a new 6-unit mixed-use building under current ICMX by-right standards. Eliminates grandfathering risk but reduces unit count by 71%.",
      "verdict": "ALTERNATE",
      "unit_count": 6,
      "building_sf": 12000,
      "use_mix": [
        {"use_category": "residential", "square_feet": 9000, "unit_count": 6, "notes": "floors 2-3"},
        {"use_category": "commercial", "square_feet": 3000, "unit_count": null, "notes": "ground floor"}
      ],
      "operating_strategy": "New construction with modern unit finishes targeting market-rate Northern Liberties rents; lease ground floor commercial as artisan industrial or retail",
      "zoning_pathway": {
        "pathway_type": "BY_RIGHT",
        "approval_body": "Philadelphia Department of Licenses and Inspections",
        "estimated_timeline_months": 6,
        "estimated_soft_cost_usd": 75000,
        "success_probability_pct": 95,
        "fallback_if_denied": "Not applicable — by-right under current ICMX"
      },
      "construction_budget_delta_usd": 1500000,
      "rent_delta_pct": 0.10,
      "timeline_delta_months": 12,
      "financial_outputs": null,
      "excel_filename": null,
      "key_risks": [
        "71% reduction in unit count materially compresses NOI vs. preferred",
        "Demolition and ground-up construction adds 12 months to timeline",
        "Higher per-unit construction cost on new build vs. renovation"
      ],
      "entitlement_risk_flag": null
    },
    {
      "scenario_id": "variance_15u",
      "rank": 3,
      "scenario_name": "Variance Pathway (15 Units)",
      "business_thesis": "Pursue a density variance from the Philadelphia Zoning Board of Adjustment for 15 units — splitting the difference between the by-right 6 and the existing 21. Eliminates grandfathering risk while preserving most of the unit economics.",
      "verdict": "ALTERNATE",
      "unit_count": 15,
      "building_sf": 22000,
      "use_mix": [
        {"use_category": "residential", "square_feet": 18000, "unit_count": 15, "notes": "floors 2-4"},
        {"use_category": "commercial", "square_feet": 4000, "unit_count": null, "notes": "ground floor"}
      ],
      "operating_strategy": "Renovate-and-rebuild hybrid: preserve facade and structure where possible while reconfiguring upper floors to accommodate 15 units",
      "zoning_pathway": {
        "pathway_type": "VARIANCE",
        "approval_body": "Philadelphia Zoning Board of Adjustment",
        "estimated_timeline_months": 9,
        "estimated_soft_cost_usd": 150000,
        "success_probability_pct": 60,
        "fallback_if_denied": "Fall back to Scenario 2 (by-right rebuild at 6 units)"
      },
      "construction_budget_delta_usd": 750000,
      "rent_delta_pct": 0.05,
      "timeline_delta_months": 9,
      "financial_outputs": null,
      "excel_filename": null,
      "key_risks": [
        "Variance approval is at ZBA discretion — 60% success probability",
        "9-month entitlement timeline carries significant carry cost",
        "$150k variance soft cost is sunk if denied"
      ],
      "entitlement_risk_flag": {
        "severity": "HIGH",
        "risk_summary": "Variance approvals in Philadelphia are subject to a hardship test that is increasingly difficult to meet. The 60% success probability reflects the corridor's recent ZBA precedent on density variances; recent denials in adjacent submarkets create headline risk.",
        "diligence_required": [
          "Pre-application meeting with ZBA staff",
          "Community engagement with Northern Liberties Neighbors Association",
          "Comparable variance precedent research within 1-mile radius"
        ]
      }
    }
  ],

  "zoning_extensions": {
    "use_flexibility_score": 5,
    "use_flexibility_explanation": "ICMX zoning permits the broadest range of uses among Philadelphia's commercial districts including residential, light industrial, artisan, office, retail, and restaurant — providing maximum flexibility for repositioning",
    "overlay_impact_assessment": [],
    "development_upside": {
      "far_remaining_sf": 21836,
      "units_remaining": -15,
      "height_remaining_ft": 22,
      "summary": "FAR cap of 5.0 leaves substantial theoretical capacity (21,836 SF), but density and height caps bind well below FAR. Existing 21 units exceeds by-right density cap by 15 units."
    },
    "cross_scenario_recommendation": "Scenario 1 (As-Built Renovation, 21 units) is the preferred path given its by-right entitlement assuming grandfathering confirms, its lower construction cost vs. ground-up rebuild, and its preservation of the 1930 building's character which may qualify for federal Historic Tax Credits. The $1.1M acquisition basis at $41/SF makes this scenario decisively accretive at the 21-unit economics, with stabilized Year 2 NOI of $343,605 supporting an LP IRR of 37.1%.\n\nScenario 2 (By-Right Rebuild, 6 units) is the recommended fallback if grandfathering is denied. While unit economics are dramatically reduced, the by-right pathway eliminates entitlement risk and preserves capital. This scenario should be modeled and pre-funded as an insurance policy.\n\nScenario 3 (Variance Pathway, 15 units) is mathematically attractive but operationally fragile. The 60% success probability, 9-month timeline, and $150k sunk-cost-on-denial make this option higher-risk than its IRR suggests. Recommended only if Scenario 1 grandfathering is denied AND the sponsor has demonstrated variance success in the corridor.",
    "preferred_scenario_id": "asbuilt_reno_21u"
  }
}
```

This fixture is the regression test that every subsequent session must pass. Save it before declaring Session 1 done.

---

## 10. Gate criteria checklist (final)

Per master plan, all of these must pass before Session 2 begins:

- [ ] New `models.py` imports cleanly with no syntax or type errors
- [ ] Test fixture deserializes into `DealData` without exception
- [ ] `mirror_preferred_to_legacy(deal)` produces correct output on the test fixture (after mock financial_outputs are populated)
- [ ] `mirror_preferred_to_legacy(deal)` raises `ValueError` on empty scenarios list
- [ ] `mirror_preferred_to_legacy(deal)` raises `ValueError` on missing PREFERRED verdict
- [ ] `ConformityAssessment` validator rejects `CONFORMING` status with nonconformity_details
- [ ] `ConformityAssessment` validator rejects `LEGAL_NONCONFORMING_*` status without grandfathering_status
- [ ] `DevelopmentScenario` validator rejects scenario_id with special characters
- [ ] `DevelopmentScenario` validator rejects scenario_id over 30 chars
- [ ] `DevelopmentScenario` validator rejects rank outside {1, 2, 3}
- [ ] `DealData` validator rejects more than 3 scenarios
- [ ] `DealData` validator rejects duplicate ranks across scenarios
- [ ] `DealData` validator rejects zero or multiple PREFERRED verdicts
- [ ] `DealData` validator rejects PREFERRED scenario without rank=1
- [ ] `DealData` validator rejects mismatched preferred_scenario_id
- [ ] Existing deal fixture (from prior runs) still deserializes with new optional fields defaulting correctly
- [ ] `git diff` shows changes ONLY in `models.py` and the new fixture file (no scope creep)
- [ ] Commit message matches format: `Session 1: Schema (models.py) — Add ConformityAssessment, DevelopmentScenario, ZoningExtensions`
- [ ] Repo tagged: `zoning-overhaul-session-1-passed`
- [ ] Master plan document updated: Session 1 status set to COMPLETED with timestamp

---

## 11. Session 1 estimated effort

**Code volume:** Approximately 350 lines added to `models.py` (5 enums, 8 sub-models, 3 top-level models, 1 utility function, validators, comments). Plus a ~250-line JSON test fixture.

**Time:** 3-5 hours of Claude Code work, including testing.

**Difficulty:** Low. This is mechanical schema work. The hard analytical decisions (what data structures we need, how they relate) are all locked in this document. The implementation itself is straightforward Pydantic.

**Highest risk:** Forgetting `model_rebuild()` calls for forward references, which would cause runtime errors when `DevelopmentScenario.financial_outputs` is accessed. Test deserialization carefully.

---

*End of Session 1 schema design specification. After implementation, update master plan and proceed to Session 2 prompt design.*
