"""
DealDesk CRE Underwriting — Global Configuration
Pure constants: API refs, model strings, paths, template routing.
"""

from pathlib import Path

# ── Root paths ────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
TEMPLATES_DIR = DATA_DIR / "templates"
OUTPUT_DIR = DATA_DIR / "output"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
WORD_TEMPLATES_DIR = PROJECT_ROOT / "templates"

# ── Excel template routing ────────────────────────────────────
EXCEL_TEMPLATE_MAP = {
    "stabilized": TEMPLATES_DIR / "Hold_Template_v3.xlsx",
    "value_add":  TEMPLATES_DIR / "Hold_Template_v3.xlsx",
    "for_sale":   TEMPLATES_DIR / "Sale_Template_v3.xlsx",
}

# ── Word / report template ────────────────────────────────────
WORD_TEMPLATE = WORD_TEMPLATES_DIR / "DealDesk_Report_Template_v4.docx"

# ── Reference data ────────────────────────────────────────────
MUNICIPAL_REGISTRY_CSV = DATA_DIR / "municipal_registry.csv"

# ── Anthropic / LLM ──────────────────────────────────────────
ANTHROPIC_SECRET_KEY = "anthropic"          # key in st.secrets
MODEL_HAIKU  = "claude-haiku-4-5-20251001"
MODEL_SONNET = "claude-sonnet-4-5-20250514"

# ── HUD ──────────────────────────────────────────────────────
HUD_API_KEY = "hud"                    # key in st.secrets

# ── Email (SMTP) ─────────────────────────────────────────────
EMAIL_SECRET_KEY = "email"                  # key in st.secrets
SMTP_PORT_DEFAULT = 587

# ── Slack ─────────────────────────────────────────────────────
SLACK_SECRET_KEY = "slack"                  # key in st.secrets

# ── Pipeline defaults ────────────────────────────────────────
PDF_CONVERSION_TIMEOUT = 60                 # seconds
