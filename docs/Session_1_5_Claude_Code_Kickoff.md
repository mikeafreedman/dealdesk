# Session 1.5 — Claude Code Kickoff Instructions

**Purpose:** Schema additions surfaced by Session 2 (claude.ai prompt design). Add two new Pydantic models to `models.py`. No prompts are wired in this session. Pure schema work.

**Estimated effort:** 30–45 minutes including testing and commit.
**Risk level:** LOW (pure additions, no modifications to existing fields).

---

## Step 0 — Open a new Claude Code session in `C:\Users\mikea\dealdesk\docs`

Make sure your working directory is the DealDesk repo root. Confirm git status is clean before starting (commit or stash any pending work).

```powershell
cd C:\Users\mikea\dealdesk
git status
```

If anything is uncommitted, deal with it first.

---

## Step 1 — Paste this opening message to Claude Code

Copy everything between the `===BEGIN===` and `===END===` markers below into Claude Code as your first message.

===BEGIN===

I'm starting Session 1.5 of the DealDesk Zoning Overhaul. This is a small schema-only session that bridges Session 1 (already complete) and Session 3 (prompt wiring).

Before you write any code, please read these files in this exact order:

1. `DealDesk_Zoning_Overhaul_Plan.md` — the master plan with all locked design decisions
2. `Session_1_Schema_Design.md` — the schema spec already implemented in models.py
3. `Session_2_Prompt_Specification.md` — Section 8 ("Schema gaps surfaced by Session 2") is the source of truth for this session
4. `FINAL_APPROVED_Prompt_Catalog_v5.md` — Section 5 ("Schema dependencies") confirms the same two additions

After you've read all four, summarize back to me:
- What two models you're adding
- Where in models.py each goes (which section header, what import order)
- What tests you'll run before declaring done
- What the commit message will be

Do NOT start coding until I confirm your plan.

===END===

---

## Step 2 — What Claude Code should do (your reference, not its instructions)

The session has three concrete deliverables. Use this as your checklist when reviewing Claude Code's plan.

### 2.1 Add `WorkflowControls` model to `models.py`

Location: in the "ZONING OVERHAUL — Session 1 additions (April 2026)" section. Place AFTER the `ZoningExtensions` model and BEFORE the `DealData` extensions.

```python
class WorkflowControls(BaseModel):
    """
    User-facing controls that shape the zoning synthesis chain output.
    Read by Prompt 3C-SCEN to constrain scenario generation.

    Defaults preserve current pipeline behavior (multi-scenario, no strategy
    lock, max 3 scenarios). Frontend is expected to expose these controls
    as optional toggles in a future iteration.
    """
    single_scenario_mode: bool = Field(
        default=False,
        description="If True, 3C-SCEN returns exactly 1 PREFERRED scenario."
    )
    strategy_lock: Optional[InvestmentStrategy] = Field(
        default=None,
        description="If set, every generated scenario must use this strategy."
    )
    max_scenarios: int = Field(
        default=3,
        ge=1,
        le=3,
        description="Hard cap on number of scenarios returned by 3C-SCEN."
    )
```

Then add to `DealData`:
```python
workflow_controls: WorkflowControls = Field(default_factory=WorkflowControls)
```

### 2.2 Add `EncumbranceType` enum and `Encumbrance` model

Location: top of the new enums block in the "ZONING OVERHAUL" section (alphabetical with the existing enums, so `EncumbranceType` goes between `ConformityStatus` and `NonconformityType`).

```python
class EncumbranceType(str, Enum):
    """
    Type of recorded title encumbrance affecting the parcel. Materially
    affects buildable area, scenario generation, and risk analysis.
    """
    EASEMENT = "EASEMENT"
    LEASE = "LEASE"
    LEASE_TO_EASEMENT = "LEASE_TO_EASEMENT"  # original lease later converted to easement
    ROW = "ROW"
    DEED_RESTRICTION = "DEED_RESTRICTION"
    OTHER = "OTHER"
```

Then add the `Encumbrance` sub-model in the sub-models block:

```python
class Encumbrance(BaseModel):
    """
    A single recorded title encumbrance burdening the parcel.
    Sourced from title commitment, recorded documents, or survey notes.
    """
    type: EncumbranceType
    doc_id: Optional[str] = Field(default=None, description="Recorded document ID")
    grantee: Optional[str] = None
    grantor: Optional[str] = None
    description: Optional[str] = None
    exclusive_area_sf: Optional[float] = Field(
        default=None,
        description="SF of land withdrawn from buildable area (e.g., exclusive easement area)."
    )
    access_easement_width_ft: Optional[float] = Field(
        default=None,
        description="Width of any companion access/utility easement, in feet."
    )
    term: Optional[str] = Field(
        default=None,
        description="Free-text term description (e.g., 'perpetual', '50 years')."
    )
    expiration: Optional[date] = None
    right_of_first_refusal: bool = False
    annual_income_usd: Optional[float] = Field(
        default=None,
        description="If encumbrance generates revenue (e.g., cell tower lease)."
    )
    notes: Optional[str] = None
```

Then add to `DealData`:
```python
encumbrances: List[Encumbrance] = Field(default_factory=list)
```

### 2.3 Test, commit, tag

Run the existing models.py test fixtures to confirm:
- New models import cleanly
- Existing test fixtures from Session 1 still deserialize (defaults must work)
- A new fixture with populated `workflow_controls` and `encumbrances` deserializes correctly
- `EncumbranceType` enum round-trips through JSON serialization

Commit message:
```
Session 1.5: Schema (models.py) — Add WorkflowControls + Encumbrance

- Add WorkflowControls model (single_scenario_mode, strategy_lock, max_scenarios)
- Add EncumbranceType enum (6 values)
- Add Encumbrance sub-model with 11 typed fields
- Add workflow_controls and encumbrances fields to DealData
- Pure additive schema change; no existing field modified
- Surfaced by Session 2 prompt design; required before Session 3 wiring
```

Tag the commit:
```
git tag zoning-overhaul-session-1-5-passed
```

Update master plan with completion timestamp.

---

## Step 3 — Gate criteria for Session 1.5 (verify before declaring done)

- [ ] `models.py` imports cleanly with no syntax or type errors
- [ ] All Session 1 test fixtures still deserialize (backward compatibility preserved)
- [ ] New fixture exercising `workflow_controls={"single_scenario_mode": true}` deserializes
- [ ] New fixture exercising `encumbrances=[{...}]` deserializes (use Indian Queen American Tower easement as test data)
- [ ] `EncumbranceType` enum values round-trip through `.json()` and back via `.parse_raw()`
- [ ] `git diff` shows changes ONLY in `models.py` and the new fixture file (no scope creep)
- [ ] Commit message matches the format above
- [ ] Repo tagged `zoning-overhaul-session-1-5-passed`
- [ ] Master plan document updated: Session 1.5 status set to COMPLETED with timestamp

---

## Step 4 — When Session 1.5 is done, you're ready for Session 3

Session 3 is the wiring session — the actual implementation of `_SYSTEM_3C_CONF`, `_USER_3C_CONF`, `_apply_3c_conf`, and the equivalent triples for 3C-SCEN and 3C-HBU, plus the `run_zoning_synthesis_chain` orchestrator. The next Claude Code session reads:

1. `DealDesk_Zoning_Overhaul_Plan.md` (master plan)
2. `Session_1_Schema_Design.md` (Session 1 schema, already implemented)
3. **`Session_2_Prompt_Specification.md`** (your implementation source of truth)
4. **`FINAL_APPROVED_Prompt_Catalog_v5.md`** (the approved prompt text)

You'll start Session 3 with a similar kickoff message — I'll prepare that sheet when you're ready.

---

*End of Session 1.5 instructions.*
