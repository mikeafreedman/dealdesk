# Session 2 — Prompt Design Specification

**Document version:** 1.0 — DRAFT FOR APPROVAL
**Created:** April 27, 2026
**Owner:** Mike Freedman, Freedman Properties
**Status:** Awaiting approval — DO NOT implement until Mike approves
**Target file (after approval):** `C:\Users\mikea\dealdesk\docs\FINAL_APPROVED_Prompt_Catalog_v5.md`
**Replaces:** Prompt 3C (single Highest & Best Use Opinion) in catalog v4
**Build session:** 2 of 5 (Zoning Overhaul)
**Master plan reference:** `DealDesk_Zoning_Overhaul_Plan.md`
**Schema dependency:** `Session_1_Schema_Design.md` (must be implemented first)

---

## 0. How to read this document

This is the complete specification for the three prompts being designed in Session 2. The structure follows the project's standing **prompt-first rule**: the exact prompt text is reviewed and approved here, in claude.ai, BEFORE any code is written in Claude Code.

Each prompt section contains the design brief, inputs, the full system prompt text, the full user prompt template with placeholders, the output JSON schema, the schema mapping to DealData, edge cases and failure modes, and test expectations per reference deal. After all three prompts, the document covers orchestration logic in market.py, the confidence gate, retry and fallback policies, test fixtures for Reference Deals A, B, and C, and the gate criteria checklist for Session 2 sign-off.

---

## 1. The big picture — what changed and why

### What's being replaced

The current `_SYSTEM_3C` and `_USER_3C` in `market.py` form a single Sonnet call that tries to do everything at once: address the four classical Highest and Best Use tests, return an `hbu_conclusion` string, and produce a paragraph of `hbu_narrative`. It returns one nested JSON, and any failure in any field collapses the entire section in the output report. There is no concept of conformity status, no multi-scenario generation, and no per-scenario zoning pathway. The output is shallow and brittle.

### What replaces it

Three sequential Sonnet prompts, each with a single, focused job:

| Prompt | Name | Job | Populates |
|---|---|---|---|
| **3C-CONF** | Conformity Assessment | Determine the existing or proposed configuration's conformity to current zoning | `DealData.conformity_assessment` |
| **3C-SCEN** | Scenario Generation | Generate 1–3 ranked development scenarios with explicit zoning pathways | `DealData.scenarios[]` |
| **3C-HBU** | Cross-Scenario Synthesis | Recommend the preferred scenario and produce the IC-ready synthesis paragraphs | `DealData.zoning_extensions` |

Each prompt has structured input, structured output, isolated retry logic, and a typed-empty fallback when it fails. A failure in 3C-CONF does not block 3C-SCEN from running — the scenario prompt receives an `INDETERMINATE` conformity assessment and proceeds with caveats. This is the most important reliability change in the entire zoning overhaul.

### How the chain runs

```
                  ┌──────────────┐
   parcel data    │              │   ConformityAssessment
   zoning (3A) ──▶│   3C-CONF    │──────────────────────┐
   capacity (3B)  │              │                      │
                  └──────────────┘                      │
                                                        ▼
                  ┌──────────────┐               ┌──────────────┐
   conformity ───▶│              │               │              │
   market data ──▶│   3C-SCEN    │──────────────▶│   3C-HBU     │
   baseline ─────▶│              │               │              │
                  └──────────────┘               └──────────────┘
                         │                              │
                         ▼                              ▼
                  scenarios[] (1-3)            ZoningExtensions
                                               (preferred_scenario_id,
                                                cross_scenario_recommendation,
                                                use_flexibility_score,
                                                overlay_impact_assessment)
```

### Why three prompts, not one

Three reasons. **First, reliability:** each prompt has a single job, a small output schema, and isolated retry logic. The 967-73 N. 9th St report would have shown a populated conformity assessment plus an INDETERMINATE scenarios block, not a "this section is pending" placeholder. **Second, testability:** each prompt can be regression-tested against a single field rather than against a 2,000-token nested response. **Third, clarity for the LLM:** Sonnet performs measurably better when given one focused task with a clear output schema than when given three tasks bundled into one nested JSON.

---

## 2. Prompt 3C-CONF — Conformity Assessment

### 2.1 Design brief

**What it does:** Examines the property's existing or proposed configuration against the current zoning district's standards (extracted by Prompt 3A) and the buildable capacity calculations (from Prompt 3B). Returns a structured `ConformityAssessment` with a status enum, confidence level, list of specific nonconformities, grandfathering posture, risk summary, and required diligence actions.

**Why this is its own prompt:** Conformity is a yes/no/maybe judgment that drives downstream rendering decisions. Section 6 of the report opens with this status badge — it must be deterministic, structurally consistent, and never collapse into prose. Bundling this into a synthesis prompt produces shallow conformity findings ("the property appears to be nonconforming") with no structure for the report to render.

**Why it runs first:** Both 3C-SCEN and 3C-HBU need the conformity finding as input. A nonconforming property fundamentally changes which scenarios are realistic and how the HBU recommendation is framed. A conforming property unlocks by-right pathways that a nonconforming one cannot use.

**The confidence gate:** This prompt is gated by a programmatic confidence check. If the upstream zoning data is too thin to support a real conformity assessment, the prompt does not run — instead, the function writes an `INDETERMINATE` assessment with an explanation. Gate criteria are spelled out in Section 5.

### 2.2 Inputs

The prompt receives a JSON object assembled from `DealData`. Variable names match the `DealData` schema.

| Variable | Type | Source | Notes |
|---|---|---|---|
| `property_address` | str | `deal.address.full_address` | For the prompt's own reference |
| `asset_type` | str | `deal.asset_type` (enum value) | One of: multifamily, mixed_use, office, retail, industrial, single_family |
| `investment_strategy` | str | `deal.investment_strategy` (enum value) | One of: stabilized_hold, value_add, opportunistic |
| `current_units` | int / null | `deal.assumptions.current_units` | Existing unit count if known |
| `current_use` | str | derived from extractor or asset_type | Plain English: "36-unit apartment building", "vacant land", "warehouse", etc. |
| `building_sf` | float / null | `deal.assumptions.building_sf` | Existing building gross SF |
| `lot_sf` | float / null | `deal.assumptions.lot_sf` | Lot area in square feet |
| `year_built` | int / null | `deal.assumptions.year_built` | Used to assess grandfathering presumption |
| `zoning_json` | str (JSON) | `deal.zoning.dict()` | Full output of Prompt 3A — district code, permitted uses list, dimensional standards |
| `buildable_capacity_json` | str (JSON) | `deal.zoning` capacity fields | Output of Prompt 3B — by-right max units, max SF, binding constraint |
| `is_split_zoned` | bool | derived | True if parcel spans multiple zoning districts (per parcel_fetcher) |
| `split_zoning_codes` | list[str] | derived | Empty unless `is_split_zoned` is True |

### 2.3 System prompt — full text

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

### 2.4 User prompt template — full text

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

### 2.5 Output schema mapping to DealData

Every field in the JSON above maps to a field on `ConformityAssessment` (defined in `Session_1_Schema_Design.md`). The mapping is one-to-one — `_apply_3c_conf(data, deal)` simply constructs the Pydantic model and assigns it to `deal.conformity_assessment`. No field flattening, no field renaming.

### 2.6 Edge cases and failure modes

| Edge case | Prompt's behavior |
|---|---|
| `zoning_json` is mostly null (Prompt 3A failed) | Confidence gate refuses to call this prompt; fallback writes INDETERMINATE assessment |
| Property is `vacant_land` with no existing use | `current_use` set to "Vacant land — no existing structure"; nonconformity_details empty; status CONFORMING if proposed use is permitted, otherwise VARIANCE_REQUIRED_FOR_PROPOSED |
| Split-zoned parcel | Prompt receives `is_split_zoned=True` and both district codes; nonconformity_details addresses each district's standards separately |
| Existing use is `industrial` in a residential district | Status LEGAL_NONCONFORMING_USE with grandfathering presumed if year_built precedes current code |
| Year built is null | grandfathering_status.basis explicitly notes that grandfathering cannot be presumed without build-date verification; verification_required=True |
| LLM returns a status not in the enum list | `_apply_3c_conf` raises a parse error; orchestrator retries once with stricter system prompt prefix; on second failure, writes INDETERMINATE |

### 2.7 Test expectations per reference deal

**Deal A — 967-73 N. 9th St (21 units in 6-unit-cap district)**
- status: `LEGAL_NONCONFORMING_DENSITY` (use itself is permitted under ICMX; density nonconforms — primary axis)
- confidence: `HIGH`
- nonconformity_details: at least one entry of type `DENSITY` with magnitude "21 units exceeds 6-unit by-right cap by 250%"
- grandfathering_status.is_presumed_grandfathered: `true`
- proposed_pathway_requirement: `null` (preferred plan is renovation within grandfathered envelope; by-right contingent on grandfathering confirmation)
- diligence_actions: includes verification of continuous use and substantial-improvement threshold

**Deal B — Belmont Apartments, 2217 N 51st St, RSD-3**
- status: `LEGAL_NONCONFORMING_USE` (RSD-3 is single-family detached; 36-unit apartment building is not a permitted use)
- confidence: `HIGH`
- nonconformity_details: at least one entry of type `USE`; possible additional entries for density, setbacks, lot coverage
- grandfathering_status.is_presumed_grandfathered: `true` (1926 build predates 2012 zoning code rewrite by 86 years)
- grandfathering_status.basis: explicit reference to 1926 build date
- proposed_pathway_requirement: `null` (preferred plan is stabilized hold within grandfathered envelope; no discretionary approval contemplated)
- diligence_actions: includes confirmation of continuous multifamily use, prior L&I records, no substantial improvements that would have triggered loss

**Deal C — 3520 Indian Queen Lane, split-zoned RSA-1 / RSA-5, existing industrial warehouse + American Tower easement**
- status: `LEGAL_NONCONFORMING_USE` (industrial warehouse in a residential district)
- confidence: `MEDIUM` (split zoning + easement complicate the assessment)
- nonconformity_details: USE entry, possibly LOT_COVERAGE entry
- is_split_zoned reflected in confidence_reasons
- proposed_pathway_requirement: `VARIANCE_REQUIRED` (any of the three CMA Revised Schemes — courtyard, townhomes, garden — requires a use variance to permit residential redevelopment in RSA-1/RSA-5)
- diligence_actions: includes review of American Tower Easement and Assignment Agreement (Doc Id 53136944), confirmation of warehouse use continuity, lot survey to confirm RSA-1 / RSA-5 boundary

---

## 3. Prompt 3C-SCEN — Scenario Generation

### 3.1 Design brief

**What it does:** Generates one to three ranked development scenarios for the property, each with its own physical configuration, operating strategy, zoning pathway, and per-scenario delta against the baseline assumptions. Each scenario is a meaningfully different business plan, not a sensitivity case.

**Why this is its own prompt:** The current Prompt 3C produces a single recommendation paragraph. The new architecture treats scenario generation as a structured task that produces typed objects the financial pipeline can fan out across (Session 4 work). Bundling scenario generation into a synthesis prompt produces unstructured prose that cannot drive separate Excel workbooks.

**Why it runs second:** It depends on the conformity finding (a nonconforming use unlocks "rebuild by-right" as a scenario; a conforming use does not). It also depends on having a baseline to compute deltas against, which only exists after the user submits the deal.

**The "no padding" rule:** This is the most important behavioral rule. If the deal is a clean stabilized hold with no realistic value-add lever, the prompt MUST return a single PREFERRED scenario and refuse to invent alternatives. Padding scenarios is worse than offering one — it dilutes the analysis and forces the report to render meaningless comparison tables.

### 3.2 Inputs

| Variable | Type | Source | Notes |
|---|---|---|---|
| `property_address` | str | `deal.address.full_address` | |
| `asset_type` | str | `deal.asset_type` | |
| `investment_strategy` | str | `deal.investment_strategy` | The user's submitted strategy — informs but does not constrain scenario generation |
| `baseline_assumptions_json` | str (JSON) | `deal.assumptions.dict()` | Acquisition price, units, building SF, in-place rents, etc. — the baseline for delta calculations |
| `current_use` | str | derived | |
| `current_units` | int / null | `deal.assumptions.current_units` | |
| `building_sf` | float / null | `deal.assumptions.building_sf` | |
| `lot_sf` | float / null | `deal.assumptions.lot_sf` | |
| `zoning_json` | str (JSON) | `deal.zoning.dict()` | |
| `buildable_capacity_json` | str (JSON) | `deal.zoning` capacity fields | |
| `conformity_assessment_json` | str (JSON) | `deal.conformity_assessment.dict()` | Full output of 3C-CONF |
| `market_context_summary` | str | derived from `deal.market_data` | One paragraph of plain-text market context — submarket trends, rent comps, sale comps, supply pipeline |
| `single_scenario_mode` | bool | `deal.workflow_controls.single_scenario_mode` | If True, prompt returns exactly one PREFERRED scenario |
| `strategy_lock` | str / null | `deal.workflow_controls.strategy_lock` | If set, all returned scenarios must use this strategy |
| `max_scenarios` | int | `deal.workflow_controls.max_scenarios` | Hard cap; default 3, range 1–3 |

### 3.3 System prompt — full text

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
9. Express budget, rent, and timeline as deltas against the
   baseline_assumptions provided. Specifically:
   - construction_budget_delta_usd is the dollar delta vs. the baseline
     construction budget. A delta of 0 means no change from baseline.
     Use null only when not applicable (e.g., stabilized hold of
     existing building with no construction).
   - rent_delta_pct is the fractional delta vs. baseline rents (0.05 =
     +5% premium; -0.10 = -10% concession). Use null when not applicable.
   - timeline_delta_months is the integer month delta vs. baseline
     timeline. Negative numbers shorten; positive numbers extend.
   These delta fields drive the financial fan-out in Session 4. A
   non-anchored absolute number will produce wrong NOI / IRR.

OUTPUT FORMAT
Return ONLY the JSON array of scenario objects below. No preamble, no
postamble, no markdown fences. The array length is between 1 and
{max_scenarios}. The first element is always rank 1 / PREFERRED.
```

### 3.4 User prompt template — full text

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

### 3.5 Output schema mapping to DealData

Each element of `scenarios[]` maps directly to a `DevelopmentScenario` Pydantic model. The orchestrator iterates the array and appends each constructed model to `deal.scenarios`. The PREFERRED scenario's `scenario_id` is also written to `deal.zoning_extensions.preferred_scenario_id` (set by 3C-HBU, not this prompt).

### 3.6 Edge cases and failure modes

| Edge case | Prompt's behavior |
|---|---|
| Deal is clean stabilized hold with no value-add lever | Returns 1 scenario marked PREFERRED; refuses to pad |
| `single_scenario_mode=True` | Returns exactly 1 scenario; ignores other scenarios it would have generated |
| `strategy_lock="stabilized_hold"` | All scenarios use stabilized_hold; e.g., for Belmont, returns "Stabilized Hold As-Built" only |
| Conformity is INDETERMINATE | Default scenarios use existing configuration; each scenario's `entitlement_risk_flag.severity` is HIGH with explanation |
| Conformity is LEGAL_NONCONFORMING_USE and renovation is contemplated | Scenario's `entitlement_risk_flag` flags the substantial-improvement threshold risk |
| Property is vacant land | First scenario is by-right development at buildable_capacity; alternates may include variance pathways for higher density |
| Existing structure exceeds by-right caps | "Demolish and rebuild by-right" appears as ALTERNATE only if rebuild economics make sense; otherwise omitted entirely (this is exactly the 967-73 N. 9th St case) |
| LLM returns 4+ scenarios | `_apply_3c_scen` truncates to `max_scenarios` and logs a warning |
| LLM returns 0 scenarios | `_apply_3c_scen` writes a single fallback "as-submitted" scenario from baseline assumptions, marked PREFERRED with HIGH entitlement_risk_flag |
| LLM returns duplicate scenario_ids | Parser raises; orchestrator retries once with stricter prefix |

### 3.7 Test expectations per reference deal

**Deal A — 967-73 N. 9th St (21 units in 6-unit-cap district)**
- 2-3 scenarios expected
- Rank 1 (PREFERRED): "asbuilt_reno_21u" — light cosmetic renovation maintaining the legal nonconforming density; pathway BY_RIGHT (working within grandfathered configuration)
- Rank 2 (ALTERNATE): possibly "variance_pathway_higher_value" — pursue formal variance to remove substantial-improvement risk; pathway VARIANCE; success_probability_pct around 60%
- Rank 3 (ALTERNATE) optional: "demo_rebuild_byright_6u" — demolish and rebuild to by-right density; pathway BY_RIGHT; included only if 6-unit economics work, which is unlikely

**Deal B — Belmont Apartments, RSD-3**
- 1-2 scenarios expected
- Rank 1 (PREFERRED): "stabilized_hold_36u" — operate as-is, no displacement, lease vacancies to market; pathway BY_RIGHT (within grandfathered envelope)
- Rank 2 (ALTERNATE) possible: "light_value_add_36u" — kitchen/bath upgrades on turnover with rent premium; pathway BY_RIGHT; flagged with substantial-improvement-threshold risk if total renovation budget approaches 50% of structure value
- NOT generated: "demo and rebuild" (RSD-3 max is single-family detached, so 36-unit rebuild is not by-right and economics are unworkable)

**Deal C — 3520 Indian Queen Lane, RSA-1/RSA-5, existing warehouse + American Tower easement**
- 3 scenarios expected (this is the gold-tier multi-scenario test)
- Rank 1, 2, 3 should approximate the three CMA Revised Schemes:
  - "scheme_c_courtyard_95u" — apartment + courtyard concept, 95 units, pathway likely VARIANCE or REZONE given current zoning (success_probability_pct ~50%)
  - "scheme_a_townhomes_88u" — 60 one-bed + 22 two-bed apartments + 6 three-bed townhomes, 88 units total, pathway likely VARIANCE
  - "scheme_b_garden_84u" — 24 studios + 44 one-bed + 16 two-bed in garden + double-loaded corridor format, 84 units, pathway likely VARIANCE
- Each scenario must explicitly account for the American Tower exclusive easement area (2,625 SF) being unbuildable
- Each scenario flags entitlement risk as MEDIUM or HIGH given residential zoning + redevelopment scale

---

## 4. Prompt 3C-HBU — Cross-Scenario Synthesis

### 4.1 Design brief

**What it does:** Reads the conformity assessment and all generated scenarios, then produces the IC-grade synthesis: the cross-scenario recommendation paragraphs, the preferred scenario ID, the parcel's use flexibility score (1-5), and the overlay impact assessment. This is the "what does it all mean for the committee" layer.

**Why this is its own prompt:** The first two prompts produce structured data — facts and options. This prompt produces narrative judgment. Separating data from judgment improves both: the data prompts get crisper schemas, and the judgment prompt has a focused, narrative-only job.

**Why it runs third:** It needs everything the first two prompts produce. It is purely a synthesis layer with no new data fetching.

**What "use flexibility score" means:** A 1-5 integer score capturing how much optionality the parcel offers under current zoning. Score 1 = single-purpose parcel with no flexibility (e.g., narrow rowhouse lot in a strict RSA district). Score 5 = highly flexible parcel with multiple by-right uses, generous dimensional capacity, and overlay opportunities (e.g., a CMX-3 parcel with an MIH bonus). The score is a quick visual indicator on the report's section 6 header.

### 4.2 Inputs

| Variable | Type | Source | Notes |
|---|---|---|---|
| `property_address` | str | `deal.address.full_address` | |
| `asset_type` | str | `deal.asset_type` | |
| `conformity_assessment_json` | str (JSON) | `deal.conformity_assessment.dict()` | |
| `scenarios_json` | str (JSON) | `[s.dict() for s in deal.scenarios]` | All scenarios from 3C-SCEN |
| `zoning_json` | str (JSON) | `deal.zoning.dict()` | |
| `overlay_districts` | list[str] | `deal.zoning.overlay_districts` | E.g., ["MIH Overlay", "/CTR Center City Overlay"] |
| `market_context_summary` | str | derived from `deal.market_data` | Same paragraph passed to 3C-SCEN |

### 4.3 System prompt — full text

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

### 4.4 User prompt template — full text

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

### 4.5 Output schema mapping to DealData

The output populates `DealData.zoning_extensions`, which is a `ZoningExtensions` Pydantic model:

| JSON field | DealData field |
|---|---|
| `cross_scenario_recommendation` | `deal.zoning_extensions.cross_scenario_recommendation` |
| `preferred_scenario_id` | `deal.zoning_extensions.preferred_scenario_id` (validated against `[s.scenario_id for s in deal.scenarios]`) |
| `use_flexibility_score` (flat int) | `deal.zoning_extensions.use_flexibility_score` |
| `use_flexibility_explanation` (flat str) | `deal.zoning_extensions.use_flexibility_explanation` |
| `overlay_impact_assessment` (list of OverlayImpact) | `deal.zoning_extensions.overlay_impact_assessment` |

A model-level validator on `DealData` ensures `preferred_scenario_id` is found in `[s.scenario_id for s in deal.scenarios]`. Mismatch raises a parse error and triggers retry.

### 4.6 Edge cases and failure modes

| Edge case | Prompt's behavior |
|---|---|
| Only 1 scenario in input | Recommendation explicitly notes "single scenario presented" and states unlock conditions |
| No overlays present | overlay_impact_assessment states "No overlay districts apply to this parcel" plus brief context |
| LLM recommends a scenario_id not in the array | Parser raises; orchestrator retries once; on second failure, defaults to the rank-1 scenario and logs warning |
| Conformity is INDETERMINATE | Synthesis explicitly acknowledges the indeterminacy and recommends the most defensible scenario |
| LLM produces bullet lists or markdown headers in cross_scenario_recommendation | Parser strips them and logs warning; if stripping leaves <100 chars, retries once |

### 4.7 Test expectations per reference deal

**Deal A — 967-73 N. 9th St**
- preferred_scenario_id: "asbuilt_reno_21u" (matches rank 1 from 3C-SCEN)
- use_flexibility_score: 2 (parcel is locked into nonconforming density; meaningful redevelopment requires variance)
- cross_scenario_recommendation: opens with "Recommend the as-built renovation pathway..."; addresses the variance alternate; flags substantial-improvement threshold as the single most important diligence item

**Deal B — Belmont Apartments**
- preferred_scenario_id: "stabilized_hold_36u"
- use_flexibility_score: 1 (RSD-3 is the most restrictive multifamily-blocking district; current building is the only legal multifamily configuration)
- cross_scenario_recommendation: emphasizes preservation of the grandfathered status; explicitly notes that any meaningful upside requires accepting renovation budget discipline to stay below the substantial-improvement threshold; if only one scenario was generated, explains that the single-family zoning prevents redevelopment scenarios

**Deal C — 3520 Indian Queen Lane**
- preferred_scenario_id: likely "scheme_c_courtyard_95u" (highest unit count, best site utilization, but model could reasonably prefer scheme A or B based on construction risk)
- use_flexibility_score: 2 (residential zoning blocks by-right multifamily redevelopment; site requires variance pathway for any of the CMA schemes)
- cross_scenario_recommendation: addresses the entitlement risk explicitly, ties recommendation to the demonstrated CMA design work and the existing American Tower lease income as a holding-cost offset during entitlement; identifies the single most important diligence item as a pre-application meeting with the Philadelphia City Planning Commission

---

## 5. Orchestration logic in market.py

### 5.1 The new orchestrator function

A single new function in `market.py` replaces the existing 3C call site. Pseudocode:

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

### 5.2 The confidence gate

A programmatic check before 3C-CONF runs. The gate passes only if ALL of:

| Criterion | Threshold |
|---|---|
| `deal.zoning.zoning_code` is populated | not None and not empty string |
| `deal.zoning.permitted_uses` length | ≥ 3 entries |
| `deal.zoning.dimensional_standards` populated fields | ≥ 4 of 9 core dimensional fields populated (max_height_ft, max_stories, min_lot_area_sf, max_lot_coverage_pct, max_far, front_setback_ft, rear_setback_ft, side_setback_ft, min_parking_spaces) |
| `deal.assumptions.lot_sf` is populated | not None and > 0 |

If any criterion fails, 3C-CONF is skipped and `_indeterminate_conformity_assessment(deal)` is called. The fallback writes a fully-typed `ConformityAssessment` with `status=CONFORMITY_INDETERMINATE`, populated `confidence_reasons` listing each failed criterion, an explanatory `risk_summary`, and a `diligence_actions_required` list directing the user to manually retrieve the zoning ordinance and rerun.

### 5.3 Retry policy

Each prompt gets one retry on parse failure. The retry uses an identical user prompt but prepends the system prompt with a strict reminder:

```
RETRY: The previous response failed JSON parsing. You MUST return ONLY
valid JSON matching the exact schema below — no preamble, no markdown
fences, no commentary. If you cannot match the schema, return the schema
with all fields set to null/empty rather than malformed JSON.
```

If the retry also fails, the typed-empty fallback fires and the pipeline continues.

### 5.4 Fallback scenarios per failure mode

| Failure | Fallback |
|---|---|
| Confidence gate fails | INDETERMINATE conformity; 3C-SCEN still runs and produces single fallback scenario flagged HIGH entitlement risk |
| 3C-CONF call fails (both attempts) | Same as confidence gate failure |
| 3C-SCEN call fails (both attempts) | Single "as-submitted" scenario from baseline assumptions; rank 1, PREFERRED, BY_RIGHT pathway with success_probability_pct=null, entitlement_risk_flag.severity=HIGH with description "Scenario generation failed; manual review required" |
| 3C-HBU call fails (both attempts) | Minimal `ZoningExtensions` per `market.py:_minimal_zoning_extensions` (the canonical source): `preferred_scenario_id` = first scenario's ID; `cross_scenario_recommendation` = sentinel text noting the synthesis failed and manual review is required; `use_flexibility_score` = `1` (the conservative-default sentinel — "single-purpose, locked in" — fail-toward-forcing-manual-review per CP2 reasoning); `use_flexibility_explanation` = sentinel string explicitly noting this is a fallback default, not a real assessment; `overlay_impact_assessment` = `[]` (empty list, not a string). |

Every fallback writes a clearly labeled placeholder so the report renders without errors and the user can see the gap immediately.

---

## 6. Test fixtures

Three JSON test fixtures live in `tests/fixtures/zoning_overhaul_session_2/`. Each fixture is a complete `DealData` JSON that exercises one prompt chain end-to-end.

### 6.1 Reference Deal A — 967-73 N. 9th St (existing fixture from Session 1)

This fixture already exists from Session 1. Session 2 adds expected-output JSON files alongside it for regression testing:
- `deal_a_input.json` — DealData input
- `deal_a_expected_3c_conf.json` — expected `conformity_assessment` output
- `deal_a_expected_3c_scen.json` — expected `scenarios[]` output
- `deal_a_expected_3c_hbu.json` — expected `zoning_extensions` output

### 6.2 Reference Deal B — Belmont Apartments

New fixture file: `deal_b_belmont_input.json`. Key fields populated from the Belmont Offering Memorandum:

```json
{
  "address": {
    "full_address": "2217 N 51st St, Philadelphia, PA 19131",
    "city": "Philadelphia",
    "state": "PA",
    "county": "Philadelphia",
    "neighborhood": "Wynnefield"
  },
  "asset_type": "multifamily",
  "investment_strategy": "stabilized_hold",
  "assumptions": {
    "asking_price": null,
    "current_units": 36,
    "building_sf": 20640,
    "lot_sf": 10140,
    "year_built": 1926,
    "monthly_gross_rent": 41401,
    "in_place_noi_annual": 230438
  },
  "zoning": {
    "zoning_code": "RSD-3",
    "zoning_district_name": "Residential Single-Family Detached, District 3",
    "permitted_uses": [
      "Single-family detached dwelling (by-right)",
      "Religious assembly (in completely enclosed detached building, with use registration permit)",
      "Day care",
      "Educational facility (special exception)",
      "Public park"
    ],
    "max_height_ft": 38,
    "max_stories": 3,
    "min_lot_area_sf": 5000,
    "max_lot_coverage_pct": 0.30,
    "front_setback_ft": 8,
    "rear_setback_ft": 30,
    "side_setback_ft": 8,
    "min_parking_spaces": 1
  }
}
```

Expected outputs follow Section 2.7, 3.7, 4.7 above.

### 6.3 Reference Deal C — 3520 Indian Queen Lane

New fixture file: `deal_c_indian_queen_input.json`. Key fields from the documents you uploaded (CMA site plans, recorded American Tower easement, survey site plan):

```json
{
  "address": {
    "full_address": "3520 Indian Queen Ln, Philadelphia, PA 19129",
    "city": "Philadelphia",
    "state": "PA",
    "county": "Philadelphia",
    "neighborhood": "East Falls"
  },
  "asset_type": "industrial",
  "investment_strategy": "opportunistic",
  "assumptions": {
    "asking_price": 5000000,
    "current_units": 0,
    "building_sf": 42420,
    "lot_sf": 68389,
    "year_built": null
  },
  "zoning": {
    "zoning_code": "RSA-5",
    "zoning_district_name": "Residential Single-Family Attached, District 5",
    "permitted_uses": [
      "Single-family attached dwelling (by-right)",
      "Single-family semi-detached dwelling (by-right)"
    ]
  },
  "is_split_zoned": true,
  "split_zoning_codes": ["RSA-1", "RSA-5"],
  "encumbrances": [
    {
      "type": "EASEMENT",
      "doc_id": "53136944",
      "grantee": "American Tower Asset Sub II, LLC",
      "exclusive_area_sf": 2625,
      "access_easement_width_ft": 20,
      "term": "perpetual",
      "annual_income_usd": null
    },
    {
      "type": "LEASE_TO_EASEMENT",
      "doc_id": "52842941",
      "grantee": "SBC Tower Holdings LLC",
      "expiration": "2064-11-30",
      "right_of_first_refusal": true
    }
  ]
}
```

The `encumbrances` field is a Session 2 addition flagged for inclusion in the schema (see Section 8).

---

## 7. Cost estimation

Per the catalog v4 baseline (~$0.17–$0.47 per deal in Claude API spend), this change replaces 1 Sonnet call with 3 Sonnet calls. Estimated incremental cost per deal:

| Prompt | Model | Avg input tokens | Avg output tokens | Per-call cost |
|---|---|---|---|---|
| 3C-CONF | Sonnet | ~3,500 | ~1,000 | ~$0.025 |
| 3C-SCEN | Sonnet | ~4,500 | ~2,500 | ~$0.052 |
| 3C-HBU | Sonnet | ~4,000 | ~1,200 | ~$0.030 |
| **Total new** | | | | **~$0.107** |
| Prior 3C single call | Sonnet | ~3,500 | ~1,500 | ~$0.034 |
| **Net incremental cost per deal** | | | | **~$0.073** |

A retry on any single prompt adds ~$0.025 to ~$0.052 per occurrence. Expected per-deal cost ceiling: **$0.55** assuming one retry on the most expensive prompt (3C-SCEN). Token estimates are approximate; track actuals in the first 10 deal runs and update.

---

## 8. Schema gaps surfaced by Session 2

Two fields appeared in Session 2 design that are not yet in the Session 1 schema. These need to be added before Session 3 (the wiring session) begins:

### 8.1 `DealData.workflow_controls`

The Session 1 schema does not include workflow controls. Add:

```python
class WorkflowControls(BaseModel):
    single_scenario_mode: bool = False
    strategy_lock: Optional[InvestmentStrategy] = None
    max_scenarios: int = Field(default=3, ge=1, le=3)

# Add to DealData:
workflow_controls: WorkflowControls = Field(default_factory=WorkflowControls)
```

### 8.2 `DealData.encumbrances`

The Session 1 schema does not have a structured place for easements, leases, or other title encumbrances that materially affect buildability. The Indian Queen deal makes this gap obvious. Add:

```python
class EncumbranceType(str, Enum):
    EASEMENT = "EASEMENT"
    LEASE = "LEASE"
    LEASE_TO_EASEMENT = "LEASE_TO_EASEMENT"  # original lease later converted to easement
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
    annual_income_usd: Optional[float] = None  # if encumbrance generates revenue
    notes: Optional[str] = None

# Add to DealData:
encumbrances: List[Encumbrance] = Field(default_factory=list)
```

**Recommendation:** Roll both additions into a Session 1.5 micro-session in Claude Code BEFORE Session 3 begins. Both are pure schema additions with no logic — safe, fast, low risk.

---

## 9. Session 2 gate criteria

Session 2 is complete when ALL of the following are true. This is the explicit pass/fail checklist for sign-off.

- [ ] The three prompts (3C-CONF, 3C-SCEN, 3C-HBU) are reviewed and approved by Mike with no pending edits.
- [ ] Each prompt's full system text and user template is captured verbatim in `FINAL_APPROVED_Prompt_Catalog_v5.md`.
- [ ] Catalog v5 marks the prior Prompt 3C as DEPRECATED and references Session 2 as the replacement.
- [ ] Catalog v5 documents the orchestration logic in Section 5 above.
- [ ] Catalog v5 documents the confidence gate criteria in Section 5.2 above.
- [ ] Catalog v5 documents the retry policy and fallback scenarios in Sections 5.3 and 5.4 above.
- [ ] Test fixture for Reference Deal B (Belmont) is captured in this document or a companion file.
- [ ] Test fixture for Reference Deal C (Indian Queen) is captured in this document or a companion file.
- [ ] Schema gaps surfaced by Session 2 (Section 8) are noted and queued for Session 1.5 micro-session.
- [ ] Master plan document (`DealDesk_Zoning_Overhaul_Plan.md`) is updated with Session 2 completion timestamp and a pointer to catalog v5.
- [ ] No code has been written. Session 2 is documentation-only. Implementation begins in Session 3 in Claude Code.

---

## 10. Session 2 to Session 3 handoff

When Session 2 is approved, the next Claude Code session reads:

1. `DealDesk_Zoning_Overhaul_Plan.md` (master plan)
2. `Session_1_Schema_Design.md` (already implemented)
3. **`Session_2_Prompt_Specification.md`** (this file — implementation source of truth)
4. `FINAL_APPROVED_Prompt_Catalog_v5.md` (the approved prompt text)

Session 3 then implements:
- The `_SYSTEM_3C_CONF`, `_USER_3C_CONF`, `_apply_3c_conf` triple in `market.py`
- Same triple for 3C-SCEN and 3C-HBU
- The `run_zoning_synthesis_chain` orchestrator
- The `_confidence_gate_passes` function
- The `_indeterminate_conformity_assessment`, `_fallback_as_submitted_scenario`, `_minimal_zoning_extensions` fallback constructors
- Removal (or deprecation) of the old `_SYSTEM_3C` and `_USER_3C` constants
- Update to `enrich_market_data()` to call `run_zoning_synthesis_chain(deal)` instead of the old 3C call

Session 3 gate criteria are listed in the master plan, Section 5.

---

*End of Session 2 prompt specification. Awaiting Mike's review and approval.*
