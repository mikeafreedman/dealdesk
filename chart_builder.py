"""
chart_builder.py — DealDesk Financial Chart Generator
=======================================================
Generates 8 PNG chart images for the PDF report using matplotlib.

    Figure 8.1  — Demographic Trends (2x2 grid)
    Figure 9.1  — Supply Pipeline (skipped when no data)
    Figure 12.1 — Pro Forma Charts (NOI/CFBT bars + DSCR line)
    Figure 12.2 — IRR Sensitivity Heatmap (7x7)
    Figure 13.1 — Capital Stack (stacked bar)
    Figure 13.2 — Financing Options Comparison
    Figure 16.1 — Risk Matrix (4x4 scatter)
    Figure 18.1 — Gantt Chart (project timeline)

Each function returns PNG bytes or None on failure.
Pipeline continues cleanly if any chart fails.

Called by word_builder.py during report generation.
"""

from __future__ import annotations

import io
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)

# ── DealDesk brand colors ─────────────────────────────────────────────────
C_WALNUT    = "#2B1F14"   # deep walnut — headers, axis labels
C_SAGE      = "#5C8A6B"   # sage green — primary bars/fills
C_SAGE_LT   = "#B2C9B4"  # light sage — secondary bars
C_PARCHMENT = "#F5EFE4"  # parchment — background
C_BRONZE    = "#8B6914"   # bronze — accent lines, highlights
C_FAIL      = "#8B2020"   # deep red — fail/negative
C_WATCH     = "#5C3D26"   # walnut brown — watch
C_PASS      = "#B2C9B4"   # light sage — pass
C_GRID      = "#E0D8CC"   # light grid lines

CHART_W = 7.5   # inches
CHART_H = 4.0   # inches
DPI     = 96


def _fig_to_bytes(fig) -> bytes:
    """Convert a matplotlib figure to PNG bytes and close it."""
    import matplotlib.pyplot as plt
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight",
                facecolor=C_PARCHMENT, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _base_fig(w=CHART_W, h=CHART_H):
    """Create a base figure with DealDesk parchment background."""
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(w, h))
    fig.patch.set_facecolor(C_PARCHMENT)
    ax.set_facecolor(C_PARCHMENT)
    for spine in ax.spines.values():
        spine.set_color(C_GRID)
    ax.tick_params(colors=C_WALNUT, labelsize=8)
    ax.xaxis.label.set_color(C_WALNUT)
    ax.yaxis.label.set_color(C_WALNUT)
    ax.title.set_color(C_WALNUT)
    return fig, ax


def _fmt_dollar(val):
    if abs(val) >= 1_000_000:
        return f"${val/1_000_000:.1f}M"
    if abs(val) >= 1_000:
        return f"${val/1_000:.0f}K"
    return f"${val:.0f}"


# ═══════════════════════════════════════════════════════════════════════════
# FIGURE 8.1 — DEMOGRAPHIC TRENDS (2x2 grid)
# ═══════════════════════════════════════════════════════════════════════════

def build_demographic_chart(deal) -> Optional[bytes]:
    """
    Figure 8.1 — 2x2 demographic snapshot:
    Population, Median HH Income, Renter %, Unemployment Rate.
    Uses Census ACS data already in deal.market_data.
    """
    try:
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        md = deal.market_data

        fig, axes = plt.subplots(2, 2, figsize=(CHART_W, CHART_H + 0.5))
        fig.patch.set_facecolor(C_PARCHMENT)
        fig.suptitle("Key Demographic Indicators", fontsize=11,
                     fontweight="bold", color=C_WALNUT, y=1.01)

        metrics = [
            ("Population (3-mi)", md.population_3mi, "{:,.0f}", C_SAGE),
            ("Median HH Income (3-mi)", md.median_hh_income_3mi, "${:,.0f}", C_BRONZE),
            ("Renter Occupancy (3-mi)", md.pct_renter_occ_3mi, "{:.1%}", C_SAGE_LT),
            ("Unemployment Rate", md.unemployment_rate, "{:.1%}", C_FAIL),
        ]

        for ax, (label, value, fmt, color) in zip(axes.flat, metrics):
            ax.set_facecolor(C_PARCHMENT)
            for spine in ax.spines.values():
                spine.set_color(C_GRID)

            if value is not None:
                display = fmt.format(value)
                ax.text(0.5, 0.55, display, transform=ax.transAxes,
                        ha="center", va="center", fontsize=18,
                        fontweight="bold", color=color)
            else:
                ax.text(0.5, 0.55, "N/A", transform=ax.transAxes,
                        ha="center", va="center", fontsize=16, color=C_GRID)

            ax.text(0.5, 0.20, label, transform=ax.transAxes,
                    ha="center", va="center", fontsize=8,
                    color=C_WALNUT, style="italic")
            ax.set_xticks([])
            ax.set_yticks([])
            ax.text(0.5, 0.88, "2022 ACS", transform=ax.transAxes,
                    ha="center", fontsize=7, color=C_GRID)

        fig.tight_layout()
        result = _fig_to_bytes(fig)
        logger.info("Demographic chart built — %d bytes", len(result))
        return result

    except Exception as exc:
        logger.error("Demographic chart failed: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# FIGURE 12.1 — PRO FORMA CHARTS (NOI/CFBT bars + DSCR line)
# ═══════════════════════════════════════════════════════════════════════════

def build_proforma_chart(deal) -> Optional[bytes]:
    """
    Figure 12.1 — 10-year NOI and CFBT bar chart with DSCR line overlay.
    Requires deal.financial_outputs.pro_forma_years to be populated.
    """
    try:
        import matplotlib.pyplot as plt
        import numpy as np

        pfy = deal.financial_outputs.pro_forma_years
        if not pfy:
            logger.info("Pro forma chart skipped — no pro_forma_years data")
            return None

        years  = [row.get("year", i + 1) for i, row in enumerate(pfy)]
        noi    = [row.get("noi", 0) for row in pfy]
        cfbt   = [row.get("cfbt", row.get("free_cash_flow", 0)) for row in pfy]
        dscr   = [row.get("dscr", 0) for row in pfy]

        x = np.arange(len(years))
        width = 0.35

        fig, ax1 = plt.subplots(figsize=(CHART_W, CHART_H))
        fig.patch.set_facecolor(C_PARCHMENT)
        ax1.set_facecolor(C_PARCHMENT)

        bars1 = ax1.bar(x - width/2, noi,  width, label="NOI",  color=C_SAGE,    alpha=0.85)
        bars2 = ax1.bar(x + width/2, cfbt, width, label="CFBT", color=C_BRONZE, alpha=0.85)

        ax1.set_xlabel("Year", color=C_WALNUT, fontsize=9)
        ax1.set_ylabel("Amount ($)", color=C_WALNUT, fontsize=9)
        ax1.set_xticks(x)
        ax1.set_xticklabels([f"Yr {y}" for y in years], fontsize=8, color=C_WALNUT)
        ax1.yaxis.set_major_formatter(
            plt.FuncFormatter(lambda v, _: _fmt_dollar(v))
        )
        ax1.tick_params(colors=C_WALNUT)
        ax1.set_facecolor(C_PARCHMENT)
        for spine in ax1.spines.values():
            spine.set_color(C_GRID)
        ax1.yaxis.grid(True, color=C_GRID, linewidth=0.5, linestyle="--")
        ax1.set_axisbelow(True)

        # DSCR line on secondary axis
        ax2 = ax1.twinx()
        valid_dscr = [d for d in dscr if d and d != 0]
        if valid_dscr:
            ax2.plot(x, dscr, color=C_FAIL, linewidth=1.5,
                     marker="o", markersize=4, label="DSCR", zorder=5)
            ax2.axhline(y=1.20, color=C_FAIL, linestyle="--",
                        linewidth=0.8, alpha=0.6, label="1.20x min")
            ax2.set_ylabel("DSCR", color=C_FAIL, fontsize=9)
            ax2.tick_params(colors=C_FAIL, labelsize=8)
        ax2.set_facecolor(C_PARCHMENT)
        for spine in ax2.spines.values():
            spine.set_color(C_GRID)

        # Combined legend
        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(lines1 + lines2, labels1 + labels2,
                   fontsize=8, loc="upper left",
                   facecolor=C_PARCHMENT, edgecolor=C_GRID)

        ax1.set_title("10-Year Pro Forma: NOI & Cash Flow Before Tax",
                      fontsize=10, fontweight="bold", color=C_WALNUT, pad=8)

        fig.tight_layout()
        result = _fig_to_bytes(fig)
        logger.info("Pro forma chart built — %d bytes", len(result))
        return result

    except Exception as exc:
        logger.error("Pro forma chart failed: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# FIGURE 12.2 — IRR SENSITIVITY HEATMAP
# ═══════════════════════════════════════════════════════════════════════════

def _safe_numeric_matrix(matrix):
    """Convert sensitivity matrix to float, replacing 'N/A' with np.nan.

    Protects downstream numpy ops (max, imshow, arithmetic) from blowing up
    on string sentinels when the underwriting produced non-numeric cells
    (e.g., negative NOI, failed IRR solve).
    """
    import numpy as np
    result = []
    for row in matrix:
        result.append([
            float(v) if v not in ('N/A', None, '', 'nan') else np.nan
            for v in row
        ])
    return np.array(result, dtype=float)


def build_irr_heatmap(deal) -> Optional[bytes]:
    """
    Figure 12.2 — IRR sensitivity heatmap (rent growth vs exit cap rate).
    Requires deal.financial_outputs.sensitivity_matrix to be populated.
    """
    try:
        import matplotlib.pyplot as plt
        import matplotlib.colors as mcolors
        import numpy as np

        fo = deal.financial_outputs
        matrix = fo.sensitivity_matrix
        rg_axis = fo.sensitivity_axis_rent_growth
        cap_axis = fo.sensitivity_axis_exit_cap

        if not matrix or not rg_axis or not cap_axis:
            logger.info("IRR heatmap skipped — no sensitivity matrix data")
            return None

        data = _safe_numeric_matrix(matrix)
        if np.all(np.isnan(data)):
            logger.info("IRR heatmap skipped — all values N/A (negative NOI deal)")
            return None
        if np.nanmax(data) == 0:
            logger.info("IRR heatmap skipped — all-zero sensitivity matrix")
            return None

        fig, ax = plt.subplots(figsize=(CHART_W, CHART_H))
        fig.patch.set_facecolor(C_PARCHMENT)
        ax.set_facecolor(C_PARCHMENT)

        # Color map: red (fail) → parchment (neutral) → sage (pass)
        colors_list = [C_FAIL, "#D4A574", C_PARCHMENT, C_SAGE_LT, C_SAGE]
        cmap = mcolors.LinearSegmentedColormap.from_list("dealdesk", colors_list)

        target_irr = deal.assumptions.target_lp_irr or 0.15
        im = ax.imshow(data * 100, cmap=cmap, aspect="auto",
                       vmin=0, vmax=max(np.nanmax(data) * 100, target_irr * 100 * 1.5))

        # Axis labels
        ax.set_xticks(range(len(cap_axis)))
        ax.set_xticklabels([f"{c:.1%}" for c in cap_axis], fontsize=7, color=C_WALNUT)
        ax.set_yticks(range(len(rg_axis)))
        ax.set_yticklabels([f"{r:.1%}" for r in rg_axis], fontsize=7, color=C_WALNUT)
        ax.set_xlabel("Exit Cap Rate", fontsize=9, color=C_WALNUT)
        ax.set_ylabel("Annual Rent Growth", fontsize=9, color=C_WALNUT)
        ax.set_title("LP IRR Sensitivity — Rent Growth vs. Exit Cap Rate",
                     fontsize=10, fontweight="bold", color=C_WALNUT, pad=8)

        # Cell annotations
        for i in range(len(rg_axis)):
            for j in range(len(cap_axis)):
                raw = data[i, j]
                if np.isnan(raw):
                    ax.text(j, i, "N/A", ha="center", va="center",
                            fontsize=7, color=C_WALNUT, fontweight="bold")
                    continue
                val = raw * 100
                color = "white" if val < target_irr * 100 * 0.7 else C_WALNUT
                ax.text(j, i, f"{val:.1f}%", ha="center", va="center",
                        fontsize=7, color=color, fontweight="bold")

        # Highlight base case (middle cell)
        mid_r = len(rg_axis) // 2
        mid_c = len(cap_axis) // 2
        rect = plt.Rectangle((mid_c - 0.5, mid_r - 0.5), 1, 1,
                              linewidth=2, edgecolor=C_BRONZE, facecolor="none")
        ax.add_patch(rect)
        ax.text(mid_c, mid_r - 0.75, "BASE", ha="center", fontsize=6,
                color=C_BRONZE, fontweight="bold")

        plt.colorbar(im, ax=ax, label="LP IRR (%)", shrink=0.8)
        fig.tight_layout()
        result = _fig_to_bytes(fig)
        logger.info("IRR heatmap built — %d bytes", len(result))
        return result

    except Exception as exc:
        logger.error("IRR heatmap failed: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# FIGURE 13.1 — CAPITAL STACK
# ═══════════════════════════════════════════════════════════════════════════

def build_capital_stack_chart(deal) -> Optional[bytes]:
    """
    Figure 13.1 — Capital stack stacked bar showing debt vs equity split.
    """
    try:
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches

        fo = deal.financial_outputs
        a  = deal.assumptions

        total_uses   = fo.total_uses or a.purchase_price or 0
        senior_debt  = fo.initial_loan_amount or 0
        equity_gap   = total_uses - senior_debt
        logger.info("Capital stack equity gap: %.2f  (total_uses=%.2f - senior_debt=%.2f)",
                     equity_gap, total_uses, senior_debt)
        gp_equity    = equity_gap * a.gp_equity_pct
        lp_equity    = equity_gap * a.lp_equity_pct

        if total_uses <= 0:
            logger.info("Capital stack chart skipped — no total_uses data")
            return None

        fig, ax = plt.subplots(figsize=(4.0, CHART_H))
        fig.patch.set_facecolor(C_PARCHMENT)
        ax.set_facecolor(C_PARCHMENT)

        bar_w = 0.5
        bars = [
            ("Senior Debt",   senior_debt,  C_WALNUT),
            ("LP Equity",     lp_equity,    C_SAGE),
            ("GP Equity",     gp_equity,    C_BRONZE),
        ]

        bottom = 0
        patches = []
        for label, val, color in bars:
            if val > 0:
                ax.bar(0, val, bar_w, bottom=bottom, color=color, alpha=0.9)
                mid = bottom + val / 2
                pct = val / total_uses * 100
                ax.text(0.35, mid, f"{label}\n{_fmt_dollar(val)} ({pct:.0f}%)",
                        va="center", fontsize=8, color=C_PARCHMENT if color == C_WALNUT else C_WALNUT)
                patches.append(mpatches.Patch(color=color, label=f"{label}: {_fmt_dollar(val)}"))
                bottom += val

        ax.set_xlim(-0.5, 1.5)
        ax.set_ylim(0, total_uses * 1.15)
        ax.set_xticks([])
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _fmt_dollar(v)))
        ax.tick_params(colors=C_WALNUT, labelsize=8)
        ax.set_title(f"Capital Stack\nTotal: {_fmt_dollar(total_uses)}",
                     fontsize=10, fontweight="bold", color=C_WALNUT)
        ax.legend(handles=patches, fontsize=8, loc="upper right",
                  facecolor=C_PARCHMENT, edgecolor=C_GRID)
        for spine in ax.spines.values():
            spine.set_color(C_GRID)

        fig.tight_layout()
        result = _fig_to_bytes(fig)
        logger.info("Capital stack chart built — %d bytes", len(result))
        return result

    except Exception as exc:
        logger.error("Capital stack chart failed: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# FIGURE 13.2 — FINANCING OPTIONS COMPARISON
# ═══════════════════════════════════════════════════════════════════════════

def build_financing_options_chart(deal) -> Optional[bytes]:
    """
    Figure 13.2 — Horizontal bar comparison of financing options.
    Shows active refi events and the acquisition loan.
    """
    try:
        import matplotlib.pyplot as plt
        import numpy as np

        a = deal.assumptions
        active_refis = [r for r in a.refi_events if r.active]

        # Build options list
        options = []
        # Base loan
        if a.purchase_price > 0:
            loan_amt = a.purchase_price * a.ltv_pct
            options.append({
                "label": f"Acquisition Loan\n{a.ltv_pct:.0%} LTV @ {a.interest_rate:.2%}",
                "amount": loan_amt,
                "rate": a.interest_rate,
                "recommended": not active_refis,
            })
        # Refi events
        for i, refi in enumerate(active_refis):
            options.append({
                "label": f"Refi Yr {refi.year}\n{refi.ltv:.0%} LTV @ {refi.rate:.2%}",
                "amount": refi.new_loan_amount,
                "rate": refi.rate,
                "recommended": True,
            })

        if not options:
            logger.info("Financing options chart skipped — no loan data")
            return None

        fig, ax = plt.subplots(figsize=(CHART_W, max(2.5, len(options) * 1.2)))
        fig.patch.set_facecolor(C_PARCHMENT)
        ax.set_facecolor(C_PARCHMENT)

        labels  = [o["label"] for o in options]
        amounts = [o["amount"] for o in options]
        colors  = [C_BRONZE if o["recommended"] else C_SAGE_LT for o in options]

        y = np.arange(len(labels))
        bars = ax.barh(y, amounts, color=colors, alpha=0.85, height=0.5)

        for bar, amt, opt in zip(bars, amounts, options):
            ax.text(bar.get_width() * 1.01, bar.get_y() + bar.get_height() / 2,
                    f"{_fmt_dollar(amt)}  {opt['rate']:.2%}",
                    va="center", fontsize=8, color=C_WALNUT)
            if opt["recommended"]:
                ax.text(-amounts[0] * 0.02, bar.get_y() + bar.get_height() / 2,
                        "★", va="center", ha="right", fontsize=10, color=C_BRONZE)

        ax.set_yticks(y)
        ax.set_yticklabels(labels, fontsize=8, color=C_WALNUT)
        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _fmt_dollar(v)))
        ax.tick_params(colors=C_WALNUT, labelsize=8)
        ax.set_title("Financing Options Comparison  ★ = Recommended",
                     fontsize=10, fontweight="bold", color=C_WALNUT, pad=8)
        ax.set_xlabel("Loan Proceeds", fontsize=9, color=C_WALNUT)
        for spine in ax.spines.values():
            spine.set_color(C_GRID)
        ax.xaxis.grid(True, color=C_GRID, linewidth=0.5, linestyle="--")
        ax.set_axisbelow(True)

        fig.tight_layout()
        result = _fig_to_bytes(fig)
        logger.info("Financing options chart built — %d bytes", len(result))
        return result

    except Exception as exc:
        logger.error("Financing options chart failed: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# FIGURE 16.1 — RISK MATRIX (4x4 scatter)
# ═══════════════════════════════════════════════════════════════════════════

def build_risk_matrix_chart(deal) -> Optional[bytes]:
    """
    Figure 16.1 — 4x4 Risk Matrix: Likelihood vs. Impact scatter plot.
    Plots DD flags by likelihood/impact score with color coding.
    """
    try:
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches

        flags = deal.dd_flags
        if not flags:
            logger.info("Risk matrix chart skipped — no DD flags")
            return None

        fig, ax = plt.subplots(figsize=(CHART_W, CHART_H))
        fig.patch.set_facecolor(C_PARCHMENT)
        ax.set_facecolor(C_PARCHMENT)

        color_map = {"RED": C_FAIL, "AMBER": C_BRONZE, "GREEN": C_SAGE}
        plotted = {}

        for i, flag in enumerate(flags):
            # Assign likelihood/impact from flag color (approximation)
            color_key = flag.color.value if hasattr(flag.color, "value") else str(flag.color)
            base_x = {"RED": 3.5, "AMBER": 2.5, "GREEN": 1.5}.get(color_key, 2.0)
            base_y = {"RED": 3.5, "AMBER": 2.5, "GREEN": 1.5}.get(color_key, 2.0)
            # Jitter to avoid overlap
            key = (round(base_x), round(base_y))
            offset = plotted.get(key, 0)
            x = base_x + (offset % 3) * 0.2 - 0.2
            y = base_y + (offset // 3) * 0.2
            plotted[key] = offset + 1

            color = color_map.get(color_key, C_WATCH)
            ax.scatter(x, y, s=120, color=color, zorder=5, alpha=0.85)
            ax.annotate(str(i + 1), (x, y), fontsize=7, ha="center",
                        va="center", color="white", fontweight="bold", zorder=6)

        # 4x4 grid
        ax.set_xlim(0, 4)
        ax.set_ylim(0, 4)
        ax.set_xticks([1, 2, 3, 4])
        ax.set_yticks([1, 2, 3, 4])
        ax.set_xticklabels(["Low", "Med-Low", "Med-High", "High"],
                            fontsize=8, color=C_WALNUT)
        ax.set_yticklabels(["Low", "Med-Low", "Med-High", "High"],
                            fontsize=8, color=C_WALNUT)
        ax.set_xlabel("Impact", fontsize=9, color=C_WALNUT)
        ax.set_ylabel("Likelihood", fontsize=9, color=C_WALNUT)
        ax.set_title("Due Diligence Risk Matrix",
                     fontsize=10, fontweight="bold", color=C_WALNUT, pad=8)

        # Background quadrant shading
        ax.fill_between([2, 4], [2, 2], [4, 4], alpha=0.06, color=C_FAIL)
        ax.fill_between([0, 2], [2, 2], [4, 4], alpha=0.04, color=C_BRONZE)
        ax.fill_between([0, 4], [0, 0], [2, 2], alpha=0.04, color=C_SAGE)

        ax.grid(True, color=C_GRID, linewidth=0.5)
        for spine in ax.spines.values():
            spine.set_color(C_GRID)

        patches = [
            mpatches.Patch(color=C_FAIL, label="High Risk (RED)"),
            mpatches.Patch(color=C_BRONZE, label="Medium Risk (AMBER)"),
            mpatches.Patch(color=C_SAGE, label="Low Risk (GREEN)"),
        ]
        ax.legend(handles=patches, fontsize=8, loc="lower right",
                  facecolor=C_PARCHMENT, edgecolor=C_GRID)

        fig.tight_layout()
        result = _fig_to_bytes(fig)
        logger.info("Risk matrix chart built — %d bytes", len(result))
        return result

    except Exception as exc:
        logger.error("Risk matrix chart failed: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# FIGURE 18.1 — GANTT CHART
# ═══════════════════════════════════════════════════════════════════════════

def build_gantt_chart(deal) -> Optional[bytes]:
    """
    Figure 18.1 — Project timeline Gantt chart.
    Builds from standard phases based on strategy and hold period.
    """
    try:
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from models.models import InvestmentStrategy

        a = deal.assumptions
        strategy = deal.investment_strategy

        # Define phases based on strategy
        if strategy == InvestmentStrategy.VALUE_ADD:
            phases = [
                ("Due Diligence & Closing",   0,   3,  C_BRONZE),
                ("Construction / Renovation", 3,   3 + a.const_period_months, C_FAIL),
                ("Lease-Up",                  3 + a.const_period_months,
                                              3 + a.const_period_months + a.leaseup_period_months, C_WATCH),
                ("Stabilized Operations",     3 + a.const_period_months + a.leaseup_period_months,
                                              a.hold_period * 12, C_SAGE),
                ("Disposition",               a.hold_period * 12, a.hold_period * 12 + 3, C_SAGE_LT),
            ]
        elif strategy == InvestmentStrategy.OPPORTUNISTIC:
            phases = [
                ("Due Diligence & Closing",   0,  3,  C_BRONZE),
                ("Construction",              3,  3 + a.sale_const_period_months, C_FAIL),
                ("Marketing & Sales",         3 + a.sale_const_period_months,
                                              3 + a.sale_const_period_months + a.sale_marketing_period_months,
                                              C_WATCH),
                ("Closeout",                  3 + a.sale_const_period_months + a.sale_marketing_period_months,
                                              3 + a.sale_const_period_months + a.sale_marketing_period_months + 3,
                                              C_SAGE_LT),
            ]
        else:  # STABILIZED
            phases = [
                ("Due Diligence & Closing",  0,  3,  C_BRONZE),
                ("Stabilized Hold",          3,  a.hold_period * 12, C_SAGE),
                ("Disposition",              a.hold_period * 12, a.hold_period * 12 + 3, C_SAGE_LT),
            ]

        # Add refi events as milestones
        refi_milestones = [
            (f"Refi Yr {r.year}", r.year * 12) for r in a.refi_events if r.active
        ]

        fig, ax = plt.subplots(figsize=(CHART_W, max(3.0, len(phases) * 0.7)))
        fig.patch.set_facecolor(C_PARCHMENT)
        ax.set_facecolor(C_PARCHMENT)

        for i, (label, start, end, color) in enumerate(phases):
            duration = max(end - start, 1)
            ax.barh(i, duration, left=start, height=0.5,
                    color=color, alpha=0.85, edgecolor=C_GRID, linewidth=0.5)
            ax.text(start + duration / 2, i, label,
                    ha="center", va="center", fontsize=7.5,
                    color="white" if color in [C_WALNUT, C_FAIL, C_WATCH] else C_WALNUT,
                    fontweight="bold")

        # Refi milestone markers
        for label, month in refi_milestones:
            ax.axvline(month, color=C_BRONZE, linewidth=1.5, linestyle="--", alpha=0.7)
            ax.text(month, len(phases) - 0.3, label,
                    rotation=90, fontsize=7, color=C_BRONZE, va="top", ha="right")

        # X axis in years
        max_month = max(end for _, _, end, _ in phases) + 3
        year_ticks = range(0, max_month + 12, 12)
        ax.set_xticks(list(year_ticks))
        ax.set_xticklabels([f"Yr {t//12}" if t > 0 else "Start"
                            for t in year_ticks], fontsize=8, color=C_WALNUT)
        ax.set_yticks([])
        ax.set_xlabel("Project Timeline", fontsize=9, color=C_WALNUT)
        ax.set_title("Project Timeline & Milestone Schedule",
                     fontsize=10, fontweight="bold", color=C_WALNUT, pad=8)
        ax.set_xlim(-1, max_month + 3)

        for spine in ax.spines.values():
            spine.set_color(C_GRID)
        ax.xaxis.grid(True, color=C_GRID, linewidth=0.5, linestyle="--")
        ax.set_axisbelow(True)

        fig.tight_layout()
        result = _fig_to_bytes(fig)
        logger.info("Gantt chart built — %d bytes", len(result))
        return result

    except Exception as exc:
        logger.error("Gantt chart failed: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# KPI DASHBOARD — 12-metric traffic light grid
# ═══════════════════════════════════════════════════════════════════════════

def build_kpi_dashboard(deal) -> Optional[bytes]:
    """Generate a 3×4 KPI traffic-light dashboard PNG."""
    try:
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches

        fo = deal.financial_outputs
        a = deal.assumptions

        # ── Compute the 12 metrics ───────────────────────────────
        yr1_noi = None
        yr1_egi = None
        yr1_opex = None
        if fo.pro_forma_years and len(fo.pro_forma_years) > 0:
            yr1 = fo.pro_forma_years[0]
            yr1_noi = yr1.get("noi")
            yr1_egi = yr1.get("egi")
            yr1_opex = yr1.get("opex")

        cap_rate = fo.going_in_cap_rate
        dscr = fo.dscr_yr1
        coc = fo.cash_on_cash_yr1
        lp_irr = fo.lp_irr
        em = fo.lp_equity_multiple
        ltv = a.ltv_pct
        vacancy = a.vacancy_rate

        # Compute price metrics from assumptions
        price = a.purchase_price or 0
        gba = a.gba_sf or 0
        n_units = a.num_units or 0
        price_per_sf = price / gba if gba > 0 else None
        price_per_unit = price / n_units if n_units > 0 else None

        # Debt yield = NOI / loan amount
        debt_yield = None
        if yr1_noi and fo.initial_loan_amount and fo.initial_loan_amount > 0:
            debt_yield = yr1_noi / fo.initial_loan_amount

        # NOI margin = NOI / EGI
        noi_margin = None
        if yr1_noi and yr1_egi and yr1_egi > 0:
            noi_margin = yr1_noi / yr1_egi

        # Expense ratio = OpEx / EGI
        expense_ratio = None
        if yr1_opex and yr1_egi and yr1_egi > 0:
            expense_ratio = yr1_opex / yr1_egi

        # ── Thresholds: (metric, value, format, pass_test, watch_test) ──
        # pass_test(v) → PASS, watch_test(v) → WATCH, else FAIL
        metrics = [
            ("Cap Rate",       cap_rate,       _kpi_fmt_pct,
             lambda v: v >= 0.06, lambda v: v >= 0.045),
            ("DSCR",           dscr,           _kpi_fmt_x,
             lambda v: v >= 1.25, lambda v: v >= 1.10),
            ("CoC Return",     coc,            _kpi_fmt_pct,
             lambda v: v >= 0.08, lambda v: v >= 0.05),
            ("LP IRR",         lp_irr,         _kpi_fmt_pct,
             lambda v: v >= a.target_lp_irr, lambda v: v >= a.min_lp_irr),
            ("Equity Multiple", em,            _kpi_fmt_x,
             lambda v: v >= 2.0, lambda v: v >= 1.5),
            ("Debt Yield",     debt_yield,     _kpi_fmt_pct,
             lambda v: v >= 0.10, lambda v: v >= 0.08),
            ("LTV",            ltv,            _kpi_fmt_pct,
             lambda v: v <= 0.75, lambda v: v <= 0.80),
            ("NOI Margin",     noi_margin,     _kpi_fmt_pct,
             lambda v: v >= 0.60, lambda v: v >= 0.50),
            ("Price / SF",     price_per_sf,   _kpi_fmt_dollar,
             lambda v: v <= 250, lambda v: v <= 350),
            ("Price / Unit",   price_per_unit, _kpi_fmt_dollar_k,
             lambda v: v <= 150_000, lambda v: v <= 200_000),
            ("Vacancy Rate",   vacancy,        _kpi_fmt_pct,
             lambda v: v <= 0.07, lambda v: v <= 0.10),
            ("Expense Ratio",  expense_ratio,  _kpi_fmt_pct,
             lambda v: v <= 0.45, lambda v: v <= 0.55),
        ]

        # ── Build the 3×4 grid figure ────────────────────────────
        n_cols, n_rows = 4, 3
        cell_w, cell_h = 1.8, 0.9
        fig_w = n_cols * cell_w + 0.4
        fig_h = n_rows * cell_h + 0.7
        fig, ax = plt.subplots(figsize=(fig_w, fig_h))
        fig.patch.set_facecolor(C_PARCHMENT)
        ax.set_facecolor(C_PARCHMENT)
        ax.set_xlim(0, n_cols * cell_w)
        ax.set_ylim(0, n_rows * cell_h)
        ax.axis("off")
        ax.set_title("Key Performance Indicators", fontsize=12,
                     fontweight="bold", color=C_WALNUT, pad=10)

        for idx, (name, value, fmt_fn, pass_fn, watch_fn) in enumerate(metrics):
            col = idx % n_cols
            row = n_rows - 1 - idx // n_cols  # top-to-bottom
            x = col * cell_w + cell_w / 2
            y = row * cell_h + cell_h / 2

            # Determine status
            if value is None:
                status, color = "N/A", C_GRID
                display = "—"
            else:
                display = fmt_fn(value)
                if pass_fn(value):
                    status, color = "PASS", C_PASS
                elif watch_fn(value):
                    status, color = "WATCH", C_WATCH
                else:
                    status, color = "FAIL", C_FAIL

            # Tile background
            tile = mpatches.FancyBboxPatch(
                (col * cell_w + 0.08, row * cell_h + 0.06),
                cell_w - 0.16, cell_h - 0.12,
                boxstyle="round,pad=0.05", facecolor="white",
                edgecolor=C_GRID, linewidth=0.8)
            ax.add_patch(tile)

            # Metric name
            ax.text(x, y + 0.22, name, ha="center", va="center",
                    fontsize=7, color=C_WALNUT, fontweight="bold")
            # Value
            ax.text(x, y + 0.02, display, ha="center", va="center",
                    fontsize=11, color=C_WALNUT, fontweight="bold")
            # Status pill
            pill_w, pill_h = 0.55, 0.18
            pill = mpatches.FancyBboxPatch(
                (x - pill_w / 2, y - 0.30), pill_w, pill_h,
                boxstyle="round,pad=0.04", facecolor=color,
                edgecolor="none")
            ax.add_patch(pill)
            pill_text_color = "white" if color in (C_FAIL, C_WATCH) else C_WALNUT
            ax.text(x, y - 0.21, status, ha="center", va="center",
                    fontsize=6, color=pill_text_color, fontweight="bold")

        fig.tight_layout()
        result = _fig_to_bytes(fig)
        logger.info("KPI dashboard built — %d bytes", len(result))
        return result

    except Exception as exc:
        logger.error("KPI dashboard failed: %s", exc)
        return None


def _kpi_fmt_pct(v):
    return f"{v:.1%}"

def _kpi_fmt_x(v):
    return f"{v:.2f}x"

def _kpi_fmt_dollar(v):
    return f"${v:,.0f}"

def _kpi_fmt_dollar_k(v):
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:,.0f}"


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

class ChartImages:
    """Container for all chart PNG byte strings."""
    def __init__(self):
        self.kpi_dashboard : Optional[bytes] = None
        self.demographic   : Optional[bytes] = None
        self.proforma      : Optional[bytes] = None
        self.irr_heatmap   : Optional[bytes] = None
        self.capital_stack : Optional[bytes] = None
        self.financing     : Optional[bytes] = None
        self.risk_matrix   : Optional[bytes] = None
        self.gantt         : Optional[bytes] = None
        # supply_pipeline intentionally omitted — skipped when no data


def build_all_charts(deal) -> ChartImages:
    """
    Build all charts. Each is attempted independently.
    Returns ChartImages container with PNG bytes or None for each chart.
    """
    logger.info("chart_builder: starting chart generation")
    charts = ChartImages()

    builders = [
        ("kpi_dashboard", build_kpi_dashboard),
        ("demographic",   build_demographic_chart),
        ("proforma",      build_proforma_chart),
        ("irr_heatmap",   build_irr_heatmap),
        ("capital_stack", build_capital_stack_chart),
        ("financing",     build_financing_options_chart),
        ("risk_matrix",   build_risk_matrix_chart),
        ("gantt",         build_gantt_chart),
    ]

    succeeded = 0
    for attr, fn in builders:
        try:
            result = fn(deal)
            setattr(charts, attr, result)
            if result:
                succeeded += 1
        except Exception as exc:
            logger.error("Chart '%s' error: %s", attr, exc)

    logger.info("chart_builder: %d/%d charts generated", succeeded, len(builders))
    return charts
