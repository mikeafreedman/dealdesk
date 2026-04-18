"""
expense_pricing.py — Public-Record Property Tax & Insurance Estimator
======================================================================
Populates annual property-tax and property-insurance expense estimates for
the financials model using public-record data that is already on the deal:

    Taxes     = assessed_value × effective property-tax rate
    Insurance = replacement cost × TIV rate × catastrophe loading

Tax rate lookup precedence:
    1. (state, county) override table — high-volume markets
    2. (state, municipality) override table — select cities
    3. state-level rate (Census ACS 2022 effective rates)
    4. national fallback (1.10%)

Insurance methodology:
    TIV         = GBA × replacement-cost-PSF (by asset type)
    base_rate   = TIV × per-asset-type base rate (% of TIV)
    loaded_rate = base_rate × FEMA loading × First Street loading × state multiplier

Every estimate is annotated with its provenance so the pipeline can log how
each number was derived.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from models.models import AssetType, DealData

logger = logging.getLogger(__name__)


# ── Property tax effective rates (annual tax / market value) ─────────────────
# Source: U.S. Census Bureau, ACS 2022 5-year median effective rate by state
# for owner-occupied housing. Commercial effective rates are within 10-20 bps
# of residential in most states (commercial reassessment cycles track).

_STATE_EFFECTIVE_PROPERTY_TAX_RATE: dict[str, float] = {
    "AL": 0.0040, "AK": 0.0118, "AZ": 0.0062, "AR": 0.0064, "CA": 0.0075,
    "CO": 0.0055, "CT": 0.0214, "DE": 0.0061, "FL": 0.0091, "GA": 0.0092,
    "HI": 0.0032, "ID": 0.0069, "IL": 0.0208, "IN": 0.0084, "IA": 0.0157,
    "KS": 0.0141, "KY": 0.0086, "LA": 0.0056, "ME": 0.0128, "MD": 0.0106,
    "MA": 0.0120, "MI": 0.0138, "MN": 0.0111, "MS": 0.0081, "MO": 0.0098,
    "MT": 0.0074, "NE": 0.0163, "NV": 0.0059, "NH": 0.0189, "NJ": 0.0247,
    "NM": 0.0080, "NY": 0.0173, "NC": 0.0082, "ND": 0.0098, "OH": 0.0156,
    "OK": 0.0089, "OR": 0.0093, "PA": 0.0149, "RI": 0.0153, "SC": 0.0057,
    "SD": 0.0128, "TN": 0.0066, "TX": 0.0168, "UT": 0.0063, "VT": 0.0183,
    "VA": 0.0082, "WA": 0.0093, "WV": 0.0058, "WI": 0.0176, "WY": 0.0061,
    "DC": 0.0062,
}

# County-level overrides (effective rate on assessed value). Keys are
# (state, county-or-city), case-insensitive on lookup. Covers high-volume
# metros where state averages mask large intra-state variation.
_COUNTY_PROPERTY_TAX_RATE: dict[Tuple[str, str], float] = {
    ("PA", "philadelphia"):   0.013998,   # combined city + school
    ("PA", "montgomery"):     0.01620,
    ("PA", "allegheny"):      0.02070,
    ("PA", "delaware"):       0.02150,
    ("PA", "bucks"):          0.01720,
    ("PA", "chester"):        0.01920,
    ("NJ", "hudson"):         0.01890,
    ("NJ", "essex"):           0.02500,
    ("NJ", "bergen"):          0.02160,
    ("NY", "new york"):        0.00880,   # NYC Manhattan Class 2
    ("NY", "kings"):           0.00880,   # Brooklyn
    ("NY", "queens"):          0.00880,
    ("NY", "bronx"):           0.00880,
    ("NY", "richmond"):        0.00880,   # Staten Island
    ("NY", "nassau"):          0.02100,
    ("NY", "suffolk"):         0.02110,
    ("NY", "westchester"):     0.02320,
    ("DC", "washington"):      0.00550,   # Class 2 commercial
    ("FL", "miami-dade"):      0.01050,
    ("FL", "broward"):         0.01140,
    ("FL", "palm beach"):      0.01040,
    ("FL", "orange"):          0.00970,
    ("FL", "hillsborough"):    0.01200,
    ("CA", "los angeles"):     0.01250,
    ("CA", "san francisco"):   0.01180,
    ("CA", "orange"):          0.01100,
    ("CA", "san diego"):       0.01160,
    ("CA", "alameda"):         0.01250,
    ("IL", "cook"):            0.02290,
    ("MA", "suffolk"):         0.01050,   # Boston
    ("MA", "middlesex"):       0.01170,
    ("TX", "harris"):          0.02110,   # Houston
    ("TX", "dallas"):          0.01930,
    ("TX", "travis"):          0.01960,   # Austin
    ("TX", "bexar"):           0.02050,   # San Antonio
    ("GA", "fulton"):          0.01070,   # Atlanta
    ("WA", "king"):            0.00940,   # Seattle
    ("OR", "multnomah"):       0.01140,   # Portland
    ("AZ", "maricopa"):        0.00610,   # Phoenix
    ("NV", "clark"):           0.00600,   # Las Vegas
    ("CO", "denver"):          0.00570,
    ("MD", "baltimore city"):  0.01280,
    ("MD", "montgomery"):      0.00920,
    ("MD", "prince george's"): 0.01090,
    ("OH", "cuyahoga"):        0.02110,   # Cleveland
    ("OH", "franklin"):        0.01650,   # Columbus
    ("MI", "wayne"):           0.01890,   # Detroit
    ("MN", "hennepin"):        0.01100,   # Minneapolis
    ("TN", "davidson"):        0.00850,   # Nashville
    ("LA", "orleans"):         0.00630,   # New Orleans
}

_NATIONAL_FALLBACK_TAX_RATE = 0.0110


# ── Insurance replacement cost and base TIV rates ────────────────────────────

_REPLACEMENT_COST_PSF: dict[AssetType, float] = {
    AssetType.MULTIFAMILY:  180.0,   # garden/midrise blended
    AssetType.OFFICE:       250.0,
    AssetType.RETAIL:       180.0,
    AssetType.INDUSTRIAL:   100.0,
    AssetType.MIXED_USE:    210.0,
    AssetType.SINGLE_FAMILY: 160.0,
}

# Base insurance rate = annual premium as % of Total Insurable Value (TIV).
# Benchmarks from commercial P&C placement data; used as the no-loading base.
_BASE_INSURANCE_RATE: dict[AssetType, float] = {
    AssetType.MULTIFAMILY:  0.0030,
    AssetType.OFFICE:       0.0025,
    AssetType.RETAIL:       0.0035,
    AssetType.INDUSTRIAL:   0.0030,
    AssetType.MIXED_USE:    0.0035,
    AssetType.SINGLE_FAMILY: 0.0040,
}

# Catastrophe-prone-state multipliers applied to the base rate. Reflects
# reinsurance cost in each geography (public ISO/AIR loss-cost data).
_STATE_CATASTROPHE_MULTIPLIER: dict[str, float] = {
    "FL": 2.50, "LA": 1.80, "MS": 1.70, "AL": 1.40, "TX": 1.40,
    "CA": 1.50, "OR": 1.15, "WA": 1.05,
    "NC": 1.30, "SC": 1.35, "GA": 1.20,
    "NY": 1.20, "NJ": 1.20, "CT": 1.10,    # coastal / high reinsurance
    "OK": 1.30, "KS": 1.20, "MO": 1.10,    # tornado alley
    "CO": 1.15,
}


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _normalize_county(raw: Optional[str]) -> str:
    """Strip trailing 'County' / 'Parish' / case and whitespace."""
    if not raw:
        return ""
    s = raw.strip().lower()
    for suffix in (" county", " parish", " borough", " city"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s.strip()


def _county_from_registry(deal: DealData) -> Optional[str]:
    """Pull the county name from the municipal registry match, if available."""
    try:
        import registry
        row = registry.lookup(deal)
        if row is None:
            return None
        county = row.get("county")
        return str(county) if county else None
    except Exception as exc:
        logger.debug("registry lookup for county failed: %s", exc)
        return None


def _resolve_tax_rate(deal: DealData) -> Tuple[float, str]:
    """Return (effective_tax_rate, source_label). Uses county/city override
    first, then state, then national fallback."""
    state = (deal.address.state or "").strip().upper()
    county = _normalize_county(_county_from_registry(deal))
    city = _normalize_county(deal.address.city)

    # County-level override
    if state and county:
        rate = _COUNTY_PROPERTY_TAX_RATE.get((state, county))
        if rate:
            return rate, f"county:{state}-{county}"

    # City-level override (many cities double as county-equivalents)
    if state and city:
        rate = _COUNTY_PROPERTY_TAX_RATE.get((state, city))
        if rate:
            return rate, f"city:{state}-{city}"

    # State-level
    if state and state in _STATE_EFFECTIVE_PROPERTY_TAX_RATE:
        return _STATE_EFFECTIVE_PROPERTY_TAX_RATE[state], f"state:{state}"

    return _NATIONAL_FALLBACK_TAX_RATE, "national-fallback"


def _insurance_loading(deal: DealData) -> Tuple[float, str]:
    """Return (loading_multiplier, source_label) built from FEMA flood zone,
    First Street risk scores, and state catastrophe multiplier."""
    md = deal.market_data
    loading = 1.0
    notes = []

    # FEMA flood zone (A/AE/V require flood insurance)
    zone = (md.fema_flood_zone or "").strip().upper()
    if zone and zone[0] in ("A", "V"):
        loading *= 1.50
        notes.append(f"FEMA-{zone}")

    # First Street peril scores (0-10 scale)
    wind = getattr(md, "first_street_wind", None)
    if wind is not None and wind >= 7:
        loading *= 1.20
        notes.append(f"wind-{int(wind)}")
    fire = getattr(md, "first_street_fire", None)
    if fire is not None and fire >= 7:
        loading *= 1.15
        notes.append(f"fire-{int(fire)}")

    # State catastrophe multiplier
    state = (deal.address.state or "").strip().upper()
    state_mult = _STATE_CATASTROPHE_MULTIPLIER.get(state, 1.0)
    if state_mult != 1.0:
        loading *= state_mult
        notes.append(f"cat-{state}-{state_mult:.2f}x")

    return loading, ";".join(notes) if notes else "base"


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

def estimate_property_taxes(deal: DealData) -> Optional[Tuple[float, str]]:
    """Estimate annual real estate taxes from assessed value × effective rate.

    Returns (annual_taxes, source_description) or None if no usable basis
    (no parcel data and no purchase price).
    """
    a = deal.assumptions
    parcel = deal.parcel_data

    # Basis: assessed value first, then purchase price as a market-value proxy
    basis = None
    basis_label = None
    if parcel and parcel.assessed_value and parcel.assessed_value > 0:
        basis = float(parcel.assessed_value)
        basis_label = "assessed_value"
    elif a.purchase_price and a.purchase_price > 0:
        basis = float(a.purchase_price)
        basis_label = "purchase_price"
    else:
        return None

    rate, rate_source = _resolve_tax_rate(deal)
    annual = round(basis * rate, 2)
    source = f"{basis_label}=${basis:,.0f} × rate={rate:.4%} ({rate_source})"
    return annual, source


def estimate_property_insurance(deal: DealData) -> Optional[Tuple[float, str]]:
    """Estimate annual property insurance premium.

    Formula: GBA × replacement-cost-PSF × base-rate × loading
    Returns (annual_premium, source_description) or None if GBA is missing.
    """
    a = deal.assumptions
    parcel = deal.parcel_data
    gba = a.gba_sf or (parcel.building_sf if parcel else None)
    if not gba or gba <= 0:
        return None

    asset = deal.asset_type
    rc_psf = _REPLACEMENT_COST_PSF.get(asset, 180.0)
    base_rate = _BASE_INSURANCE_RATE.get(asset, 0.0030)

    tiv = gba * rc_psf
    loading, loading_src = _insurance_loading(deal)
    annual = round(tiv * base_rate * loading, 2)

    source = (
        f"TIV=${tiv:,.0f} (GBA={gba:,.0f} SF × ${rc_psf:.0f}/SF) × "
        f"rate={base_rate:.2%} × loading={loading:.2f}x ({loading_src})"
    )
    return annual, source
