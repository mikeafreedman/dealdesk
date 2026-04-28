# DealDesk Zoning Analysis Overhaul — Master Plan

**Document version:** 1.0
**Created:** April 27, 2026
**Owner:** Mike Freedman, Freedman Properties
**Status:** Approved — Session 1 ready to begin
**Persistence:** This file lives at `C:\Users\mikea\dealdesk\DealDesk_Zoning_Overhaul_Plan.md` and is read at the start of every Claude Code session in this build.

---

## How to use this document

This is the single source of truth for the DealDesk zoning analysis overhaul. It spans 5 Claude Code sessions across multiple weeks. Because each session is a separate Claude instance with no memory of prior sessions, this document is how continuity is preserved.

**At the start of every session, the working Claude instance must:**

1. Read this entire document.
2. Confirm which session is active (see the "Current Session Status" table below).
3. Confirm all gates from prior sessions are marked as PASSED.
4. Confirm the current session's deliverables and gate criteria before starting work.

**At the end of every session, the working Claude instance must update:**

1. The "Current Session Status" table (mark gate as PASSED or FAILED with notes).
2. The "Session History Log" at the bottom of this document.
3. Any open issues or carry-forward notes for the next session.

---

## Current Session Status

| Session | Layer | Status | Gate | Started | Completed |
|---|---|---|---|---|---|
| 1 | Schema (models.py) | COMPLETED (commit `8a80f32`, tag `zoning-overhaul-session-1-passed`) | PASSED (18/18 criteria) | 2026-04-27 | 2026-04-27 |
| 1.5 | Schema bridge (models.py — WorkflowControls + Encumbrance) | COMPLETED (commit `827149a`, tag `zoning-overhaul-session-1-5-passed`) | PASSED (8/8 criteria) | 2026-04-28 | 2026-04-28 |
| 1.6 | Schema realignment (models.py — drift resolution) | COMPLETED (commit `ad849cd`, tag `zoning-overhaul-session-1-6-passed`) | PASSED (13/13 criteria) | 2026-04-28 | 2026-04-28 |
| 2 | Prompt design + approval (claude.ai) | COMPLETED (commit `eab540c`, tag `zoning-overhaul-session-2-passed`) | PASSED (11/11 criteria) | 2026-04-27 | 2026-04-27 |
| 3 | Pipeline orchestration (market.py) | READY (awaiting Mike approval to resume; original kickoff sheet remains valid) | Not yet evaluated | — | — |
| 4 | Financial integration (financials.py + excel_builder.py) | BLOCKED on Session 3 | Not yet evaluated | — | — |
| 5 | Rendering (report_template.html + report.css) | BLOCKED on Session 4 | Not yet evaluated | — | — |

*The Current Session Status table above is the canonical view of where the project is right now. The Session History Log below is the audit trail of how we got here. Both must be updated as part of every session-close docs commit; the table reflects current state, the log reflects history.*

---

## Locked design decisions (do not relitigate)

These were decided in the planning conversation on April 27, 2026, between Mike Freedman and Claude. They are not open for discussion in any session unless Mike explicitly reopens them.

### D1 — Scenario definition

A scenario is any meaningfully different business plan, including:
- Different physical configuration (units, SF, height)
- Different zoning pathway (by-right vs. variance vs. rezone vs. special exception vs. conditional use)
- Same physical config but different operating strategy (e.g., lease as office vs. lease as artisan industrial)

A scenario is NOT:
- Different rent growth assumptions
- Different cap rate stress tests
- Different vacancy assumptions
- Different expense growth rates

These are sensitivities and stay inside each scenario's existing Sensitivity tab.

### D2 — Maximum scenarios per deal

Hard cap of 3 scenarios. Scenarios are ranked by IRR potential (rank 1 = highest expected LP IRR). When 4+ genuinely different paths exist, the prompt must rank top 3 and explain in the cross-scenario synthesis why others were excluded.

### D3 — Always all scenarios produce Excel files

If the prompt generates 3 scenarios, the system produces 3 Excel files plus 1 PDF. No user toggle, no conditional generation. Every scenario gets a workbook.

### D4 — Conformity assessment confidence gating

The conformity assessment runs ONLY when ALL of the following are present:

- `zoning_code` is populated and source-verified (not LLM training fallback)
- `permitted_uses` list has at least 3 entries
- At least 4 of the 6 dimensional standards (height, FAR, lot coverage, density, setbacks, parking) are populated

If any criterion fails → fallback to status `CONFORMITY_INDETERMINATE` with explicit explanation listing which criteria failed and what diligence is needed to resolve. The INDETERMINATE callout remains PROMINENT in the report — it is never hidden or downgraded.

### D5 — Frontend strategy

No frontend changes in this build. Outputs land in `/outputs/{deal_id}/` and are accessed manually. Frontend updates for multi-scenario download UI are deferred to a separate future phase.

### D6 — Schema coexistence (financial_outputs)

Both `deal.scenarios[]` and `deal.financial_outputs` are populated. The financial fan-out writes to scenarios first, then mirrors the preferred scenario's outputs back to `deal.financial_outputs` via a single utility function `mirror_preferred_to_legacy(deal)`.

Risk acknowledged: if future code accidentally writes to `deal.financial_outputs` directly, the two structures could drift. The `mirror_preferred_to_legacy(deal)` utility is the only sanctioned write path; new code should always read from `deal.scenarios[i].financial_outputs`.

### D7 — Excel filename format

Short format: `{address_slug}_S{rank}_{descriptor}.xlsx`

The descriptor is generated by the AI prompt as a 3-4 word snake_case tag, validated against regex `^[A-Za-z0-9_]+$`, with a hard 30-char cap on the descriptor portion.

Example for 967-73 N. 9th Street:
- `967-73_N_9th_St_S1_AsBuilt_Reno_21u.xlsx`
- `967-73_N_9th_St_S2_ByRight_Rebuild_6u.xlsx`
- `967-73_N_9th_St_S3_Variance_15u.xlsx`

PDF stays as `{address_slug}_Underwriting_Report.pdf`.

### D8 — Single-scenario rendering

Always new layout. Even single-scenario deals get the comparison table (with one row) and a per-scenario block. The cross-scenario recommendation collapses to "Single development pathway identified" with a one-paragraph rationale. Benefit: consistency across all reports.

### D9 — Prompt sequencing

Strictly sequential within Section 06's enrichment: `3C-CONF` → `3C-SCEN` → `3C-HBU`. Parallel execution is a Phase 2 optimization and out of scope for this build.

### D10 — Phasing model

5 Claude Code sessions, one per architectural layer, with explicit gate review between each. No combining sessions, no skipping gates.

---

## Session-by-session deliverable summary

### Session 1 — Schema (models.py)

**Goal:** Establish all new data structures the rest of the system will consume.

**Deliverables:**
- New Pydantic models: `ConformityAssessment`, `DevelopmentScenario`, `ZoningExtensions`
- New supporting models: `NonconformityItem`, `GrandfatheringStatus`, `OverlayImpact`, `DevelopmentUpside`, `UseAllocation`, `ZoningPathway`, `EntitlementRiskFlag`
- New enums: `ConformityStatus` (7 values), `ConfidenceLevel` (4 values), `ScenarioVerdict` (3 values), `ZoningPathwayType` (5 values), `NonconformityType` (6 values)
- Extensions to `DealData` model: `scenarios: List[DevelopmentScenario]`, `conformity_assessment: Optional[ConformityAssessment]`, `zoning_extensions: Optional[ZoningExtensions]`
- Utility function: `mirror_preferred_to_legacy(deal)` — reads from preferred scenario, writes to legacy `deal.financial_outputs`
- Pydantic validators: scenario_id format, descriptor 30-char cap, rank uniqueness within scenarios list

**Spec document:** `Session_1_Schema_Design.md` — must be read in full before any code is written.

**Gate criteria (all must pass):**
1. New `models.py` imports cleanly with no syntax or type errors
2. Sample fixture deserializes into new models without exception
3. `mirror_preferred_to_legacy(deal)` produces correct output on a 3-scenario test fixture
4. All Pydantic validators reject invalid inputs with clear error messages
5. No existing code that reads from `deal.financial_outputs` breaks (backward compatibility verified)
6. `git diff` shows changes ONLY in `models.py` (no scope creep into other files)

**Time estimate:** 1 session (3-5 hours of Claude Code work)

---

### Session 2 — Prompt design and approval (no code yet)

**Goal:** Design and approve three new prompts replacing the current single Prompt 3C.

**Deliverables:**
- Prompt drafts for `3C-CONF`, `3C-SCEN`, `3C-HBU`
- Each draft contains: system prompt, user template, output JSON schema, 2-3 input/output examples
- Test outputs against 3 reference deals manually (967-73 N. 9th St mixed-use; one clean conforming multifamily; one development/vacant land deal)
- Updated `FINAL_APPROVED_Prompt_Catalog_v5.md` with the three new prompts replacing v4 prompts 3C-* (catalog version bump from v4 to v5)

**Approval workflow (per project rules):**
1. Claude drafts all three prompts.
2. Mike reviews and either approves or requests revision.
3. Claude tests approved prompts against 3 reference deals (manual API calls, not pipeline integration).
4. Mike reviews test outputs.
5. Mike formally approves catalog v5.
6. ONLY THEN does Session 3 begin.

**Gate criteria (all must pass):**
1. Mike has formally approved all three prompts (catalog v5 is signed)
2. Each prompt has been tested against the 3 reference deals
3. All test outputs parse as valid JSON matching the schema
4. 967-73 N. 9th St test output flags `LEGAL_NONCONFORMING_DENSITY`
5. Clean conforming multifamily test output returns `CONFORMING` status
6. Vacant land test output handles the no-current-use case gracefully

**Time estimate:** 1 session for drafting + 1-3 days for review and testing cycle

---

### Session 3 — Pipeline orchestration (market.py)

**Goal:** Wire approved prompts into the live pipeline with confidence gating, retries, and logging.

**Deliverables:**
- `_assess_zoning_confidence(deal)` returning `ConfidenceLevel`
- Three new prompt-runner functions: `_run_zoning_conformity_prompt(deal)`, `_run_zoning_scenarios_prompt(deal)`, `_run_zoning_hbu_synthesis_prompt(deal)`
- Each prompt-runner has retry-on-parse-failure (max 2 retries) and falls back to typed empty result on persistent failure
- New orchestrator `_enrich_zoning_section_06(deal)` that calls confidence gate → conformity → scenarios → HBU in sequence
- Existing single-3C call site removed and replaced
- Per-prompt logging to `ctx` log: input, output, retries, parse failures
- Sequential execution only (per D9)

**Gate criteria (all must pass):**
1. Run on 967-73 N. 9th St → all three prompts execute
2. `deal.conformity_assessment` is populated with `LEGAL_NONCONFORMING_DENSITY` status
3. `deal.scenarios` has 1-3 entries, all valid Pydantic
4. `deal.zoning_extensions.cross_scenario_recommendation` is populated
5. Run on a deliberately-broken deal (zoning code missing) → conformity falls back to `INDETERMINATE` with explanation, pipeline does not crash
6. Run on a clean conforming deal → conformity returns `CONFORMING`, scenarios returns 1 entry, no padding
7. ctx log shows all three prompt calls with timing and token counts

**Time estimate:** 1 session

---

### Session 4 — Financial integration (financials.py + excel_builder.py)

**Goal:** Make scenarios drive the financial model fan-out and Excel file emission.

**Deliverables:**
- `financials.py`: `run_financials(deal)` loops scenarios, deep-copies assumptions, applies deltas
- For each scenario: applies physical config deltas (unit count, building SF, use mix), construction budget delta, rent delta %, timeline delta months
- Per-scenario `financial_outputs` written to `scenarios[i].financial_outputs`
- `mirror_preferred_to_legacy(deal)` called after fan-out completes
- `excel_builder.py`: emits one workbook per scenario to `outputs/{deal_id}/`
- Filename generator using D7 format with regex validation and 30-char descriptor cap
- Strategy-based template routing per scenario (`Hold_Template_v3.xlsx` for stabilized hold + value add; `Sale_Template_v3.xlsx` for opportunistic) — selected per scenario, not per deal
- `main.py` updated to return list of file paths (not single path)

**Gate criteria (all must pass):**
1. Run on 967-73 N. 9th St → 1-3 Excel files emit to `outputs/{deal_id}/`
2. All emitted Excel files open cleanly in Excel and LibreOffice
3. Each Excel file's Returns Summary tab shows IRR consistent with the scenario's assumptions
4. Filename format matches D7 pattern exactly; all special characters stripped from descriptor
5. `deal.financial_outputs` is populated and equals `deal.scenarios[preferred].financial_outputs`
6. PDF generation continues to work (consumes preferred scenario's outputs via legacy field)
7. Total deal runtime under 120 seconds (target: under 90 seconds)

**Time estimate:** 1 session

---

### Session 5 — Rendering (report_template.html + report.css)

**Goal:** Section 06 rebuild with conformity callout, comparison table, per-scenario blocks, cross-scenario recommendation.

**Deliverables:**
- Section 06 template replacement (full block, ~280 lines)
- New top callout: `conformity-status-callout` with status badge + explanation + diligence checklist
- New section: `use-flexibility-block` with 1-5 score visualization
- New section: `overlay-impact-block` (conditional on overlays existing)
- New section: `scenario-comparison-table` showing 1-3 scenarios side-by-side with key metrics + Excel filename references
- New section: `per-scenario-detail-block` (one per scenario)
- New section: `cross-scenario-recommendation` block
- CSS additions for status badges, comparison table, scenario detail cards, conformity callout variants
- Conditional rendering for INDETERMINATE conformity (different visual treatment)

**Gate criteria (all must pass):**
1. Run end-to-end on 967-73 N. 9th St → PDF renders with all new components visible
2. Conformity callout shows `LEGAL_NONCONFORMING_DENSITY` with red/amber visual treatment
3. Comparison table shows 1-3 rows with linked Excel filenames
4. Per-scenario detail blocks render in rank order
5. Cross-scenario recommendation paragraph is populated and substantive
6. Single-scenario test deal renders new layout with one-row comparison table
7. INDETERMINATE conformity test deal renders gracefully with diligence-needed callout
8. All rendering passes WeasyPrint without errors or warnings
9. PDF file size under 5 MB

**Time estimate:** 1 session

---

## Cross-cutting build standards

These apply to every session:

### Code quality

- All new code uses type hints
- All new public functions have docstrings
- No new external dependencies introduced (must work with existing `requirements.txt`)
- No deletion of files (retire by renaming with `_DEPRECATED_` prefix if needed)

### Testing

- Each session's gate criteria includes at least one test against the 967-73 N. 9th St reference deal
- Each session's gate criteria includes at least one negative test (deliberately bad input)
- Backward compatibility verified at each session — no existing report sections break

### Logging

- All new prompt calls log: input, output, retries, parse failures, latency, token counts
- All new pipeline functions log entry/exit at INFO level
- ctx log structure preserved (existing report sections should be able to introspect new ctx entries)

### Git discipline

- One commit per session minimum, with clear commit message referencing this plan
- Commit message format: `Session {N}: {layer name} — {summary}`
- Example: `Session 1: Schema (models.py) — Add ConformityAssessment, DevelopmentScenario, ZoningExtensions`
- Tag the repo at each gate pass: `zoning-overhaul-session-{N}-passed`

### Documentation

- This master plan document is updated at the end of every session
- New code in `models.py`, `market.py`, `financials.py`, `excel_builder.py`, etc. has inline comments referencing the relevant Session and design decision (e.g. `# Session 1, D6: backward-compat mirror field`)

---

## Reference deals for testing

Three deals are designated as the regression test set across all sessions:

### Deal A — 967-73 N. 9th Street, Philadelphia (LEGAL_NONCONFORMING_DENSITY)

The mixed-use 21-unit-over-2-commercial-tenants ICMX-zoned deal. Existing 21 units exceeds by-right cap of 6 units by 250%. Tests: nonconforming density assessment, grandfathering risk, multi-scenario generation (renovation vs. by-right rebuild vs. variance pathway).

### Deal B — Clean conforming multifamily (TBD address)

A vanilla buy-and-hold multifamily deal that is fully conforming under current zoning. Tests: prompt's ability to return single scenario without padding, CONFORMING status, clean rendering with one-row comparison table.

### Deal C — Vacant land or pre-development (TBD address)

A development site with no current building. Tests: scenario prompt's handling of greenfield deals, conformity assessment when there is no existing use to evaluate.

Mike will identify Deal B and Deal C addresses before Session 2 begins.

---

## Risks and mitigations

### R1 — Prompt quality variance across deals

**Risk:** A prompt that works on Deal A may fail on Deal B or C with very different characteristics.
**Mitigation:** All three reference deals tested before catalog v5 approval. Manual review of test outputs.

### R2 — Schema drift between scenarios and legacy financial_outputs

**Risk:** Future code accidentally writes to `deal.financial_outputs` directly, bypassing scenarios.
**Mitigation:** `mirror_preferred_to_legacy(deal)` is the only sanctioned write path. Inline comment in `models.py`. Code review checks for direct writes.

### R3 — Excel filename collisions

**Risk:** Two scenarios on the same deal generate identical descriptors.
**Mitigation:** Filename generator validates uniqueness across the scenario set; if collision detected, appends rank to descriptor (e.g. `_v2`, `_v3`).

### R4 — Runtime regression

**Risk:** Multi-scenario fan-out triples financial pipeline runtime; three prompts replace one in market.py.
**Mitigation:** Session 4 gate criterion includes runtime under 120 seconds total (90 seconds target). If exceeded, profile and optimize before passing gate.

### R5 — Session continuity loss

**Risk:** A new Claude instance starts a session without reading this document, makes inconsistent decisions.
**Mitigation:** This document explicitly says it must be read at session start. Mike's first message in any new session should reference this file by absolute path.

### R6 — Backward compatibility break

**Risk:** A session's changes break existing single-scenario deals or existing report sections.
**Mitigation:** Each session's gate criteria includes a backward compatibility check. Tag the repo at each gate pass for clean rollback if needed.

### R7 — Working tree drift between sessions

**Risk:** A session begins with uncommitted work in the tree from prior speculative work, causing scope-creep gate failures and schema collisions. Session 1 hit this and required a 1-hour surgical-reset detour before any planned work could begin.
**Mitigation:** Every session's opening protocol now includes "verify `git status` returns clean before reading the spec." If not clean, identify what's preservable, commit it in isolation, restore the rest, and confirm clean state before proceeding.

---

## Session History Log

*This section is appended to at the end of each session. Format: timestamp, session number, what shipped, what's open, gate verdict.*

### April 27, 2026 — Planning conversation (claude.ai chat)
- Mike Freedman and Claude completed the architectural design conversation
- All 10 design decisions (D1-D10) locked in
- Master plan document created
- Session 1 schema design document created
- Word checkpoint produced for records
- Next: Session 1 begins in Claude Code with `Session_1_Schema_Design.md` as the spec

### April 27, 2026 — Session 1 (Schema) — COMPLETED
- Implemented in `models\models.py` (package layout — spec referenced legacy `models.py` path)
- 5 enums added: `ConfidenceLevel`, `ConformityStatus` (7 values per D4), `NonconformityType`, `ScenarioVerdict`, `ZoningPathwayType`
- 7 sub-models added: `NonconformityItem`, `GrandfatheringStatus`, `ZoningPathway`, `EntitlementRiskFlag`, `UseAllocation`, `OverlayImpact`, `DevelopmentUpside`
- 3 top-level models added: `ConformityAssessment` (with status↔grandfathering consistency validator), `DevelopmentScenario` (with `scenario_id` format + 30-char cap + rank 1-3 validators), `ZoningExtensions` (with use_flexibility_score 1-5 validator)
- `DealData` extended with 3 optional fields (`conformity_assessment`, `scenarios`, `zoning_extensions`) plus `validate_scenarios_constraints` model_validator enforcing D2 cap, rank uniqueness, exactly-one-PREFERRED, PREFERRED-rank-1, and matching `preferred_scenario_id`
- `mirror_preferred_to_legacy(deal)` utility added — D6's sole sanctioned write path from scenarios to legacy `deal.financial_outputs`
- `model_rebuild()` calls added to resolve forward references
- Test fixture written: `tests/fixtures/zoning_overhaul_session_1_fixture.json` (967-73 N. 9th St, 3 scenarios, full nonconformity/grandfathering/cross-scenario detail)
- Pre-Session-1 housekeeping commit: prior speculative Phase 1/Phase 2/Phase 1.5a working-tree changes were discarded via `git restore` (they conflicted with Session 1's schema definitions and depended on the to-be-replaced 3C prompt). Step 4A Moody's MCP integration was preserved as a separate clean commit (`f73eabf`) prior to Session 1.
- Gate verdict: PASSED — 18/18 criteria green (the spec's 19 items collapse to 18 distinct assertions because gate 13 splits into 13a/13b for zero-PREFERRED and multiple-PREFERRED). Validator rejection paths verified for all bad-input cases. Backward compatibility verified via legacy-shaped DealData deserialization.
- Commit: `8a80f32` (`Session 1: Schema (models.py) — Add ConformityAssessment, DevelopmentScenario, ZoningExtensions`)
- Tag: `zoning-overhaul-session-1-passed`
- Open carry-forwards: spec referenced `models.py` at the project root, but the actual file is `models\models.py` (package layout). Session 2+ should expect this corrected path. Spec also referenced 8 sub-models in Section 1.1 prose but listed 7 in Section 2 (Section 2.8 is explicitly noted as "NOT a new model" — the cross-reference helper). Implemented the 7 actual sub-models.
- Next: Session 2 (Prompt design + approval). Blocked only on Mike's go-ahead to begin draft-and-test cycle for `3C-CONF`, `3C-SCEN`, `3C-HBU`.

### April 27, 2026 — Session 2 (Prompt Design) approved (claude.ai chat)
- Three replacement prompts designed and approved:
  - 3C-CONF (Conformity Assessment) — Sonnet
  - 3C-SCEN (Scenario Generation) — Sonnet
  - 3C-HBU (Cross-Scenario Synthesis) — Sonnet
- Old single Prompt 3C marked DEPRECATED
- Orchestrator function `run_zoning_synthesis_chain(deal)` specified
- Four-criterion confidence gate specified for 3C-CONF
- Four typed-empty fallback paths specified
- Reference Deals B (Belmont Apartments, RSD-3) and C (3520 Indian Queen Lane,
  split-zoned RSA-1/RSA-5 with American Tower easement) formally adopted as
  fixtures
- Two schema gaps surfaced and queued for Session 1.5 micro-session:
  WorkflowControls model and Encumbrance model
- Net incremental cost per deal: ~$0.073
- Deliverables produced:
  - `Session_2_Prompt_Specification.md` (working spec for Claude Code)
  - `DealDesk_Session_2_Prompt_Design_Checkpoint.docx` (formal Word checkpoint)
  - `FINAL_APPROVED_Prompt_Catalog_v5.md` (catalog v5 supersedes v4)
- Gate verdict: PASSED — all 11 Session 2 gate criteria satisfied
- Next: Session 1.5 micro-session in Claude Code (schema additions),
  then Session 3 (wire prompts into pipeline)

### April 28, 2026 — Session 1.5 (Schema micro-session) — COMPLETED
- Bridge session between Session 1 (schema foundation) and Session 3 (prompt wiring). Pure additive schema work surfaced by Session 2 prompt design.
- Implemented in `models\models.py` (Session 1 ZONING OVERHAUL section, additive only):
  - `EncumbranceType` enum added alphabetically between `ConformityStatus` and `NonconformityType` (6 values: EASEMENT, LEASE, LEASE_TO_EASEMENT, ROW, DEED_RESTRICTION, OTHER)
  - `Encumbrance` sub-model added after `DevelopmentUpside` (11 typed fields including `expiration: Optional[date]`)
  - `WorkflowControls` top-level model added after `ZoningExtensions`, before `mirror_preferred_to_legacy()` (3 fields: `single_scenario_mode`, `strategy_lock`, `max_scenarios` with `ge=1, le=3` bounds)
- `DealData` extended with two new fields appended to the existing "ZONING OVERHAUL — Session 1 additions" block: `workflow_controls: WorkflowControls` (default-constructed) and `encumbrances: List[Encumbrance]` (defaults to empty list)
- New top-level import: `from datetime import date` (required by `Encumbrance.expiration`)
- New regression fixture: `tests/fixtures/zoning_overhaul_session_1_5_fixture.json` — 3520 Indian Queen Lane with populated `workflow_controls` (single_scenario_mode=True, strategy_lock=value_add, max_scenarios=2) and 2 encumbrances (American Tower LEASE_TO_EASEMENT with $26,400/yr income + 2,625 SF exclusive area; Philadelphia stormwater EASEMENT)
- Carry-forward — lambda workaround for forward-referenced default factory: `Field(default_factory=WorkflowControls)` would `NameError` because `default_factory`'s right-hand side is evaluated eagerly at class-body time, *not* deferred by `from __future__ import annotations` (which only defers type annotations). Wrapped as `Field(default_factory=lambda: WorkflowControls())` so the name lookup happens at instance-construction time. Functionally identical from Pydantic's perspective. Inline comment in `models.py` documents the why so future readers don't "fix" it. Alternative would be to reorder the Session 1 ZONING OVERHAUL section so `WorkflowControls` is defined above `DealData` — declined as more risk than the one-line workaround.
- Gate verdict: PASSED — 8/8 criteria green:
  1. models.py imports cleanly with new symbols
  2. Session 1 fixture still deserializes; new `workflow_controls` and `encumbrances` fields default-engage
  3. New fixture `workflow_controls` deserializes (single_scenario_mode, strategy_lock, max_scenarios all round-trip)
  4. American Tower encumbrance deserializes with all 11 fields populated
  5. EncumbranceType + `date` round-trip through `model_dump_json()` / `model_validate_json()`
  6. `WorkflowControls.max_scenarios` bounds (ge=1, le=3) enforced — both 0 and 4 rejected
  7. Default `DealData()` instantiates with correct Session 1.5 defaults
  8. `git diff` scoped to `models/models.py` + new fixture only (gate script deleted before commit)
- Deliverables:
  - `models/models.py` — 91 line additions, 0 deletions
  - `tests/fixtures/zoning_overhaul_session_1_5_fixture.json` — 43 new lines
- Commit: `827149a` (`Session 1.5: Schema (models.py) — Add WorkflowControls + Encumbrance`)
- Tag: `zoning-overhaul-session-1-5-passed`
- Next: Session 3 (Pipeline orchestration in market.py) — wire `_SYSTEM_3C_CONF` / `_USER_3C_CONF` / `_apply_3c_conf` plus the 3C-SCEN and 3C-HBU equivalents, the `run_zoning_synthesis_chain` orchestrator, and the four-criterion confidence gate. Reads `Session_2_Prompt_Specification.md` and `FINAL_APPROVED_Prompt_Catalog_v5.md` as the implementation source of truth.

### April 28, 2026 — Session 1.6 (Schema realignment micro-session) — COMPLETED
- Bridge session inserted between Session 1.5 (schema foundation complete) and Session 3 (pipeline wiring, halted during read phase). Resolves 8 drift points + 1 cross-cutting Reference Deal A status contradiction surfaced during Session 3's reading phase.
- Drift root cause: Session 2 prompt JSON shapes did not match Session 1 Pydantic models. Session 3 would have required bridging logic in `_apply_3c_*` apply functions to translate prompt shapes into schema shapes — a design responsibility that belonged with Mike, not the agent. Session 3 was halted before any code was written; the drift report (`docs/Session_1_6_Drift_Report.md`) was produced as the input artifact, reviewed in claude.ai with locked design decisions per drift point, and Session 1.6 was inserted to land the realignment.
- Implemented in `models/models.py` (Session 1 ZONING OVERHAUL section; pure additive + rename, no destructive deletions):
  - `NonconformityType` enum: added FRONT_SETBACK / REAR_SETBACK / SIDE_SETBACK / OTHER (12 values total; SETBACKS retained as coarse fallback per Drift #1 Option B1)
  - `NonconformityItem`: renamed `existing_value` → `actual_value` (Drift #2); added `standard_description` (Drift #3); dropped `triggers_loss_of_grandfathering` — loss triggers now live only on `GrandfatheringStatus` (Drift #4 Option C4a)
  - `GrandfatheringStatus`: replaced 5-field documentation-tracking shape with 6-field presumption-tracking shape per Drift #5, with Mike's refinement dropping `is_documented` and `documentation_source` (almost no pre-WWII multifamily has documented grandfathering — modeling presumption + loss triggers is the more honest IC framing)
  - New enum `ProposedPathwayRequirement` (NONE / VARIANCE_REQUIRED / SPECIAL_EXCEPTION_REQUIRED / REZONE_REQUIRED) per Drift #6 — splits existing-condition `ConformityStatus` from proposed-plan entitlement requirement
  - `ConformityAssessment`: added `proposed_pathway_requirement: Optional[ProposedPathwayRequirement] = None`. None means "not assessed"; NONE means "assessed and no discretionary approval is required"
  - `UseAllocation`: renamed `square_feet` → `sf` (CRE convention, matches rest of codebase); added `share_pct` (Drift #7)
  - `ZoningPathway`: added `rationale` (Drift #7)
- Migrated `tests/fixtures/zoning_overhaul_session_1_fixture.json` in place: `existing_value` → `actual_value` (value preserved verbatim); `GrandfatheringStatus` reshape (`is_documented:false` → `is_presumed_grandfathered:true` semantic mapping; `presumption_basis` → `basis` with text preserved verbatim; `confirmation_action_required` and `risk_if_denied` preserved verbatim; added `loss_triggers` — the 3 items moved from the dropped `triggers_loss_of_grandfathering` array; added `verification_required: true`); 6× `square_feet` → `sf` renames with values unchanged. **`status: "LEGAL_NONCONFORMING_DENSITY"` preserved exactly** (master plan precedence over Session 2 spec on the cross-cutting Reference Deal A status contradiction).
- Added `tests/fixtures/zoning_overhaul_session_1_6_fixture.json`: Belmont-style RSD-3 property exercising all 8 realignment surfaces (4 nonconformity_details including a FRONT_SETBACK directional and a coarse SETBACKS, 6-field GrandfatheringStatus, `proposed_pathway_requirement: VARIANCE_REQUIRED`, UseAllocation with `sf` and `share_pct` populated, ZoningPathway with `rationale` populated, both PREFERRED and ALTERNATE scenarios). Distinct from the Belmont fixture Session 3 will produce — this one is for schema-realignment testing, not prompt regression.
- Updated `docs/Session_2_Prompt_Specification.md`: §2.3 (CONFORMITY STATUS list realigned, added PROPOSED PATHWAY REQUIREMENT block), §2.4 (JSON schema with new status enum + proposed_pathway_requirement field + expanded nonconformity_type enum + new GrandfatheringStatus shape), §2.7 (Deal A status `LEGAL_NONCONFORMING_DIMENSIONAL` → `LEGAL_NONCONFORMING_DENSITY`; proposed_pathway_requirement notes for all three deals), §3.3 (added rule #9 with delta-anchoring instruction), §3.4 (flattened physical_config and assumption_deltas to top level, renamed prompt fields per new schema, added IC-grade fields key_risks / approval_body / fallback_if_denied / risk_summary / diligence_required), §4.4 (flattened use_flexibility to two top-level fields, restructured overlay_impact_assessment as a structured list per OverlayImpact shape), §4.5 (mapping table updated for flat use_flexibility + structured overlay list).
- Updated `docs/FINAL_APPROVED_Prompt_Catalog_v5.md`: §1 (3C-CONF system text + JSON schema, mirroring Session 2 spec changes), §4 (Deal A status `LEGAL_NONCONFORMING_DIMENSIONAL` → `LEGAL_NONCONFORMING_DENSITY`; proposed_pathway_requirement notes for all three deals).
- Tracked `docs/Session_1_6_Drift_Report.md` (input artifact, was untracked since Session 3 read) and `docs/Session_1_6_Claude_Code_Kickoff.md` (kickoff sheet) into the docs commit for audit-trail completeness — same pattern as Session 2's docs commit, which tracked the Session 2 design checkpoint.
- Gate verdict: PASSED — 13/13 criteria green:
  1. models.py imports cleanly with new symbol `ProposedPathwayRequirement`
  2. NonconformityType has 12 values incl. directional setbacks + SETBACKS coarse + OTHER
  3. NonconformityItem rejects construction with old `existing_value` field name (ValidationError on missing required `actual_value`)
  4. NonconformityItem accepts new `actual_value` field
  5. GrandfatheringStatus rejects old shape (ValidationError on missing required `is_presumed_grandfathered` and `basis`)
  6. ProposedPathwayRequirement enum has 4 values + JSON round-trip
  7. ConformityAssessment.proposed_pathway_requirement defaults to None + accepts enum values
  8. UseAllocation.sf works; old square_feet raises ValidationError
  9. ZoningPathway.rationale accepts string + defaults to None
  10. Migrated Session 1 fixture round-trips with `LEGAL_NONCONFORMING_DENSITY` preserved
  11. Session 1.5 fixture round-trips with NO edits required (regression check — Session 1.6 must not break 1.5)
  12. Session 1.6 fixture round-trips + exercises all 8 realignment surfaces
  13. `git status` scope: only `models/models.py` + 2 fixture files in code commit (drift report + kickoff sheet excluded from code commit; tracked in docs commit)
- Code commit: `ad849cd` (`Session 1.6: Schema realignment — Resolve drift between Session 1 schema and Session 2 prompts`)
- Tag: `zoning-overhaul-session-1-6-passed`
- Next: Session 3 resumes from its existing plan summary unchanged. The bridging logic the agent would have invented in `_apply_3c_*` functions becomes unnecessary — apply functions are now straightforward Pydantic constructions with no field translation. The Session 3 kickoff sheet at `docs/Session_3_Claude_Code_Kickoff.md` remains the operative instruction set.

---

## Glossary

- **Scenario** — A meaningfully different business plan for the subject property. See D1.
- **Conformity** — The state of compliance between an existing or proposed use/configuration and current zoning. See D4.
- **Confidence gate** — A pre-check that determines whether enough zoning data exists to run conformity analysis. See D4.
- **Mirror function** — `mirror_preferred_to_legacy(deal)`, the utility that copies the preferred scenario's financial outputs back to the legacy `deal.financial_outputs` field for backward compatibility. See D6.
- **Catalog v5** — The next version of `FINAL_APPROVED_Prompt_Catalog.md` after Session 2 approves the three new zoning prompts. Replaces v4 prompts 3C-*.
- **Reference deals** — A, B, and C. The fixed regression test set used across all 5 sessions.

---

*End of master plan document. Next file to read: `Session_1_Schema_Design.md`*
