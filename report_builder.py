"""
report_builder.py — DealDesk Playwright PDF Report Generator
=============================================================
Playwright + headless Chromium + Jinja2/HTML/CSS.

Runs narrative generation (context_builder.generate_narratives), builds the
template context (context_builder.build_context), then renders the HTML
template under templates/ and prints to PDF via Playwright.

Image layer is self-contained:
    - maps  → map_builder.build_all_maps(deal) (PNG bytes)
    - street view → context_builder.fetch_street_view_image(addr, deal_id)
Each call is wrapped so a single image failure never aborts the PDF.

Output: outputs/{deal_id}_report.pdf
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

from config import OUTPUTS_DIR, WORD_TEMPLATES_DIR
from context_builder import (
    build_context,
    fetch_street_view_image,
    generate_narratives,
)

logger = logging.getLogger(__name__)

_TEMPLATE_NAME = "report_template.html"
_CSS_NAME      = "report.css"


# ── Jinja filters ────────────────────────────────────────────────────────────

def _fmt_currency(val: Any) -> str:
    """Currency format. Negative → ($X,XXX) (parentheses, no minus sign)."""
    if val is None or val == "":
        return "—"
    try:
        n = float(val)
    except (TypeError, ValueError):
        return str(val)
    if n < 0:
        return f"(${abs(n):,.0f})"
    return f"${n:,.0f}"


def _fmt_percent(val: Any, digits: int = 1) -> str:
    """Percent format. Input is a decimal fraction (0.035 → 3.5%)."""
    if val is None or val == "":
        return "—"
    try:
        n = float(val)
    except (TypeError, ValueError):
        return str(val)
    return f"{n * 100:.{digits}f}%"


def _fmt_multiple(val: Any, digits: int = 2) -> str:
    """Equity-multiple format: 1.85 → '1.85x'."""
    if val is None or val == "":
        return "—"
    try:
        n = float(val)
    except (TypeError, ValueError):
        return str(val)
    return f"{n:.{digits}f}x"


# ── Image layer ──────────────────────────────────────────────────────────────

def _bytes_to_data_uri(data: bytes, mime: str = "image/png") -> str:
    """Return data:<mime>;base64,<...> — embeddable in <img src>."""
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _file_to_data_uri(path: str | Path) -> Optional[str]:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return None
    ext = p.suffix.lower().lstrip(".")
    mime = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png",  "gif": "image/gif",
        "webp": "image/webp",
    }.get(ext, "application/octet-stream")
    return _bytes_to_data_uri(p.read_bytes(), mime)


def _build_image_context(deal) -> dict:
    """Generate all images for the report. Every step is defensively wrapped —
    a single failed image never aborts the PDF. Returns ctx keys as base64
    data URIs (data:image/...;base64,...) that embed directly in <img src>:
        aerial_map_b64, neighborhood_map_b64, fema_map_b64, street_view_b64
    """
    img_ctx: dict = {}

    # Maps
    try:
        from map_builder import build_all_maps
        maps = build_all_maps(deal)
        if getattr(maps, "aerial", None):
            img_ctx["aerial_map_b64"] = _bytes_to_data_uri(maps.aerial)
            logger.info("REPORT_IMG: aerial map OK (%d bytes)", len(maps.aerial))
        if getattr(maps, "neighborhood", None):
            img_ctx["neighborhood_map_b64"] = _bytes_to_data_uri(maps.neighborhood)
            logger.info("REPORT_IMG: neighborhood map OK (%d bytes)",
                        len(maps.neighborhood))
        if getattr(maps, "fema", None):
            img_ctx["fema_map_b64"] = _bytes_to_data_uri(maps.fema)
            logger.info("REPORT_IMG: FEMA map OK (%d bytes)", len(maps.fema))
    except Exception as exc:
        logger.warning("REPORT_IMG: map generation failed — %s", exc)

    # Street view
    try:
        sv_path = fetch_street_view_image(
            deal.address.full_address or "",
            deal.deal_id or "unknown",
        )
        if sv_path:
            uri = _file_to_data_uri(sv_path)
            if uri:
                img_ctx["street_view_b64"] = uri
                logger.info("REPORT_IMG: street view OK (%s)", sv_path)
    except Exception as exc:
        logger.warning("REPORT_IMG: street view failed — %s", exc)

    return img_ctx


# ── Context sanitization ─────────────────────────────────────────────────────

# ── Main entry point ─────────────────────────────────────────────────────────

def generate_report(deal, pdf_path: str | Path | None = None) -> Path:
    """Generate the Playwright PDF for a deal.

    Parameters
    ----------
    deal     : DealData — fully populated upstream (post-financials).
    pdf_path : optional override; defaults to
               outputs/{deal_id}_report_playwright.pdf.

    Returns
    -------
    Path to the generated PDF.
    """
    # ── Resolve output path ───────────────────────────────────────
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    if pdf_path is None:
        pdf_path = OUTPUTS_DIR / f"{deal.deal_id or 'unknown'}_report.pdf"
    pdf_path = Path(pdf_path)

    # ── Generate narratives (Sonnet Prompts 4-MASTER + 5D) ───────
    try:
        generate_narratives(deal)
    except Exception as exc:
        logger.error("REPORT: narrative generation failed — %s", exc, exc_info=True)
        raise RuntimeError(f"report_builder: narrative generation failed: {exc}") from exc

    # ── Build template context ───────────────────────────────────
    try:
        ctx = build_context(deal)
    except Exception as exc:
        logger.error("REPORT: build_context failed — %s", exc, exc_info=True)
        raise RuntimeError(f"report_builder: context build failed: {exc}") from exc
    logger.info("REPORT: context built (%d keys)", len(ctx))

    # ── Image layer (independent, defensive) ─────────────────────
    ctx.update(_build_image_context(deal))

    # ── Jinja2 render ────────────────────────────────────────────
    env = Environment(
        loader=FileSystemLoader(str(WORD_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["currency"] = _fmt_currency
    env.filters["percent"]  = _fmt_percent
    env.filters["multiple"] = _fmt_multiple

    try:
        template = env.get_template(_TEMPLATE_NAME)
        html_content = template.render(**ctx)
        logger.info("REPORT: HTML rendered (%d chars)", len(html_content))
    except Exception as exc:
        logger.error("REPORT: Jinja2 render failed — %s", exc, exc_info=True)
        raise RuntimeError(f"report_builder: HTML render failed: {exc}") from exc

    # Load the CSS once and pass as <style> — avoids Playwright's need to
    # resolve <link href="report.css"> against a file:// base URL.
    css_path = WORD_TEMPLATES_DIR / _CSS_NAME
    css_text = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    html_content = re.sub(
        r'<link\s+rel="stylesheet"\s+href="report\.css"\s*/?>',
        f"<style>{css_text}</style>",
        html_content,
        count=1,
    )

    # ── Playwright render to PDF ─────────────────────────────────
    # Playwright's sync API refuses to run on a thread that has a live
    # asyncio event loop (main.py's request handlers are async). Detect
    # that case and hop to a worker thread.
    # Footer: centered page number only (no "DealDesk" label, no total
    # pages). Playwright substitutes <span class="pageNumber"></span> at
    # print time.
    footer_template = (
        '<div style="width:100%;'
        'font-size:8pt;color:#6B6460;'
        "font-family:'EB Garamond',serif;"
        'text-align:center;">'
        '<span class="pageNumber"></span>'
        '</div>'
    )
    header_template = '<div></div>'   # empty but required when footer is shown

    def _do_render() -> None:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.set_content(html_content, wait_until="load", timeout=30000)
            page.pdf(
                path=str(pdf_path),
                format="Letter",
                print_background=True,
                prefer_css_page_size=True,
                display_header_footer=True,
                header_template=header_template,
                footer_template=footer_template,
                # Reserve bottom space for the footer; keep others at 0.
                margin={"top": "0", "bottom": "0.45in", "left": "0", "right": "0"},
            )
            browser.close()

    try:
        import asyncio
        import concurrent.futures
        try:
            asyncio.get_running_loop()
            in_async_loop = True
        except RuntimeError:
            in_async_loop = False

        if in_async_loop:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                ex.submit(_do_render).result(timeout=120)
        else:
            _do_render()
    except Exception as exc:
        logger.error("REPORT: Playwright render failed — %s", exc, exc_info=True)
        raise RuntimeError(f"report_builder: Playwright PDF failed: {exc}") from exc

    size_kb = pdf_path.stat().st_size / 1024.0
    logger.info("REPORT: PDF written %s (%.1f KB)", pdf_path, size_kb)
    return pdf_path
