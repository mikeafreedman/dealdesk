# Session 3 — Claude Code Kickoff Instructions

**Purpose:** Wire the three approved prompts (3C-CONF, 3C-SCEN, 3C-HBU) into `market.py`. Build the orchestrator, the confidence gate, and the four typed-empty fallback constructors. Replace the legacy single Prompt 3C call site.

**Estimated effort:** 2.5–3.5 hours of agent time, with 4 mandatory review checkpoints in the middle. Plan for a focused single sitting; do not start if you can't see it through to a tagged completion.

**Risk level:** MEDIUM — substantial new code (~700–900 lines additions, ~150 lines deletions). Multiple integration points. Failure to enforce checkpoints turns this into a runaway session.

---

## Step 0 — Pre-flight checks

Before opening Claude Code, confirm in PowerShell:

```powershell
cd C:\Users\mikea\dealdesk
git status                              # expect clean tree
git tag --list zoning-overhaul-*        # expect 3 tags (1, 1.5, 2)
git log --oneline -7                    # confirm Session 1.5 docs commit at top
```

If any of those fail, stop and resolve before starting. Session 3 must begin against a clean tree with all three prior tags in place.

Open a new Claude Code session in `C:\Users\mikea\dealdesk\`. Increase your context budget if you can — this session will read `market.py` (~3,400 lines), `Session_2_Prompt_Specification.md` (~930 lines), and `FINAL_APPROVED_Prompt_Catalog_v5.md` (~730 lines) in full.

---

## Step 1 — Paste this opening message to Claude Code

Copy everything between the `===BEGIN===` and `===END===` markers below into Claude Code as your first message.

===BEGIN===

I'm starting Session 3 of the DealDesk Zoning Overhaul. This is the wiring session — the largest session in the build. You will replace the legacy single Prompt 3C in `market.py` with the three-prompt synthesis chain (3C-CONF, 3C-SCEN, 3C-HBU) plus its orchestrator, confidence gate, retry policy, and four typed-empty fallback constructors.

Before you write any code, please read these files in this exact order:

1. `docs/DealDesk_Zoning_Overhaul_Plan.md` — master plan with all locked design decisions
2. `docs/Session_1_Schema_Design.md` — schema spec (Session 1, already implemented in models/models.py)
3. `docs/Session_2_Prompt_Specification.md` — **THIS IS YOUR IMPLEMENTATION SOURCE OF TRUTH.** Read all 10 sections in full. Sections 2, 3, and 4 contain the complete system + user prompt text for the three new prompts. Section 5 contains the orchestration logic, confidence gate criteria, retry policy, and fallback specifications. Section 6 contains test fixture references for Reference Deals A, B, and C.
4. `docs/FINAL_APPROVED_Prompt_Catalog_v5.md` — the catalog form of the same prompts (a useful cross-reference; should match Session_2_Prompt_Specification.md character-for-character)

Also re-read these for orientation (you've seen them before):

5. `models/models.py` — confirm WorkflowControls, EncumbranceType, Encumbrance, ConformityAssessment, DevelopmentScenario, ZoningExtensions all exist (they should — Sessions 1 and 1.5 added them)
6. `market.py` — your target file. Read it in full. You'll be making substantial additions and one substantial deletion.

After you've read all six, summarize back to me:

- The seven concrete deliverables you'll produce (per Section 5 / Section 10 of Session_2_Prompt_Specification.md)
- The four review checkpoints you'll pause at, what each will show me, and what I need to approve at each
- How you'll handle the existing `_call_llm` returning `None` on failure rather than raising (Session 2 spec assumes exceptions; you'll need to bridge this)
- Where in `market.py` each block of new code goes (line numbers, function ordering)
- What the new test fixtures will be and where they'll live
- What the commit message will be

Do NOT start coding until I confirm your plan. The plan summary is critical — Session 3 has more moving parts than any prior session, and I want to verify you have all of them in scope before you touch any code.

===END===

---

## Step 2 — What Claude Code should do (your reference, not its instructions)

The session has seven concrete deliverables in `market.py`, two new test fixtures, and one update to `enrich_market_data()`. Use this checklist when reviewing the agent's plan summary.

### 2.1 The seven concrete deliverables in market.py

Per `Session_2_Prompt_Specification.md` Section 10, the agent must produce:

1. **The three system + user prompt constant pairs.** Six string constants total: `_SYSTEM_3C_CONF` + `_USER_3C_CONF`, `_SYSTEM_3C_SCEN` + `_USER_3C_SCEN`, `_SYSTEM_3C_HBU` + `_USER_3C_HBU`. The text is verbatim from Session 2 spec Sections 2.3, 2.4, 3.3, 3.4, 4.3, 4.4. **Verbatim** means character-for-character. The agent should not rewrite or "improve" any prompt text.

2. **Three user-prompt builder functions.** `_build_3c_conf_user(deal)`, `_build_3c_scen_user(deal)`, `_build_3c_hbu_user(deal)`. Each assembles the variable substitutions per the Inputs tables in Session 2 spec Sections 2.2, 3.2, 4.2 and returns the formatted user message string.

3. **Three apply functions.** `_apply_3c_conf(data, deal)`, `_apply_3c_scen(data, deal)`, `_apply_3c_hbu(data, deal)`. Each parses the LLM response JSON, constructs the appropriate Pydantic model(s), and assigns to the correct DealData field. Mappings are in Session 2 spec Sections 2.5, 3.5, 4.5.

4. **The orchestrator function.** `run_zoning_synthesis_chain(deal)` — see Session 2 spec Section 5.1 for the full pseudocode. Strict sequential execution: gate → 3C-CONF → 3C-SCEN → 3C-HBU. Failure isolation per Section 5.4. Logging at every step.

5. **The confidence gate.** `_confidence_gate_passes(deal)` — four-criterion check per Session 2 spec Section 5.2 / Catalog v5 Section 2.2. Returns bool.

6. **The three typed-empty fallback constructors.** `_indeterminate_conformity_assessment(deal)`, `_fallback_as_submitted_scenario(deal)`, `_minimal_zoning_extensions(deal)`. Each returns a fully-typed Pydantic model with explicit placeholder content per Session 2 spec Sections 5.2 and 5.4.

7. **The retry helper.** `_call_llm_with_retry(model, system, user_msg, max_retries=1)` — wraps the existing `_call_llm` with retry-on-parse-failure logic per Session 2 spec Section 5.3. On retry, prepends the system prompt with the strict-JSON reminder. Raises an exception on persistent failure (NOT returns None) so the orchestrator's try/except in Section 5.1 works as written.

### 2.2 The legacy code to remove

Per the deprecation handling in Catalog v5:

- `_SYSTEM_3C` constant (currently around line 2008)
- `_USER_3C` constant (currently around line 2022)
- `_apply_3c(data, deal)` function (currently around line 2039)
- The Prompt 3C call block in `enrich_market_data()` (currently lines 3281–3303 — the `# Prompt 3C — Highest & Best Use (Sonnet)` block through the warning log line)

The `enrich_market_data()` call site is replaced with a single line: `run_zoning_synthesis_chain(deal)`.

### 2.3 The integration point

`enrich_market_data()` (currently at line 2741) needs ONE modification: replace the legacy 3C call block with the new orchestrator call. No other changes to `enrich_market_data()` are in scope. If the agent proposes touching anything else in this function, push back.

### 2.4 New test fixtures

Two new fixtures in `tests/fixtures/`:

- `zoning_overhaul_session_3_fixture_belmont.json` — Reference Deal B (per Session 2 spec Section 6.2)
- `zoning_overhaul_session_3_fixture_indian_queen.json` — Reference Deal C (per Session 2 spec Section 6.3)

Reference Deal A's fixture already exists from Session 1. Session 3 adds expected-output fixtures alongside it for regression testing if the agent has time and budget — but these are nice-to-have, not blocking. The two NEW fixtures (B and C) are required.

### 2.5 The four review checkpoints

The agent must pause at each of these and show diff + run tests before proceeding. **You must approve at each checkpoint before the agent moves to the next.**

**Checkpoint 1 — Prompt constants and builder functions.** After the six prompt constants (1) and the three user-prompt builder functions (2) are written. Diff scope: only new constants and builder functions in `market.py`. Verification: run a smoke test that imports the constants and calls each builder against the existing Session 1 fixture (Deal A). Builders should return strings without errors.

**Checkpoint 2 — Apply functions, retry helper, fallback constructors.** After deliverables 3, 6, and 7 are written. Diff scope: only new apply functions, retry helper, and fallback constructors. Verification: smoke test that constructs each fallback against the Session 1 fixture, dumps to JSON, and re-parses. All three fallbacks must round-trip.

**Checkpoint 3 — Orchestrator + confidence gate + integration.** After deliverables 4 and 5 are written, the legacy code is removed, and `enrich_market_data()` is updated. Diff scope: new `run_zoning_synthesis_chain` and `_confidence_gate_passes` functions, removal of `_SYSTEM_3C` / `_USER_3C` / `_apply_3c`, and the 3C-block replacement in `enrich_market_data()`. Verification: full smoke test against the Session 1 fixture (Deal A) that runs the full chain end-to-end. Note: this will require valid Anthropic API credentials in `.env`; if they're absent, the orchestrator should still complete via fallbacks without raising.

**Checkpoint 4 — Reference fixtures B and C + gate script + final commit prep.** After the two new fixtures are written and a Session 3 gate script (similar to the Session 1.5 one, ~10 criteria) passes. Diff scope: two new fixture files and any final adjustments. Verification: gate script results.

### 2.6 Anti-scope-creep guardrails

The agent should **NOT** touch any of the following in this session. If the agent proposes changes to any of these, push back and ask why:

- `financials.py` (any file) — that's Session 4 work
- `excel_builder.py` (any file) — that's Session 4 work
- `report_builder.py`, `chart_builder.py`, `word_builder.py` — that's Session 5 work
- `models/models.py` — frozen since Session 1.5; if the agent finds a missing field, stop and add it via a Session 1.6 micro-session, do not bundle into Session 3
- `extractor.py`, `parcel_fetcher.py`, `iasworld_fetcher.py` — upstream data; out of scope
- The frontend (`fp_underwriting_FINAL_v7.html`) — Session 5 work
- Any other prompts in `market.py` (3A, 3B, 3D, 5B) — out of scope; this session is exclusively about the 3C replacement chain

The only files that should be modified or created in this session are:

- `market.py` (modifications + additions)
- `tests/fixtures/zoning_overhaul_session_3_fixture_belmont.json` (new)
- `tests/fixtures/zoning_overhaul_session_3_fixture_indian_queen.json` (new)
- `docs/DealDesk_Zoning_Overhaul_Plan.md` (Session 3 history-log entry + status table sync, in a separate docs commit at the end)

### 2.7 Commit and tag plan

Two commits at the end (same pattern as Session 1.5):

**Commit 1 — Code commit:**

```
Session 3: market.py — Replace Prompt 3C with three-prompt synthesis chain

- Add 3C-CONF, 3C-SCEN, 3C-HBU prompt constants (verbatim from Catalog v5)
- Add three user-prompt builder functions (_build_3c_*_user)
- Add three apply functions (_apply_3c_*) mapping JSON to DealData fields
- Add run_zoning_synthesis_chain orchestrator (sequential, isolated retry)
- Add _confidence_gate_passes (four-criterion check)
- Add _call_llm_with_retry (one retry on parse failure, raises on persistent)
- Add three typed-empty fallback constructors:
  _indeterminate_conformity_assessment, _fallback_as_submitted_scenario,
  _minimal_zoning_extensions
- Remove legacy _SYSTEM_3C, _USER_3C, _apply_3c
- Update enrich_market_data to call new orchestrator
- Add Reference Deal B (Belmont) and C (Indian Queen) test fixtures

Implements Session 2 prompt design. Pure replacement; no schema changes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

Tag: `zoning-overhaul-session-3-passed`

**Commit 2 — Docs commit:**

Append Session 3 history-log entry to master plan, sync status table (Session 3: READY → COMPLETED, Session 4: BLOCKED → READY).

```
docs: Session 3 complete + master plan status table sync

- Append Session 3 history-log entry to master plan
- Sync Current Session Status table:
  - Session 3: READY → COMPLETED
  - Session 4: BLOCKED on Session 3 → READY (kickoff sheet pending)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

No tag on the docs commit — Session 3 is already tagged at the code commit.

---

## Step 3 — Gate criteria for Session 3 (verify before declaring done)

The agent's gate script should test all of these:

- [ ] `market.py` imports cleanly with no syntax or type errors
- [ ] All six new prompt constants exist as module-level strings and are non-empty
- [ ] All three user-prompt builder functions exist, accept a `DealData`, and return a non-empty string
- [ ] All three apply functions exist, accept `(data: dict, deal: DealData)`, and successfully populate the target field on a synthetic test deal
- [ ] `_call_llm_with_retry` raises (does not return None) when the underlying call returns None twice
- [ ] `_confidence_gate_passes` returns False when zoning_code is missing; returns False when fewer than 3 permitted_uses; returns False when fewer than 4 dimensional fields are populated; returns False when lot_sf is None or 0; returns True when all four criteria pass
- [ ] All three typed-empty fallback constructors return fully-typed Pydantic models that round-trip through `model_dump_json()` / `model_validate_json()`
- [ ] `run_zoning_synthesis_chain(deal)` does NOT raise even when the LLM returns None for all three prompts (fallback path runs cleanly)
- [ ] Belmont fixture (Deal B) deserializes and includes a `RSD-3` zoning code
- [ ] Indian Queen fixture (Deal C) deserializes and includes both encumbrances + the split-zoning indicator
- [ ] Legacy `_SYSTEM_3C`, `_USER_3C`, `_apply_3c` are no longer present in `market.py`
- [ ] `enrich_market_data()` calls `run_zoning_synthesis_chain(deal)` (not the old `_call_llm(MODEL_SONNET, _SYSTEM_3C, ...)`)
- [ ] `git diff --stat` shows changes ONLY in `market.py` and the two new fixture files (no scope creep)

---

## Step 4 — When Session 3 is done, you're ready for Session 4

Session 4 is the financial integration session — fanning out the existing pro forma / Excel builder logic across the per-scenario `DealData.scenarios[]` list, writing per-scenario financial outputs back to each `DevelopmentScenario` model, and updating the `mirror_preferred_to_legacy()` utility to copy the preferred scenario's financials to the legacy `deal.financial_outputs` field for backward compatibility.

The Session 4 kickoff sheet will be prepared from claude.ai when you're ready. Do NOT auto-proceed into Session 4 from Session 3 — the financial fan-out has its own design questions worth thinking through carefully.

---

## Step 5 — Operator notes (you, the human)

Three things to keep in mind during this session that don't apply to the agent:

**Take breaks at the checkpoints.** Each of the four checkpoints is a natural pause point. The session is long. If you feel decision fatigue setting in, halt at a checkpoint, walk away for an hour, and come back fresh. The agent will hold its position cleanly.

**Watch for verbatim prompt text in Checkpoint 1.** When the agent shows you the diff for the prompt constants, your job is to verify the text is character-for-character what's in `Session_2_Prompt_Specification.md` Sections 2.3, 2.4, 3.3, 3.4, 4.3, 4.4. Open those sections in another window and diff visually. The agent should NOT have "improved" or "cleaned up" any prompt text. If it did, push back.

**Don't approve fallback shortcuts.** At Checkpoint 2, the agent might propose simplifying one of the fallback constructors ("the spec says these fields, but really we only need the first three"). Don't let that happen. The fallback constructors are designed to write fully-typed placeholders with explicit content so downstream rendering doesn't break. Every field in the spec must be populated, even if with a sentinel value. If the agent thinks a field can be skipped, it's a flag that they're not understanding why the fallback exists.

---

*End of Session 3 instructions.*
