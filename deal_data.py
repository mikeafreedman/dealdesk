"""
deal_data.py — Deal Data Assembly Module
=========================================
Master merge step: combines extracted document data (extractor.py output)
with user inputs from the Streamlit frontend into one validated DealData object.

Merge rules:
    1. User inputs ALWAYS win over extracted values (explicit > inferred).
    2. Extracted values backfill any field the user left blank/null/zero.
    3. Enum fields are validated with graceful fallback to defaults.
    4. Provenance is logged: every backfilled field records its source.

Called by main.py after extractor.py, before market.py.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List

from models.models import (
    AssetType,
    CompsData,
    DealData,
    ExtractedDocumentData,
    FinancialAssumptions,
    InvestmentStrategy,
    NotificationConfig,
    PropertyAddress,
    RefiEvent,
    SectionsConfig,
    WaterfallType,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# ENUM RESOLUTION
# ═══════════════════════════════════════════════════════════════════════════

# Fuzzy aliases → canonical enum values
_ASSET_TYPE_ALIASES: Dict[str, AssetType] = {
    "multifamily":   AssetType.MULTIFAMILY,
    "multi-family":  AssetType.MULTIFAMILY,
    "multi family":  AssetType.MULTIFAMILY,
    "apartment":     AssetType.MULTIFAMILY,
    "mixed-use":     AssetType.MIXED_USE,
    "mixed use":     AssetType.MIXED_USE,
    "retail":        AssetType.RETAIL,
    "office":        AssetType.OFFICE,
    "industrial":    AssetType.INDUSTRIAL,
    "warehouse":     AssetType.INDUSTRIAL,
    "single-family": AssetType.SINGLE_FAMILY,
    "single family": AssetType.SINGLE_FAMILY,
    "sfr":           AssetType.SINGLE_FAMILY,
}

_STRATEGY_ALIASES: Dict[str, InvestmentStrategy] = {
    "stabilized":      InvestmentStrategy.STABILIZED_HOLD,
    "stabilized_hold": InvestmentStrategy.STABILIZED_HOLD,
    "buy and hold":    InvestmentStrategy.STABILIZED_HOLD,
    "buy & hold":      InvestmentStrategy.STABILIZED_HOLD,
    "hold":            InvestmentStrategy.STABILIZED_HOLD,
    "value_add":       InvestmentStrategy.VALUE_ADD,
    "value-add":       InvestmentStrategy.VALUE_ADD,
    "value add":       InvestmentStrategy.VALUE_ADD,
    "renovation":      InvestmentStrategy.VALUE_ADD,
    "ground-up":       InvestmentStrategy.VALUE_ADD,
    "adaptive reuse":  InvestmentStrategy.VALUE_ADD,
    "kd&r":            InvestmentStrategy.VALUE_ADD,
    "opportunistic":   InvestmentStrategy.OPPORTUNISTIC,
    "for_sale":        InvestmentStrategy.OPPORTUNISTIC,
    "for sale":        InvestmentStrategy.OPPORTUNISTIC,
    "flip":            InvestmentStrategy.OPPORTUNISTIC,
    "subdivision":     InvestmentStrategy.OPPORTUNISTIC,
}


def _resolve_asset_type(raw: Any, default: AssetType = AssetType.MULTIFAMILY) -> AssetType:
    """Resolve a raw string to a canonical AssetType enum, or return default."""
    if isinstance(raw, AssetType):
        return raw
    if not raw:
        return default
    key = str(raw).strip().lower()
    # Try direct enum match first
    for member in AssetType:
        if key == member.value.lower():
            return member
    # Try aliases
    if key in _ASSET_TYPE_ALIASES:
        return _ASSET_TYPE_ALIASES[key]
    logger.warning("Unrecognized asset_type '%s' — defaulting to %s", raw, default.value)
    return default


def _resolve_strategy(raw: Any, default: InvestmentStrategy = InvestmentStrategy.STABILIZED_HOLD) -> InvestmentStrategy:
    """Resolve a raw string to a canonical InvestmentStrategy enum, or return default."""
    if isinstance(raw, InvestmentStrategy):
        return raw
    if not raw:
        return default
    key = str(raw).strip().lower()
    for member in InvestmentStrategy:
        if key == member.value.lower():
            return member
    if key in _STRATEGY_ALIASES:
        return _STRATEGY_ALIASES[key]
    logger.warning("Unrecognized investment_strategy '%s' — defaulting to %s", raw, default.value)
    return default


def _resolve_waterfall_type(raw: Any, default: WaterfallType = WaterfallType.FULL) -> WaterfallType:
    """Resolve a raw value to WaterfallType enum."""
    if isinstance(raw, WaterfallType):
        return raw
    if raw is None:
        return default
    try:
        return WaterfallType(int(raw))
    except (ValueError, TypeError):
        logger.warning("Unrecognized waterfall_type '%s' — defaulting to FULL", raw)
        return default


# ═══════════════════════════════════════════════════════════════════════════
# FIELD-LEVEL MERGE HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _is_blank(value: Any) -> bool:
    """True if the value is considered 'not provided' by the user."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, (int, float)) and value == 0:
        return True
    return False


def _pick(user_val: Any, extracted_val: Any, field_name: str, provenance: Dict[str, str]) -> Any:
    """
    Return the winning value. User input wins if present; otherwise fall back
    to extracted value and log it in provenance.
    """
    if not _is_blank(user_val):
        return user_val
    if not _is_blank(extracted_val):
        provenance[field_name] = "extracted"
        return extracted_val
    return user_val  # both blank — return user's blank


# ═══════════════════════════════════════════════════════════════════════════
# ADDRESS MERGE
# ═══════════════════════════════════════════════════════════════════════════

def _merge_address(address: PropertyAddress, user_inputs: Dict[str, Any],
                   extracted: ExtractedDocumentData, provenance: Dict[str, str]) -> None:
    """Merge address fields: user inputs > existing address > extracted OM data."""
    # full_address is the primary input from the New Underwrite screen
    user_addr = user_inputs.get("full_address") or user_inputs.get("address", "")
    if not _is_blank(user_addr):
        address.full_address = str(user_addr).strip()

    # Component fields backfill from user_inputs dict
    for field in ("street", "city", "state", "zip_code"):
        user_val = user_inputs.get(field, "")
        if not _is_blank(user_val):
            setattr(address, field, str(user_val).strip())

    # Build full_address from components if still blank
    if _is_blank(address.full_address):
        parts = [address.street, address.city, address.state, address.zip_code]
        composed = ", ".join(p for p in parts if p)
        if composed:
            address.full_address = composed
            provenance["full_address"] = "composed_from_components"


# ═══════════════════════════════════════════════════════════════════════════
# ASSUMPTIONS MERGE
# ═══════════════════════════════════════════════════════════════════════════

# Fields on FinancialAssumptions that can be backfilled from extracted docs
_BACKFILL_MAP: List[tuple] = [
    # (assumptions_field, extracted_field, transform)
    ("num_units",  "num_units_extracted",  None),
    ("num_units",  "total_units_from_rr",  None),   # second source — rent roll
    ("gba_sf",     "gba_sf_extracted",     None),
    ("lot_sf",     "lot_sf_extracted",     None),
    ("year_built", "year_built_extracted",  None),
    ("vacancy_rate", "occupancy_rate",     lambda occ: round(1.0 - occ, 4) if occ and occ <= 1.0 else None),
    ("cam_reimbursements", "cam_reimbursements_t12", None),
]

# Direct user-input fields on FinancialAssumptions (no extraction source)
_ASSUMPTION_FIELDS = list(FinancialAssumptions.model_fields.keys())

# ── 3-tier cost defaults ────────────────────────────────────────────────
# For each dollar-value cost field:
#   Tier 1: user override (already set by _build_deal from form)
#   Tier 2: extracted value (via _COST_EXTRACTION_MAP below)
#   Tier 3: percentage-based fallback (base_field, pct)
#            None means zero_default — field must come from input/extraction
#
# Format: (assumptions_field, extracted_field_or_None, (base_field, pct) or None)
_COST_DEFAULTS: List[tuple] = [
    # Hard costs: must come from input or extraction, no % default
    ("const_hard",          "construction_hard_costs_extracted", None),
    ("renovations_yr1",     "renovation_cost_extracted",        None),
    # Soft / closing costs: % of purchase_price fallback
    ("closing_costs_fixed", "closing_costs_extracted",          ("purchase_price", 0.02)),
    ("const_reserve",       None,                               ("const_hard", 0.05)),
    ("acq_fee_fixed",       None,                               ("purchase_price", 0.015)),
]


def _merge_assumptions(assumptions: FinancialAssumptions, user_inputs: Dict[str, Any],
                       extracted: ExtractedDocumentData, provenance: Dict[str, str]) -> None:
    """
    Merge user-provided assumption overrides and backfill from extracted data.

    Priority: user_inputs > extracted_docs > existing defaults.
    """
    # Step 1: Apply all user-provided assumption values
    for field_name in _ASSUMPTION_FIELDS:
        if field_name in user_inputs:
            raw_val = user_inputs[field_name]
            if not _is_blank(raw_val):
                current_type = type(getattr(assumptions, field_name))
                try:
                    if current_type == float:
                        setattr(assumptions, field_name, float(raw_val))
                    elif current_type == int:
                        setattr(assumptions, field_name, int(raw_val))
                    else:
                        setattr(assumptions, field_name, raw_val)
                except (ValueError, TypeError):
                    logger.warning("Could not cast user input for %s=%r", field_name, raw_val)

    # Step 2: Handle special nested objects from user inputs
    # Refi events
    if "refi_events" in user_inputs and isinstance(user_inputs["refi_events"], list):
        events = []
        for re_dict in user_inputs["refi_events"][:3]:
            if isinstance(re_dict, dict):
                events.append(RefiEvent(**re_dict))
            elif isinstance(re_dict, RefiEvent):
                events.append(re_dict)
        if events:
            assumptions.refi_events = events

    # Waterfall type
    if "waterfall_type" in user_inputs:
        assumptions.waterfall_type = _resolve_waterfall_type(user_inputs["waterfall_type"])

    # Step 3: Backfill from extracted document data where assumptions are still at default/zero
    for assume_field, extract_field, transform in _BACKFILL_MAP:
        current_val = getattr(assumptions, assume_field, None)
        if not _is_blank(current_val):
            continue  # user or prior step already set it
        extracted_val = getattr(extracted, extract_field, None)
        if _is_blank(extracted_val):
            continue
        if transform:
            extracted_val = transform(extracted_val)
            if _is_blank(extracted_val):
                continue
        try:
            setattr(assumptions, assume_field, extracted_val)
            provenance[assume_field] = f"extracted:{extract_field}"
            logger.info("Backfilled assumptions.%s from extracted_docs.%s", assume_field, extract_field)
        except (ValueError, TypeError) as exc:
            logger.warning("Backfill failed for %s: %s", assume_field, exc)

    # Step 4: If purchase_price is still 0, try asking_price from OM
    if _is_blank(assumptions.purchase_price) and not _is_blank(extracted.asking_price):
        assumptions.purchase_price = extracted.asking_price
        provenance["purchase_price"] = "extracted:asking_price"
        logger.info("Backfilled purchase_price from extracted asking_price")

    # Step 5: 3-tier cost defaults — Tier 1 (user) already applied above,
    #         now try Tier 2 (extraction) then Tier 3 (% default).
    #         NEVER sum two sources — one input, one output.
    for field, ext_field, pct_rule in _COST_DEFAULTS:
        current = getattr(assumptions, field, 0.0)

        # Tier 1: user already set a non-zero value
        if not _is_blank(current):
            source = "user_override"
            logger.info("ASSUMPTIONS %s source=%s value=%s", field, source, current)
            continue

        # Tier 2: extraction
        if ext_field:
            ext_val = getattr(extracted, ext_field, None)
            if not _is_blank(ext_val):
                setattr(assumptions, field, float(ext_val))
                provenance[field] = f"extracted:{ext_field}"
                logger.info("ASSUMPTIONS %s source=%s value=%s",
                            field, "extracted", ext_val)
                continue

        # Tier 3: percentage-based fallback
        if pct_rule:
            base_field, pct = pct_rule
            base_val = getattr(assumptions, base_field, 0.0)
            if base_val and base_val > 0:
                computed = round(base_val * pct, 2)
                setattr(assumptions, field, computed)
                provenance[field] = f"pct_default:{base_field}*{pct}"
                logger.info("ASSUMPTIONS %s source=%s value=%s",
                            field, "pct_default", computed)
                continue

        # No value from any tier
        logger.info("ASSUMPTIONS %s source=%s value=%s",
                    field, "zero_default", 0.0)


# ═══════════════════════════════════════════════════════════════════════════
# EXPENSE BACKFILL FROM T-12
# ═══════════════════════════════════════════════════════════════════════════

# Map from T-12 expense_line_items keys → FinancialAssumptions fields.
# Keys are normalized (lowercased, underscored) before lookup. Covers the
# ~40 most-common T-12 chart-of-accounts line names so Haiku's
# snake_case-every-line output maps cleanly into the assumptions model.
_EXPENSE_MAP: Dict[str, str] = {
    # Real estate taxes
    "real_estate_taxes":       "re_taxes",
    "real_estate_tax":         "re_taxes",
    "re_taxes":                "re_taxes",
    "property_taxes":          "re_taxes",
    "property_tax":            "re_taxes",
    "taxes":                   "re_taxes",
    "re_tax":                  "re_taxes",
    # Insurance
    "insurance":               "insurance",
    "property_insurance":      "insurance",
    "hazard_insurance":        "insurance",
    "liability_insurance":     "insurance",
    "insurance_expense":       "insurance",
    # Utilities — gas. `utilities_*` prefix covers Haiku's common
    # snake_case output when the OM uses a "Utilities:" grouping header.
    "gas":                     "gas",
    "natural_gas":             "gas",
    "heating":                 "gas",
    "utilities_gas":           "gas",
    "utility_gas":             "gas",
    # Utilities — water / sewer
    "water_sewer":             "water_sewer",
    "water_and_sewer":         "water_sewer",
    "water":                   "water_sewer",
    "sewer":                   "water_sewer",
    "water_sewer_trash":       "water_sewer",
    "utilities_water":         "water_sewer",
    "utilities_water_sewer":   "water_sewer",
    "utilities_sewer":         "water_sewer",
    "utility_water":           "water_sewer",
    # Utilities — electric
    "electric":                "electric",
    "electricity":             "electric",
    "power":                   "electric",
    "common_area_electric":    "electric",
    "utilities_electric":      "electric",
    "utilities_electricity":   "electric",
    "utility_electric":        "electric",
    # Generic "utilities" catch-all — if the T-12 rolls all utilities
    # into one line, map to electric so at least it shows up (better
    # than being silently unmapped). Split lines still override.
    "utilities":               "electric",
    # Trash
    "trash":                   "trash",
    "trash_removal":           "trash",
    "garbage":                 "trash",
    "waste_removal":           "trash",
    "refuse":                  "trash",
    # Repairs & maintenance
    "repairs":                 "repairs",
    "repairs_maintenance":     "repairs",
    "repairs_and_maintenance": "repairs",
    "maintenance":             "repairs",
    "rm":                      "repairs",
    "r_and_m":                 "repairs",
    "rm_hvac":                 "repairs",
    "rm_plumbing":              "repairs",
    "rm_electrical":            "repairs",
    "building_repairs":        "repairs",
    "general_repairs":         "repairs",
    # Cleaning / janitorial
    "cleaning":                "cleaning",
    "janitorial":              "cleaning",
    "custodial":               "cleaning",
    "common_area_cleaning":    "cleaning",
    # Landscaping / snow
    "landscaping":             "landscape_snow",
    "landscape":               "landscape_snow",
    "landscape_snow":          "landscape_snow",
    "grounds":                 "landscape_snow",
    "grounds_maintenance":     "landscape_snow",
    "snow_removal":            "landscape_snow",
    "snow":                    "landscape_snow",
    "lawn_care":               "landscape_snow",
    # Advertising / marketing
    "advertising":             "advertising",
    "marketing":               "advertising",
    "advertising_marketing":   "advertising",
    "leasing_marketing":       "advertising",
    # Salaries / payroll
    "salaries":                "salaries",
    "payroll":                 "salaries",
    "wages":                   "salaries",
    "staff_payroll":           "salaries",
    "on_site_payroll":         "salaries",
    "salaries_wages":          "salaries",
    "employee_salaries":       "salaries",
    # Admin / legal / accounting
    "admin_legal_acct":        "admin_legal_acct",
    "administrative":          "admin_legal_acct",
    "admin":                   "admin_legal_acct",
    "legal":                   "admin_legal_acct",
    "accounting":              "admin_legal_acct",
    "legal_professional":      "admin_legal_acct",
    "professional_fees":       "admin_legal_acct",
    "audit":                   "admin_legal_acct",
    "bank_fees":               "admin_legal_acct",
    "bookkeeping":             "admin_legal_acct",
    # Pest control
    "exterminator":            "exterminator",
    "pest_control":            "exterminator",
    "pest":                    "exterminator",
    # Office / phone / internet
    "office":                  "office_phone",
    "office_phone":            "office_phone",
    "office_expense":          "office_phone",
    "phone":                   "office_phone",
    "telephone":               "office_phone",
    "internet":                "office_phone",
    "communications":          "office_phone",
    "office_supplies":         "office_phone",
    # Licenses / inspections / permits
    "license_inspections":     "license_inspections",
    "licenses":                "license_inspections",
    "licenses_and_permits":    "license_inspections",
    "permits":                 "license_inspections",
    "inspections":             "license_inspections",
    "regulatory_fees":         "license_inspections",
    # Turnover / make-ready
    "turnover":                "turnover",
    "make_ready":              "turnover",
    "unit_turnover":           "turnover",
    "make_readies":            "turnover",
}


def _normalize_expense_key(k: str) -> str:
    """Normalize a T-12 account key for robust lookup: lowercased,
    non-alphanumeric → underscore, de-duplicated underscores."""
    if not k:
        return ""
    s = re.sub(r"[^a-z0-9]+", "_", k.lower()).strip("_")
    s = re.sub(r"_+", "_", s)
    return s


def _backfill_expenses(assumptions: FinancialAssumptions, extracted: ExtractedDocumentData,
                       provenance: Dict[str, str]) -> None:
    """Populate Year-1 expense assumptions using this precedence:
        1. Extracted T-12 line item (WINS whenever it has a value)
        2. User input (fallback when extraction is absent / blank)

    Overrides any user-entered value with the extracted figure when the
    uploaded financials carry a usable number. When extraction returns
    nothing for a given line, the user-entered value is preserved.
    """
    if not extracted.expense_line_items:
        logger.info("EXPENSE BACKFILL: no T-12 line items extracted — "
                    "user-entered expense values retained")
        return
    unmapped = []
    overrides = 0
    fills = 0
    for t12_key, amount in extracted.expense_line_items.items():
        assume_field = _EXPENSE_MAP.get(_normalize_expense_key(t12_key))
        if not assume_field:
            unmapped.append(t12_key)
            continue
        if _is_blank(amount):
            continue
        try:
            new_val = float(amount)
        except (ValueError, TypeError):
            continue
        prior = getattr(assumptions, assume_field, None)
        prior_set = (prior is not None and prior != 0.0 and not _is_blank(prior))
        setattr(assumptions, assume_field, new_val)
        provenance[assume_field] = f"t12:{t12_key}"
        if prior_set and abs((prior or 0.0) - new_val) > 0.01:
            overrides += 1
            logger.info(
                "EXPENSE BACKFILL (extraction wins): %s $%.2f → $%.2f "
                "(user-entered value overridden by T-12 line '%s')",
                assume_field, float(prior or 0.0), new_val, t12_key,
            )
        else:
            fills += 1
            logger.info("EXPENSE BACKFILL: %s = $%.2f from T-12 line '%s'",
                        assume_field, new_val, t12_key)
    logger.info(
        "EXPENSE BACKFILL SUMMARY: %d fields filled from extraction, "
        "%d user values overridden by extraction, %d unmapped line items",
        fills, overrides, len(unmapped),
    )
    if unmapped:
        logger.info("T-12 BACKFILL: unmapped keys — %s", unmapped[:10])


def _apply_expense_defaults(assumptions: FinancialAssumptions,
                            provenance: Dict[str, str]) -> None:
    """Tier 3 of the expense cascade: when a field is still 0 AND was
    NOT populated by extraction, apply the rule-based default.

    Precedence is preserved:
       1. Extraction (T-12 line item)  — handled by _backfill_expenses
       2. User input (non-zero)        — never overwritten here
       3. Rule-based default           — this function

    Taxes + insurance are intentionally excluded — those have their
    own pipeline (parcel-assessed-value × local effective rate + TIV ×
    insurance rate with catastrophe loading) driven by public data.
    """
    n = int(assumptions.num_units or 0)

    # (field, default) pairs. Each `default` is a closure that computes
    # the fallback dollar amount when num_units is known. Fields whose
    # defaults don't scale with unit count use constants.
    PER_UNIT_MONTHLY = {
        "water_sewer": 75.0,   # $75 per unit per month
        "electric":    25.0,   # $25 per unit per month
        "repairs":    200.0,   # $200 per unit per month
    }
    PER_UNIT_YEARLY = {
        "advertising":  50.0,  # $50 per unit per year
    }
    FLAT_YEARLY = {
        "trash":             5000.0,
        "exterminator":      1200.0,
        "cleaning":          2400.0,   # $200/mo × 12
        "landscape_snow":    1000.0,
        "admin_legal_acct":  5000.0,
        "office_phone":       600.0,
        "miscellaneous":      500.0,
    }

    applied: list = []

    def _should_apply(field: str) -> bool:
        # Skip if extraction populated this field.
        if str(provenance.get(field, "") or "").startswith("t12:"):
            return False
        cur = getattr(assumptions, field, None)
        # Apply when user left it blank / zero.
        return cur in (None, 0, 0.0)

    # Per-unit monthly fields — require num_units > 0 to derive a total.
    for field, psu_monthly in PER_UNIT_MONTHLY.items():
        if not _should_apply(field):
            continue
        if n <= 0:
            # Without a unit count we can't back into a dollar total.
            # Leave at 0 so the Excel Assumptions tab flags it visually.
            continue
        val = round(psu_monthly * n * 12, 2)
        setattr(assumptions, field, val)
        provenance[field] = f"default:${psu_monthly:.0f}/unit/mo × {n} units"
        applied.append(f"{field}=${val:,.0f}")

    for field, psu_yearly in PER_UNIT_YEARLY.items():
        if not _should_apply(field):
            continue
        if n <= 0:
            continue
        val = round(psu_yearly * n, 2)
        setattr(assumptions, field, val)
        provenance[field] = f"default:${psu_yearly:.0f}/unit/yr × {n} units"
        applied.append(f"{field}=${val:,.0f}")

    # Flat yearly fields — apply regardless of unit count.
    for field, yearly in FLAT_YEARLY.items():
        if not _should_apply(field):
            continue
        setattr(assumptions, field, yearly)
        provenance[field] = f"default:${yearly:,.0f}/year flat"
        applied.append(f"{field}=${yearly:,.0f}")

    # Turnover cost: $800 per unit × turnover_rate_pct × num_units.
    # Turnover rate default (30%) lives on assumptions; user can
    # override it in the form.
    if _should_apply("turnover") and n > 0:
        rate = float(assumptions.turnover_rate_pct or 0.30)
        val = round(800.0 * rate * n, 2)
        assumptions.turnover = val
        provenance["turnover"] = (
            f"default:$800/unit × {rate:.0%} rate × {n} units"
        )
        applied.append(f"turnover=${val:,.0f}")

    # Management fee — user/extraction both live in mgmt_fee_pct (a
    # percentage, not a dollar figure). Leave at its 0.05 class default.

    # License / inspections — intentionally not defaulted; municipal
    # fees vary so much per deal that a flat default would be misleading.
    # Log a reminder if blank.
    if _should_apply("license_inspections"):
        logger.info(
            "EXPENSE DEFAULTS: license_inspections left at $0 — municipal "
            "fees vary per deal; confirm local rates before investment "
            "committee review",
        )

    if applied:
        logger.info(
            "EXPENSE DEFAULTS: %d rule-based defaults applied "
            "(extraction + user inputs remain authoritative where set) — %s",
            len(applied), "; ".join(applied[:10]),
        )
    else:
        logger.info("EXPENSE DEFAULTS: no fields needed rule-based defaults")


def _synthesize_rent_roll(deal: DealData) -> None:
    """
    If no rent roll was extracted from documents, synthesize one from:
    1. Number of units (from form input or extraction)
    2. Bedroom mix (from form input, or inferred from market comps)
    3. Market rents (from extracted rent comps, then HUD FMR, then form input)

    This prevents the pipeline from carrying over a prior deal's rent roll
    or producing a pro forma with $0 GPR.

    Sets: deal.extracted_docs.unit_mix, deal.extracted_docs.total_monthly_rent
    Logs provenance as "synthesized" so it's visible in the report.
    """
    ext = deal.extracted_docs

    # ── Re-synth gate: a prior synth that fell back to the hardcoded
    # $1,200 floor (because HUD FMR wasn't yet fetched) should be
    # reconsidered once market data is available. Detect the stale
    # default-sourced synth and clear unit_mix so the FMR-aware path
    # below can rebuild it.
    _rr_src = (deal.provenance.field_sources.get("rent_roll_source") or "")
    if _rr_src.endswith("_default") and deal.market_data and (
        deal.market_data.fmr_1br or deal.market_data.fmr_2br
    ):
        logger.info(
            "SYNTH RENT ROLL: prior synth used default rents (%s) — "
            "re-synthesizing now that HUD FMR is available",
            _rr_src,
        )
        ext.unit_mix = []
        ext.total_monthly_rent = None
        ext.avg_rent_per_unit = None

    # ── Gate: only run if no units were extracted ─────────────────────
    if ext.unit_mix and len(ext.unit_mix) > 0:
        # Even when units exist, backfill a pro-forma rent on any unit
        # whose monthly_rent and market_rent are both 0/None but has a
        # usable SF. This covers vacant commercial space in a broker OM
        # that lists square footage but no in-place rent.
        psf_samples = [
            float(c.asking_rent_per_sf) for c in
            ((deal.comps.commercial_comps if deal.comps else []) or [])
            if c.asking_rent_per_sf
        ] if deal.comps else []
        psf_annual = (sum(psf_samples) / len(psf_samples)) if psf_samples else 20.0
        filled = 0
        for u in ext.unit_mix:
            if not isinstance(u, dict):
                continue
            try:
                mr = float(u.get("monthly_rent") or 0)
                mkt = float(u.get("market_rent") or 0)
                sf = float(u.get("sf") or 0)
            except (TypeError, ValueError):
                continue
            if mr == 0 and mkt == 0 and sf > 0:
                proforma_monthly = round(sf * psf_annual / 12.0, 2)
                u["market_rent"] = proforma_monthly
                u["market_rent_sf"] = psf_annual / 12.0
                u["notes"] = (
                    (u.get("notes") or "")
                    + f" | Pro-forma rent synthesised at ${psf_annual:.2f}/SF/yr "
                    f"(market comp average)"
                ).strip(" |")
                filled += 1
        if filled:
            logger.info(
                "SYNTH RENT ROLL: backfilled %d unit(s) with pro-forma rent "
                "@ $%.2f/SF/yr market PSF",
                filled, psf_annual,
            )
        logger.info("SYNTH RENT ROLL: unit_mix already populated (%d units) — skipping synth",
                    len(ext.unit_mix))
        return

    num_units = deal.assumptions.num_units or 0
    if num_units <= 0:
        # Non-residential deals (land, office leased on a $/SF basis, retail
        # pads) don't carry a unit count. Build a single-cell synthetic row
        # using GBA × market PSF so GPR isn't silently $0.
        gba = deal.assumptions.gba_sf or 0
        if gba <= 0:
            logger.warning(
                "SYNTH RENT ROLL: num_units=0 and gba_sf=0 — cannot "
                "synthesize (asset has no unit basis AND no square footage)."
            )
            return
        # Pull a market PSF from extracted commercial comps, else a
        # conservative $20/SF/yr floor.
        psf_samples = [
            float(c.asking_rent_per_sf) for c in
            ((deal.comps.commercial_comps if deal.comps else []) or [])
            if c.asking_rent_per_sf
        ] if deal.comps else []
        psf_annual = (sum(psf_samples) / len(psf_samples)) if psf_samples else 20.0
        annual_gpr = round(gba * psf_annual, 2)
        monthly = round(annual_gpr / 12.0, 2)
        ext.unit_mix = [{
            "unit_id": "Whole-building",
            "unit_type": "Commercial",
            "sf": gba,
            "monthly_rent": monthly,
            "market_rent": monthly,
            "current_rent_sf": psf_annual / 12.0,
            "market_rent_sf": psf_annual / 12.0,
            "status": "Vacant",
            "is_vacant": True,
            "notes": f"Synthesised from GBA × ${psf_annual:.2f}/SF/yr market rate",
        }]
        ext.total_monthly_rent = monthly
        ext.avg_rent_per_unit = monthly
        logger.info(
            "SYNTH RENT ROLL: no units — synthesised whole-building row "
            "(%d SF × $%.2f/SF/yr = $%s/mo)",
            gba, psf_annual, f"{monthly:,.0f}",
        )
        return

    logger.info("SYNTH RENT ROLL: no extracted rent roll — synthesizing for %d units",
                num_units)

    # ── Step 1: Determine market rents by bedroom type ────────────────
    # Priority: extracted rent comps → HUD FMR → form monthly_rent → $1,200 fallback

    md = deal.market_data

    market_rents = {
        "Studio": None,
        "1BR": None,
        "2BR": None,
        "3BR": None,
        "4BR+": None,
    }

    # Renovation-tier multiplier is applied to FMR-sourced defaults only.
    # Comp-sourced rents already reflect post-renovation market quality.
    from models.models import RENOVATION_TIER_MULTIPLIERS
    _tier = (getattr(deal.assumptions, "renovation_tier", None) or "light_cosmetic")
    _tier_mult = RENOVATION_TIER_MULTIPLIERS.get(_tier, 1.0)

    # From extracted rent comps (if any)
    comps = deal.comps.rent_comps if deal.comps else []
    for comp in comps:
        br = (comp.unit_type or "").strip()
        rent = comp.monthly_rent
        if rent and br in market_rents and market_rents[br] is None:
            market_rents[br] = float(rent)

    # Fill missing tiers from HUD FMR data. Some jurisdictions return
    # non-numeric strings ("Data Not Available"); _hud_to_float silently
    # skips those rather than crashing the rent synth. FMR values are
    # multiplied by the renovation-tier factor (light=0.90, heavy=1.00,
    # new_construction=1.15) before being stored as the market rent.
    def _hud_to_float(v):
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    for tier_key, fmr_attr in (
        ("Studio", "fmr_studio"),
        ("1BR",    "fmr_1br"),
        ("2BR",    "fmr_2br"),
        ("3BR",    "fmr_3br"),
    ):
        if market_rents[tier_key] is None:
            f = _hud_to_float(getattr(md, fmr_attr, None))
            if f is not None and f > 0:
                market_rents[tier_key] = round(f * _tier_mult, 0)

    # Fill any remaining None values by interpolation from what we have
    filled = {k: v for k, v in market_rents.items() if v is not None}
    if not filled:
        # Last resort: derive a base rent from the extracted rent roll if we
        # have one, then fall back to quality_adjusted_market_rent, then
        # to the hardcoded $1,200 floor. FinancialAssumptions has no
        # monthly_rent field — reading it raises AttributeError.
        _units = (deal.extracted_docs.unit_mix
                  if deal.extracted_docs and deal.extracted_docs.unit_mix
                  else [])
        # Use explicit None-check rather than `or` so a legit $0 rent (vacant
        # unit) is included as a value rather than silently skipped. Filter
        # zero rents from the average computation since vacant units would
        # pull the mean below market.
        def _unit_rent(u):
            for k in ("monthly_rent", "current_rent"):
                v = u.get(k)
                if v is not None and v != "":
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
            return None
        _sampled_rents = [
            r for r in (_unit_rent(u) for u in _units)
            if r is not None and r > 0
        ]
        base = (
            sum(_sampled_rents) / len(_sampled_rents)
            if _sampled_rents
            else float(deal.assumptions.quality_adjusted_market_rent or 1200)
        )
        market_rents = {
            "Studio": round(base * 0.75),
            "1BR":    round(base * 0.90),
            "2BR":    round(base * 1.00),
            "3BR":    round(base * 1.20),
            "4BR+":   round(base * 1.40),
        }
    else:
        # Interpolate missing tiers from the ones we have
        ratios = {"Studio": 0.75, "1BR": 0.90, "2BR": 1.00, "3BR": 1.20, "4BR+": 1.40}
        pivot_key = next(iter(filled))
        pivot_val = filled[pivot_key]
        implied_base = pivot_val / ratios[pivot_key]
        for br, ratio in ratios.items():
            if market_rents[br] is None:
                market_rents[br] = round(implied_base * ratio)

    logger.info("SYNTH RENT ROLL: market rents — %s", market_rents)

    # ── Step 2: Determine unit type distribution ──────────────────────
    # Use a typical bedroom distribution when the asset has a
    # residential component. Mixed-Use is treated as residential for
    # defaulting purposes because the residential portion drives the
    # per-unit rent; commercial SF is priced separately via the
    # whole-building path (num_units == 0). Pure commercial asset
    # types (Office, Retail, Industrial) get a generic label so we
    # don't fabricate bedroom counts.
    from models.models import AssetType
    _is_residential = deal.asset_type in (
        AssetType.MULTIFAMILY, AssetType.SINGLE_FAMILY, AssetType.MIXED_USE,
    )
    if not _is_residential:
        # Generic units labeled by asset type — no bedroom fabrication.
        distribution = {deal.asset_type.value: 1.0}
    elif num_units == 1:
        distribution = {"2BR": 1.0}
    elif num_units <= 4:
        distribution = {"1BR": 0.50, "2BR": 0.50}
    elif num_units <= 10:
        distribution = {"Studio": 0.10, "1BR": 0.50, "2BR": 0.40}
    elif num_units <= 24:
        distribution = {"Studio": 0.15, "1BR": 0.45, "2BR": 0.35, "3BR": 0.05}
    else:
        distribution = {"Studio": 0.10, "1BR": 0.45, "2BR": 0.35, "3BR": 0.08, "4BR+": 0.02}

    # ── Step 3: Build synthetic unit_mix ─────────────────────────────
    unit_mix = []
    unit_counts = {}
    remaining = num_units

    # Allocate units to bedroom types (ensure total = num_units exactly)
    types = list(distribution.keys())
    for i, br_type in enumerate(types):
        if i == len(types) - 1:
            count = remaining  # last type absorbs remainder
        else:
            count = round(num_units * distribution[br_type])
            count = min(count, remaining)
        unit_counts[br_type] = count
        remaining -= count
        if remaining < 0:
            remaining = 0

    # Build individual unit records
    unit_num = 1
    for br_type, count in unit_counts.items():
        if count <= 0:
            continue
        # Rent cascade:
        #   1. Exact bedroom-type match (Studio/1BR/2BR/3BR/4BR+)
        #   2. 2BR as neutral residential proxy (most common tier)
        #   3. 1BR, then any populated tier
        #   4. $1,200 absolute floor (should be unreachable when HUD FMR
        #      fetched successfully — a miss here signals a data-source
        #      failure upstream and warrants a log warning).
        rent = (
            market_rents.get(br_type)
            or market_rents.get("2BR")
            or market_rents.get("1BR")
            or next((v for v in market_rents.values() if v), None)
            or 1200
        )
        if market_rents.get(br_type) is None:
            logger.warning(
                "SYNTH RENT ROLL: no market rent for unit type %r — "
                "using $%s fallback (check HUD FMR fetch + renovation tier)",
                br_type, f"{rent:,.0f}",
            )
        for _ in range(count):
            unit_mix.append({
                "unit_id": f"Unit {unit_num}",
                "unit_type": br_type,
                "sf": None,
                "monthly_rent": rent,
                "lease_status": "occupied",
                "lease_start": None,
                "lease_end": None,
            })
            unit_num += 1

    total_monthly = sum(u["monthly_rent"] for u in unit_mix)

    ext.unit_mix = unit_mix
    ext.total_monthly_rent = total_monthly
    ext.avg_rent_per_unit = round(total_monthly / num_units) if num_units > 0 else 0

    deal.provenance.field_sources["rent_roll_source"] = (
        f"synthesized_{num_units}units_from_"
        + ("comps" if comps else "hud_fmr" if md.fmr_1br else "default")
    )

    logger.info(
        "SYNTH RENT ROLL: built %d units, total_monthly=$%s, avg=$%s/unit. Source: %s",
        len(unit_mix),
        f"{total_monthly:,.0f}",
        f"{ext.avg_rent_per_unit:,.0f}",
        deal.provenance.field_sources["rent_roll_source"],
    )

    # Warn in the report context that this is synthetic
    deal.provenance.field_sources["rent_roll_note"] = (
        "SYNTHETIC: No rent roll provided. Unit mix and rents estimated from "
        "market comparables and HUD Fair Market Rent data. Verify before closing."
    )


def _synthesize_commercial_for_mixed_use(deal: DealData) -> None:
    """For Mixed-Use deals, append a synthetic commercial tenant row to
    unit_mix when none is present. Ground-floor retail / office is the
    norm in mixed-use, so an all-residential rent roll understates GPR.

    Sizing assumptions (configurable, based on typical urban 4-5 story
    mixed-use in Philadelphia):
      - Commercial SF ≈ 18% of GBA (ground-floor footprint proportion)
      - Market PSF/yr: median of extracted commercial comps, else $22/SF/yr
        (Phila. CMX/ICMX retail range, mid-2020s)
      - Lease type: NNN (default for ground-floor retail)
    """
    from models.models import AssetType
    if deal.asset_type != AssetType.MIXED_USE:
        return
    ext = deal.extracted_docs
    if ext is None:
        return
    units = ext.unit_mix or []
    has_commercial = any(
        isinstance(u, dict) and (
            (u.get("unit_type") or "").strip().lower()
            in ("commercial", "office", "retail", "industrial")
            or u.get("annual_rent_per_sf") is not None
            or u.get("lease_type")
            or u.get("tenant_name")
        )
        for u in units
    )
    if has_commercial:
        return

    gba = deal.assumptions.gba_sf or 0
    if gba <= 0:
        logger.info(
            "SYNTH COMMERCIAL: skipping — Mixed-Use deal has no GBA; "
            "cannot size ground-floor footprint",
        )
        return

    # Commercial footprint: 18% of GBA for 4-5 story mixed-use. Cap
    # at 8,000 SF so outsized buildings don't post implausibly large
    # commercial rents.
    commercial_sf = round(min(gba * 0.18, 8000))

    # Market PSF/yr: commercial comps → default floor
    psf_samples = [
        float(c.asking_rent_per_sf) for c in
        ((deal.comps.commercial_comps if deal.comps else []) or [])
        if c.asking_rent_per_sf and c.asking_rent_per_sf > 0
    ] if deal.comps else []
    if psf_samples:
        psf_samples.sort()
        n = len(psf_samples)
        psf_annual = (psf_samples[n // 2] if n % 2
                      else (psf_samples[n // 2 - 1] + psf_samples[n // 2]) / 2)
    else:
        psf_annual = 22.0  # Phila. CMX/ICMX ground-floor retail mid-range

    annual_rent = round(commercial_sf * psf_annual)
    monthly_rent = round(annual_rent / 12)

    synthetic_commercial = {
        "unit_id": "GF-Commercial",
        "unit_type": "Commercial",
        "tenant_name": "Ground Floor Retail (projected)",
        "sf": commercial_sf,
        "monthly_rent": monthly_rent,
        "market_rent": monthly_rent,
        "annual_rent_per_sf": round(psf_annual, 2),
        "market_rent_sf": round(psf_annual, 2),
        "lease_type": "NNN",
        "status": "Vacant",
        "is_vacant": True,
        "notes": (
            f"SYNTHETIC: ground-floor commercial sized at 18% of GBA "
            f"({commercial_sf:,} SF @ ${psf_annual:.2f}/SF/yr). Verify against "
            f"actual floor plan + lease prior to closing."
        ),
    }
    ext.unit_mix = units + [synthetic_commercial]
    logger.info(
        "SYNTH COMMERCIAL: added ground-floor retail placeholder — "
        "%d SF × $%.2f/SF/yr = $%s/yr (Mixed-Use deal, no commercial "
        "extracted from OM)",
        commercial_sf, psf_annual, f"{annual_rent:,}",
    )
    deal.provenance.field_sources["commercial_tenant_source"] = (
        f"synthetic_ground_floor_{commercial_sf}sf_at_{round(psf_annual)}psf"
    )


# ═══════════════════════════════════════════════════════════════════════════
# SECTIONS & NOTIFICATION CONFIG MERGE
# ═══════════════════════════════════════════════════════════════════════════

def _merge_sections_config(deal: DealData, user_inputs: Dict[str, Any]) -> None:
    """Apply user section toggles. s21 and s22 are locked True."""
    sections = user_inputs.get("sections_config")
    if isinstance(sections, dict):
        for key in SectionsConfig.model_fields:
            if key in sections:
                setattr(deal.sections_config, key, bool(sections[key]))
        # Enforce locked sections
        deal.sections_config.s21 = True
        deal.sections_config.s22 = True

    # Investor mode suppression
    if deal.investor_mode:
        deal.sections_config.s16 = False
        deal.sections_config.s17 = False
        deal.sections_config.s22 = False


def _merge_notification_config(deal: DealData, user_inputs: Dict[str, Any]) -> None:
    """Apply notification preferences from user inputs."""
    notif = user_inputs.get("notification_config")
    if isinstance(notif, dict):
        deal.notification_config = NotificationConfig(**notif)


# ═══════════════════════════════════════════════════════════════════════════
# PROVENANCE SETUP
# ═══════════════════════════════════════════════════════════════════════════

def _init_provenance(deal: DealData) -> None:
    """Initialize provenance metadata for this pipeline run."""
    if not deal.provenance.deal_id:
        deal.provenance.deal_id = deal.deal_id
    deal.provenance.run_timestamp = datetime.utcnow().isoformat(timespec="seconds") + "Z"



# ═══════════════════════════════════════════════════════════════════════════
# COMPS MERGE
# ═══════════════════════════════════════════════════════════════════════════

def _merge_comps(deal: DealData) -> None:
    """
    Merge comp data from extracted docs into deal.comps.

    Priority: deal.comps (manual frontend entry) > extracted_docs.comps.
    If the user has manually entered any comps, those win entirely for that
    comp type. If a comp type is empty on deal.comps but present in
    extracted_docs.comps, backfill from extraction.
    """
    extracted_comps = deal.extracted_docs.comps
    if not extracted_comps:
        return  # nothing extracted — leave deal.comps as-is (manual or empty)

    # For each comp type: only backfill if the deal.comps list is empty
    if not deal.comps.rent_comps and extracted_comps.rent_comps:
        deal.comps.rent_comps = extracted_comps.rent_comps[:8]
        logger.info("Backfilled %d rent comps from extracted OM", len(deal.comps.rent_comps))

    if not deal.comps.commercial_comps and extracted_comps.commercial_comps:
        deal.comps.commercial_comps = extracted_comps.commercial_comps[:5]
        logger.info("Backfilled %d commercial comps from extracted OM", len(deal.comps.commercial_comps))

    if not deal.comps.sale_comps and extracted_comps.sale_comps:
        deal.comps.sale_comps = extracted_comps.sale_comps[:5]
        logger.info("Backfilled %d sale comps from extracted OM", len(deal.comps.sale_comps))


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

def assemble_deal(deal: DealData, user_inputs: Dict[str, Any]) -> DealData:
    """
    Master assembly function — merges user inputs + extracted data into a
    validated DealData object ready for downstream pipeline modules.

    Args:
        deal: DealData with extracted_docs already populated by extractor.py.
        user_inputs: Dict of raw values from the Streamlit frontend.

    Returns:
        The same DealData object, fully merged and validated.
    """
    provenance: Dict[str, str] = {}
    extracted = deal.extracted_docs

    # ── 1. Identity ───────────────────────────────────────────────
    if not deal.deal_id:
        deal.deal_id = user_inputs.get("deal_id") or str(uuid.uuid4())[:12]
    deal.deal_code = user_inputs.get("deal_code", deal.deal_code)
    deal.deal_type = user_inputs.get("deal_type", deal.deal_type)
    deal.report_date = user_inputs.get("report_date") or datetime.utcnow().strftime("%Y-%m-%d")

    # ── 2. Classification (Enum validation) ───────────────────────
    deal.asset_type = _resolve_asset_type(
        user_inputs.get("asset_type", deal.asset_type)
    )
    deal.investment_strategy = _resolve_strategy(
        user_inputs.get("investment_strategy", deal.investment_strategy)
    )

    # ── 3. Address ────────────────────────────────────────────────
    _merge_address(deal.address, user_inputs, extracted, provenance)

    # ── 4. Sponsor ────────────────────────────────────────────────
    # Default to "DealDesk" when no sponsor name is submitted — removes
    # the previous hardcoded firm attribution.
    deal.sponsor_name = (
        user_inputs.get("sponsor_name")
        or getattr(deal, "sponsor_name", None)
        or "DealDesk"
    )
    if user_inputs.get("sponsor_description"):
        deal.sponsor_description = user_inputs["sponsor_description"]

    # ── 5. Deal description ───────────────────────────────────────
    deal.deal_description = user_inputs.get("deal_description", deal.deal_description)

    # ── 6. Financial assumptions ──────────────────────────────────
    _merge_assumptions(deal.assumptions, user_inputs, extracted, provenance)
    _backfill_expenses(deal.assumptions, extracted, provenance)
    # Tier 3: apply rule-based defaults (per-unit & flat-yearly) to any
    # expense field still at 0 after extraction + user input. Respects
    # the extraction provenance — never overwrites a T-12 value.
    _apply_expense_defaults(deal.assumptions, provenance)
    _synthesize_rent_roll(deal)

    # ── 7. Investor mode ──────────────────────────────────────────
    deal.investor_mode = bool(user_inputs.get("investor_mode", deal.investor_mode))

    # ── 8. Section config ─────────────────────────────────────────
    _merge_sections_config(deal, user_inputs)

    # ── 9. Notification config ────────────────────────────────────
    _merge_notification_config(deal, user_inputs)

    # ── 10. Provenance ────────────────────────────────────────────
    _init_provenance(deal)
    deal.provenance.field_sources.update(provenance)
    if provenance:
        logger.info("Provenance: %d fields backfilled from extraction", len(provenance))

    logger.info(
        "Deal assembled: %s | %s | %s | price=$%s",
        deal.deal_id,
        deal.asset_type.value,
        deal.investment_strategy.value,
        f"{deal.assumptions.purchase_price:,.0f}",
    )

    return deal
