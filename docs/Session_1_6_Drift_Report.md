# Session 1.6 — Schema/Prompt Drift Report

**Document version:** 1.0 — DRAFT FOR MIKE'S REVIEW
**Created:** April 28, 2026
**Status:** Awaiting design decisions in claude.ai before Session 1.6 begins
**Produced by:** Claude Code during Session 3 reading phase (no code changes made)
**Purpose:** Input artifact for Session 1.6, a schema realignment micro-session that must precede Session 3's pipeline wiring

---

## How to use this document

This report was generated during the reading phase of Session 3 (the pipeline-wiring session). Session 3 was halted before any code was written because the Session 2 prompt JSON shapes do not align with the Pydantic schema implemented in Sessions 1 and 1.5. Bridging the two in Session 3's `_apply_3c_*` functions would force the agent to make design decisions that belong with Mike — and would leave the regression test set internally inconsistent.

This document enumerates every drift point, classifies the severity of each, and identifies the file location where each schema fix would land in `models/models.py`. The intended workflow is:

1. Mike and Claude review this report in claude.ai and pick a fix per drift point
2. The fixes are written into a Session 1.6 kickoff sheet
3. A Claude Code session (Session 1.6) implements the schema realignment
4. Session 3 resumes from Checkpoint 1 with the bridging problem dissolved

The Session 3 plan summary itself (deliverables, line numbers, checkpoints, commit message) is unchanged by this realignment — it can be re-used as-is once Session 1.6 closes.

---

## Severity classification

Each drift point below is tagged with one of four severities:

- **(a) Trivial rename** — fix in schema or prompt, no design implications
- **(b) Information loss** — schema gains a field or enum value; no semantic change
- **(c) Structural** — nested vs. flat, or list vs. string; requires a design decision about which shape is correct
- **(d) Semantic divergence** — the two artifacts model different concepts; requires a design decision about which mental model is correct

(a) and (b) can be resolved with mechanical edits. (c) and (d) need Mike's judgment.

---

## Drift point #1 — NonconformityType setback split

**Prompt JSON field:** `nonconformity_details[*].nonconformity_type` enum value `FRONT_SETBACK | REAR_SETBACK | SIDE_SETBACK`
**Schema field:** `NonconformityType.SETBACKS` (single value, no directional split)
**Issue:** The prompt expects three distinct setback variants; the schema enum has one coarse `SETBACKS` value. A prompt response with `FRONT_SETBACK` will fail Pydantic enum validation outright.
**Severity:** **(b) Information loss with a small design choice**
**Recommended fix:** Add `FRONT_SETBACK`, `REAR_SETBACK`, `SIDE_SETBACK` to the schema enum. Two sub-options for `SETBACKS`:
  - **Option B1:** Keep `SETBACKS` as a coarse fallback for cases where the prompt can't isolate which side. Recommended — preserves backward compatibility with the existing fixture and gives the LLM an escape hatch.
  - **Option B2:** Drop `SETBACKS` and require directional precision. Cleaner, but forces a backfill of the existing fixture and removes the escape hatch.
**Schema location:** `models/models.py` lines 1331–1345 (`NonconformityType` enum)

---

## Drift point #2 — `actual_value` vs `existing_value`

**Prompt JSON field:** `nonconformity_details[*].actual_value`
**Schema field:** `NonconformityItem.existing_value`
**Issue:** Same concept, different identifier. Pydantic will silently drop the prompt's `actual_value` and refuse to populate the required `existing_value` field, raising on construction.
**Severity:** **(a) Trivial rename**
**Recommended fix:** Rename schema field `existing_value` → `actual_value`. The prompt phrasing ("What the property has") matches the schema docstring ("e.g. '21 units'"); both refer to the same concept, but the prompt name is the more conventional appraisal-report term and is what the LLM will produce naturally.
**Schema location:** `models/models.py` line 1380 (`NonconformityItem.existing_value: str`)

---

## Drift point #3 — `standard_description` extra prompt field

**Prompt JSON field:** `nonconformity_details[*].standard_description` ("Brief plain-English label")
**Schema field:** *(no equivalent)*
**Issue:** Prompt produces this field; schema has no place to put it. It would be silently dropped by Pydantic's default extra-field handling, but the information is useful for report rendering.
**Severity:** **(b) Information loss**
**Recommended fix:** Add `standard_description: Optional[str] = None` to `NonconformityItem`. It's a short label that explains what the dimensional standard is (e.g., "Density cap (units per lot SF)") — useful for §06 report rendering as a row label in the nonconformity table.
**Schema location:** `models/models.py` lines 1374–1383 (`NonconformityItem` model)

---

## Drift point #4 — `triggers_loss_of_grandfathering` in schema, no prompt source

**Prompt JSON field:** *(no equivalent at the nonconformity-item level)*
**Schema field:** `NonconformityItem.triggers_loss_of_grandfathering: List[str]`
**Issue:** The schema attaches loss triggers per-nonconformity item. The prompt instead returns loss triggers once at the property level via `grandfathering_status.loss_triggers`. Different cardinality, same information.
**Severity:** **(c) Structural** — design decision required
**Recommended fix:** Pick one location for loss triggers, not both:
  - **Option C4a (recommended):** Drop `NonconformityItem.triggers_loss_of_grandfathering` and rely on `GrandfatheringStatus.loss_triggers` as the single source. Loss triggers are a property of the grandfathering posture, not of any one nonconformity, so the prompt's structure is correct.
  - **Option C4b:** Keep per-nonconformity triggers and update the prompt to produce them per item. More work for the LLM with marginal benefit.
**Schema location:** `models/models.py` line 1383 (`NonconformityItem.triggers_loss_of_grandfathering`)

---

## Drift point #5 — GrandfatheringStatus complete field set mismatch

**Prompt JSON field:** `grandfathering_status: { is_presumed_grandfathered, basis, loss_triggers, verification_required }`
**Schema field:** `GrandfatheringStatus: { is_documented, documentation_source, presumption_basis, confirmation_action_required, risk_if_denied }`
**Issue:** The two artifacts model different mental models of grandfathering:
  - Schema: "Is grandfathering documented? If so, by what record? If not, what's the presumption? What action would confirm? What's the risk if confirmation fails?"
  - Prompt: "Is grandfathering presumed? On what basis? What would void it? Does it need verification?"

There is no clean field-by-field mapping. `is_presumed_grandfathered=True` does not imply `is_documented=False` (a property can be both presumed AND documented). `risk_if_denied` has no prompt source. `loss_triggers` has no schema home.
**Severity:** **(d) Semantic divergence** — design decision required
**Recommended fix:** Pick the prompt's mental model with one schema addition. New `GrandfatheringStatus` shape:
```python
class GrandfatheringStatus(BaseModel):
    is_presumed_grandfathered: bool
    is_documented: bool = False                          # kept from schema — useful for IC
    documentation_source: Optional[str] = None           # kept from schema
    basis: str                                            # from prompt; replaces presumption_basis
    loss_triggers: List[str] = Field(default_factory=list)   # from prompt
    verification_required: bool = True                    # from prompt
    confirmation_action_required: Optional[str] = None    # kept from schema
    risk_if_denied: Optional[str] = None                  # kept from schema; downgraded to optional
```
This keeps the IC-relevant schema fields (documentation status, risk-if-denied) while accepting the prompt's primary axes (presumption + triggers). Apply function in 3C-CONF populates the prompt-sourced fields directly; the schema-only fields are derived heuristics or left null.
**Schema location:** `models/models.py` lines 1386–1397 (`GrandfatheringStatus` model)

---

## Drift point #6 — ConformityStatus enum mismatch

**Prompt JSON field:** `status` enum values: `CONFORMING | LEGAL_NONCONFORMING_USE | LEGAL_NONCONFORMING_DIMENSIONAL | ILLEGAL_NONCONFORMING | VARIANCE_REQUIRED_FOR_PROPOSED | SPECIAL_EXCEPTION_REQUIRED_FOR_PROPOSED | CONFORMITY_INDETERMINATE`
**Schema field:** `ConformityStatus` enum values: `CONFORMING | LEGAL_NONCONFORMING_USE | LEGAL_NONCONFORMING_DENSITY | LEGAL_NONCONFORMING_DIMENSIONAL | MULTIPLE_NONCONFORMITIES | ILLEGAL_NONCONFORMING | CONFORMITY_INDETERMINATE`
**Issue:** Two diverged enum sets:
  - Prompt has `VARIANCE_REQUIRED_FOR_PROPOSED` and `SPECIAL_EXCEPTION_REQUIRED_FOR_PROPOSED` (the schema does not)
  - Schema has `LEGAL_NONCONFORMING_DENSITY` and `MULTIPLE_NONCONFORMITIES` (the prompt does not)

Conceptually, the prompt's two extras describe *the proposed business plan's* relationship to zoning, while the schema's two extras refine *the existing condition's* relationship. Both axes are real; conflating them into one enum is the root cause.
**Severity:** **(d) Semantic divergence** — design decision required
**Recommended fix:** Split `status` into two enums with separate concerns:
```python
class ConformityStatus(str, Enum):
    """Existing-condition conformity. Always populated."""
    CONFORMING                       = "CONFORMING"
    LEGAL_NONCONFORMING_USE          = "LEGAL_NONCONFORMING_USE"
    LEGAL_NONCONFORMING_DENSITY      = "LEGAL_NONCONFORMING_DENSITY"
    LEGAL_NONCONFORMING_DIMENSIONAL  = "LEGAL_NONCONFORMING_DIMENSIONAL"
    MULTIPLE_NONCONFORMITIES         = "MULTIPLE_NONCONFORMITIES"
    ILLEGAL_NONCONFORMING            = "ILLEGAL_NONCONFORMING"
    CONFORMITY_INDETERMINATE         = "CONFORMITY_INDETERMINATE"

class ProposedPathwayRequirement(str, Enum):
    """Proposed business plan's entitlement requirement. Optional."""
    NONE                              = "NONE"     # proposed plan needs no discretionary approval
    VARIANCE_REQUIRED                 = "VARIANCE_REQUIRED"
    SPECIAL_EXCEPTION_REQUIRED        = "SPECIAL_EXCEPTION_REQUIRED"
    REZONE_REQUIRED                   = "REZONE_REQUIRED"
```
Then add `proposed_pathway_requirement: Optional[ProposedPathwayRequirement] = None` to `ConformityAssessment`. Update prompt 3C-CONF to return both `status` (existing-condition only) and `proposed_pathway_requirement` (or null if N/A). This restores the per-axis precision both designers had in mind.
**Schema location:** `models/models.py` lines 1298–1312 (`ConformityStatus` enum) + `ConformityAssessment` model at lines 1495–1547

---

## Drift point #7 — DevelopmentScenario nested vs. flat + multiple field renames

**Prompt JSON field:** Nested:
```json
{
  "physical_config": { "unit_count", "building_sf", "use_mix": [{"use_label", "sf", "share_pct"}] },
  "zoning_pathway": { "pathway_type", "rationale", "success_probability_pct", "estimated_timeline_months", "estimated_pathway_cost_usd" },
  "assumption_deltas": { "construction_budget_usd", "rent_premium_pct_vs_baseline", "operating_strategy_note", "timeline_months_to_stabilization" },
  "entitlement_risk_flag": { "severity", "description" }
}
```
**Schema field:** Flat at the scenario level:
```python
DevelopmentScenario:
    unit_count, building_sf, use_mix: List[UseAllocation{use_category, square_feet, unit_count, notes}],
    zoning_pathway: ZoningPathway{pathway_type, approval_body, estimated_timeline_months,
                                  estimated_soft_cost_usd, success_probability_pct, fallback_if_denied},
    construction_budget_delta_usd, rent_delta_pct, timeline_delta_months, operating_strategy,
    entitlement_risk_flag: EntitlementRiskFlag{severity, risk_summary, diligence_required},
    key_risks: List[str]
```
**Issue:** Three sub-issues stacked:
1. **Nested vs. flat** — prompt nests `physical_config` and `assumption_deltas`; schema flattens. Structural mismatch.
2. **Field renames** — multiple:
   - prompt `use_label / sf / share_pct` vs. schema `use_category / square_feet / (no share field) / unit_count / notes`
   - prompt `construction_budget_usd` vs. schema `construction_budget_delta_usd` (semantic difference: total vs. delta)
   - prompt `rent_premium_pct_vs_baseline` vs. schema `rent_delta_pct`
   - prompt `operating_strategy_note` vs. schema `operating_strategy`
   - prompt `timeline_months_to_stabilization` vs. schema `timeline_delta_months` (semantic difference: absolute vs. delta)
   - prompt `estimated_pathway_cost_usd` vs. schema `estimated_soft_cost_usd`
3. **Asymmetric extras** — schema has `approval_body`, `fallback_if_denied`, `key_risks`, `EntitlementRiskFlag.risk_summary`, `EntitlementRiskFlag.diligence_required`; prompt has `zoning_pathway.rationale`, `use_mix[*].share_pct`.

The semantic differences in #2 are subtle but important: `construction_budget_usd` (total) vs. `construction_budget_delta_usd` (delta against baseline) are *different numbers* with different downstream consequences in Session 4's financial fan-out. Same for `timeline_months_to_stabilization` vs. `timeline_delta_months`.

**Severity:** **(c) Structural** — design decision required, plus several **(a)** trivial renames and one borderline **(d)** on the total-vs-delta semantic
**Recommended fix:** Three-part:
  - **Flatten the prompt** to match the schema's flat structure. Have 3C-SCEN return flat fields directly. Nesting was a stylistic choice in the prompt; flattening preserves the schema's existing shape with no information loss.
  - **Rename each pair** to a single canonical name. Prefer the schema names where they're more conventional (`use_category`, `square_feet`); prefer prompt names where they're more conventional (`rationale`). Specifically: keep schema names for `use_category`, `square_feet`, `unit_count`, `operating_strategy`; rename prompt-side to match. Keep prompt name `rationale` and add it to `ZoningPathway`.
  - **Resolve total-vs-delta semantics explicitly.** Critical decision for Session 4. Recommend: schema is canonical (delta-against-baseline), since Session 4's financial fan-out is delta-based. Update the prompt to ask for deltas with explicit instructions — and require the LLM to anchor each delta against the baseline_assumptions input it receives.
  - **Add `share_pct` to `UseAllocation`** and `rationale` to `ZoningPathway` — both useful for §06 rendering and IC review.
  - **Drop nothing** — schema's `approval_body`, `fallback_if_denied`, `key_risks`, `EntitlementRiskFlag.risk_summary`, `EntitlementRiskFlag.diligence_required` are all useful and should be added to the prompt's output schema. These are real IC-grade fields that the prompt was missing, not schema bloat.

**Schema location:**
- `DevelopmentScenario` lines 1550–1609
- `ZoningPathway` lines 1400–1410
- `UseAllocation` lines 1425–1433
- `EntitlementRiskFlag` lines 1413–1422

---

## Drift point #8 — ZoningExtensions nested score + structured-list overlay vs. paragraph

**Prompt JSON field:**
```json
{
  "use_flexibility_score": { "score": 3, "rationale": "..." },
  "overlay_impact_assessment": "Short paragraph on overlay materiality."
}
```
**Schema field:**
```python
ZoningExtensions:
    use_flexibility_score: int           # flat
    use_flexibility_explanation: str     # flat
    overlay_impact_assessment: List[OverlayImpact]   # structured list, one per overlay
```
**Issue:** Two sub-issues:
1. **Use-flexibility nested vs. flat** — minor structural mismatch. Trivial to flatten the prompt.
2. **Overlay impact: structured list vs. paragraph** — significant. Schema's `List[OverlayImpact]` carries `overlay_name`, `overlay_type`, `impact_summary`, `triggers_review`, `additional_diligence` *per overlay*. The prompt produces a single narrative paragraph that conflates everything. The schema is materially richer for §06 rendering (per-overlay rows in a table) and for diligence-checklist generation.

**Severity:** **(c) Structural** — design decision required for #8.2; #8.1 is **(a)**
**Recommended fix:**
  - **#8.1:** Flatten prompt's `use_flexibility_score` to two top-level fields: `use_flexibility_score: int` and `use_flexibility_explanation: str`. Match schema names.
  - **#8.2:** Update the prompt to return a structured list. New 3C-HBU output shape:
    ```json
    "overlay_impact_assessment": [
      {
        "overlay_name": "MIH Overlay",
        "overlay_type": "incentive",
        "impact_summary": "...",
        "triggers_review": false,
        "additional_diligence": ["..."]
      }
    ]
    ```
    Include a documented "if no overlays apply, return empty list" rule in the prompt. The renderer in Session 5 can collapse an empty list to the "no overlay districts apply to this parcel" treatment.

**Schema location:**
- `ZoningExtensions` lines 1612–1631
- `OverlayImpact` lines 1436–1445

---

## Cross-cutting issue — Reference Deal A test expectation contradiction

**Separate from the eight drift points above, but must be resolved alongside them.**

The master plan and the Session 1 fixture file (`tests/fixtures/zoning_overhaul_session_1_fixture.json`) describe Reference Deal A (967-73 N. 9th Street) with `ConformityStatus.LEGAL_NONCONFORMING_DENSITY`:
- Master plan, line 303: *"967-73 N. 9th Street, Philadelphia (LEGAL_NONCONFORMING_DENSITY)"*
- Master plan §1 deliverable example, line 93: *"`LEGAL_NONCONFORMING_DENSITY` — Use complies but unit count or FAR exceeds current limits *(this is 967-73 N. 9th St)*"*
- Session 1 fixture (`tests/fixtures/zoning_overhaul_session_1_fixture.json`): `"status": "LEGAL_NONCONFORMING_DENSITY"`

But the Session 2 spec describes the *same property* with `LEGAL_NONCONFORMING_DIMENSIONAL`:
- Session 2 spec §2.7, line 233: *"status: `LEGAL_NONCONFORMING_DIMENSIONAL` (use itself is permitted; density nonconforms)"*
- Catalog v5 §4 Deal A, line 641: *"status `LEGAL_NONCONFORMING_DIMENSIONAL`"*

These are inconsistent. The status of the regression-test gold fixture cannot be both. Resolution must be made deliberately, not by whichever document the apply-function-author opens first.

**Recommended resolution:** Keep `LEGAL_NONCONFORMING_DENSITY` as the canonical status for Deal A. Two reasons:
1. **Master plan precedence** — the master plan is the explicit source of truth document. All sub-specs are derived from it.
2. **Semantic precision** — density is a distinct dimensional standard from height, FAR, setbacks, and lot coverage. Treating density as a first-class nonconformity status (rather than collapsing into the `_DIMENSIONAL` bucket) lets §06 reports carry density-specific risk language without ambiguity. This is also why drift #6 recommends keeping `LEGAL_NONCONFORMING_DENSITY` in the schema enum.

**Action:** Session 1.6 should update Session 2 spec (`docs/Session_2_Prompt_Specification.md` §2.7) and Catalog v5 (`docs/FINAL_APPROVED_Prompt_Catalog_v5.md` §4 Deal A) to use `LEGAL_NONCONFORMING_DENSITY` to match the master plan. The prompt 3C-CONF system text should also be updated to include `LEGAL_NONCONFORMING_DENSITY` and `MULTIPLE_NONCONFORMITIES` in the status enum list (these were dropped when the Session 2 spec was drafted in claude.ai).

---

## Summary of recommended Session 1.6 work

| # | Drift | Severity | Resolution touches |
|---|---|---|---|
| 1 | Setback enum split | (b) | `NonconformityType` enum |
| 2 | `actual_value` rename | (a) | `NonconformityItem.existing_value` |
| 3 | `standard_description` extra | (b) | `NonconformityItem` (add field) |
| 4 | Loss triggers location | (c) | `NonconformityItem` (drop field) |
| 5 | GrandfatheringStatus reshape | (d) | `GrandfatheringStatus` (replace) |
| 6 | ConformityStatus + add proposed-pathway enum | (d) | `ConformityStatus`, `ConformityAssessment`, new enum |
| 7 | DevelopmentScenario flatten + renames + delta semantics | (c)+(a)+(d-ish) | `DevelopmentScenario`, `ZoningPathway`, `UseAllocation`, `EntitlementRiskFlag` |
| 8 | ZoningExtensions flatten + overlay-list reshape | (a)+(c) | `ZoningExtensions`, `OverlayImpact` |
| ✱ | Deal A status contradiction | doc-only | Session 2 spec §2.7, Catalog v5 §4 |

Total schema work: 1 enum split, 1 enum addition, 1 new enum, 5 model field reshapes, 1 docs alignment. All additive or rename-style — no destructive deletions of fields downstream code currently depends on.

Estimated Session 1.6 effort: 90 minutes for the schema realignment + ~30 minutes for the prompt-text and Session 2 spec doc updates. Session 1.6 is purely additive/rename schema work — it does not touch `market.py`, `financials.py`, or any other module.

---

*End of Session 1.6 drift report. Awaiting Mike's review and design decisions in claude.ai before Session 1.6 begins.*
