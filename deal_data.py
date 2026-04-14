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

# Map from T-12 expense_line_items keys → FinancialAssumptions fields
_EXPENSE_MAP: Dict[str, str] = {
    "real_estate_taxes":    "re_taxes",
    "re_taxes":             "re_taxes",
    "insurance":            "insurance",
    "gas":                  "gas",
    "water_sewer":          "water_sewer",
    "electric":             "electric",
    "trash":                "trash",
    "repairs":              "repairs",
    "repairs_maintenance":  "repairs",
    "cleaning":             "cleaning",
    "landscaping":          "landscape_snow",
    "landscape_snow":       "landscape_snow",
    "advertising":          "advertising",
    "salaries":             "salaries",
    "admin_legal_acct":     "admin_legal_acct",
    "exterminator":         "exterminator",
    "pest_control":         "exterminator",
}


def _backfill_expenses(assumptions: FinancialAssumptions, extracted: ExtractedDocumentData,
                       provenance: Dict[str, str]) -> None:
    """Backfill Year-1 expense assumptions from T-12 extracted line items."""
    if not extracted.expense_line_items:
        return
    for t12_key, amount in extracted.expense_line_items.items():
        assume_field = _EXPENSE_MAP.get(t12_key.lower())
        if not assume_field:
            continue
        current = getattr(assumptions, assume_field, None)
        if not _is_blank(current):
            continue  # user already set
        if _is_blank(amount):
            continue
        try:
            setattr(assumptions, assume_field, float(amount))
            provenance[assume_field] = f"t12:{t12_key}"
            logger.info("Backfilled expense %s from T-12 line '%s'", assume_field, t12_key)
        except (ValueError, TypeError):
            pass


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
    if user_inputs.get("sponsor_name"):
        deal.sponsor_name = user_inputs["sponsor_name"]
    if user_inputs.get("sponsor_description"):
        deal.sponsor_description = user_inputs["sponsor_description"]

    # ── 5. Deal description ───────────────────────────────────────
    deal.deal_description = user_inputs.get("deal_description", deal.deal_description)

    # ── 6. Financial assumptions ──────────────────────────────────
    _merge_assumptions(deal.assumptions, user_inputs, extracted, provenance)
    _backfill_expenses(deal.assumptions, extracted, provenance)

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
