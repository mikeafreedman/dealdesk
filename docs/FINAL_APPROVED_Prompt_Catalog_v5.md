# FINAL_APPROVED_Prompt_Catalog_v5.md

**DealDesk CRE Underwriting System — Approved Claude API Prompt Catalog**
**Owner:** Mike Freedman, Freedman Properties
**Catalog version:** v5
**Approved:** April 27, 2026
**Supersedes:** `FINAL_APPROVED_Prompt_Catalog_v4.md`

---

## What changed in v5

Catalog v5 is the result of Session 2 of the Zoning Analysis Overhaul. It deprecates the single Prompt 3C (Highest and Best Use Opinion) and replaces it with three sequential, focused prompts: 3C-CONF (Conformity Assessment), 3C-SCEN (Scenario Generation), and 3C-HBU (Cross-Scenario Synthesis). All other prompts in v4 remain unchanged.

### Why three prompts replaced one

The old Prompt 3C tried to do everything in one Sonnet call: address all four classical Highest and Best Use tests, return a conclusion string, and produce a narrative paragraph. When any field failed, the entire section collapsed in the output report. There was no concept of conformity status as a structured field, no multi-scenario generation, and no per-scenario zoning pathway. The replacement chain has three benefits: failure isolation (a failure in any one prompt does not block the others), structured outputs that drive downstream rendering and financial fan-out (Session 4), and measurably better LLM performance from focused single-task prompts.

### Catalog summary

| ID | Name | Module | Model | Status |
|----|------|--------|-------|--------|
| 1A | OM Extraction | extractor.py | Haiku | Active (unchanged from v4) |
| 1B | Rent Roll Extraction | extractor.py | Haiku | Active (unchanged from v4) |
| 1C | T-12 / Financial Extraction | extractor.py | Haiku | Active (unchanged from v4) |
| 3A | Zoning Parameter Extraction | market.py | Haiku | Active (unchanged from v4) |
| 3B | Buildable Capacity Analysis | market.py | Sonnet | Active (unchanged from v4) |
| ~~3C~~ | ~~Highest & Best Use Opinion~~ | ~~market.py~~ | ~~Sonnet~~ | **DEPRECATED in v5 — replaced by 3C-CONF + 3C-SCEN + 3C-HBU** |
| **3C-CONF** | **Conformity Assessment** | **market.py** | **Sonnet** | **NEW in v5** |
| **3C-SCEN** | **Scenario Generation** | **market.py** | **Sonnet** | **NEW in v5** |
| **3C-HBU** | **Cross-Scenario Synthesis** | **market.py** | **Sonnet** | **NEW in v5** |
| 3D | Supply Pipeline Analysis | market.py | Sonnet | Active (unchanged from v4) |
| 4B | Insurance Analysis | risk.py | Sonnet | Active (unchanged from v4) |
| 4-MASTER | All Report Narratives (batched) | word_builder.py | Sonnet | Active (unchanged from v4) |
| 5A | Monte Carlo Simulation Narrative | financials.py | Sonnet | Active (unchanged from v4) |
| 5B | Debt Market Snapshot Narrative | market.py | Sonnet | Active (unchanged from v4) |
| 5C | Interactive HTML Report | html_builder.py | Sonnet | Active — optional (unchanged from v4) |
| 5D | Investor-Facing Report Narrative | word_builder.py | Sonnet | Active — optional (unchanged from v4) |
| 5E | LP Pitch Deck Content | deck_builder.py | Sonnet | Active — optional (unchanged from v4) |
| 5F | Lender Package Cover Letter & Exec Summary | lender_package.py | Sonnet | Active — optional (unchanged from v4) |
| 5G | Deal Alert Notification | notifier.py | Haiku | Active — optional (unchanged from v4) |

**Total active prompts in v5: 19** (16 carried forward from v4 + 3 new) — Prompt 3C deprecated.

### Model strings (locked, project-wide)

- Haiku: `claude-haiku-4-5-20251001`
- Sonnet: `claude-sonnet-4-20250514`

### Deprecation handling for Prompt 3C

- The constants `_SYSTEM_3C` and `_USER_3C` in `market.py` will be REMOVED in Session 3, not kept as deprecated stubs. The new chain replaces them entirely.
- The function `_apply_3c(data, deal)` will be REMOVED in Session 3. Replaced by `_apply_3c_conf`, `_apply_3c_scen`, `_apply_3c_hbu`.
- The call site in `enrich_market_data()` that runs Prompt 3C will be replaced with a call to `run_zoning_synthesis_chain(deal)`.
- Any historical deal pickles that reference the old `deal.zoning.hbu_*` fields remain readable because the schema is additive (Session 1 did not remove the old fields). New deals will have those legacy fields default to None.

---

# Section 1 — New Prompts in v5

The three new prompts run in strict sequence: 3C-CONF first, then 3C-SCEN (which consumes 3C-CONF's output), then 3C-HBU (which consumes both prior outputs). Each is gated by retry-on-parse-failure and falls back to a typed-empty placeholder on persistent failure. The full orchestration logic, confidence gate criteria, and fallback specifications are in Section 2.

---

## Prompt 3C-CONF — Conformity Assessment

**Module:** `market.py`
**Model:** Sonnet (`claude-sonnet-4-20250514`)
**Runs:** First in the zoning synthesis chain, after Prompt 3B completes
**Gate:** Confidence gate (see Section 2.2). If gate fails, this prompt is skipped and `_indeterminate_conformity_assessment(deal)` writes the fallback.
**Populates:** `deal.conformity_assessment` (a `ConformityAssessment` Pydantic model)

### Purpose

Examines the property's existing or proposed configuration against the current zoning district's standards (extracted by Prompt 3A) and the buildable capacity calculations (from Prompt 3B). Returns a structured `ConformityAssessment` with a status enum, confidence level, list of specific nonconformities with magnitude, grandfathering posture, risk summary, and required diligence actions.

### Inputs (assembled by `_build_3c_conf_user(deal)`)

| Variable | Type | Source | Notes |
|---|---|---|---|
| `property_address` | str | `deal.address.full_address` | For prompt's own reference |
| `asset_type` | str | `deal.asset_type` (enum value) | One of: multifamily, mixed_use, office, retail, industrial, single_family |
| `investment_strategy` | str | `deal.investment_strategy` (enum value) | One of: stabilized_hold, value_add, opportunistic |
| `current_units` | int / null | `deal.assumptions.current_units` | |
| `current_use` | str | derived | Plain English description |
| `building_sf` | float / null | `deal.assumptions.building_sf` | |
| `lot_sf` | float / null | `deal.assumptions.lot_sf` | |
| `year_built` | int / null | `deal.assumptions.year_built` | Used for grandfathering presumption |
| `zoning_json` | str (JSON) | `deal.zoning.dict()` | Output of Prompt 3A |
| `buildable_capacity_json` | str (JSON) | `deal.zoning` capacity fields | Output of Prompt 3B |
| `is_split_zoned` | bool | derived | True if parcel spans multiple zoning districts |
| `split_zoning_codes` | list[str] | derived | Empty unless split-zoned |

### System prompt — full text

```
You are a senior land use attorney and zoning analyst preparing a formal
conformity assessment for a commercial real estate underwriting report.
Your audience is an investment committee and an institutional lender. The
assessment will be relied upon to determine deal viability, insurance
underwriting, and lender approval.

YOUR TASK
Determine the property's conformity status under current zoning, document
each specific nonconformity with magnitude, assess grandfathering posture
where applicable, and produce a one-paragraph risk summary plus a
diligence action checklist.

CONFORMITY STATUS — pick exactly ONE (describes the EXISTING condition)
- CONFORMING: existing use AND all dimensional standards comply with
  current zoning. No grandfathering required.
- LEGAL_NONCONFORMING_USE: the use itself is not permitted under current
  zoning, but is presumed grandfathered because it predates the current
  code and has been continuously maintained. Dimensional standards may
  also be nonconforming.
- LEGAL_NONCONFORMING_DENSITY: the use is permitted, but unit count or
  FAR exceeds current density caps, and the property is presumed
  grandfathered. Use this status when density is the primary or only
  nonconformity.
- LEGAL_NONCONFORMING_DIMENSIONAL: the use is permitted, but one or more
  dimensional standards (height, setbacks, lot coverage, parking) do not
  comply, and the property is presumed grandfathered.
- MULTIPLE_NONCONFORMITIES: two or more LEGAL_NONCONFORMING_* conditions
  apply simultaneously (e.g., both use AND density, or both use AND
  multiple dimensional standards). Use this when no single category
  dominates.
- ILLEGAL_NONCONFORMING: the configuration violates zoning AND there is
  no defensible basis for grandfathering (e.g., post-code construction
  without permits, discontinued use beyond the abandonment period).
- CONFORMITY_INDETERMINATE: insufficient data to make a determination.
  Use this status ONLY when explicitly instructed by the orchestrator.

PROPOSED PATHWAY REQUIREMENT — separate output, optional
The conformity status above describes the EXISTING condition. Separately,
if the inputs describe a proposed business plan that requires a
discretionary approval to execute, return a proposed_pathway_requirement
value. If no proposed plan is articulated, or the proposed plan is
by-right, return null.
- null: no proposed plan, OR proposed plan is by-right (no discretionary
  approval needed). Default for stabilized-hold scenarios.
- NONE: assessed and confirmed no discretionary approval required. Use
  this only when the inputs explicitly describe a by-right proposed plan
  worth flagging.
- VARIANCE_REQUIRED: proposed plan requires a use or dimensional variance
  from the zoning board.
- SPECIAL_EXCEPTION_REQUIRED: proposed plan requires a special exception
  or conditional-use approval.
- REZONE_REQUIRED: proposed plan requires legislative rezoning.

CRITICAL RULES
1. Base every conclusion on the data provided. Do not speculate beyond it.
2. For each nonconformity, state the standard, the actual value, the
   permitted value, and the magnitude of the gap (e.g., "21 units exceeds
   6-unit by-right cap by 250%").
3. Grandfathering is PRESUMED, not GUARANTEED. Always recommend that
   counsel verify continuous use and confirm no triggering events have
   occurred (substantial improvement, change of use, abandonment).
4. The substantial improvement threshold in most jurisdictions is 50% of
   pre-improvement structure value over a defined window. Flag this in
   diligence_actions for any nonconforming property where renovation is
   contemplated.
5. If the parcel is split-zoned, address each district's standards
   separately and identify which controls each part of the site.
6. Do not invent jurisdiction-specific procedural details (variance
   approval timelines, ZBA hearing schedules). Refer to local counsel.
7. risk_summary is one paragraph (3-5 sentences) of plain English an
   investment committee member can read in 30 seconds. No legal jargon.
8. diligence_actions is an ordered list of concrete checklist items the
   acquisitions team must complete before closing. Each item starts with
   a verb ("Confirm...", "Obtain...", "Verify...").

OUTPUT FORMAT
Return ONLY the JSON object below. No preamble, no postamble, no markdown
fences. All fields are required; use null for fields that do not apply
(e.g., grandfathering_status when CONFORMING).
```

### User prompt template — full text

```
Property: {property_address}
Asset type: {asset_type} | Investment strategy: {investment_strategy}
Existing configuration:
  - Current use: {current_use}
  - Current units: {current_units}
  - Building SF: {building_sf}
  - Lot SF: {lot_sf}
  - Year built: {year_built}

Split-zoned: {is_split_zoned}
Split zoning codes (if any): {split_zoning_codes}

Zoning standards (Prompt 3A output):
{zoning_json}

Buildable capacity analysis (Prompt 3B output):
{buildable_capacity_json}

Return JSON in this exact shape:
{{
  "status": "CONFORMING|LEGAL_NONCONFORMING_USE|LEGAL_NONCONFORMING_DENSITY|LEGAL_NONCONFORMING_DIMENSIONAL|MULTIPLE_NONCONFORMITIES|ILLEGAL_NONCONFORMING|CONFORMITY_INDETERMINATE",
  "confidence": "HIGH|MEDIUM|LOW|INDETERMINATE",
  "confidence_reasons": ["reason 1", "reason 2"],
  "nonconformity_details": [
    {{
      "nonconformity_type": "USE|DENSITY|HEIGHT|FAR|FRONT_SETBACK|REAR_SETBACK|SIDE_SETBACK|SETBACKS|LOT_COVERAGE|PARKING|LOT_AREA|OTHER",
      "standard_description": "Brief plain-English label",
      "permitted_value": "What zoning allows (with units)",
      "actual_value": "What the property has (with units)",
      "magnitude_description": "Plain-English magnitude statement"
    }}
  ],
  "grandfathering_status": {{
    "is_presumed_grandfathered": true|false,
    "basis": "Built {{year}} predating current code; continuous use presumed",
    "loss_triggers": ["Substantial improvement >50%", "Change of use", "Abandonment >12 months"],
    "verification_required": true,
    "confirmation_action_required": "Pull L&I rental license history and prior zoning permits",
    "risk_if_denied": "Plain-English consequence if grandfathering is not confirmed"
  }} | null,
  "proposed_pathway_requirement": "NONE|VARIANCE_REQUIRED|SPECIAL_EXCEPTION_REQUIRED|REZONE_REQUIRED" | null,
  "risk_summary": "One paragraph plain English for the IC.",
  "diligence_actions_required": [
    "Confirm with title that...",
    "Obtain prior zoning permits showing..."
  ]
}}
```

### Output schema mapping to DealData

Every field in the JSON above maps to a field on `ConformityAssessment` (defined in `Session_1_Schema_Design.md`, realigned in Session 1.6 — see `Session_1_6_Drift_Report.md`). The mapping is one-to-one; `_apply_3c_conf(data, deal)` constructs the Pydantic model and assigns it to `deal.conformity_assessment`. Note: `proposed_pathway_requirement` is the new field added in Session 1.6 to separate the existing-condition status from the proposed-plan entitlement requirement.

### Edge cases & failure modes

- `zoning_json` mostly null (3A failed) → confidence gate refuses to call this prompt; INDETERMINATE fallback fires
- Vacant land → `current_use` set to "Vacant land — no existing structure"; nonconformity_details empty; status CONFORMING if proposed use permitted
- Split-zoned parcel → both district codes passed; nonconformity_details addresses each district separately
- Industrial use in residential district → LEGAL_NONCONFORMING_USE with grandfathering presumed if year_built precedes current code
- Year built null → grandfathering basis explicitly notes verification_required=True
- LLM returns invalid status → parse error; one retry with stricter prefix; on second failure, INDETERMINATE fallback

---

## Prompt 3C-SCEN — Scenario Generation

**Module:** `market.py`
**Model:** Sonnet (`claude-sonnet-4-20250514`)
**Runs:** Second, after 3C-CONF
**Populates:** `deal.scenarios[]` (a list of 1–3 `DevelopmentScenario` Pydantic models)

### Purpose

Generates one to three ranked development scenarios for the property. Each scenario is a meaningfully different business plan with its own physical configuration, operating strategy, zoning pathway, and per-scenario assumption deltas against the baseline. Hard rule: refuse to pad scenarios. If only one realistic plan exists, return one.

### Inputs (assembled by `_build_3c_scen_user(deal)`)

| Variable | Type | Source |
|---|---|---|
| `property_address` | str | `deal.address.full_address` |
| `asset_type` | str | `deal.asset_type` |
| `investment_strategy` | str | `deal.investment_strategy` |
| `baseline_assumptions_json` | str (JSON) | `deal.assumptions.dict()` |
| `current_use` | str | derived |
| `current_units` | int / null | `deal.assumptions.current_units` |
| `building_sf` | float / null | `deal.assumptions.building_sf` |
| `lot_sf` | float / null | `deal.assumptions.lot_sf` |
| `zoning_json` | str (JSON) | `deal.zoning.dict()` |
| `buildable_capacity_json` | str (JSON) | `deal.zoning` capacity fields |
| `conformity_assessment_json` | str (JSON) | `deal.conformity_assessment.dict()` |
| `market_context_summary` | str | derived from `deal.market_data` |
| `single_scenario_mode` | bool | `deal.workflow_controls.single_scenario_mode` |
| `strategy_lock` | str / null | `deal.workflow_controls.strategy_lock` |
| `max_scenarios` | int | `deal.workflow_controls.max_scenarios` |

### System prompt — full text

```
You are a senior development advisor at a top-tier real estate private
equity firm. You are recommending business plans for an underwriting
committee that will choose ONE plan to execute. Your reputation depends
on offering only realistic, financeable options — never padding the list
to look thorough.

YOUR TASK
Generate between 1 and {max_scenarios} development scenarios for this
property. Each scenario is a meaningfully different business plan, not a
sensitivity case. Rank them by expected risk-adjusted return, with rank 1
being the recommended PREFERRED scenario.

A SCENARIO IS MEANINGFULLY DIFFERENT WHEN IT HAS A DIFFERENT
- physical configuration (unit count, building SF, or use mix), OR
- operating strategy (stabilized hold vs. renovation vs. ground-up), OR
- zoning pathway (by-right vs. variance vs. rezone)

A SCENARIO IS NOT MEANINGFULLY DIFFERENT WHEN IT IS
- the same business plan with different rent assumptions
- the same business plan with different cap rates
- the same business plan with different financing
- a "do nothing" or "sell as-is" placeholder

CRITICAL RULES — PADDING IS PROHIBITED
1. If only one realistic business plan exists, return ONE scenario.
   Returning fewer scenarios than requested is correct behavior, not a
   failure. Padding the list with weak alternatives will be flagged in
   review.
2. The PREFERRED scenario (rank 1) is the one you would actually
   recommend the firm execute. ALTERNATE scenarios (rank 2-3) are real
   alternatives the committee might prefer over your recommendation,
   not strawmen.
3. Conformity drives pathway. If the conformity_assessment shows
   LEGAL_NONCONFORMING_USE, do not generate a scenario that requires
   demolition and rebuild "by-right" unless the rebuild use is permitted
   under current zoning. If conformity is INDETERMINATE, default scenarios
   to the existing configuration with a noted entitlement risk flag.
4. If single_scenario_mode is True, return exactly ONE scenario marked
   PREFERRED.
5. If strategy_lock is set, every scenario's investment_strategy must
   match strategy_lock.
6. Each scenario's scenario_id is snake_case, max 30 characters, unique
   within the deal, and descriptive enough to identify the scenario from
   the ID alone (e.g., "asbuilt_reno_36u" or "demo_rebuild_byright_6u").
7. The unit_count and building_sf in each scenario must be ACHIEVABLE
   under the scenario's zoning_pathway. Scenarios proposing more units
   than buildable_capacity allows must use a non-by-right pathway.
8. Each scenario's business_thesis is 2-3 sentences explaining the
   rationale. Each scenario's zoning_pathway has a pathway_type enum and
   a candid success_probability_pct (0-100, your honest estimate).

OUTPUT FORMAT
Return ONLY the JSON array of scenario objects below. No preamble, no
postamble, no markdown fences. The array length is between 1 and
{max_scenarios}. The first element is always rank 1 / PREFERRED.
```

### User prompt template — full text

```
Property: {property_address}
Asset type: {asset_type} | Submitted strategy: {investment_strategy}
Existing configuration:
  - Current use: {current_use}
  - Current units: {current_units}
  - Building SF: {building_sf}
  - Lot SF: {lot_sf}

Conformity assessment (Prompt 3C-CONF output):
{conformity_assessment_json}

Zoning standards (Prompt 3A output):
{zoning_json}

Buildable capacity (Prompt 3B output):
{buildable_capacity_json}

Baseline assumptions (user-submitted):
{baseline_assumptions_json}

Market context:
{market_context_summary}

Workflow controls:
  - single_scenario_mode: {single_scenario_mode}
  - strategy_lock: {strategy_lock}
  - max_scenarios: {max_scenarios}

Return a JSON object with this exact shape (note: physical config and
assumption deltas are FLAT at the scenario level — no nested blocks):
{{
  "scenarios": [
    {{
      "scenario_id": "snake_case_max_30_chars",
      "rank": 1,
      "scenario_name": "Human-readable name (e.g., 'As-Built Renovation, 36 Units')",
      "verdict": "PREFERRED",
      "business_thesis": "2-3 sentences explaining the plan and why it leads.",
      "investment_strategy": "stabilized_hold|value_add|opportunistic",

      "unit_count": 36,
      "building_sf": 20640,
      "use_mix": [
        {{"use_category": "residential", "sf": 20640, "share_pct": 100, "unit_count": 36, "notes": null}}
      ],
      "operating_strategy": "Plain-English operating strategy paragraph (formerly operating_strategy_note).",

      "zoning_pathway": {{
        "pathway_type": "BY_RIGHT|CONDITIONAL_USE|SPECIAL_EXCEPTION|VARIANCE|REZONE",
        "rationale": "Why this pathway applies (1-2 sentences)",
        "approval_body": "Plain-English approving body (e.g., 'Philadelphia ZBA')",
        "estimated_timeline_months": 0,
        "estimated_soft_cost_usd": 0,
        "success_probability_pct": 95,
        "fallback_if_denied": "Plain-English fallback strategy if approval not obtained, or null for BY_RIGHT"
      }},

      "construction_budget_delta_usd": 350000,
      "rent_delta_pct": 0.08,
      "timeline_delta_months": 12,

      "key_risks": [
        "Risk 1 — one-line description",
        "Risk 2 — one-line description"
      ],
      "entitlement_risk_flag": {{
        "severity": "LOW|MEDIUM|HIGH",
        "risk_summary": "Plain-English entitlement risk paragraph (1-3 sentences)",
        "diligence_required": [
          "Concrete diligence action 1",
          "Concrete diligence action 2"
        ]
      }} | null
    }}
  ]
}}
```

### Output schema mapping to DealData

Each element of `scenarios[]` constructs one `DevelopmentScenario` Pydantic model. `_apply_3c_scen(data, deal)` iterates the array and appends each constructed model to `deal.scenarios`. The PREFERRED scenario's `scenario_id` is later written to `deal.zoning_extensions.preferred_scenario_id` by 3C-HBU.

### Edge cases & failure modes

- Clean stabilized hold with no value-add lever → returns 1 PREFERRED scenario; refuses to pad
- `single_scenario_mode=True` → returns exactly 1 scenario
- `strategy_lock="stabilized_hold"` → all scenarios use stabilized_hold
- Conformity is INDETERMINATE → default scenarios use existing configuration; entitlement_risk_flag.severity = HIGH on each
- LEGAL_NONCONFORMING_USE + renovation contemplated → flag substantial-improvement threshold risk on the renovation scenario
- Vacant land → first scenario is by-right development at buildable_capacity; alternates may include variance pathways for higher density
- Existing structure exceeds by-right caps → "demolish and rebuild by-right" appears as ALTERNATE only if rebuild economics work; otherwise omitted
- LLM returns 4+ scenarios → `_apply_3c_scen` truncates to `max_scenarios` and logs a warning
- LLM returns 0 scenarios → `_apply_3c_scen` writes a single fallback "as-submitted" scenario from baseline; rank 1, PREFERRED, HIGH entitlement risk
- LLM returns duplicate scenario_ids → parse error; one retry with stricter prefix

---

## Prompt 3C-HBU — Cross-Scenario Synthesis

**Module:** `market.py`
**Model:** Sonnet (`claude-sonnet-4-20250514`)
**Runs:** Third, after 3C-SCEN
**Populates:** `deal.zoning_extensions` (a `ZoningExtensions` Pydantic model)

### Purpose

Reads the conformity assessment and all generated scenarios, then produces the IC-grade synthesis: cross-scenario recommendation prose (2-3 paragraphs), the preferred scenario ID, the parcel's use flexibility score (1-5 with rationale), and an overlay impact assessment paragraph.

### Inputs (assembled by `_build_3c_hbu_user(deal)`)

| Variable | Type | Source |
|---|---|---|
| `property_address` | str | `deal.address.full_address` |
| `asset_type` | str | `deal.asset_type` |
| `conformity_assessment_json` | str (JSON) | `deal.conformity_assessment.dict()` |
| `scenarios_json` | str (JSON) | `[s.dict() for s in deal.scenarios]` |
| `zoning_json` | str (JSON) | `deal.zoning.dict()` |
| `overlay_districts` | list[str] | `deal.zoning.overlay_districts` |
| `market_context_summary` | str | derived from `deal.market_data` |

### System prompt — full text

```
You are the head of acquisitions at a $2B real estate private equity
firm, writing the synthesis section of an investment committee memo. The
prior analysis (conformity assessment + generated scenarios) is in front
of you. Your job is to tell the committee what to do and why, in
language they can act on.

YOUR TASK
Produce four outputs:

1. cross_scenario_recommendation: 2-3 paragraphs of IC-grade synthesis
   prose. Open with the recommendation. Justify it against the
   alternatives. Acknowledge the trade-offs honestly. Close with the
   single most important diligence item before close.

2. preferred_scenario_id: The scenario_id of the scenario you recommend
   the committee execute. This MUST match a scenario_id that appears in
   the input scenarios array.

3. use_flexibility_score: An integer 1-5 capturing how much zoning
   optionality this parcel offers (1 = single-purpose, locked in; 5 =
   highly flexible, multiple by-right uses, generous dimensions).

4. overlay_impact_assessment: A short paragraph (2-4 sentences) on how
   the parcel's overlay districts (if any) materially affect the
   recommendation. If no overlays apply, state that explicitly.

CRITICAL RULES
1. Recommend the scenario marked rank 1 / PREFERRED unless you have a
   substantive reason to disagree. If you disagree, you must override
   the recommendation by setting preferred_scenario_id to a different
   scenario in the array AND explaining the override in the synthesis
   prose. Do not silently disagree.
2. Tie every claim to data the committee can see. Do not introduce new
   facts not present in the inputs.
3. The use_flexibility_score is anchored to the parcel under current
   zoning, NOT to any proposed scenario. A parcel that requires a
   variance to do anything interesting is a 1 or 2, even if the variance
   has good odds.
4. Length discipline: cross_scenario_recommendation 2-3 paragraphs, NO
   bullet lists, NO headers, no markdown. The PDF renders this as
   continuous prose.
5. If only ONE scenario was generated (no alternatives), the
   cross_scenario_recommendation explains why no alternatives exist and
   what conditions would unlock them.

OUTPUT FORMAT
Return ONLY the JSON below. No preamble, no postamble, no markdown
fences.
```

### User prompt template — full text

```
Property: {property_address} | Asset type: {asset_type}

Conformity assessment (3C-CONF output):
{conformity_assessment_json}

Generated scenarios (3C-SCEN output):
{scenarios_json}

Zoning standards (3A output):
{zoning_json}

Overlay districts: {overlay_districts}

Market context:
{market_context_summary}

Return JSON in this exact shape (note: use_flexibility is FLAT at the top
level; overlay_impact_assessment is a STRUCTURED LIST, one entry per
overlay; return an empty list if no overlay districts apply to this
parcel):
{{
  "cross_scenario_recommendation": "Two to three paragraphs of synthesis prose.",
  "preferred_scenario_id": "must_match_a_scenario_id_in_input",
  "use_flexibility_score": 3,
  "use_flexibility_explanation": "1-2 sentence justification for the score.",
  "overlay_impact_assessment": [
    {{
      "overlay_name": "MIH Overlay",
      "overlay_type": "incentive|historic|environmental|design|transit|other",
      "impact_summary": "1-2 sentences on materiality to the recommendation",
      "triggers_review": false,
      "additional_diligence": ["Concrete diligence action 1", "Concrete diligence action 2"]
    }}
  ]
}}
```

### Output schema mapping to DealData

| JSON field | DealData field |
|---|---|
| `cross_scenario_recommendation` | `deal.zoning_extensions.cross_scenario_recommendation` |
| `preferred_scenario_id` | `deal.zoning_extensions.preferred_scenario_id` (validated against `[s.scenario_id for s in deal.scenarios]`) |
| `use_flexibility_score` (flat int) | `deal.zoning_extensions.use_flexibility_score` |
| `use_flexibility_explanation` (flat str) | `deal.zoning_extensions.use_flexibility_explanation` |
| `overlay_impact_assessment` (list of OverlayImpact) | `deal.zoning_extensions.overlay_impact_assessment` |

A model-level validator on `ZoningExtensions` ensures `preferred_scenario_id` is found in `[s.scenario_id for s in deal.scenarios]`. Mismatch raises a parse error and triggers retry.

### Edge cases & failure modes

- Only 1 scenario in input → recommendation explicitly notes "single scenario presented" and states unlock conditions
- No overlays present → overlay_impact_assessment states "No overlay districts apply to this parcel" plus brief context
- LLM recommends scenario_id not in array → parse error; one retry; on second failure, default to rank-1 scenario and log warning
- Conformity is INDETERMINATE → synthesis explicitly acknowledges indeterminacy and recommends most defensible scenario
- LLM produces bullet lists or markdown headers → parser strips them and logs warning; if stripping leaves <100 chars, retry once

---

# Section 2 — Orchestration in market.py

## 2.1 The new orchestrator function

A single new function in `market.py` replaces the existing 3C call site. The function is called from `enrich_market_data()` after Prompt 3B completes:

```python
def run_zoning_synthesis_chain(deal: DealData) -> None:
    """
    Orchestrates the three-prompt zoning synthesis chain.
    Called in enrich_market_data() after Prompt 3B completes.
    Failures at any step write typed-empty fallbacks and do not halt the pipeline.
    """
    # ─── GATE: confidence check before 3C-CONF ──────────────────────
    if not _confidence_gate_passes(deal):
        logger.info("Zoning confidence gate failed — writing INDETERMINATE conformity")
        deal.conformity_assessment = _indeterminate_conformity_assessment(deal)
    else:
        # ─── 3C-CONF ────────────────────────────────────────────────
        try:
            data = _call_llm_with_retry(MODEL_SONNET, _SYSTEM_3C_CONF, _build_3c_conf_user(deal), max_retries=1)
            _apply_3c_conf(data, deal)
            logger.info("3C-CONF complete: status=%s, confidence=%s",
                        deal.conformity_assessment.status.value,
                        deal.conformity_assessment.confidence.value)
        except Exception as exc:
            logger.error("3C-CONF failed: %s — writing INDETERMINATE", exc)
            deal.conformity_assessment = _indeterminate_conformity_assessment(deal)

    # ─── 3C-SCEN ────────────────────────────────────────────────────
    try:
        data = _call_llm_with_retry(MODEL_SONNET, _SYSTEM_3C_SCEN, _build_3c_scen_user(deal), max_retries=1)
        _apply_3c_scen(data, deal)
        logger.info("3C-SCEN complete: %d scenarios written", len(deal.scenarios))
    except Exception as exc:
        logger.error("3C-SCEN failed: %s — writing single fallback scenario", exc)
        deal.scenarios = [_fallback_as_submitted_scenario(deal)]

    # ─── 3C-HBU ─────────────────────────────────────────────────────
    try:
        data = _call_llm_with_retry(MODEL_SONNET, _SYSTEM_3C_HBU, _build_3c_hbu_user(deal), max_retries=1)
        _apply_3c_hbu(data, deal)
        logger.info("3C-HBU complete: preferred=%s, flexibility=%s",
                    deal.zoning_extensions.preferred_scenario_id,
                    deal.zoning_extensions.use_flexibility_score)
    except Exception as exc:
        logger.error("3C-HBU failed: %s — writing minimal extensions", exc)
        deal.zoning_extensions = _minimal_zoning_extensions(deal)
```

## 2.2 The confidence gate

A programmatic check before 3C-CONF runs. The gate passes only if ALL four criteria are met:

| Criterion | Threshold |
|---|---|
| `deal.zoning.zoning_code` is populated | not None and not empty string |
| `deal.zoning.permitted_uses` length | ≥ 3 entries |
| Dimensional standards populated | ≥ 4 of 9 core fields populated (max_height_ft, max_stories, min_lot_area_sf, max_lot_coverage_pct, max_far, front_setback_ft, rear_setback_ft, side_setback_ft, min_parking_spaces) |
| `deal.assumptions.lot_sf` is populated | not None and > 0 |

If any criterion fails, 3C-CONF is skipped and `_indeterminate_conformity_assessment(deal)` writes a fully-typed `ConformityAssessment` with `status=CONFORMITY_INDETERMINATE`, `confidence=INDETERMINATE`, populated `confidence_reasons` listing each failed criterion, an explanatory `risk_summary`, and a `diligence_actions_required` list directing the user to manually retrieve the zoning ordinance and rerun.

## 2.3 Retry policy

Each prompt receives one retry on parse failure. The retry uses an identical user prompt but prepends the system prompt with a strict reminder:

```
RETRY: The previous response failed JSON parsing. You MUST return ONLY
valid JSON matching the exact schema below — no preamble, no markdown
fences, no commentary. If you cannot match the schema, return the schema
with all fields set to null/empty rather than malformed JSON.
```

If the retry also fails, the typed-empty fallback fires and the pipeline continues.

## 2.4 Fallback scenarios per failure mode

| Failure | Fallback |
|---|---|
| Confidence gate fails | INDETERMINATE conformity; 3C-SCEN proceeds with HIGH entitlement risk on each scenario |
| 3C-CONF call fails (both attempts) | Same as confidence gate failure |
| 3C-SCEN call fails (both attempts) | Single "as-submitted" scenario from baseline; rank 1, PREFERRED, BY_RIGHT pathway with success_probability_pct=null, entitlement_risk_flag.severity=HIGH, description "Scenario generation failed; manual review required" |
| 3C-HBU call fails (both attempts) | Minimal `ZoningExtensions` per `market.py:_minimal_zoning_extensions` (the canonical source): `preferred_scenario_id` = first scenario's ID; `cross_scenario_recommendation` = sentinel text noting the synthesis failed and manual review is required; `use_flexibility_score` = `1` (the conservative-default sentinel — "single-purpose, locked in" — fail-toward-forcing-manual-review per CP2 reasoning); `use_flexibility_explanation` = sentinel string explicitly noting this is a fallback default, not a real assessment; `overlay_impact_assessment` = `[]` (empty list, not a string). |

Every fallback writes a clearly labeled placeholder so the report renders without errors and the user can see the gap immediately.

---

# Section 3 — Cost estimation

| Prompt | Avg input tokens | Avg output tokens | Per-call cost |
|---|---|---|---|
| 3C-CONF | ~3,500 | ~1,000 | ~$0.025 |
| 3C-SCEN | ~4,500 | ~2,500 | ~$0.052 |
| 3C-HBU | ~4,000 | ~1,200 | ~$0.030 |
| **Total new chain** | | | **~$0.107** |
| Prior 3C single call (replaced) | ~3,500 | ~1,500 | ~$0.034 |
| **Net incremental cost per deal** | | | **~$0.073** |

A retry on any single prompt adds $0.025 to $0.052 per occurrence. Expected per-deal cost ceiling assuming one retry on the most expensive prompt (3C-SCEN): **~$0.55**.

---

# Section 4 — Reference deal test expectations

The three reference deals form the regression test set. A change to any prompt that breaks any reference deal must be flagged before any subsequent catalog update.

## Deal A — 967-73 N. 9th St (21 units in 6-unit-cap district)

**3C-CONF:** status `LEGAL_NONCONFORMING_DENSITY` (use itself is permitted under ICMX; density is the primary nonconformity); confidence `HIGH`; nonconformity_details includes one entry of type `DENSITY` with magnitude "21 units exceeds 6-unit by-right cap by 250%"; grandfathering_status.is_presumed_grandfathered = true; proposed_pathway_requirement = `null` (preferred plan is by-right contingent on grandfathering confirmation); diligence_actions includes verification of continuous use and substantial-improvement threshold.

**3C-SCEN:** 2-3 scenarios; rank 1 PREFERRED = "asbuilt_reno_21u" (light cosmetic renovation, BY_RIGHT pathway within grandfathered config); rank 2 ALTERNATE = "variance_pathway_higher_value" (~60% success probability); rank 3 ALTERNATE optional = "demo_rebuild_byright_6u" (only if 6-unit economics work).

**3C-HBU:** preferred_scenario_id = "asbuilt_reno_21u"; use_flexibility_score = 2; recommendation opens with "Recommend the as-built renovation pathway..."; flags substantial-improvement threshold as the single most important diligence item.

## Deal B — Belmont Apartments, 2217 N 51st St, RSD-3

**3C-CONF:** status `LEGAL_NONCONFORMING_USE` (RSD-3 is single-family detached; 36-unit apartment is not permitted); confidence `HIGH`; nonconformity_details includes USE entry plus possibly density, setbacks, lot coverage; grandfathering_status.is_presumed_grandfathered = true (1926 build predates 2012 zoning code rewrite by 86 years); grandfathering_status.basis explicitly references 1926 build date; proposed_pathway_requirement = `null` (preferred plan is stabilized hold within grandfathered envelope; no discretionary approval contemplated); diligence_actions includes confirmation of continuous multifamily use, prior L&I records, no substantial improvements.

**3C-SCEN:** 1-2 scenarios; rank 1 PREFERRED = "stabilized_hold_36u" (operate as-is, no displacement, BY_RIGHT within grandfathered envelope); rank 2 ALTERNATE possible = "light_value_add_36u" (kitchen/bath upgrades on turnover, flagged with substantial-improvement-threshold risk if budget approaches 50% of structure value); NOT generated: "demo and rebuild" (RSD-3 max is single-family detached, so 36-unit rebuild is not by-right and economics are unworkable).

**3C-HBU:** preferred_scenario_id = "stabilized_hold_36u"; use_flexibility_score = 1; recommendation emphasizes preservation of grandfathered status; explicitly notes any meaningful upside requires renovation budget discipline; if only one scenario was generated, explains that single-family zoning prevents redevelopment scenarios.

## Deal C — 3520 Indian Queen Lane, split-zoned RSA-1/RSA-5, existing warehouse + American Tower easement

**3C-CONF:** status `LEGAL_NONCONFORMING_USE` (industrial warehouse in residential district); confidence `MEDIUM` (split zoning + easement complicate the assessment); nonconformity_details includes USE entry, possibly LOT_COVERAGE entry; is_split_zoned reflected in confidence_reasons; proposed_pathway_requirement = `VARIANCE_REQUIRED` (any of the three CMA Revised Schemes — courtyard, townhomes, garden — requires a use variance to permit residential redevelopment in RSA-1/RSA-5); diligence_actions includes review of American Tower Easement and Assignment Agreement (Doc Id 53136944), confirmation of warehouse use continuity, lot survey to confirm RSA-1/RSA-5 boundary.

**3C-SCEN:** 3 scenarios approximating the three CMA Revised Schemes:
- "scheme_c_courtyard_95u" — apartment + courtyard concept, 95 units, pathway VARIANCE or REZONE (success_probability_pct ~50%)
- "scheme_a_townhomes_88u" — 60 one-bed + 22 two-bed apartments + 6 three-bed townhomes, 88 units total, pathway VARIANCE
- "scheme_b_garden_84u" — 24 studios + 44 one-bed + 16 two-bed in garden + double-loaded corridor format, 84 units, pathway VARIANCE
- Each scenario must explicitly account for the American Tower exclusive easement area (2,625 SF) being unbuildable
- Each scenario flags entitlement risk as MEDIUM or HIGH

**3C-HBU:** preferred_scenario_id likely "scheme_c_courtyard_95u" (highest unit count, best site utilization, but model could reasonably prefer A or B based on construction risk); use_flexibility_score = 2; recommendation addresses entitlement risk explicitly and ties recommendation to demonstrated CMA design work plus existing American Tower lease income as a holding-cost offset during entitlement; identifies pre-application meeting with Philadelphia City Planning Commission as the single most important diligence item.

---

# Section 5 — Schema dependencies

These two new models must be added to `models.py` BEFORE Session 3 implements the prompts. They are queued for a Session 1.5 micro-session in Claude Code.

## 5.1 WorkflowControls

```python
class WorkflowControls(BaseModel):
    single_scenario_mode: bool = False
    strategy_lock: Optional[InvestmentStrategy] = None
    max_scenarios: int = Field(default=3, ge=1, le=3)

# Add to DealData:
workflow_controls: WorkflowControls = Field(default_factory=WorkflowControls)
```

## 5.2 EncumbranceType + Encumbrance

```python
class EncumbranceType(str, Enum):
    EASEMENT = "EASEMENT"
    LEASE = "LEASE"
    LEASE_TO_EASEMENT = "LEASE_TO_EASEMENT"
    ROW = "ROW"
    DEED_RESTRICTION = "DEED_RESTRICTION"
    OTHER = "OTHER"

class Encumbrance(BaseModel):
    type: EncumbranceType
    doc_id: Optional[str] = None
    grantee: Optional[str] = None
    grantor: Optional[str] = None
    description: Optional[str] = None
    exclusive_area_sf: Optional[float] = None
    access_easement_width_ft: Optional[float] = None
    term: Optional[str] = None
    expiration: Optional[date] = None
    right_of_first_refusal: bool = False
    annual_income_usd: Optional[float] = None
    notes: Optional[str] = None

# Add to DealData:
encumbrances: List[Encumbrance] = Field(default_factory=list)
```

---

# Section 6 — Change Log

| Version | Date | Change |
|---------|------|--------|
| v1 | March 31, 2026 | Initial catalog — 7 prompts approved |
| v2 | March 31, 2026 | Minor wording refinements; no structural changes |
| v3 | April 2, 2026 | Added Prompt 3D — Supply Pipeline Analysis |
| v4 | April 2, 2026 | Added Prompt 4B — Insurance Analysis; report renumbered from 11 to 12 sections; Insurance Analysis inserted as new §8; DD Flags moved to §9 |
| **v5** | **April 27, 2026** | **DEPRECATED Prompt 3C single HBU call. ADDED three replacement prompts: 3C-CONF (Conformity Assessment), 3C-SCEN (Scenario Generation), 3C-HBU (Cross-Scenario Synthesis). ADDED orchestrator `run_zoning_synthesis_chain`. ADDED four-criterion confidence gate. ADDED four typed-empty fallback paths. Two new schema models (WorkflowControls, Encumbrance) flagged for Session 1.5 micro-session before Session 3 implementation.** |

---

*This catalog is the authoritative reference for all Claude API prompts embedded in the DealDesk Automated Underwriting System. No prompt may be modified or added without updating this document and receiving explicit approval.*

*This document supersedes `FINAL_APPROVED_Prompt_Catalog_v4.md` entirely.*

*DealDesk CRE Underwriting System — Freedman Properties Internal Document*
