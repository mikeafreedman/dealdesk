# DealDesk — Zoning Analysis Overhaul
## Session 5 Kickoff Sheet
### Report Rendering · report_builder.py · Jinja2 Template

**Date:** April 29, 2026  
**Owner:** Mike Freedman, Freedman Properties  
**Status:** APPROVED — Ready for Claude Code  

---

## 1. Orientation — What Session 5 Is

Session 5 is the final layer of the DealDesk Zoning Analysis Overhaul. Sessions 1 through 4 built the infrastructure underneath the report: the schema (Session 1), the prompts (Session 2), the pipeline orchestration (Session 3), and the financial fan-out (Session 4). Session 5 makes all of that intelligence visible to the user in the PDF report.

The current `report_builder.py` was written before the overhaul. It renders a single flat HBU narrative from the old Prompt 3C output. It has no concept of `ConformityAssessment`, `DevelopmentScenario[]`, or the cross-scenario `ZoningExtensions` synthesis. Session 5 replaces those flat sections with structured, data-driven components.

---

### 1.1 The Five-Session Arc

| Session | Focus | Key Output | Status |
|---|---|---|---|
| Session 1 | Schema design | `models.py` — `ConformityAssessment`, `DevelopmentScenario`, `ZoningExtensions`, `Encumbrance` | ✅ COMPLETE — tagged `session-1-passed` |
| Session 1.5 | Schema micro-session | `WorkflowControls` + `Encumbrance` models added | ✅ COMPLETE — tagged `session-1-5-passed` |
| Session 1.6 | Schema micro-session | Split-zoning fields, `investment_strategy` on `DevelopmentScenario` | ✅ COMPLETE — tagged `session-1-6-passed` |
| Session 2 | Prompt design | `FINAL_APPROVED_Prompt_Catalog_v5.md` — 3C-CONF, 3C-SCEN, 3C-HBU | ✅ COMPLETE — tagged `session-2-passed` |
| Session 3 | Pipeline orchestration | `market.py` — `run_zoning_synthesis_chain`, confidence gate, retry, fallbacks | ✅ COMPLETE — tagged `session-3-passed` |
| Session 4 | Financial fan-out | `financials.py` + `excel_builder.py` — per-scenario loop, `mirror_preferred_to_legacy` | ✅ COMPLETE — tagged `session-4-passed` |
| **Session 5** | **Report rendering** | `context_builder.py` + `templates/report_template.html` + `dd_flag_engine.py` — conformity badge, scenario cards, HBU synthesis, DD flag | 🔜 **THIS SESSION** |

---

### 1.2 Locked Decisions from the Pre-Session Design Review

| Decision ID | Decision | Rationale |
|---|---|---|
| D-PDF-1 | PDF naming stays as `{address_slug}_Underwriting_Report.pdf` | Human-readable; folder-scannable. Session 5 explicitly adopts this. No per-scenario PDF splitting. |
| D-SCEN-1 | Design for 1–3 scenarios (production-ready, all cases) | Handles Belmont (2 scenarios), Indian Queen (3 scenarios), and any single-scenario deal. |
| D-REND-1 | One PDF per deal, not per scenario | Scenarios appear as cards within Section 8 of the single report. Per-scenario Excel files already handled by Session 4. |

---

## 2. Session 5 Scope — What Gets Built

### 2.1 Files to Modify

| File | Change Type | What Changes |
|---|---|---|
| `context_builder.py` | Modify | Update `build_context()` to pass `ConformityAssessment`, `scenarios[]`, and `ZoningExtensions` to the Jinja2 template. Update section 8 and section 9 render logic. (`report_builder.py` itself stays untouched — it imports `build_context` from `context_builder`; the new context-building logic belongs in `context_builder.py`.) |
| `templates/report_template.html` | Modify | Replace flat HBU block in §8/§9 with: (a) conformity badge, (b) dimension grid, (c) 1–3 scenario cards, (d) HBU synthesis block, (e) use flexibility score bar. |
| `dd_flag_engine.py` | Modify | Add auto-trigger rule: if `conformity_assessment.status != CONFORMING`, emit a Zoning Nonconformity DD flag with nonconforming dimensions and encumbrances listed. |
| `tests/smoke_test_s5.py` | Create | New smoke test: run Belmont (Deal B) and Indian Queen (Deal C) fixtures through the full pipeline end-to-end, assert the PDF contains expected section text. |

---

### 2.2 What Does NOT Change in Session 5

> **Anti-scope-creep guardrail:** The agent must not touch any of the following files or behaviors during Session 5.

- `market.py` — no changes. The orchestration chain is complete as of Session 3.
- `financials.py` — no changes. The per-scenario fan-out is complete as of Session 4.
- `excel_builder.py` — no changes.
- `models.py` — no changes. No new fields or validators.
- `extractor.py` — no changes.
- The frontend (`fp_underwriting_FINAL_v7.html`) — no changes.
- Excel template files — no changes.
- Any existing report section outside §8 and §9 — no changes.

---

### 2.3 New Report Components — Section 8

Section 8 (Zoning Analysis) receives five new sub-components, rendered in this order:

| Component | Jinja2 Variable(s) | Renders From | Fallback if Missing |
|---|---|---|---|
| Conformity status badge | `{{ conformity.status }}`, `{{ conformity.zoning_code }}` | `deal.conformity_assessment.status`, `zoning_code`, `district_name` | Badge reads `ASSESSMENT PENDING` in gray; no dimension grid rendered. |
| Dimension grid (6 cells) | `{{ conformity.dimensions }}` | `deal.conformity_assessment.dimensions[]` — up to 6 items, each with `label/value/status` (pass/fail/warn) | Grid omitted entirely if dimensions list is empty. |
| Scenario cards (1–3) | `{{ scenarios }}` | `deal.scenarios[]` — each with `rank`, `name`, `pathway_type`, `strategy`, `description`, `key_metrics` | Single `as-submitted` card rendered if scenarios list is empty. |
| HBU synthesis block | `{{ zoning_ext.cross_scenario_recommendation }}`, `{{ zoning_ext.preferred_scenario_id }}`, `{{ zoning_ext.use_flexibility_score }}` | `deal.zoning_extensions.*` | Block reads `HBU synthesis not available for this deal.` No score bar rendered. |
| Zoning nonconformity DD flag | Auto-triggered by `dd_flag_engine.py` | `conformity_assessment.status != CONFORMING` | DD flag only appears when triggered; absent if conforming. |

---

### 2.4 New Report Component — Section 9 (HBU Opinion)

Section 9 currently renders the output of the old Prompt 3C as a flat paragraph. Session 5 replaces this with the `cross_scenario_recommendation` text from `ZoningExtensions`, followed by the preferred scenario name and the use flexibility score statement.

| Element | Source Field | Format |
|---|---|---|
| HBU narrative paragraph | `deal.zoning_extensions.cross_scenario_recommendation` | Body text, existing §9 paragraph style. |
| Preferred scenario callout | `deal.zoning_extensions.preferred_scenario_id` + scenario name | Left-bordered callout box in Sage Deep. `Preferred scenario: [name]` |
| Use flexibility statement | `deal.zoning_extensions.use_flexibility_score.score` + label | 1-line statement: `Use flexibility score: X/10 — [Low/Medium/High]` |

---

## 3. Context Builder — Field Mapping

The `build_context()` function in `context_builder.py` assembles the dictionary passed to the Jinja2 template. The following new keys must be added for Session 5. All are read from `deal.conformity_assessment`, `deal.scenarios[]`, and `deal.zoning_extensions` respectively. All have safe fallback values so the template never throws a `KeyError`.

### 3.1 New Keys Added to Context Dict

| Context Key | Source Path | Type | Fallback Value |
|---|---|---|---|
| `conformity` | `deal.conformity_assessment` | `ConformityAssessment \| None` | `None` → template uses ASSESSMENT PENDING badge |
| `conformity.status` | `deal.conformity_assessment.status` | `str enum` | `'ASSESSMENT_PENDING'` |
| `conformity.zoning_code` | `deal.conformity_assessment.zoning_code` | `str \| None` | `deal.zoning.zoning_code or ''` |
| `conformity.district_name` | `deal.conformity_assessment.district_name` | `str \| None` | `''` |
| `conformity.dimensions` | `deal.conformity_assessment.dimensions` | `list[dict]` | `[]` |
| `scenarios` | `deal.scenarios` | `list[DevelopmentScenario]` | `[]` |
| `preferred_scenario` | `next(s for s in deal.scenarios if s.scenario_id == deal.zoning_extensions.preferred_scenario_id, None)` | `DevelopmentScenario \| None` | `None` |
| `zoning_ext` | `deal.zoning_extensions` | `ZoningExtensions \| None` | `None` → HBU block shows placeholder |
| `zoning_ext.cross_scenario_recommendation` | `deal.zoning_extensions.cross_scenario_recommendation` | `str \| None` | `''` |
| `zoning_ext.preferred_scenario_id` | `deal.zoning_extensions.preferred_scenario_id` | `str \| None` | `None` |
| `zoning_ext.use_flexibility_score` | `deal.zoning_extensions.use_flexibility_score` | `UseFlexibilityScore \| None` | `None` → score bar not rendered |

> **Note — `zoning_nonconformity_flag` deferred to CP2.** The 11th context key, `zoning_nonconformity_flag` (sourced from `dd_flag_engine.get_zoning_flag(deal)`, falling back to `None` when no flag should render in §8), is added in CP2 alongside `get_zoning_flag()` itself. CP1 adds the 10 keys above; CP2 adds the 11th. CP1's test asserts 10 keys; CP2's extends the test to assert all 11.

---

## 4. DD Flag Engine — Zoning Nonconformity Rule

Session 5 adds one new auto-trigger rule to `dd_flag_engine.py`. The rule fires when `deal.conformity_assessment.status` is not equal to `CONFORMING`. It produces a `DDFlag` object with a standardized title, body text listing the nonconforming dimensions and any encumbrances, and a severity of `HIGH`.

### 4.1 Trigger Condition (Python)

```python
def get_zoning_flag(deal) -> Optional[DDFlag]:
    ca = deal.conformity_assessment
    if ca is None or ca.status == "CONFORMING":
        return None
    # Build body text from nonconforming dimensions
    failing = [d for d in ca.dimensions if d.get("status") in ("fail", "warn")]
    encs = deal.encumbrances or []
    return DDFlag(
        flag_id="zoning_nonconformity",
        severity="HIGH",
        title="Zoning nonconformity — due diligence required",
        body=_build_nonconformity_body(ca.status, failing, encs),
    )
```

### 4.2 Body Text Template

The `_build_nonconformity_body()` helper assembles the body text as follows:

- **Opening sentence:** states conformity status (`NONCONFORMING` or `LEGAL_NONCONFORMING`) and zoning code.
- **Per failing/warn dimension:** one line describing the dimension label, the actual value, and the permitted value.
- **Per encumbrance:** one line with the document ID, grantee, type, and any key terms (perpetual, ROFR, expiration).
- **Closing instruction:** `Confirm legal nonconforming status via [municipality] L&I records before closing.` Always present; municipality pulled from `deal.address.city`.

---

## 5. Jinja2 HTML/CSS Template Specification

### 5.1 Conformity Status Badge

Three CSS classes correspond to three conformity statuses. The badge is a flex container with a colored dot and a text label.

| Status Value | CSS Class | Background | Border | Dot Color | Text Color |
|---|---|---|---|---|---|
| `CONFORMING` | `.badge-conforming` | `#EAF3DE` | `#97C459` | `#639922` | `#3B6D11` |
| `NONCONFORMING` | `.badge-nonconforming` | `#FCEBEB` | `#F09595` | `#E24B4A` | `#791F1F` |
| `LEGAL_NONCONFORMING` | `.badge-legal` | `#FAEEDA` | `#EF9F27` | `#BA7517` | `#633806` |
| `ASSESSMENT_PENDING` | `.badge-pending` | `var(--color-background-secondary)` | `var(--color-border-tertiary)` | `var(--color-text-secondary)` | `var(--color-text-secondary)` |

---

### 5.2 Dimension Grid

The dimension grid is a 3-column CSS grid of cards. Each card has a 10px uppercase label and a 12px value. Values carry one of three CSS color classes: `.pass` (green `#3B6D11`), `.fail` (red `#A32D2D`), `.warn` (amber `#854F0B`). The grid is omitted entirely when `conformity.dimensions` is an empty list.

---

### 5.3 Scenario Cards

Scenario cards are a flex row (1–3 cards). Each card contains: rank badge, pathway pill, scenario name, description, and a 4-cell metrics grid. The preferred scenario card has a **1.5px Sage Deep (`#4A6E50`) border** and a `Preferred` label tab at top right. Non-preferred cards use a standard 0.5px border.

| Pathway Type Value | Pill CSS Class | Background | Color |
|---|---|---|---|
| `by_right` | `.pathway-byright` | `#EAF3DE` | `#3B6D11` |
| `special_exception` | `.pathway-special` | `#E6F1FB` | `#185FA5` |
| `variance` | `.pathway-variance` | `#FAEEDA` | `#633806` |
| `as_submitted` | `.pathway-submitted` | `var(--color-background-secondary)` | `var(--color-text-secondary)` |

---

### 5.4 HBU Synthesis Block

The HBU synthesis block is a gray-background panel (`var(--color-background-secondary)`) below the scenario cards. It contains:

1. A 10px uppercase label: `Highest & Best Use — Cross-Scenario Synthesis`
2. The `cross_scenario_recommendation` paragraph in 12px body text
3. The preferred scenario callout in Sage Deep (`#4A6E50`)
4. The use flexibility score bar

The score bar is a flex row: label on the left, a 4px height progress bar in the middle (fill color `#4A6E50`), and a `7.2 / 10 — High` value on the right. Fill width = `(score / 10 * 100)%`. Score thresholds: Low = < 4, Medium = 4–7, High = > 7.

---

### 5.5 Zoning Nonconformity DD Flag

The DD flag is a flex row with a red circle icon and body text.

- **Background:** `#FCEBEB`
- **Border:** `0.5px solid #F09595`
- **Border-radius:** `6px`
- **Padding:** `10px 12px`
- **Icon:** Red circle (`#E24B4A`) with white `!` at 10px font size
- **Title:** `font-weight: 500`, `font-size: 11px`, `color: #791F1F`
- **Body text:** `font-size: 11px`, `line-height: 1.5`, `color: #791F1F`

---

## 6. Checkpoint Structure — CP1 through CP4

Session 5 follows the same 4-checkpoint pattern as Sessions 3 and 4. The agent halts at each checkpoint for verification before proceeding.

---

### Checkpoint 1 — Context Builder Update

**Scope:** Update `build_context()` in `context_builder.py` only. No template changes yet.

- Add 10 of the 11 new context keys specified in Section 3. The 11th key (`zoning_nonconformity_flag`) is deferred to CP2 because it depends on `dd_flag_engine.get_zoning_flag()`, which is a CP2 deliverable.
- Write a unit test function `test_context_keys(deal_b_fixture)` that asserts all 10 keys are present when Deal B (Belmont) is passed.
- Run the test. All 10 keys must be present before CP1 is declared done.
- **CP1 gate:** `python -m pytest tests/test_context_s5.py -v` → all pass, no `KeyError`.

---

### Checkpoint 2 — DD Flag Engine Update

**Scope:** Add `get_zoning_flag()` and `_build_nonconformity_body()` to `dd_flag_engine.py`, then add the 11th context key (`zoning_nonconformity_flag`) to `build_context()` in `context_builder.py` (deferred from CP1 — see CP1 note above). No template changes yet.

- CONFORMING deal → returns `None`.
- NONCONFORMING deal (Belmont fixture) → returns `DDFlag` with `severity=HIGH`, title matching Section 4.1, body containing at least one failing dimension.
- LEGAL_NONCONFORMING deal → returns `DDFlag` with `severity=HIGH`.
- `zoning_nonconformity_flag` context key now populated; `test_context_keys` extended to assert all 11 keys.
- **CP2 gate:** `python -m pytest tests/test_dd_flag_s5.py tests/test_context_s5.py -v` → all pass.

---

### Checkpoint 3 — Jinja2 Template Update

**Scope:** Update `templates/report_template.html` — replace §8 and §9 flat blocks with the new components. Do not touch any other section.

- Implement all five components in §8: badge, dimension grid, scenario cards, HBU block, DD flag.
- Implement the three §9 elements: HBU narrative paragraph, preferred scenario callout, use flexibility statement.
- All CSS follows the brand system specified in Section 5. No hardcoded colors outside the approved palette.
- **CP3 gate:** Run the pipeline on Deal B (Belmont). Open the output PDF. Confirm: badge reads `NONCONFORMING — existing use`, 2 scenario cards visible, preferred card has Sage Deep border, HBU synthesis block present, DD flag present.

---

### Checkpoint 4 — Full Smoke Test (Both Reference Deals)

**Scope:** Run both Deal B (Belmont) and Deal C (Indian Queen) through the full pipeline end-to-end.

- **Belmont:** 2 scenario cards, NONCONFORMING badge, DD flag present.
- **Indian Queen:** 3 scenario cards, NONCONFORMING badge, preferred card is Scheme C (95 units), encumbrances in DD flag body text.
- No Python errors, no `Jinja2 UndefinedError`, no `KeyError` in either run.
- PDF page count within ±3 pages of pre-Session-5 baseline.
- **CP4 gate:** Both PDFs open without error. Human review of §8 and §9 in both PDFs confirms correct rendering.

---

## 7. Gate Criteria — Session 5 Complete When All Pass

| # | Gate Item | Verification Method |
|---|---|---|
| 1 | `context_builder.py` imports cleanly | `python -m py_compile context_builder.py` |
| 2 | `dd_flag_engine.py` imports cleanly | `python -m py_compile dd_flag_engine.py` |
| 3 | `templates/report_template.html` contains `{{ conformity.status }}` | `grep '{{ conformity.status }}' templates/report_template.html` |
| 4 | `templates/report_template.html` contains `{% for scenario in scenarios %}` | `grep '{% for scenario in scenarios %}' templates/report_template.html` |
| 5 | `templates/report_template.html` contains `{{ zoning_ext.cross_scenario_recommendation }}` | `grep` for tag in report.html |
| 6 | All 4 pathway CSS classes present: `pathway-byright`, `pathway-special`, `pathway-variance`, `pathway-submitted` | `grep` for each class name in report.html |
| 7 | All 3 conformity badge CSS classes present: `badge-conforming`, `badge-nonconforming`, `badge-legal` | `grep` for each class name |
| 8 | `get_zoning_flag(deal)` returns `None` for a CONFORMING deal | Unit test assertion |
| 9 | `get_zoning_flag(deal)` returns `DDFlag` with `severity=HIGH` for Deal B | Unit test assertion |
| 10 | Deal B (Belmont) pipeline run produces a PDF without error | Run pipeline; assert PDF file size > 100KB |
| 11 | Deal C (Indian Queen) pipeline run produces a PDF without error | Run pipeline; assert PDF file size > 100KB |
| 12 | Deal B PDF §8 contains the text `Nonconforming` | PyMuPDF text extraction; assert in page text |
| 13 | Deal C PDF §8 contains at least 3 scenario card blocks | PyMuPDF; count occurrences of scenario rank marker |
| 14 | `git diff --stat` shows changes ONLY in `context_builder.py`, `templates/report_template.html`, `dd_flag_engine.py`, and `tests/` (test_context_s5.py + test_dd_flag_s5.py + smoke_test_s5.py) | `git diff --stat HEAD` |
| 15 | Commit message matches the format in Section 8 | `git log -1 --format=%B HEAD` |

---

## 8. Commit & Tag Plan

### Commit 1 — Code Commit

**Files staged:** `context_builder.py`, `templates/report_template.html`, `dd_flag_engine.py`, `tests/smoke_test_s5.py`, `tests/test_context_s5.py`, `tests/test_dd_flag_s5.py`

```
Session 5: report_builder + template — Zoning overhaul rendering

- Update build_context() in context_builder.py — add 11 new context keys for conformity, scenarios, zoning_ext (10 in CP1 + zoning_nonconformity_flag in CP2)
- Update templates/report_template.html §8 — replace flat HBU block with:
    conformity status badge (CONFORMING/NONCONFORMING/LEGAL_NONCONFORMING/ASSESSMENT_PENDING)
    dimension grid (6-cell, pass/fail/warn per dimension)
    1–3 scenario cards (preferred card flagged with Sage Deep border)
    HBU synthesis block (cross_scenario_recommendation + preferred callout + flex score bar)
    zoning nonconformity DD flag (auto-triggered by dd_flag_engine)
- Update templates/report_template.html §9 — replace flat narrative with
    cross_scenario_recommendation, preferred scenario callout, use_flexibility_score statement
- Add get_zoning_flag() to dd_flag_engine — triggers on status != CONFORMING
- Add smoke tests for Deal B (Belmont) and Deal C (Indian Queen)
- Smoke tests confirm: PDF renders, §8 contains conformity text, 3 cards for Indian Queen

Completes the DealDesk Zoning Analysis Overhaul (Sessions 1–5).
```

**Tag:** `zoning-overhaul-session-5-passed`

---

### Commit 2 — Docs Commit

Append Session 5 history-log entry to `DealDesk_Zoning_Overhaul_Plan.md`. Sync status table: Session 5 `READY → COMPLETED`. Add `Overhaul complete` notation. No tag on docs commit.

---

## 9. Reference Deal Fixtures for Session 5

Both fixtures already exist in `tests/fixtures/` from Session 2. Session 5 uses them as smoke test inputs. **Do not modify these fixtures — they are read-only inputs.**

### 9.1 Deal B — Belmont Apartments

| Field | Value |
|---|---|
| File | `tests/fixtures/zoning_overhaul_session_3_fixture_belmont.json` |
| Address | 2217 N 51st St, Philadelphia, PA 19131 |
| Asset type | `multifamily` |
| Strategy | `stabilized_hold` |
| Units | 36 |
| Zoning code | RSD-3 |
| Conformity status | NONCONFORMING — existing 36-unit MF not permitted by-right in RSD-3 |
| Expected scenarios | 2 — (1) stabilized hold as legal nonconforming, (2) conversion/variance |
| Expected DD flag | Yes — zoning nonconformity HIGH severity |

### 9.2 Deal C — 3520 Indian Queen Lane

| Field | Value |
|---|---|
| File | `tests/fixtures/zoning_overhaul_session_3_fixture_indian_queen.json` |
| Address | 3520 Indian Queen Ln, Philadelphia, PA 19129 |
| Asset type | `industrial` |
| Strategy | `opportunistic` |
| Building SF | 42,420 SF |
| Lot SF | 68,389 SF |
| Zoning code | RSA-1 / RSA-5 split |
| Encumbrances | American Tower (Doc #53136944, perpetual, 2,625 SF), SBC Tower Holdings (Doc #52842941, exp. 2064, ROFR) |
| Conformity status | NONCONFORMING — industrial use in residential zone |
| Expected scenarios | 3 — Scheme C 95 units (preferred), Scheme A 88 units, Scheme B 84 units mixed-use |
| Expected DD flag | Yes — nonconformity + both encumbrances listed in body text |

---

## 10. Carry-Forwards from Sessions 3 & 4

Three carry-forward items from prior sessions remain unresolved. Session 5 does not action them — they are post-overhaul cleanup.

| CF # | Item | From Session | Status | Action |
|---|---|---|---|---|
| CF-1 | Catalog v5 SCEN/HBU minor text re-sync — prompt text in catalog v5 vs. what was actually wired in `market.py` has minor differences | 3 | Open | Post-overhaul docs patch. One commit, no code changes. |
| CF-2 | Spec §5.4 fallback text re-sync — `Session_2_Prompt_Specification.md` §5.4 description does not perfectly match the fallback constructors as implemented | 3 | Open | Post-overhaul docs patch. Same commit as CF-1. |
| CF-3 | PDF naming convention reference in master plan D7 — placeholder hash used before Session 4 resolved it | 4 | **Closed by Session 5 (D-PDF-1)** | No action needed. |

---

## 11. What Comes After Session 5

### 11.1 Overhaul Complete

When Session 5 is tagged `zoning-overhaul-session-5-passed`, the DealDesk Zoning Analysis Overhaul is complete. The pipeline will then produce, for any deal submitted:

- A structured `ConformityAssessment` with per-dimension pass/fail/warn status
- 1–3 ranked `DevelopmentScenarios` with zoning pathways and entitlement risk ratings
- A cross-scenario HBU synthesis and use flexibility score
- Per-scenario Excel financial models (from Session 4)
- A single PDF report with the new §8/§9 components rendering all of the above
- An auto-triggered DD flag whenever zoning nonconformity is detected

### 11.2 Post-Overhaul Backlog

| Item | Description | Priority |
|---|---|---|
| Multi-tenant SaaS architecture | White-label branding, per-tenant API keys, deal history storage | Future |
| Interactive HTML report output | Browser-based companion to the PDF with expandable scenario cards | Future |
| FRED / Census API 400 errors | Fix remaining external API 400 responses identified in project memory | High — next after overhaul |
| CF-1 and CF-2 docs patches | Minor catalog v5 / spec §5.4 re-sync commits | Low — cosmetic |
| Municipal registry expansion | Continue expanding `municipal_registry.csv` beyond 10 states | Medium |

---

## Document Control

This kickoff sheet was produced on April 29, 2026 in the claude.ai Session 5 prep session. It supersedes any prior Session 5 discussion. All decisions recorded here are final unless explicitly revised in a subsequent review with Mike Freedman before the Claude Code agent is launched. The agent reads this document at session start — do not modify it after launch.
