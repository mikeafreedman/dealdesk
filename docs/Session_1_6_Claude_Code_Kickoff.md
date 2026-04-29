# Session 1.6 — Claude Code Kickoff Instructions

**Purpose:** Schema realignment surfaced during Session 3 reading. Update `models/models.py` to align with the approved prompt JSON shapes from Session 2. Update `docs/Session_2_Prompt_Specification.md` and `docs/FINAL_APPROVED_Prompt_Catalog_v5.md` to reflect the schema decisions. No prompts are wired in this session. No code outside `models/models.py` and the two doc files.

**Estimated effort:** 90–120 minutes including testing, doc updates, and commits.
**Risk level:** MEDIUM — touches more models than Session 1.5 and includes one structural reshape (GrandfatheringStatus) plus one new enum (ProposedPathwayRequirement). Backward-compatibility risk is real because Session 1's gate fixture deserializes against the existing schema.

---

## Context

Session 3 (the pipeline-wiring session) was halted during the read phase because the prompt JSON shapes approved in Session 2 do not match the Pydantic schema implemented in Session 1. The drift report at `docs/Session_1_6_Drift_Report.md` enumerates 8 drift points plus one cross-cutting Reference Deal A status contradiction.

Mike and Claude reviewed the drift report in claude.ai and locked in design decisions for each drift point. Session 1.6 implements those decisions. After Session 1.6 closes, Session 3 resumes from its existing plan summary unchanged — the bridging logic the agent would have had to invent in `_apply_3c_*` functions becomes unnecessary.

---

## Step 0 — Pre-flight checks

Before opening Claude Code, confirm in PowerShell:

```
cd C:\Users\mikea\dealdesk
git status
git tag --list zoning-overhaul-*
git log --oneline -5
```

Expected:

- `git status` shows `docs/Session_1_6_Drift_Report.md` as untracked, nothing else
- Three tags present: 1, 1.5, 2
- Top commit is the Session 1.5 docs commit (`6d6ec31`)

If anything is unexpected, stop and resolve before starting.

Open a new Claude Code session in `C:\Users\mikea\dealdesk\`.

---

## Step 1 — Paste this opening message to Claude Code

Copy everything between the `===BEGIN===` and `===END===` markers below into Claude Code as your first message.

===BEGIN===

I'm starting Session 1.6 of the DealDesk Zoning Overhaul. This is a schema realignment micro-session that bridges Session 1.5 (already complete) and Session 3 (which was halted during the read phase pending this realignment).

Before you write any code, please read these files in this exact order:

1. `docs/DealDesk_Zoning_Overhaul_Plan.md` — the master plan with all locked design decisions
2. `docs/Session_1_Schema_Design.md` — the schema spec already implemented (Session 1)
3. **`docs/Session_1_6_Drift_Report.md`** — the drift analysis you produced during the Session 3 read phase. THIS IS YOUR INPUT ARTIFACT. Read all 8 drift points, the cross-cutting Reference Deal A contradiction, and the summary table.
4. `docs/Session_2_Prompt_Specification.md` — the prompt design spec. You'll be modifying §2.7 (Deal A status), §§2.3, 2.4, 4.5 (prompt JSON schemas) in this session.
5. `docs/FINAL_APPROVED_Prompt_Catalog_v5.md` — the catalog form. You'll be modifying §1 (3C-CONF system text + JSON schema) and §4 (Deal A status) in this session.

Also re-read the current state of `models/models.py` (you've seen it before — Sessions 1 and 1.5 added the relevant ZONING OVERHAUL section).

After you've read all six, summarize back to me:

- The 9 schema changes you'll make (per Sections 2.1-2.9 of this kickoff)
- The 2 doc files you'll modify, with the specific section changes per document
- The 2 review checkpoints you'll pause at, what each will show me, and what I need to approve at each
- How you'll handle backward compatibility with the existing Session 1 gate fixture (`tests/fixtures/zoning_overhaul_session_1_fixture.json`) — specifically, which fields if any need migration
- What test fixtures will be added or modified
- What the gate script will test (10 criteria minimum)
- What the two commit messages will be (one for code, one for docs)

Do NOT start coding until I confirm your plan.

===END===

---

## Step 2 — What Claude Code should do (your reference, not its instructions)

The session has 9 schema changes in `models/models.py`, 2 doc-file updates, and 2 commits at the end. Use this checklist when reviewing the agent's plan summary.

### 2.1 NonconformityType enum — split SETBACKS into 4 values

Per Drift #1, Option B1 (approved). Add three new directional enum values; keep `SETBACKS` as a coarse fallback.

```python
class NonconformityType(str, Enum):
    USE = "USE"
    DENSITY = "DENSITY"
    HEIGHT = "HEIGHT"
    FAR = "FAR"
    FRONT_SETBACK = "FRONT_SETBACK"      # NEW in 1.6
    REAR_SETBACK = "REAR_SETBACK"        # NEW in 1.6
    SIDE_SETBACK = "SIDE_SETBACK"        # NEW in 1.6
    SETBACKS = "SETBACKS"                # KEPT as coarse fallback
    LOT_COVERAGE = "LOT_COVERAGE"
    PARKING = "PARKING"
    LOT_AREA = "LOT_AREA"
    OTHER = "OTHER"
```

Schema location per drift report: `models/models.py` lines 1331–1345.

### 2.2 NonconformityItem — three field changes

Per Drifts #2, #3, #4 (all approved).

- Rename: `existing_value: str` → `actual_value: str`
- Add: `standard_description: Optional[str] = None`
- Drop: `triggers_loss_of_grandfathering: List[str]` (loss triggers move to GrandfatheringStatus only)

Schema location per drift report: `models/models.py` lines 1374–1383.

### 2.3 GrandfatheringStatus — replace with 6-field model

Per Drift #5, with Mike's refinement (drop `is_documented` and `documentation_source`).

```python
class GrandfatheringStatus(BaseModel):
    """
    Grandfathering posture for a legal nonconforming property.
    Session 1.6 reshape: replaces the documentation-tracking model
    with the prompt's risk-modeling structure.
    """
    is_presumed_grandfathered: bool
    basis: str = Field(
        description="Plain-English basis for the presumption "
                    "(e.g., 'Built 1926, predating current code; continuous use presumed'). "
                    "May reference documentation if any exists."
    )
    loss_triggers: List[str] = Field(
        default_factory=list,
        description="Events that would void grandfathering "
                    "(e.g., 'Substantial improvement >50%', 'Change of use', 'Abandonment >12 months')."
    )
    verification_required: bool = Field(
        default=True,
        description="Whether counsel verification is required before closing."
    )
    confirmation_action_required: Optional[str] = Field(
        default=None,
        description="Specific action to confirm grandfathering (e.g., 'Pull L&I rental license history')."
    )
    risk_if_denied: Optional[str] = Field(
        default=None,
        description="Plain-English consequence if grandfathering is not confirmed."
    )
```

Schema location per drift report: `models/models.py` lines 1386–1397.

**Backward compatibility note:** The existing Session 1 gate fixture has the OLD `GrandfatheringStatus` shape. The agent must update the fixture to match the new shape. This is the only fixture migration in scope this session.

### 2.4 ConformityStatus enum — confirm shape

Per Drift #6 — the schema enum already has all the right values. No changes needed except verify that `LEGAL_NONCONFORMING_DENSITY` and `MULTIPLE_NONCONFORMITIES` are present and `VARIANCE_REQUIRED_FOR_PROPOSED` and `SPECIAL_EXCEPTION_REQUIRED_FOR_PROPOSED` are NOT present (those move to a new enum per 2.5 below).

Schema location per drift report: `models/models.py` lines 1298–1312.

### 2.5 New ProposedPathwayRequirement enum

Per Drift #6. Captures the proposed-business-plan's entitlement requirement separately from the existing-condition conformity status.

```python
class ProposedPathwayRequirement(str, Enum):
    """
    Entitlement requirement for the proposed business plan.
    Session 1.6 addition. Distinct from ConformityStatus (which
    describes the existing condition under current zoning).
    """
    NONE = "NONE"
    VARIANCE_REQUIRED = "VARIANCE_REQUIRED"
    SPECIAL_EXCEPTION_REQUIRED = "SPECIAL_EXCEPTION_REQUIRED"
    REZONE_REQUIRED = "REZONE_REQUIRED"
```

Place alphabetically in the Session 1 ZONING OVERHAUL enum block, between `NonconformityType` and any successor.

### 2.6 ConformityAssessment — add proposed_pathway_requirement field

Per Drift #6.

```python
proposed_pathway_requirement: Optional[ProposedPathwayRequirement] = Field(
    default=None,
    description="Set only when the proposed business plan (not the existing condition) "
                "requires a discretionary approval. None if the proposed plan is by-right "
                "or no proposed plan has been articulated."
)
```

Add to ConformityAssessment model. Schema location per drift report: lines 1495–1547.

### 2.7 UseAllocation — rename + add field

Per Drift #7, with Mike's `square_feet` → `sf` rename.

- Rename: `square_feet: float` → `sf: float`
- Add: `share_pct: Optional[float] = None`

Other UseAllocation fields (`use_category`, `unit_count`, `notes`) remain unchanged.

### 2.8 ZoningPathway — add rationale field

Per Drift #7.

- Add: `rationale: Optional[str] = None`

Other ZoningPathway fields remain unchanged. Schema location per drift report: lines 1400–1410.

### 2.9 ZoningExtensions and OverlayImpact — confirm shape

Per Drift #8. The agent's drift report confirms the schema shape is already correct (`use_flexibility_score: int` and `use_flexibility_explanation: str` as flat fields; `overlay_impact_assessment: List[OverlayImpact]` as a structured list). No schema changes needed in this section — but the prompt-text updates in §3 below DO need to flatten the prompt and restructure the overlay output.

### 2.10 DevelopmentScenario, EntitlementRiskFlag — confirm IC-grade fields

Per Drift #7, the schema already has the IC-grade fields the prompt was missing (`approval_body`, `fallback_if_denied`, `key_risks`, `risk_summary`, `diligence_required`). No schema changes needed — these become required additions to the prompt JSON schema in §3 below.

### 2.11 Session 1 fixture — backward compatibility migration

The existing fixture at `tests/fixtures/zoning_overhaul_session_1_fixture.json` was built against the pre-1.6 schema. Specifically, it likely has:

- `existing_value` field (now `actual_value`)
- Possibly a `triggers_loss_of_grandfathering` list on a NonconformityItem (now dropped)
- Old GrandfatheringStatus shape (now replaced)
- Possibly `square_feet` in UseAllocation (now `sf`)

Migrate the fixture in-place to match the new schema. Document the migration in the commit message. Do NOT delete or rename the fixture file — its identity is referenced by the Session 1 gate script.

### 2.12 Session 1.5 fixture — verify no migration needed

The fixture at `tests/fixtures/zoning_overhaul_session_1_5_fixture.json` covers WorkflowControls and Encumbrance — neither of which is touched by Session 1.6. Verify by re-deserializing it after the schema changes; should round-trip with no edits.

---

## Step 3 — Doc work in scope

Two documentation files must be updated to match the schema decisions. **The agent should NOT modify any other docs** (master plan, Session 1 schema spec, drift report, this kickoff sheet).

### 3.1 docs/Session_2_Prompt_Specification.md

Four sections to modify:

**§2.3 (3C-CONF system prompt — full text):**

- Update the CONFORMITY STATUS list. Current list has 7 values including VARIANCE_REQUIRED_FOR_PROPOSED and SPECIAL_EXCEPTION_REQUIRED_FOR_PROPOSED. New list has 7 values: CONFORMING, LEGAL_NONCONFORMING_USE, LEGAL_NONCONFORMING_DENSITY, LEGAL_NONCONFORMING_DIMENSIONAL, MULTIPLE_NONCONFORMITIES, ILLEGAL_NONCONFORMING, CONFORMITY_INDETERMINATE.
- Add a new section to the system prompt instructing the LLM to return `proposed_pathway_requirement` separately if a proposed business plan is described in the inputs (otherwise return null).

**§2.4 (3C-CONF user prompt template + JSON schema):**

- Update the JSON schema. `status` enum values now reflect the existing-condition list above.
- Add `proposed_pathway_requirement` field with enum values: `"NONE|VARIANCE_REQUIRED|SPECIAL_EXCEPTION_REQUIRED|REZONE_REQUIRED|null"`.
- Update `nonconformity_type` enum to include FRONT_SETBACK, REAR_SETBACK, SIDE_SETBACK (and keep SETBACKS as coarse fallback).
- Rename `actual_value` is already correct (matches new schema).
- Add `standard_description` field to nonconformity_details items (already present; verify).
- Replace the `grandfathering_status` field shape to match the new 6-field GrandfatheringStatus model.

**§2.7 (3C-CONF test expectations per reference deal):**

- Deal A status: change `LEGAL_NONCONFORMING_DIMENSIONAL` → `LEGAL_NONCONFORMING_DENSITY`. Add a note: `proposed_pathway_requirement: null` (since no proposed business plan changes the existing config).
- Deal B status: keep `LEGAL_NONCONFORMING_USE`. Add `proposed_pathway_requirement: null`.
- Deal C status: keep `LEGAL_NONCONFORMING_USE`. Add `proposed_pathway_requirement: VARIANCE_REQUIRED` (any of the three CMA schemes requires variance).

**§3.4 (3C-SCEN user prompt template + JSON schema):**

- Flatten `physical_config` block — `unit_count` and `building_sf` move to top level of each scenario.
- Update `use_mix` items: `use_label` → `use_category`, `sf` stays (matches new schema), keep `share_pct`.
- Flatten `assumption_deltas` block — fields move to top level: `construction_budget_delta_usd` (renamed from `construction_budget_usd`, semantics now explicitly delta-vs-baseline), `rent_delta_pct` (renamed from `rent_premium_pct_vs_baseline`), `operating_strategy` (renamed from `operating_strategy_note`), `timeline_delta_months` (renamed from `timeline_months_to_stabilization`).
- Add `key_risks: ["risk 1", "risk 2", "..."]` to scenario shape.
- Update `zoning_pathway`: add `rationale`, rename `estimated_pathway_cost_usd` → `estimated_soft_cost_usd`, add `approval_body: "Plain-English approving body"`, add `fallback_if_denied: "Plain-English fallback strategy"`.
- Update `entitlement_risk_flag`: add `risk_summary` and `diligence_required: List[str]`.
- Add explicit instruction in the system prompt that all delta fields must be anchored against the `baseline_assumptions` input (use the explicit phrasing: "Express budget, rent, and timeline as deltas against the baseline_assumptions provided. A delta of 0 means no change from baseline.").

**§4.4 (3C-HBU user prompt template + JSON schema):**

- Flatten `use_flexibility_score`: `score` → top-level `use_flexibility_score: int`, `rationale` → top-level `use_flexibility_explanation: str`. Match the schema field names.
- Update `overlay_impact_assessment` from a single string to a structured list per OverlayImpact shape: `[{ "overlay_name", "overlay_type", "impact_summary", "triggers_review", "additional_diligence" }]`. Add explicit instruction: "If no overlay districts apply to this parcel, return an empty list."

### 3.2 docs/FINAL_APPROVED_Prompt_Catalog_v5.md

Two sections to modify (mirror the Session 2 spec changes):

**§1 (Prompt 3C-CONF system text and user template):**

- Update CONFORMITY STATUS list to match the new 7-value list (no more proposed-pathway values mixed in).
- Add `proposed_pathway_requirement` instruction.
- Update JSON schema in user template to reflect the new shape.

**§4 (Reference deal test expectations):**

- Deal A status: `LEGAL_NONCONFORMING_DIMENSIONAL` → `LEGAL_NONCONFORMING_DENSITY`.
- Deal B and C: confirm existing entries are still correct after the schema changes.
- Add `proposed_pathway_requirement` notes for each deal as in Session 2 spec §2.7.

---

## Step 4 — Test, gate, commit, tag

### 4.1 Test fixtures

- Migrate `tests/fixtures/zoning_overhaul_session_1_fixture.json` in-place per §2.11.
- Verify `tests/fixtures/zoning_overhaul_session_1_5_fixture.json` round-trips with no edits per §2.12.
- New fixture `tests/fixtures/zoning_overhaul_session_1_6_fixture.json` exercising:
  - The new GrandfatheringStatus 6-field shape with realistic Belmont-style content
  - The new ProposedPathwayRequirement enum with `VARIANCE_REQUIRED`
  - The renamed `actual_value` field on a NonconformityItem
  - The `standard_description` field populated
  - The new `FRONT_SETBACK` enum value (gate that distinguishes it from coarse `SETBACKS`)
  - UseAllocation with `sf` and `share_pct` populated
  - ZoningPathway with `rationale` populated

### 4.2 Gate script — 10+ criteria

Create `tests/_session1_6_gate.py`. Delete before commit. Asserts at minimum:

1. `models.py` imports cleanly with new symbols (`ProposedPathwayRequirement`)
2. `NonconformityType` has all 12 values including FRONT_SETBACK / REAR_SETBACK / SIDE_SETBACK / SETBACKS
3. `NonconformityItem` rejects the old `existing_value` field name (NameError or ValidationError on construction with old name)
4. `NonconformityItem` accepts the new `actual_value` field
5. `GrandfatheringStatus` has the exact 6-field shape; rejects construction with old fields like `is_documented` or `documentation_source` (ValidationError)
6. `ProposedPathwayRequirement` enum exists with all 4 values; round-trips through JSON
7. `ConformityAssessment.proposed_pathway_requirement` defaults to None and accepts ProposedPathwayRequirement values
8. `UseAllocation.sf` works; old `square_feet` raises ValidationError
9. `ZoningPathway.rationale` accepts string and defaults to None
10. Migrated Session 1 fixture round-trips through `model_dump_json()` / `model_validate_json()` cleanly
11. Session 1.5 fixture round-trips with NO edits required (regression check — Session 1.6 must not break 1.5)
12. Session 1.6 fixture round-trips with all new fields populated
13. `git diff --stat` shows changes ONLY in `models/models.py`, the migrated Session 1 fixture, and the new Session 1.6 fixture (no other files in code commit)

### 4.3 Commit and tag plan

**Two commits at the end** (same pattern as Sessions 1.5 and 2):

**Commit 1 — Code commit:**

```
Session 1.6: Schema realignment — Resolve drift between Session 1 schema and Session 2 prompts

- NonconformityType: add FRONT_SETBACK, REAR_SETBACK, SIDE_SETBACK enum values
  (keep SETBACKS as coarse fallback)
- NonconformityItem: rename existing_value → actual_value, add standard_description,
  drop triggers_loss_of_grandfathering (now lives only on GrandfatheringStatus)
- GrandfatheringStatus: replace 5-field documentation-tracking shape with 6-field
  presumption-tracking shape (is_presumed_grandfathered, basis, loss_triggers,
  verification_required, confirmation_action_required, risk_if_denied)
- New enum ProposedPathwayRequirement (NONE, VARIANCE_REQUIRED, SPECIAL_EXCEPTION_REQUIRED, REZONE_REQUIRED)
- ConformityAssessment: add proposed_pathway_requirement field (Optional, default None)
- UseAllocation: rename square_feet → sf, add share_pct (Optional)
- ZoningPathway: add rationale (Optional)
- Migrate tests/fixtures/zoning_overhaul_session_1_fixture.json to new schema
- Add tests/fixtures/zoning_overhaul_session_1_6_fixture.json exercising all changes

Resolves 8 drift points + 1 cross-cutting Reference Deal A status contradiction
documented in docs/Session_1_6_Drift_Report.md. Surfaced during Session 3 reading;
unblocks Session 3 wiring with no bridging logic required in apply functions.
```

Tag: `zoning-overhaul-session-1-6-passed`

**Commit 2 — Docs commit:**

```
docs: Session 1.6 complete + master plan status table sync + spec realignment

- Update Session_2_Prompt_Specification.md (§2.3, §2.4, §2.7, §3.4, §4.4) to match
  the realigned schema: ConformityStatus split, GrandfatheringStatus reshape,
  UseAllocation rename, IC-grade field additions, overlay structured-list
- Update FINAL_APPROVED_Prompt_Catalog_v5.md (§1, §4) to mirror Session 2 spec
- Save Session_1_6_Drift_Report.md (input artifact, was untracked since Session 3 read)
- Append Session 1.6 history-log entry to master plan
- Sync Current Session Status table:
  - Session 1.6: NEW ROW, COMPLETED
  - Session 3: READY (awaiting Mike approval to resume; original kickoff sheet remains valid)
```

No tag on the docs commit — Session 1.6 is already tagged at the code commit.

---

## Step 5 — The two review checkpoints

The agent must pause at each and show diff + run tests before proceeding. **You must approve at each checkpoint before the agent moves to the next.**

### Checkpoint 1 — Schema changes + fixture migrations + new fixture

After the 9 schema changes are written, the Session 1 fixture is migrated, and the new Session 1.6 fixture is created. Diff scope: only `models/models.py`, the migrated fixture, and the new fixture. Verification: gate script runs and reports 10+/10+ pass.

What you'll review:

- Each of the 9 schema changes, against the kickoff specification
- The Session 1 fixture migration diff (this is the trickiest part — verify the migration is faithful, not a destructive rewrite)
- The new Session 1.6 fixture exercising all changes
- Gate script output

### Checkpoint 2 — Doc updates

After both `docs/Session_2_Prompt_Specification.md` and `docs/FINAL_APPROVED_Prompt_Catalog_v5.md` are updated, plus the master plan history-log entry and status-table sync are drafted (but not yet committed). Diff scope: only the four doc files (two prompt-doc updates, the drift report, and the master plan).

What you'll review:

- Section-by-section diffs of the two prompt docs
- Master plan history-log entry (matching the pattern of Sessions 1, 1.5, 2)
- Master plan status-table sync (Session 1.6 row added; Session 3 row updated to "READY (awaiting Mike approval to resume)")

After Checkpoint 2 approval, the agent commits both commits and tags. Then runs verification.

---

## Step 6 — Anti-scope-creep guardrails

The agent should NOT touch any of the following in this session:

- `market.py` — that's Session 3 work
- `financials.py`, `excel_builder.py` — that's Session 4 work
- `report_builder.py`, `chart_builder.py`, `word_builder.py` — that's Session 5 work
- `extractor.py`, `parcel_fetcher.py`, `iasworld_fetcher.py` — out of scope
- The frontend — out of scope
- Any other models in `models/models.py` outside the ZONING OVERHAUL section
- The Session 1 schema design spec (`docs/Session_1_Schema_Design.md`) — that document is historical record; do not retroactively edit it
- The Session 1.6 drift report itself (`docs/Session_1_6_Drift_Report.md`) — saved as-is for the audit trail

The only files that should be modified or created in this session:

- `models/models.py` (modifications + new ProposedPathwayRequirement enum)
- `tests/fixtures/zoning_overhaul_session_1_fixture.json` (in-place migration)
- `tests/fixtures/zoning_overhaul_session_1_6_fixture.json` (new)
- `docs/Session_2_Prompt_Specification.md` (modifications to §2.3, §2.4, §2.7, §3.4, §4.4)
- `docs/FINAL_APPROVED_Prompt_Catalog_v5.md` (modifications to §1, §4)
- `docs/DealDesk_Zoning_Overhaul_Plan.md` (history log + status table)
- `docs/Session_1_6_Drift_Report.md` (untracked → tracked, no content change)

If the agent proposes touching anything else, push back and ask why.

---

## Step 7 — When Session 1.6 is done, you're ready to resume Session 3

Session 3's plan summary from the prior reading (the seven deliverables, the four checkpoints, the line-number anchors, the commit messages) remains valid and unchanged. The bridging logic problem dissolves — `_apply_3c_*` functions become straightforward Pydantic constructions with no field translation.

Resume Session 3 by opening a new Claude Code session and pasting the same opening message from `docs/Session_3_Claude_Code_Kickoff.md`. The agent will re-read the docs (now updated with Session 1.6's changes), re-summarize the plan (which should match the plan it produced before, with the bridging concern absent), and proceed to Checkpoint 1.

---

## Step 8 — Operator notes (you, the human)

Three things to keep in mind during this session:

**Watch the fixture migration carefully.** At Checkpoint 1, the most important diff to scrutinize is the Session 1 fixture migration. The agent must rename `existing_value` → `actual_value`, drop any `triggers_loss_of_grandfathering` arrays from NonconformityItem, and reshape any GrandfatheringStatus block from the old 5-field shape to the new 6-field shape. A migration that silently drops content, adds invented content, or changes the conformity status of Reference Deal A would be a serious problem. Verify the diff is faithful.

**Verify the doc updates aren't paraphrased.** At Checkpoint 2, when the agent shows you diffs of the Session 2 spec and Catalog v5 prompt-text changes, your job is to verify the changes are surgical — they update what needs to be updated and leave the rest alone. The agent should NOT rewrite or "improve" any prompt text outside the specific sections listed in §3 above. If the agent took the opportunity to clean up unrelated sections, push back.

**One thing not to worry about.** The "out of scope" list in §6 is long, but Session 1.6 is well-bounded — almost everything outside `models.py` and the two doc files is off-limits. If you see the agent proposing to touch `market.py` "just to verify it still imports cleanly", that's fine for verification (read-only), but the agent should not modify it.

---

*End of Session 1.6 instructions.*
