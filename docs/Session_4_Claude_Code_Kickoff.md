# Session 4 Claude Code Kickoff — Financial Integration

## 0. Purpose & scope

Session 4 transforms the financial pipeline from a single-deal computation into a
per-scenario fan-out, then mirrors the preferred scenario's outputs back to
`deal.financial_outputs` for backward compatibility with all downstream consumers
(report rendering, charting, DD flag engine, KPI validator).

Three primary file targets:

1. **`financials.py`** — `run_financials(deal)` becomes a thin orchestrator that
   loops `deal.scenarios` and calls a new per-scenario worker. The existing
   computation logic gets extracted into `_run_financials_for_scenario(deal, scenario)`
   that reads baseline from `deal.assumptions` and applies the scenario's deltas
   (`construction_budget_delta_usd`, `rent_delta_pct`, `timeline_delta_months`,
   `unit_count`, `building_sf`, `use_mix`).
2. **`excel_builder.py`** — `populate_excel(deal)` becomes a thin orchestrator
   that loops `deal.scenarios` and calls a new `_populate_excel_for_scenario`.
   Each scenario gets its own `.xlsx` file. A master index file aggregates
   key metrics across scenarios.
3. **`models.py`** — small additions: (a) `mirror_preferred_to_legacy(deal)`
   helper function; (b) `excel_filename: Optional[str]` field on
   `DevelopmentScenario`; (c) `financial_outputs: Optional[FinancialOutputs]`
   field on `DevelopmentScenario` (if not already present).

Plus carry-forward #6 (gate-failure short-circuit): when the confidence gate
fails in the orchestrator, skip SCEN and HBU LLM calls entirely and write the
fallback scenario directly. This change touches `market.py:run_zoning_synthesis_chain`
only — minimal surface, isolated to the orchestrator's gate-fail branch.

**Anti-scope-creep guardrails — do NOT touch in this session:**
- `report_builder.py`, `report_template.html`, `report.css` (Session 5 territory)
- `chart_builder.py`, `context_builder.py`, `dd_flag_engine.py`, `kpi_validator.py`
  (downstream consumers — must continue to work via the legacy mirror)
- `extractor.py`, `deal_data.py`, `parcel_fetcher.py` (input pipeline — unchanged)
- `auth.py`, `auth_config.py`, `main.py` pipeline order (one small touch in
  `main.py` is acceptable: after `run_financials(deal)`, the call
  `mirror_preferred_to_legacy(deal)` may need to be added — see Edit 5 below)
- Frontend `fp_underwriting_FINAL_v7.html` (no UI changes)
- The 3C synthesis chain logic itself (Session 3 territory — only the
  gate-fail short-circuit is in scope)

The session is approved for: financials.py, excel_builder.py, models.py
(narrow), market.py (narrow — gate short-circuit only), main.py (one-liner),
plus new test fixtures in tests/fixtures/.

---

## 1. Architectural decisions (locked in by Mike before kickoff)

### Decision A — Excel output structure: per-scenario files

Each scenario produces its own `.xlsx`. Naming convention:

```
{deal_id}_{scenario_id}_financial_model.xlsx
```

Examples for a Belmont deal with 3 scenarios:
- `belmont_as_built_renovation_financial_model.xlsx`
- `belmont_courtyard_95u_financial_model.xlsx`
- `belmont_townhomes_88u_financial_model.xlsx`

Plus one master index file:
```
{deal_id}_scenarios_index.xlsx
```

The index file contains a single sheet named "Scenarios Comparison" with one
row per scenario and columns for: scenario_id, scenario_name, verdict (PREFERRED
/ ALTERNATE / etc.), unit_count, total_project_cost, year-1 NOI, project IRR,
LP IRR, LP equity multiple, exit value, and excel_filename (for cross-reference).

The legacy `deal.output_xlsx_path` (which the report_builder reads) is set to
the **preferred scenario's Excel file**, not the index. Rationale: Session 5's
report renderer was designed to consume a single financial model, and the
preferred scenario IS the deal's primary recommendation. The index file is a
supplementary deliverable for users who want side-by-side comparison.

### Decision B — Carry-forward #6 (gate-failure short-circuit) IN SCOPE

In `market.py:run_zoning_synthesis_chain`, when `_confidence_gate_passes(deal)`
returns False, the orchestrator currently:
- Writes INDETERMINATE conformity assessment
- Continues to call SCEN with empty zoning data (LLM produces guesses)
- Continues to call HBU with empty zoning data (LLM produces guesses)
- Falls back to typed-empty constructors when those calls fail

After the short-circuit, the gate-fail path becomes:
- Write INDETERMINATE conformity assessment
- Skip the SCEN LLM call entirely; write `_fallback_as_submitted_scenario(deal)` directly
- Skip the HBU LLM call entirely; write `_minimal_zoning_extensions(deal)` directly
- Log clearly that scenarios and HBU were short-circuited due to gate failure

Cost savings: 2 LLM calls per gate-failed deal (~$0.05–0.10 in Sonnet pricing
plus 5–10 seconds latency from the retry timeouts).

Correctness: same fallback content gets written either way, but now without
the LLM noise of generating "scenarios" against essentially-empty zoning.

### Decision C — Two-dimensional scenario fan-out

The existing `financials.py` already loops `SCENARIO_DELTAS` (Base / Upside /
Downside macro sensitivities) and stores per-macro-scenario results in
`fo.scenario_results`. **This existing system is preserved unchanged.**

After Session 4, the fan-out is:
- N zoning scenarios from `deal.scenarios` (typically 1–4)
- × 3 macro sensitivity scenarios per zoning scenario (Base/Upside/Downside)
- = up to 3N `ScenarioResult` entries total

The macro scenarios stay nested inside each zoning scenario's
`financial_outputs.scenario_results`. The zoning scenarios surface at the deal
level via `deal.scenarios[i].financial_outputs`.

### Decision D — Per-scenario function signatures

```python
# In financials.py:
def run_financials(deal: DealData) -> DealData:
    """Orchestrator: loops deal.scenarios, calls _run_financials_for_scenario
    per scenario, writes per-scenario financial_outputs."""

def _run_financials_for_scenario(deal: DealData, scenario: DevelopmentScenario) -> FinancialOutputs:
    """Worker: applies scenario deltas to baseline assumptions, runs the full
    financial pipeline (sources/uses, pro forma, exit, sensitivity, MC,
    macro scenarios, etc.), returns a fully-populated FinancialOutputs.
    Pure function — does NOT mutate deal."""

# In excel_builder.py:
def populate_excel(deal: DealData) -> Path:
    """Orchestrator: loops deal.scenarios, calls _populate_excel_for_scenario
    per scenario, builds the master index file. Returns the path of the
    PREFERRED scenario's Excel file (for legacy backward compat)."""

def _populate_excel_for_scenario(deal: DealData, scenario: DevelopmentScenario,
                                  output_path: Path) -> Path:
    """Worker: copies the strategy template, populates with the scenario's
    financial_outputs, runs LibreOffice recalc, returns output_path."""

def _build_scenarios_index(deal: DealData, scenario_files: List[Tuple[str, Path]]) -> Path:
    """Helper: builds the master Scenarios Comparison index xlsx."""

# In models.py:
def mirror_preferred_to_legacy(deal: DealData) -> None:
    """Copies the preferred scenario's financial_outputs to deal.financial_outputs
    so downstream consumers (chart_builder, context_builder, dd_flag_engine,
    kpi_validator, report_builder) keep working unchanged."""
```

### Decision E — Delta application semantics

Each scenario's deltas apply against `deal.assumptions` (the deal-level baseline)
to produce a scenario-specific assumptions snapshot. The worker function takes
a baseline copy, applies deltas, computes financials, and returns outputs.

**Mapping from scenario field to assumptions adjustment:**

| Scenario field                  | Assumptions adjustment                                          |
|---------------------------------|------------------------------------------------------------------|
| `construction_budget_delta_usd` | `a.const_hard += delta` (hard construction cost)                |
| `rent_delta_pct`                | `a.monthly_rent *= (1 + delta)` for residential; per-unit-mix loop for commercial |
| `timeline_delta_months`         | `a.const_period_months += delta`                                 |
| `unit_count`                    | `a.num_units = scenario.unit_count` (override, not delta)        |
| `building_sf`                   | `a.gba_sf = scenario.building_sf` (override, not delta)          |
| `use_mix`                       | Drives commercial rent roll if any non-residential allocation    |

The "as_submitted" fallback scenario carries zero deltas, so its computed
financials are byte-identical to today's per-deal output. This is the
backward-compatibility check.

---

## 2. Checkpoint structure

Four checkpoints, modeled on Session 3's CP1–CP4 pattern. Mike reviews and
approves each before proceeding. Use the deterministic verification pattern
(programmatic counts and exact-match scripts, not terminal display) — the
streaming-display artifact has been corrupting visual review and we trust
programmatic checks over rendered output.

### CP1 — `models.py` schema additions + `mirror_preferred_to_legacy` helper

Smallest checkpoint. Three changes:

1. Add `excel_filename: Optional[str] = None` field to `DevelopmentScenario`
2. Confirm `financial_outputs: Optional[FinancialOutputs] = None` field exists
   on `DevelopmentScenario` (Session 1 should have added this — verify or add)
3. Add `mirror_preferred_to_legacy(deal: DealData) -> None` function

The mirror function:
- Finds the scenario where `scenario.verdict == ScenarioVerdict.PREFERRED`
- If found and that scenario has `financial_outputs` populated, copy that
  scenario's outputs to `deal.financial_outputs` via deep copy or reassign
- If no preferred scenario exists or financials aren't populated, log a
  WARNING and leave `deal.financial_outputs` as-is (likely None or stale)
- Return None (mutates deal in place, like the existing
  `_apply_3c_*` functions)

Verification (CP1 gate):
- `from models import mirror_preferred_to_legacy` succeeds
- `DevelopmentScenario.model_fields.keys()` includes `excel_filename` and
  `financial_outputs`
- Calling `mirror_preferred_to_legacy` on a synthetic deal with one preferred
  scenario carrying mock financials results in `deal.financial_outputs` being
  populated with that scenario's outputs (round-trip via `model_dump_json`)
- Calling `mirror_preferred_to_legacy` on a deal with no preferred scenario
  logs a WARNING and does not raise

### CP2 — `financials.py` per-scenario fan-out

The architectural meat of the session. Two changes:

1. Extract the existing computation logic from `run_financials` into a new
   `_run_financials_for_scenario(deal, scenario)` worker function
2. Reduce `run_financials(deal)` to an orchestrator that loops `deal.scenarios`,
   calls the worker per scenario, writes results back to
   `scenario.financial_outputs`, and finally calls `mirror_preferred_to_legacy(deal)`

The worker function takes a deep-copied or scenario-adjusted view of the deal's
assumptions and runs the existing pipeline on that. Critical: the worker must
NOT mutate `deal.assumptions` directly, because the next scenario in the loop
needs the unmodified baseline.

**Recommended approach for assumption snapshots:**
```python
def _run_financials_for_scenario(deal: DealData, scenario: DevelopmentScenario) -> FinancialOutputs:
    # Take an isolated copy of assumptions for this scenario
    scenario_assumptions = deal.assumptions.model_copy(deep=True)
    # Apply deltas
    scenario_assumptions.const_hard += (scenario.construction_budget_delta_usd or 0)
    scenario_assumptions.monthly_rent *= (1 + (scenario.rent_delta_pct or 0))
    scenario_assumptions.const_period_months += (scenario.timeline_delta_months or 0)
    if scenario.unit_count is not None:
        scenario_assumptions.num_units = scenario.unit_count
    if scenario.building_sf is not None:
        scenario_assumptions.gba_sf = scenario.building_sf
    # Construct a temporary deal view with adjusted assumptions
    scenario_deal = deal.model_copy(deep=False)  # shallow — share extracted_docs etc.
    scenario_deal.assumptions = scenario_assumptions
    scenario_deal.financial_outputs = FinancialOutputs()  # fresh outputs
    # Run the existing pipeline against this scenario_deal
    _compute_full_financials(scenario_deal)  # extracted body of old run_financials
    return scenario_deal.financial_outputs
```

Then the orchestrator:
```python
def run_financials(deal: DealData) -> DealData:
    if not deal.scenarios:
        # Pre-Session-3 deals or gate-failure deals with no scenarios:
        # run on legacy baseline directly (preserves backward compat)
        _compute_full_financials(deal)
        return deal
    for scenario in deal.scenarios:
        try:
            scenario.financial_outputs = _run_financials_for_scenario(deal, scenario)
            logger.info("FINANCIALS [%s]: NOI yr1=%s, project IRR=%s",
                        scenario.scenario_id,
                        scenario.financial_outputs.noi_yr1,
                        scenario.financial_outputs.project_irr)
        except Exception as exc:
            logger.error("FINANCIALS [%s] FAILED (non-fatal): %s",
                         scenario.scenario_id, exc)
            scenario.financial_outputs = None  # signal failure; mirror will skip
    mirror_preferred_to_legacy(deal)
    return deal
```

Verification (CP2 gate):
- Synthetic deal with 3 zoning scenarios → all 3 get `financial_outputs`
  populated, all 3 contain non-null `noi_yr1` and pro_forma_years
- Synthetic deal with 0 zoning scenarios (pre-Session-3 baseline) → falls
  through to legacy single-deal path, `deal.financial_outputs` populated
  directly (today's behavior preserved)
- Synthetic deal with the "as_submitted" fallback scenario only → its
  computed `financial_outputs` is byte-identical (within float tolerance)
  to running the legacy path on the same deal
- After fan-out, `deal.financial_outputs` is populated by the mirror
  function (preferred scenario's outputs)
- Synthetic failure injection: one scenario raises in worker → that
  scenario's `financial_outputs` stays None, other scenarios complete,
  mirror function logs WARNING if preferred scenario was the failed one

### CP3 — `excel_builder.py` per-scenario fan-out + master index

Two changes:

1. Extract the existing template-population logic from `populate_excel` into
   `_populate_excel_for_scenario(deal, scenario, output_path)`. The worker
   reads from `scenario.financial_outputs` (not `deal.financial_outputs`) for
   all financial values, but reads from `deal` (not scenario-specific) for
   things like address, parcel_data, brand styling, etc.
2. Reduce `populate_excel(deal)` to an orchestrator that loops scenarios,
   calls the worker per scenario, writes the master index, and returns the
   path of the preferred scenario's Excel file.

The master index file:
- Created via openpyxl from scratch (no template — it's a new artifact)
- Single sheet "Scenarios Comparison"
- Header row: Scenario ID, Scenario Name, Verdict, Unit Count, Building SF,
  Total Project Cost, Year 1 NOI, Project IRR, LP IRR, LP Equity Multiple,
  Exit Value, Excel Filename
- One data row per scenario, ordered by `scenario.rank` (1 = preferred first)
- Saved as `{deal_id}_scenarios_index.xlsx` in OUTPUTS_DIR

The orchestrator returns the preferred scenario's Excel file path, which gets
stored on `deal.output_xlsx_path` by `main.py` (existing behavior, no main.py
change needed for this).

If no preferred scenario exists (legacy / single-deal path), the orchestrator
falls through to the original single-file behavior using `deal.financial_outputs`.

Verification (CP3 gate):
- Synthetic deal with 3 scenarios → 3 per-scenario .xlsx files plus 1 index
  file all created in OUTPUTS_DIR
- Each per-scenario file has its scenario's NOI, IRR, etc. in the
  Assumptions tab (read with `read_only=True, data_only=True` after
  LibreOffice recalc)
- The index file has 4 rows (header + 3 data rows), with PREFERRED scenario
  in row 2 (rank=1)
- `populate_excel` returns the path of the preferred scenario's file (NOT
  the index file)
- Synthetic deal with 0 scenarios (legacy path) → single .xlsx file as
  before, no index file
- LibreOffice headless recalc succeeds for at least 2 of the 3 scenario
  files (failure isolation — if one fails recalc, the others should not)

### CP4 — `market.py` gate-failure short-circuit + integration test

Two changes:

1. In `market.py:run_zoning_synthesis_chain`, modify the gate-failure branch
   to skip SCEN and HBU LLM calls entirely and write the fallbacks directly
2. Update test fixtures (or add new ones) that exercise the gate-failure
   short-circuit and verify the financial pipeline runs cleanly on the
   resulting single-fallback-scenario deal

Specifically, the orchestrator's gate-fail branch should look like:

```python
if not _confidence_gate_passes(deal):
    gate_reasons = _confidence_gate_reasons(deal)
    logger.info(
        "Zoning confidence gate FAILED (%d criterion(a)) — short-circuiting "
        "SCEN and HBU LLM calls. Reasons: %s",
        len(gate_reasons), gate_reasons,
    )
    deal.conformity_assessment = _indeterminate_conformity_assessment(deal)
    deal.scenarios = [_fallback_as_submitted_scenario(deal)]
    deal.zoning_extensions = _minimal_zoning_extensions(deal)
    return  # Short-circuit: no SCEN/HBU LLM calls
```

The existing post-gate code path (3C-CONF, 3C-SCEN, 3C-HBU) stays intact for
the gate-pass case.

Then run an end-to-end integration smoke test using the existing Belmont
fixture (gate-pass) and a new gate-failure fixture (Belmont with `zoning_code=None`)
to verify:
- Gate-pass path: 3 LLM calls happen (or fall back as appropriate), N
  scenarios produced, financials fan out correctly
- Gate-fail path: 0 LLM calls in the synthesis chain, single fallback
  scenario produced, financials run cleanly on the fallback

Verification (CP4 gate):
- Mock `_call_llm` to track calls. Run gate-pass fixture → ≥2 LLM calls in
  market chain (CONF, SCEN, HBU; some may fall back)
- Run gate-fail fixture → 0 LLM calls in market chain (short-circuit
  fires before any of the three try/except blocks)
- Both paths complete `run_financials(deal)` without raising
- Gate-fail path produces exactly one scenario, with verdict=PREFERRED and
  scenario_id=as_submitted
- The fallback scenario's `financial_outputs.noi_yr1` matches what the
  legacy single-deal pipeline produces on the same baseline
- Master plan history-log entry can credibly claim Carry-forward #6 is
  closed

---

## 3. Gate criteria (15 criteria — all must pass before commit)

1. `from models import mirror_preferred_to_legacy` succeeds (CP1)
2. `DevelopmentScenario.model_fields` contains `excel_filename` and
   `financial_outputs` keys (CP1)
3. `mirror_preferred_to_legacy` round-trips: synthetic deal with preferred
   scenario carrying mock financials results in `deal.financial_outputs`
   populated identically (CP1)
4. `_run_financials_for_scenario(deal, scenario)` runs to completion on the
   "as_submitted" fallback scenario without raising (CP2)
5. `run_financials(deal)` produces per-scenario `financial_outputs` for all
   N scenarios (CP2)
6. `run_financials(deal)` populates `deal.financial_outputs` via the mirror
   after fan-out (CP2)
7. `run_financials(deal)` on a legacy deal with `deal.scenarios == []` falls
   through to single-deal path, populating `deal.financial_outputs`
   directly (CP2 backward compat)
8. The "as_submitted" fallback scenario's computed `noi_yr1` matches the
   legacy single-deal pipeline's `noi_yr1` within float tolerance
   (CP2 backward compat)
9. `populate_excel(deal)` produces N per-scenario .xlsx files plus 1 master
   index file for a deal with N scenarios (CP3)
10. The master index file's "Scenarios Comparison" sheet has N+1 rows
    (header + N data rows) ordered by scenario rank (CP3)
11. `populate_excel(deal)` returns the path of the preferred scenario's
    Excel file, not the index file (CP3)
12. LibreOffice headless recalc succeeds for at least one per-scenario file
    (CP3 — confirms the existing recalc pipeline still works per-file)
13. `run_zoning_synthesis_chain` with gate-pass deal → 3 LLM calls in chain
    (or fall back per-prompt as today); gate-fail deal → 0 LLM calls (CP4
    short-circuit)
14. End-to-end integration: gate-pass Belmont fixture → orchestrator + financials
    + Excel build all complete without raising; ≥2 per-scenario .xlsx files
    plus index (CP4 integration)
15. End-to-end integration: gate-fail fixture → orchestrator short-circuits,
    financials produces single-deal output via fallback scenario, Excel
    builds 1 file (no index needed for single scenario, but emit one if
    that's simpler) (CP4 integration)

Gate criteria 1–8 are unit-level and can be exercised via mock data inside the
gate script. Criteria 9–12 require LibreOffice and write actual .xlsx files
to disk. Criteria 13–15 require the market.py orchestrator to run end-to-end.

---

## 4. Test fixtures

Reuse existing fixtures from Session 3:
- `tests/fixtures/zoning_overhaul_session_3_fixture_belmont.json` — RSD-3
  conforming multifamily, gate-pass case
- `tests/fixtures/zoning_overhaul_session_3_fixture_indian_queen.json` —
  split-zoned RSA-1/RSA-5, gate-pass case (different fan-out shape)

Add one new fixture:
- `tests/fixtures/zoning_overhaul_session_4_fixture_gate_fail.json` —
  Belmont but with `zoning_code = ""` and `permitted_uses = []` to force
  gate failure. This exercises Carry-forward #6 (the gate-failure
  short-circuit) end-to-end through the financial pipeline.

The gate-fail fixture's expected behavior:
- Confidence gate fails on 2 criteria (zoning_code empty, permitted_uses
  count < 3)
- Orchestrator short-circuits: 0 LLM calls in the chain
- `deal.scenarios = [as_submitted fallback]`
- `deal.zoning_extensions = minimal extensions fallback`
- `run_financials(deal)` runs the as_submitted scenario through the full
  pipeline; produces non-zero NOI, pro forma, etc.
- `populate_excel(deal)` produces 1 per-scenario file plus 1 trivial index
  file (only one row of data, but the index is still emitted for
  consistency with the multi-scenario case)

---

## 5. Commit & tag plan

Two commits, same pattern as Sessions 1.5 / 1.6 / 3:

### Code commit (after CP4 passes the gate script)

Files staged:
- `financials.py` (refactored)
- `excel_builder.py` (refactored)
- `models.py` (small additions)
- `market.py` (gate short-circuit only)
- `main.py` (one-line addition if needed)
- `tests/fixtures/zoning_overhaul_session_4_fixture_gate_fail.json` (new)

Commit message:
```
Session 4: financials + excel_builder — Per-scenario fan-out + legacy mirror

- Refactor run_financials into per-scenario orchestrator + worker
  - run_financials(deal) loops deal.scenarios, calls
    _run_financials_for_scenario per scenario, writes per-scenario
    financial_outputs back to each DevelopmentScenario
  - _run_financials_for_scenario applies scenario deltas to baseline
    assumptions snapshot, runs the full financial pipeline, returns
    populated FinancialOutputs (no mutation of deal.assumptions)
  - Backward-compat fallback: deals with empty scenarios list run the
    legacy single-deal path unchanged
- Refactor populate_excel into per-scenario orchestrator + worker
  - populate_excel(deal) creates one .xlsx per scenario plus a master
    {deal_id}_scenarios_index.xlsx with side-by-side comparison
  - Returns the preferred scenario's Excel path (legacy contract preserved)
- Add mirror_preferred_to_legacy(deal) in models.py — copies preferred
  scenario's financial_outputs to deal.financial_outputs for backward
  compatibility with chart_builder, context_builder, dd_flag_engine,
  kpi_validator, report_builder
- Add excel_filename and financial_outputs fields to DevelopmentScenario
- Close carry-forward #6: run_zoning_synthesis_chain short-circuits
  SCEN and HBU LLM calls when the confidence gate fails, writing
  fallbacks directly. Saves ~$0.05–0.10 and 5–10s per gate-failed deal.
- Add Session 4 gate-fail test fixture exercising the short-circuit
  end-to-end through the financial pipeline

Implements the multi-scenario architecture from the zoning overhaul plan.
Closes carry-forward #6 (gate-failure short-circuit).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

Tag: `zoning-overhaul-session-4-passed`

### Docs commit (after code commit lands)

Files staged:
- `docs/DealDesk_Zoning_Overhaul_Plan.md` (status table sync + history-log entry)

Status table updates:
- Session 4: READY → COMPLETED with commit hash + tag
- Session 5: BLOCKED on Session 4 → READY (awaiting kickoff)

History-log entry: matches Session 3's verbosity pattern. Document carry-forward
#6 closure. Document the per-scenario file architecture decision for future
readers. Note remaining open carry-forwards (#4, #5 — Session 1.8 schema work,
still deferred).

No tag for docs commit (consistent with prior pattern).

---

## 6. Review pattern

Same as Session 3:
- Each CP staged but NOT committed
- Agent shows the full diff plus deterministic verification output (programmatic
  marker counts, NOT terminal display)
- Mike pastes back to claude.ai for review
- After approval, agent proceeds to next CP
- After CP4 passes, agent prepares the staging-and-commit plan but does NOT
  execute until Mike approves

Key reminder: the streaming-display artifact has been corrupting visual review
throughout Session 3. Trust programmatic checks (count occurrences, run difflib
equality, extract blocks and assert character-for-character match) over rendered
terminal output. Mike will explicitly request these in checkpoint reviews.

---

## 7. Reading sequence at session start

Before drafting any plan summary, read these files in order:

1. `docs/DealDesk_Zoning_Overhaul_Plan.md` — confirm Session 4 status row says
   READY, confirm carry-forwards #4, #5, #6 are listed and #6 is in scope
2. `docs/Session_2_Prompt_Specification.md` §3 (3C-SCEN scenario shape) — to
   ground the delta-application semantics for `construction_budget_delta_usd`,
   `rent_delta_pct`, `timeline_delta_months`
3. `models.py` (full file) — `DevelopmentScenario`, `FinancialOutputs`,
   `Assumptions`, `DealData` schemas
4. `financials.py` lines 2640–2960 (`run_financials` function) — the main
   refactor target
5. `excel_builder.py` lines 1–250 (`populate_excel` and supporting helpers) —
   the other refactor target
6. `market.py:run_zoning_synthesis_chain` (post-Session-3 location) — the
   gate-failure branch to short-circuit
7. `main.py` lines 990–1050 — the pipeline call site

After reading, provide a plan summary covering:
- What signatures the new functions will have
- Where to extract `_compute_full_financials` from (the body of today's
  `run_financials`)
- How `_run_financials_for_scenario` will create the assumptions snapshot
  without mutating the baseline
- The macro-scenario nesting question (does each scenario's
  `financial_outputs.scenario_results` carry its own Base/Upside/Downside,
  or is that simplified)
- The gate-failure short-circuit's exact location in
  `run_zoning_synthesis_chain`

Mike approves the plan summary before any code is touched. Same approval
discipline as Session 3.

---

## 8. Stop conditions

Halt and request Mike's input if:
- The reading phase reveals drift between Session 3's master plan claims and
  the actual on-disk state (would imply Session 3 didn't fully land — would be
  surprising but not impossible)
- `_compute_full_financials` extraction proves harder than expected because the
  existing `run_financials` body has more deal-level mutations than the surface
  read suggests (particularly the `_scale_expenses_for_asset_type` call and
  the title insurance auto-calculate at lines 2666–2671 — these mutate
  `deal.assumptions` and need careful handling)
- The Excel template's circular formula chain at C50/C71/C76 doesn't fan out
  cleanly per scenario (would be a real architectural surprise — current
  expectation is that each scenario's Excel file has its own independent
  template copy with its own pre-computed values)
- LibreOffice headless recalc fails on a per-scenario basis in a way that
  blocks the multi-file path
- Anything in the macro scenario suite (Base/Upside/Downside) interacts oddly
  with the per-scenario fan-out

---

## 9. Out-of-scope items (deferred)

- Carry-forwards #4 and #5 (Session 1.8 schema additions for split-zoning
  fields and `investment_strategy` on DevelopmentScenario) — still deferred.
  Will be addressed in a Session 1.8 micro-session after Session 4 closes
  and before Session 5 kickoff.
- Any changes to report rendering (Session 5 territory)
- Any UI changes to the frontend (out of scope for the entire zoning overhaul)
- Production validation against live LLM calls — Session 4 is validated
  structurally; live validation happens when Mike runs an actual deal through
  the pipeline after Session 4 closes
