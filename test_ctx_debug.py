"""Minimal test: exercise _populate_docx with default DealData to capture CTX logging."""
import logging
import sys

# Configure logging to stdout so we capture everything
logging.basicConfig(
    level=logging.DEBUG,
    format="%(name)s %(levelname)s %(message)s",
    stream=sys.stdout,
)

from models.models import DealData
from word_builder import _populate_docx

deal = DealData(deal_id="CTX_TEST_001")
try:
    docx_path = _populate_docx(deal)
    print(f"\nSUCCESS: {docx_path}")
except Exception as e:
    print(f"\nFAILED: {e}")
