"""
DealDesk CRE Underwriting — Global Configuration
Pure constants: API refs, model strings, paths, template routing.
"""

import logging
import os
from dotenv import load_dotenv
load_dotenv(override=True)
from pathlib import Path

# ── Anthropic key validation (fail-fast at startup) ─────────
_key = os.getenv("ANTHROPIC_API_KEY", "NOT_FOUND")
print(f"DEBUG ANTHROPIC KEY AT INIT: starts={_key[:12] if len(_key) > 12 else _key}, len={len(_key)}")
if not _key or not _key.startswith("sk-ant-"):
    raise RuntimeError(
        "ANTHROPIC_API_KEY is missing or malformed. "
        "Set it in .env before starting the server."
    )

# ── Root paths ────────────────────────────────────────────────
PROJECT_ROOT       = Path(__file__).resolve().parent
DATA_DIR           = PROJECT_ROOT / "data"
TEMPLATES_DIR      = DATA_DIR / "templates"
OUTPUT_DIR         = DATA_DIR / "output"
OUTPUTS_DIR        = PROJECT_ROOT / "outputs"
WORD_TEMPLATES_DIR = PROJECT_ROOT / "templates"

# ── Excel template routing ────────────────────────────────────
# Driven entirely by InvestmentStrategy — asset_type plays no role.
from models.models import InvestmentStrategy

logger = logging.getLogger(__name__)

_STRATEGY_TEMPLATE_MAP: dict[InvestmentStrategy, Path] = {
    InvestmentStrategy.STABILIZED_HOLD: TEMPLATES_DIR / "hold_template_v3.xlsx",
    InvestmentStrategy.VALUE_ADD:       TEMPLATES_DIR / "hold_template_v3.xlsx",
    InvestmentStrategy.OPPORTUNISTIC:   TEMPLATES_DIR / "sale_template_v3.xlsx",
}

_DEFAULT_TEMPLATE = TEMPLATES_DIR / "hold_template_v3.xlsx"


def get_excel_template(strategy: InvestmentStrategy | None) -> Path:
    """
    Return the correct Excel template Path for a given investment strategy.

    Falls back to Hold_Template_v3.xlsx with a warning if strategy is None
    or not recognised.

    Raises
    ------
    FileNotFoundError  Template file does not exist on disk.
    """
    if strategy is None or strategy not in _STRATEGY_TEMPLATE_MAP:
        logger.warning(
            "Unrecognised or missing investment_strategy %r — "
            "defaulting to Hold_Template_v3.xlsx",
            strategy,
        )
        path = _DEFAULT_TEMPLATE
    else:
        path = _STRATEGY_TEMPLATE_MAP[strategy]

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
