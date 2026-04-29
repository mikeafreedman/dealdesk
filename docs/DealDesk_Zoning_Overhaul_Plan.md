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
| 3 | Pipeline orchestration (market.py) | COMPLETED (commit `62c76e4`, tag `zoning-overhaul-session-3-passed`) | PASSED (13/13 criteria) | 2026-04-28 | 2026-04-28 |
| 4 | Financial integration (financials.py + excel_builder.py) | COMPLETED (commit `fc39204`, tag `zoning-overhaul-session-4-passed`) | PASSED (12/12 criteria) | 2026-04-29 | 2026-04-29 |
| 5 | Rendering (report_template.html + report.css) | READY (awaiting Mike approval to begin; kickoff sheet pending) | Not yet evaluated | — | — |

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

### April 28, 2026 — Session 3 (Pipeline orchestration in market.py) — COMPLETED
- Implemented the three-prompt zoning synthesis chain in `market.py`, replacing the legacy single Prompt 3C with sequential `3C-CONF` → `3C-SCEN` → `3C-HBU` orchestration per D9. Pure pipeline-wiring session — no schema changes; all field shapes consumed exactly as Session 1.6 realigned them. Spec sources of truth: `docs/Session_2_Prompt_Specification.md` (Sections 2/3/4 for prompt text, Section 5 for orchestration logic) and `docs/FINAL_APPROVED_Prompt_Catalog_v5.md`.
- Total diff: +1,244 / −68 across `market.py` (+1,191 / −68) and two new fixture files (+49 Belmont, +72 Indian Queen).
- Shipped across four checkpoints (CP1 — confidence gate + 3C-CONF; CP2 — 3C-SCEN; CP3 — 3C-HBU; CP4 — orchestrator + integration into `enrich_market_data`):
  - CP1 deliverables: `_confidence_gate_passes(deal)` implementing the four-criterion check from spec §5 (zoning_code populated, ≥3 permitted_uses, ≥4 of 6 dimensional fields populated, lot_sf populated and >0); `_SYSTEM_3C_CONF` and `_USER_3C_CONF` prompt constants (verbatim from Session 2 spec §§2.3/2.4); `_build_3c_conf_user(deal)` user-template builder; `_apply_3c_conf(data, deal)` apply function writing to `deal.conformity_assessment`; `_indeterminate_conformity_assessment(deal)` typed-empty fallback constructor; `_call_llm_with_retry(...)` retry helper (one retry on parse failure, raises on persistent failure — does NOT return None).
  - CP2 deliverables: `_SYSTEM_3C_SCEN` and `_USER_3C_SCEN` prompt constants (verbatim from Session 2 spec §§3.3/3.4); `_build_3c_scen_user(deal)` builder; `_apply_3c_scen(data, deal)` writing to `deal.scenarios`; `_fallback_as_submitted_scenario(deal)` typed-empty fallback constructor.
  - CP3 deliverables: `_SYSTEM_3C_HBU` and `_USER_3C_HBU` prompt constants (verbatim from Session 2 spec §§4.3/4.4); `_build_3c_hbu_user(deal)` builder; `_apply_3c_hbu(data, deal)` writing to `deal.zoning_extensions`; `_minimal_zoning_extensions(deal)` typed-empty fallback constructor.
  - CP4 deliverables: `run_zoning_synthesis_chain(deal)` orchestrator (sequential per D9, isolated retry per prompt — a parse failure on 3C-CONF does not abort 3C-SCEN/3C-HBU, typed-empty fallback per prompt); legacy `_SYSTEM_3C`, `_USER_3C`, `_apply_3c` removed (cleanly — no `_DEPRECATED_` rename needed since the replacement is functionally complete); `enrich_market_data()` updated to call `run_zoning_synthesis_chain(deal)` in place of the legacy `_call_llm(MODEL_SONNET, _SYSTEM_3C, ...)` site.
- New regression fixtures (CP4): `tests/fixtures/zoning_overhaul_session_3_fixture_belmont.json` (Reference Deal B — RSD-3 conforming multifamily) and `tests/fixtures/zoning_overhaul_session_3_fixture_indian_queen.json` (Reference Deal C — split-zoned RSA-1/RSA-5 with American Tower easement and Philadelphia stormwater easement, exercising `is_split_zoned` + 2 `Encumbrance` entries).
- **Carry-forwards (six items surfaced across CP1–CP3 — must not get lost):**
  - **Docs realignment (post-Session-3 patch):**
    1. Catalog v5 §1 SCEN/HBU JSON schema re-sync — pre-Session-1.6 drift; spec was realigned but catalog wasn't fully re-synced for SCEN and HBU. Identified during Session 3's reading phase.
    2. Spec §5.4 fallback-shape re-sync — `_minimal_zoning_extensions` shape in spec §5.4 still references nested `use_flexibility_score.score` and string `overlay_impact_assessment`. CP2 used `score=1` + empty overlay list as defensible fallback; spec needs re-sync to flat shape.
    3. Spec §5.1 orchestrator pseudocode realignment — success-log line for 3C-HBU references nested `use_flexibility_score.score`; CP3 used flat `use_flexibility_score: int` with inline NOTE comment; spec needs realignment.
  - **Session 1.8 schema additions (post-Session-3 micro-session):**
    4. `is_split_zoned: bool = False` and `split_zoning_codes: List[str] = Field(default_factory=list)` on the `Zoning` sub-model. CP1 surfaced that the defensive `getattr` reads always return defaults due to Pydantic v2's default behavior; the prompts always receive `is_split_zoned=False`.
    5. `investment_strategy: Optional[InvestmentStrategy]` on `DevelopmentScenario` for server-side validation against `workflow_controls.strategy_lock`. CP2 surfaced that the prompt's per-scenario `investment_strategy` field is silently dropped by Pydantic v2's `extra='ignore'`; LLM compliance with `strategy_lock` is not validated server-side.
  - **Optimization (deferred to Session 4 or beyond):**
    6. Gate-failure short-circuit to skip SCEN/HBU LLM calls when the confidence gate fails, writing fallbacks directly. Current behavior runs the LLM with essentially-empty zoning data and gets back guesses. CP3 surfaced this; current behavior matches spec.
- Gate verdict: PASSED — 13/13 criteria green:
  1. `market.py` imports cleanly with no syntax or type errors
  2. All six new prompt constants exist as module-level strings and are non-empty
  3. All three user-prompt builder functions exist, accept a `DealData`, and return a non-empty string
  4. All three apply functions exist, accept `(data: dict, deal: DealData)`, and successfully populate the target field on a synthetic test deal
  5. `_call_llm_with_retry` raises (does not return None) when the underlying call returns None twice
  6. `_confidence_gate_passes` returns False when zoning_code is missing; returns False when fewer than 3 permitted_uses; returns False when fewer than 4 dimensional fields are populated; returns False when lot_sf is None or 0; returns True when all four criteria pass
  7. All three typed-empty fallback constructors return fully-typed Pydantic models that round-trip through `model_dump_json()` / `model_validate_json()`
  8. `run_zoning_synthesis_chain(deal)` does NOT raise even when the LLM returns None for all three prompts (fallback path runs cleanly)
  9. Belmont fixture (Deal B) deserializes and includes a `RSD-3` zoning code
  10. Indian Queen fixture (Deal C) deserializes and includes both encumbrances + the split-zoning indicator
  11. Legacy `_SYSTEM_3C`, `_USER_3C`, `_apply_3c` are no longer present in `market.py`
  12. `enrich_market_data()` calls `run_zoning_synthesis_chain(deal)` (not the old `_call_llm(MODEL_SONNET, _SYSTEM_3C, ...)`)
  13. `git diff --stat` shows changes ONLY in `market.py` and the two new fixture files (no scope creep)
- Code commit: `62c76e4` (`Session 3: market.py — Replace Prompt 3C with three-prompt synthesis chain`)
- Tag: `zoning-overhaul-session-3-passed`
- Next: Session 4 (Financial integration in `financials.py` + `excel_builder.py`) — fan out the existing pro forma / Excel builder logic across `DealData.scenarios[]`, write per-scenario `financial_outputs` back to each `DevelopmentScenario`, and update `mirror_preferred_to_legacy()` to copy the preferred scenario's financials to legacy `deal.financial_outputs`. Per Session 3 kickoff Step 4: do NOT auto-proceed into Session 4 — Mike will prepare the Session 4 kickoff sheet from claude.ai when ready.

### April 28, 2026 — Post-Session-3 docs realignment
- Pure docs patch — no code changes, no schema changes — closing carry-forwards #1, #2, #3 from the Session 3 history-log entry above.
- Catalog v5 §1 SCEN/HBU JSON schema blocks re-synced character-for-character to the canonical post-Session-1.6 shapes in `docs/Session_2_Prompt_Specification.md` §§3.4 and 4.4. Eight additional drift sites surfaced during realignment verification (catalog HBU mapping table, catalog §2.1 orchestrator pseudocode, catalog §2.4 fallback row, catalog §4 and spec §4.7 reference-deal HBU expectations) all realigned in the same patch — closing carry-forward #1 fully across both documents rather than at the JSON-schema layer only.
- Spec §5.4 `_minimal_zoning_extensions` fallback row re-synced to the actual implementation in `market.py:_minimal_zoning_extensions` (`use_flexibility_score=1` sentinel + empty overlay list, matching CP2's fail-toward-manual-review reasoning) (carry-forward #2).
- Spec §5.1 orchestrator pseudocode HBU success-log realigned from `deal.zoning_extensions.use_flexibility_score.score` (old nested shape) to `deal.zoning_extensions.use_flexibility_score` (flat int per Session 1.6 realignment) (carry-forward #3).
- Carry-forwards #4 and #5 (Session 1.8 schema additions) and #6 (gate-failure short-circuit optimization) remain OPEN and DEFERRED per Session 3's carry-forward groupings.
- Commit: `7dd732f` — no tag (consistent with prior docs commits).

### April 29, 2026 — Session 4 (Per-scenario fan-out) — COMPLETED
- Implemented per-scenario fan-out across `financials.py` and `excel_builder.py`, replacing the single-deal computation/Excel-emission pipeline with per-`DevelopmentScenario` workers + thin orchestrators that loop `deal.scenarios`. Session 4's primary architectural shift: scenarios become first-class drivers of the financial model, with `mirror_preferred_to_legacy(deal)` as the sole sanctioned write path back to legacy `deal.financial_outputs` (per D6). Plus carry-forward #6 (gate-failure short-circuit) closed in `market.py`. Spec source of truth: `docs/Session_4_Claude_Code_Kickoff.md`.
- Total diff: +502 / −49 across 5 files — `models/models.py` (CP1 mirror semantics rewrite + CP2 `rent_multiplier` field addition), `financials.py` (CP2 fan-out: orchestrator + per-scenario worker + delta helper + sync helper + GPR multiplier wiring + line 2851 narratives-write removal), `excel_builder.py` (CP3 fan-out: orchestrator + per-scenario worker + master index file builder + line 194 `output_xlsx_path`-write removal), `market.py` (CP4 gate-failure short-circuit, surgical change to one branch in `run_zoning_synthesis_chain`), and one new fixture `tests/fixtures/zoning_overhaul_session_4_fixture_gate_fail.json`.
- Shipped across four checkpoints (CP1 — `mirror_preferred_to_legacy` semantics rewrite; CP2 — `financials.py` fan-out; CP3 — `excel_builder.py` fan-out; CP4 — `market.py` gate-failure short-circuit + integration test):
  - **CP1 deliverables:** Rewrote `mirror_preferred_to_legacy(deal)` from raise-on-error to log-WARNING-and-skip semantics across all three failure conditions (empty `deal.scenarios`, no scenario with `verdict=PREFERRED`, preferred scenario's `financial_outputs is None`). Schema fields `excel_filename: Optional[str]` and `financial_outputs: Optional[FinancialOutputs]` on `DevelopmentScenario` confirmed already present from Session 1; CP1 was narrower than the kickoff implied. **13/13 per-CP gate.**
  - **CP2 deliverables:** Extracted `_compute_full_financials(deal, assumptions)` from the body of legacy `run_financials`. Added `_run_financials_for_scenario(deal, scenario)` (pure-function per-scenario worker that deep-copies `deal.assumptions`, applies the scenario's deltas, and computes a `FinancialOutputs`), `_apply_scenario_deltas_to_assumptions(snapshot, scenario)` (delta helper applying `construction_budget_delta_usd`, `rent_delta_pct`, `timeline_delta_months`, plus physical-config fields), and `_sync_narratives_from_fo(deal)` (orchestrator-level helper that writes the preferred scenario's narrative back to `deal.narratives.monte_carlo_narrative`). Reduced `run_financials(deal)` to an orchestrator that loops `deal.scenarios`, writes per-scenario `financial_outputs`, calls `mirror_preferred_to_legacy(deal)`, then calls `_sync_narratives_from_fo(deal)`. Legacy single-deal fallthrough preserved for `deal.scenarios == []`. **15/15 per-CP gate** including byte-identical backward-compat anchor (G6) and baseline non-mutation check (G7).
  - **CP3 deliverables:** Extracted `_do_populate_excel(deal, assumptions, output_path)` from the body of legacy `populate_excel`. Added `_populate_excel_for_scenario(deal, scenario)` (per-scenario worker; reuses `_apply_scenario_deltas_to_assumptions` from `financials.py` via lazy import to avoid a circular dependency) and `_build_scenarios_index(deal, output_dir)` (master index file builder writing `{deal_id}_scenarios_index.xlsx` with one row per scenario covering scenario_id / verdict / unit_count / total_project_cost / NOI yr1 / project IRR / LP IRR / LP equity multiple / exit value / excel_filename). Reduced `populate_excel(deal)` to an orchestrator producing per-scenario Excel files (filename format `{deal_id}_{scenario_id}_financial_model.xlsx` per Decision A) plus the master index file. **13/13 per-CP gate** including LibreOffice headless recalc verification (G10).
  - **CP4 deliverables:** Modified `run_zoning_synthesis_chain(deal)` gate-failure branch to short-circuit SCEN and HBU LLM calls and write all three fallbacks (`_indeterminate_conformity_assessment`, `[_fallback_as_submitted_scenario]`, `_minimal_zoning_extensions`) directly. New gate-fail fixture `tests/fixtures/zoning_overhaul_session_4_fixture_gate_fail.json` (Belmont with `zoning_code = ""` and `permitted_uses = []`) exercises the path end-to-end through `run_financials` and `populate_excel`. **12/12 per-CP gate** (also the final session gate) including the architectural-payoff assertion (gate-pass attempts 3 LLM calls; gate-fail attempts 0).
- **Two discovery-driven course corrections — captured because these halts are why the discipline pattern exists:**
  1. **Narratives shared-object leak (CP2).** During CP2 extraction, the discovery scan surfaced a deal-level mutation in the body to extract: `deal.narratives.monte_carlo_narrative = narrative` at line 2851 of legacy `run_financials`. Because `scenario_deal = deal.model_copy(deep=False)` shallow-shares the `narratives` object with the parent deal, this write would clobber a shared field across scenarios in fan-out — last-scenario-wins regression. **Resolved with Option A:** stripped the in-body write from the extracted `_compute_full_financials` body, added `_sync_narratives_from_fo(deal)` orchestrator-level helper called on both the multi-scenario and legacy single-deal fallback branches. Preserves byte-identical legacy behavior for the empty-scenarios path; the multi-scenario path now correctly reflects the **preferred scenario's** narrative.
  2. **Kickoff Decision E delta-mapping error (CP2).** The kickoff sheet's Decision E table prescribed `snapshot.monthly_rent *= (1 + delta)` for `rent_delta_pct`, but `monthly_rent` does not exist on `FinancialAssumptions` — it was removed in a prior refactor (per the inline comment at `financials.py:386`). Caught when the gate script's test-fixture builder attempted `a.monthly_rent = 1500.0` and raised `ValidationError: extra fields not permitted`. **Resolved with Option β:** added `rent_multiplier: float = 1.0` field to `FinancialAssumptions`, wired through every non-zero return path of `_gpr_yr1` as a final multiplier on Gross Potential Rent, set in `_apply_scenario_deltas_to_assumptions` via assignment (not multiplication) since the snapshot is freshly deep-copied from a baseline where `rent_multiplier == 1.0`. G14 gate assertion verifies a 10% rent delta produces a ~1.165× NOI lift.
- **Carry-forward inventory — status update post-Session 4:**
  - Closed by Session 4:
    - ✅ #6 — Gate-failure short-circuit. Closed in CP4. Verified by gate-pass attempts 3 LLM calls; gate-fail attempts 0 (architectural-payoff assertion in the CP4 gate script).
  - Surfaced and resolved in-session (no follow-up needed):
    - ✅ Discovery: narratives shared-object leak. Resolved with `_sync_narratives_from_fo` helper in CP2.
    - ✅ Discovery: kickoff Decision E `monthly_rent` field gap. Resolved with `rent_multiplier` field addition to `FinancialAssumptions` in CP2.
  - Surfaced during Session 4, deferred to a small post-Session-4 docs realignment commit (mirrors the post-Session-3 docs realignment pattern):
    - 🔧 Master plan D7 filename format `{address_slug}_S{rank}_{descriptor}.xlsx` superseded by Session 4 kickoff Decision A format `{deal_id}_{scenario_id}_financial_model.xlsx`. D7 description in the master plan needs realignment.
    - 🔧 Session 4 kickoff §1 Decision E table contained an incorrect prescription (`snapshot.monthly_rent *= ...` against a nonexistent field). Kickoff text needs correction with a note that the actual implementation uses `rent_multiplier`.
  - Remaining open and tracked (Session 1.8 schema additions, deferred):
    - ⏸️ #4 — `is_split_zoned: bool = False` and `split_zoning_codes: List[str] = Field(default_factory=list)` on the `Zoning` sub-model. CP1 of Session 3 surfaced; defensive `getattr` reads always return defaults due to Pydantic v2 behavior.
    - ⏸️ #5 — `investment_strategy: Optional[InvestmentStrategy]` on `DevelopmentScenario` for server-side validation against `workflow_controls.strategy_lock`. CP2 of Session 3 surfaced; LLM compliance with `strategy_lock` not validated server-side.
- Gate verdict: PASSED — 12/12 criteria green (final CP4 gate, which is the gate that gates the entire session):
  1. `from models import mirror_preferred_to_legacy` succeeds; `DevelopmentScenario.model_fields` contains both `excel_filename` and `financial_outputs` keys (CP1)
  2. `mirror_preferred_to_legacy(deal)` round-trips a synthetic deal with a preferred scenario carrying mock `FinancialOutputs` into populated `deal.financial_outputs`; logs WARNING and returns cleanly on each of the three failure conditions (CP1)
  3. `_run_financials_for_scenario(deal, scenario)` runs to completion on the `as_submitted` fallback scenario without raising (CP2)
  4. `run_financials(deal)` fans out across N scenarios producing per-scenario `financial_outputs`, then calls `mirror_preferred_to_legacy(deal)` so `deal.financial_outputs` reflects the preferred scenario's outputs (CP2)
  5. `run_financials(deal)` legacy fallthrough on `deal.scenarios == []` produces `deal.financial_outputs.noi_yr1` byte-identical (within float tolerance) to the pre-fan-out baseline (CP2 — G6)
  6. Baseline `deal.assumptions` is non-mutated by the fan-out (per-scenario workers operate on deep-copied snapshots) (CP2 — G7)
  7. `rent_multiplier` payoff: a 10% `rent_delta_pct` on the as_submitted scenario produces a ~1.165× NOI lift relative to baseline (CP2 — G14)
  8. `populate_excel(deal)` produces N per-scenario `.xlsx` files plus 1 master `{deal_id}_scenarios_index.xlsx` for a deal with N scenarios; index has N+1 rows (header + N data rows) ordered by scenario rank (CP3)
  9. `populate_excel(deal)` returns the path of the **preferred scenario's** Excel file, not the index file (CP3)
  10. LibreOffice headless recalc succeeds on at least one per-scenario file (CP3 — G10)
  11. Architectural-payoff: gate-pass Belmont fixture → `run_zoning_synthesis_chain` attempts 3 LLM calls (CONF, SCEN, HBU); gate-fail fixture → attempts 0 LLM calls (short-circuit fires before any try/except block) (CP4)
  12. End-to-end integration: gate-fail fixture → orchestrator short-circuits, `run_financials(deal)` produces single-scenario output via the `as_submitted` fallback, `populate_excel(deal)` builds 1 per-scenario file plus a 1-data-row index file (CP4)
- Code commit: `fc39204` (`Session 4: financials + excel_builder — Per-scenario fan-out + legacy mirror`)
- Tag: `zoning-overhaul-session-4-passed`
- Next: Session 5 (Rendering — `report_template.html` + `report.css`). Per Session 4 kickoff: do NOT auto-proceed into Session 5 — Mike will prepare the Session 5 kickoff sheet from claude.ai when ready. The two docs-realignment carry-forwards (D7 filename format + kickoff Decision E correction) are deferred to a small post-Session-4 docs realignment commit, mirroring the post-Session-3 docs realignment pattern.

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
