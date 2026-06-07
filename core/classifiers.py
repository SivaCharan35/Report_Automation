"""
Risk classification functions for Flood and Heat hazards.
Thresholds match the ResSolv™ scoring methodology used in app.py.
"""

from __future__ import annotations

RISK_LABELS: dict[int, str] = {
    1: "Very Low",
    2: "Low",
    3: "Moderate",
    4: "High",
    5: "Very High",
}

RISK_ORDER: list[str] = ["Very Low", "Low", "Moderate", "High", "Very High"]


# ── Flood ─────────────────────────────────────────────────────────────────────

def classify_flood_rcp(score: float) -> str:
    if score <= 1.0: return "Very Low"
    if score <= 2.0: return "Low"
    if score <= 3.0: return "Moderate"
    if score <= 4.0: return "High"
    return "Very High"


def classify_flood_ssp(score: float) -> str:
    if score <= 1.1: return "Very Low"
    if score <= 2.1: return "Low"
    if score <= 3.1: return "Moderate"
    if score <= 4.1: return "High"
    return "Very High"


def classify_current_flood(score: float) -> str:
    s = round(score)
    return RISK_LABELS.get(s, classify_flood_rcp(score))


# ── Heat ──────────────────────────────────────────────────────────────────────

def classify_heat_rcp(score: float) -> str:
    if score <= 2.7: return "Very Low"
    if score <= 3.7: return "Low"
    if score <= 4.7: return "Moderate"
    if score <= 5.7: return "High"
    return "Very High"


def classify_heat_ssp(score: float) -> str:
    if score <= 3.0: return "Very Low"
    if score <= 4.0: return "Low"
    if score <= 5.0: return "Moderate"
    if score <= 6.0: return "High"
    return "Very High"


def classify_current_heat(score: float) -> str:
    s = round(score)
    return RISK_LABELS.get(s, classify_heat_rcp(score))
