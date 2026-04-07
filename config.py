"""
DealDesk CRE Underwriting — Global Configuration
Pure constants: API refs, model strings, paths, template routing.
"""

import os
from pathlib import Path

# ── Root paths ────────────────────────────────────────────────
PROJECT_ROOT       = Path(__file__).resolve().parent
DATA_DIR           = PROJECT_ROOT / "data"
TEMPLATES_DIR      = DATA_DIR / "templates"
OUTPUT_DIR         = DATA_DIR / "output"
OUTPUTS_DIR        = PROJECT_ROOT / "outputs"
WORD_TEMPLATES_DIR = PROJECT_ROOT / "templates"

# ── Excel template routing ────────────────────────────────────
# Structure: EXCEL_TEMPLATE_MAP[strategy][asset_type] → Path
# Strategies:  stabilized | value_add | for_sale
# Asset types: multifamily | mixed_use | retail | office | industrial | single_family
#
# Asset-type-specific templates that have not been built yet fall back to the
# closest available template. Each fallback is marked TODO so it is easy to
# find and replace when the dedicated template is ready.

EXCEL_TEMPLATE_MAP = {
    "stabilized": {
        "multifamily":   TEMPLATES_DIR / "hold_template.xlsx",
        "mixed_use":     TEMPLATES_DIR / "hold_template.xlsx",   # TODO: build mixed_use_hold_template.xlsx
        "retail":        TEMPLATES_DIR / "hold_template.xlsx",   # TODO: build retail_hold_template.xlsx
        "office":        TEMPLATES_DIR / "hold_template.xlsx",   # TODO: build office_hold_template.xlsx
        "industrial":    TEMPLATES_DIR / "hold_template.xlsx",   # TODO: build industrial_hold_template.xlsx
        "single_family": TEMPLATES_DIR / "hold_template.xlsx",   # TODO: build sf_hold_template.xlsx
    },
    "value_add": {
        "multifamily":   TEMPLATES_DIR / "hold_template.xlsx",
        "mixed_use":     TEMPLATES_DIR / "hold_template.xlsx",   # TODO: build mixed_use_hold_template.xlsx
        "retail":        TEMPLATES_DIR / "hold_template.xlsx",   # TODO: build retail_hold_template.xlsx
        "office":        TEMPLATES_DIR / "hold_template.xlsx",   # TODO: build office_hold_template.xlsx
        "industrial":    TEMPLATES_DIR / "hold_template.xlsx",   # TODO: build industrial_hold_template.xlsx
        "single_family": TEMPLATES_DIR / "hold_template.xlsx",   # TODO: build sf_hold_template.xlsx
    },
    "for_sale": {
        "multifamily":   TEMPLATES_DIR / "Sale_Template_v3.xlsx",
        "mixed_use":     TEMPLATES_DIR / "Sale_Template_v3.xlsx",  # TODO: build mixed_use_sale_template.xlsx
        "retail":        TEMPLATES_DIR / "Sale_Template_v3.xlsx",  # TODO: build retail_sale_template.xlsx
        "office":        TEMPLATES_DIR / "Sale_Template_v3.xlsx",  # TODO: build office_sale_template.xlsx
        "industrial":    TEMPLATES_DIR / "Sale_Template_v3.xlsx",  # TODO: build industrial_sale_template.xlsx
        "single_family": TEMPLATES_DIR / "Sale_Template_v3.xlsx",  # TODO: build sf_sale_template.xlsx
    },
}


def get_excel_template(strategy_key: str, asset_type_key: str) -> Path:
    """
    Return the correct Excel template Path for a given strategy + asset type.

    Normalises both keys (lowercase, spaces → underscores) before lookup so
    minor formatting differences in callers never cause a silent wrong-template
    selection or a hard crash.

    Raises
    ------
    KeyError        Unknown strategy_key or asset_type_key.
    FileNotFoundError  Template file does not exist on disk.
    """
    strategy_key   = strategy_key.lower().strip()
    asset_type_key = asset_type_key.lower().strip().replace("-", "_").replace(" ", "_")

    if strategy_key not in EXCEL_TEMPLATE_MAP:
        raise KeyError(
            f"Unknown strategy_key {strategy_key!r}. "
            f"Valid options: {list(EXCEL_TEMPLATE_MAP.keys())}"
        )
    strategy_map = EXCEL_TEMPLATE_MAP[strategy_key]

    if asset_type_key not in strategy_map:
        raise KeyError(
            f"Unknown asset_type_key {asset_type_key!r} for strategy {strategy_key!r}. "
            f"Valid options: {list(strategy_map.keys())}"
        )
    path = strategy_map[asset_type_key]

    if not path.exists():
        raise FileNotFoundError(
            f"Excel template not found: {path}\n"
            f"Check that the file exists in {TEMPLATES_DIR}"
        )
    return path


# ── Word / report template ────────────────────────────────────
WORD_TEMPLATE = WORD_TEMPLATES_DIR / "DealDesk_Report_Template_v4.docx"

# ── Reference data ────────────────────────────────────────────
MUNICIPAL_REGISTRY_CSV = DATA_DIR / "municipal_registry.csv"

# ── Anthropic / LLM ──────────────────────────────────────────
ANTHROPIC_SECRET_KEY = "anthropic"          # key in st.secrets
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL_HAIKU  = "claude-haiku-4-5-20251001"
MODEL_SONNET = "claude-sonnet-4-6"

# ── HUD ──────────────────────────────────────────────────────
HUD_API_KEY = "hud"                         # key in st.secrets

# ── Email (SMTP) ─────────────────────────────────────────────
EMAIL_SECRET_KEY = "email"                  # key in st.secrets
SMTP_PORT_DEFAULT = 587

# ── Slack ─────────────────────────────────────────────────────
SLACK_SECRET_KEY = "slack"                  # key in st.secrets

# ── Google Maps / Street View ────────────────────────────────
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")

# ── Pipeline defaults ─────────────────────────────────────────
PDF_CONVERSION_TIMEOUT = 60                 # seconds
