"""
Shared chart styling, colour constants, and helper for risk-score → RGBA mapping.
Import this in any script that generates matplotlib figures.
"""

from __future__ import annotations

import matplotlib.pyplot as plt

# ── Hazard colours ────────────────────────────────────────────────────────────
FLOOD_COLOR = "#2196F3"
HEAT_COLOR  = "#FF5722"

# ── Risk-level background colours (for tables) ────────────────────────────────
RISK_BG_MAP: dict[str, str] = {
    "Very Low":  "#D5E8D4",
    "Low":       "#DAE8FC",
    "Moderate":  "#FFF2CC",
    "High":      "#FFE6CC",
    "Very High": "#F8CECC",
}

# ── Risk-level face colours for map polygons (RGBA, 0-1 scale) ───────────────
_FACE = {
    "Very High": (235/255,  52/255,  52/255, 1.0),
    "High":      (235/255, 143/255,  52/255, 1.0),
    "Moderate":  (235/255, 183/255,  52/255, 1.0),
    "Low":       (235/255, 235/255,  52/255, 1.0),
    "Very Low":  ( 76/255, 235/255,  52/255, 1.0),
}


def score_to_rgba(score: float) -> tuple:
    """Map a continuous risk score (1-5) to an RGBA tuple for polygon fill."""
    if score >= 5.0: return _FACE["Very High"]
    if score >= 4.0: return _FACE["High"]
    if score >= 3.0: return _FACE["Moderate"]
    if score >= 2.0: return _FACE["Low"]
    return _FACE["Very Low"]


def set_chart_style() -> None:
    """Apply a consistent chart style, falling back gracefully."""
    for style in ("seaborn-v0_8-whitegrid", "ggplot", "default"):
        try:
            plt.style.use(style)
            return
        except Exception:
            continue
