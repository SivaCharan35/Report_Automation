"""
Module: ara_risk_insights — Risk Insights (Section 5.4)
Owns Steps 3–6 of the pipeline workflow.

This module is self-contained — all logic is copied directly from
scripts/7_appendices.py (appendix layer processing) and
scripts/9_risk_findings.py (LLM risk findings generation).
No numbered scripts are imported.

Execution order
───────────────
  Phase A — Appendix layer processing (logic from 7_appendices.py)
             Reads COG rasters + GeoJSON layers, generates PNG maps,
             computes susceptibility distributions, calls Claude for
             impact paragraphs, uploads assets, saves 7_appendices.json.

  Phase B — Risk findings generation (logic from 9_risk_findings.py)
             Generates 5 LLM-written bullet points per hazard.

  Steps 3–6 — Resolve placeholders and save section output.

Placeholders resolved
─────────────────────
  FLOOD_DETECTIVE_1 : buildings risk distribution finding (flood)
  FLOOD_DETECTIVE_2 : DEM / elevation layer finding (flood)
  FLOOD_DETECTIVE_3 : TWI layer finding (flood)
  HEAT_DETECTIVE_1  : buildings risk distribution finding (heat)
  HEAT_DETECTIVE_2  : NDVI layer finding (heat)
  HEAT_DETECTIVE_3  : NDBI layer finding (heat)

Context keys consumed
─────────────────────
  section_content    : str
  input_config       : dict
  risk_for           : str
  site_name          : str
  flood_risk_counts  : dict   — set by ara_exposure
  heat_risk_counts   : dict   — set by ara_exposure
  flood_geojson_path : Path | None
  heat_geojson_path  : Path | None
  output_dir         : Path
  assets_dir         : Path
  azure_base_path    : str

Context keys produced
─────────────────────
  appendix_map_paths    : dict[str, Path]   — local PNG paths per layer
  appendix_layer_urls   : dict[str, str]    — azure URL (or local) per layer
  appendix_stats        : dict[str, dict]   — susceptibility stats per layer
  appendix_layer_impacts: dict[str, str]    — LLM/fallback impact text per layer
  appendix_layers_json  : list[dict]        — full layer records (for ara_parametric)
  risk_findings         : dict              — {"flood": [...], "heat": [...]}
  resolved_content      : str
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np

import config
from core.storage import save_asset, save_section_output

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


# ═════════════════════════════════════════════════════════════════════════════
# PHASE A — Appendix layer processing  (from 7_appendices.py)
# ═════════════════════════════════════════════════════════════════════════════

# ── Bin / class definitions ───────────────────────────────────────────────────

_NDVI_BINS = [
    ("High Susceptibility",     None, 0.2),
    ("Moderate Susceptibility", 0.2,  0.5),
    ("Low Susceptibility",      0.5,  None),
]
_NDBI_BINS = [
    ("Low Susceptibility",      None, -0.2),
    ("Moderate Susceptibility", -0.2,  0.2),
    ("High Susceptibility",      0.2,  None),
]
_LST_BINS = [
    ("Low Susceptibility",      None, 25.0),
    ("Moderate Susceptibility", 25.0, 40.0),
    ("High Susceptibility",     40.0, None),
]
_IMPERVIOUS_BINS = [
    ("Low Susceptibility (Pervious)",    None, 0.5),
    ("High Susceptibility (Impervious)", 0.5,  None),
]

_LULC_CLASSES: dict[int, tuple[str, str]] = {
    1:  ("Water",              "#1f77b4"),
    2:  ("Trees",              "#2ca02c"),
    3:  ("Grass",              "#98df8a"),
    4:  ("Flooded Vegetation", "#17becf"),
    5:  ("Crops",              "#bcbd22"),
    6:  ("Scrub/Shrub",        "#8c564b"),
    7:  ("Built Area",         "#d62728"),
    8:  ("Bare Ground",        "#c49c94"),
    11: ("Rangeland",          "#e377c2"),
}

# ── Layer catalogue ───────────────────────────────────────────────────────────
# (key, hazard_folder, display_name, fig_ref, out_png, cmap, cbar_label, mode)

_LAYER_CATALOGUE: list[tuple] = [
    ("dem",        "Flood", "Elevation (DEM)",
     "A.1(a)", "app_dem.png",        "terrain",    "Elevation (m)",          "percentile_inverse"),
    ("twi",        "Flood", "Topographic Wetness Index (TWI)",
     "A.1(b)", "app_twi.png",        "Blues",      "TWI",                    "percentile_normal"),
    ("impervious", "Both", "Impervious Surface Cover",
     "A.1(c)", "app_impervious.png", "RdYlGn_r",  "Impervious Cover (0–1)", _IMPERVIOUS_BINS),
    ("ndvi", "Both", "Normalised Difference Vegetation Index (NDVI)",
     "A.2(a)", "app_ndvi.png", "RdYlGn",   "NDVI",   _NDVI_BINS),
    ("ndbi", "Heat", "Normalised Difference Built-up Index (NDBI)",
     "A.2(b)", "app_ndbi.png", "RdYlGn_r", "NDBI",   _NDBI_BINS),
    ("lst",  "Heat", "Land Surface Temperature (LST)",
     "A.3",    "app_lst.png",  "RdYlBu_r", "LST (°C)", "lst_auto"),
    ("lulc", "Both", "Land Use / Land Cover (LULC)",
     "A.4",    "app_lulc.png", "categorical", "LULC Class", "categorical"),
]

_GEOJSON_CATALOGUE: list[tuple] = [
    ("roads",     "roads",     "Road Network",             "A.5", "app_roads.png"),
    ("waterline", "waterline", "Waterways & Water Bodies", "A.6", "app_waterline.png"),
]

_LAYER_DESCRIPTION: dict[str, str] = {
    "dem": (
        "The Digital Elevation Model (DEM) represents the topographic surface of the study area, "
        "derived from high-resolution satellite data. Elevation data captures natural terrain "
        "features that govern surface water flow patterns and flood accumulation zones."
    ),
    "twi": (
        "The Topographic Wetness Index (TWI) is derived from the DEM and quantifies the tendency "
        "of each landscape position to accumulate water, based on the upstream contributing area "
        "and local slope gradient. Higher TWI values indicate zones where water naturally converges."
    ),
    "impervious": (
        "The Impervious Surface Cover layer maps every pixel as either fully impervious (value = 1: "
        "concrete, asphalt, rooftops) or pervious (value = 0: soil, vegetation, open water). "
        "Impervious surfaces generate near-total surface runoff during rainfall events."
    ),
    "ndvi": (
        "The Normalised Difference Vegetation Index (NDVI) measures vegetation density and health "
        "using near-infrared and red spectral bands. Values above 0.5 indicate dense, healthy "
        "vegetation while values near zero or negative indicate bare soil or impervious surfaces."
    ),
    "ndbi": (
        "The Normalised Difference Built-Up Index (NDBI) identifies impervious surfaces and urban "
        "built-up areas. Positive NDBI values indicate built-up or impervious surfaces while "
        "negative values correspond to vegetation or water bodies."
    ),
    "lst": (
        "Land Surface Temperature (LST) is derived from thermal infrared satellite imagery and "
        "represents the radiative skin temperature of the Earth's surface. LST integrates the "
        "combined thermal effects of land cover, solar radiation, and vegetation density."
    ),
    "lulc": (
        "Land Use / Land Cover (LULC) classification maps the Earth's surface into distinct "
        "functional categories including built-up areas, vegetation types, water bodies, and "
        "agricultural land, derived from multi-spectral satellite imagery."
    ),
    "roads": (
        "The road network layer maps all classified road and path features within the study area, "
        "sourced from OpenStreetMap vector data. Road infrastructure governs evacuation routes, "
        "emergency access corridors, and economic connectivity during climate events."
    ),
    "waterline": (
        "The waterways and water bodies layer maps all surface water features including rivers, "
        "canals, reservoirs, ponds, and detention basins. Surface water networks are primary "
        "determinants of flood pathway dynamics."
    ),
}

_LAYER_IMPACT_FALLBACK: dict[str, str] = {
    "dem": (
        "Lower-elevation parts of the site naturally collect water during heavy rain, "
        "which raises flood risk in those spots."
    ),
    "twi": (
        "Some parts of the site sit in natural drainage paths where rainwater tends to "
        "flow toward and pool, making them more likely to flood during intense rain."
    ),
    "impervious": (
        "Hard surfaces like concrete and rooftops prevent water from soaking into the "
        "ground, so during heavy rain more of it runs off and increases flooding risk."
    ),
    "ndvi": (
        "Areas with dense plant cover stay cooler because plants provide shade and "
        "naturally release moisture into the air. Sites with little vegetation get "
        "noticeably hotter during heatwaves."
    ),
    "ndbi": (
        "Densely built-up areas absorb and hold more heat from the sun, making them "
        "noticeably hotter than nearby open or vegetated areas."
    ),
    "lst": (
        "Surface temperatures measured here show which parts of the site heat up the "
        "most. Hot spots tend to be built-up and bare-ground areas."
    ),
    "lulc": (
        "Built-up areas and bare ground tend to make the site both hotter (less greenery "
        "to cool things down) and more flood-prone (less ground for rain to soak into)."
    ),
    "roads": (
        "The road network includes several types of roads. Low-lying stretches can flood "
        "during heavy rain, which can block evacuation routes and slow emergency access."
    ),
    "waterline": (
        "Rivers, canals, and water bodies nearby raise flood risk because they can "
        "overflow during heavy rain and standing water can collect in low spots."
    ),
}

_LAYER_EXTRA_CONTEXT: dict[str, str] = {
    "dem": "Elevation is a primary flood susceptibility indicator.",
    "twi": "TWI quantifies the tendency of each location to accumulate water based on terrain shape.",
    "impervious": "% impervious area = direct measure of runoff-generating fraction of the site.",
    "ndvi": "NDVI ranges from -1 to +1. Values above 0.5 indicate dense vegetation.",
    "ndbi": "NDBI ranges from -1 to +1. Higher positive values indicate dense built-up surfaces.",
    "lst": "LST measures radiative surface temperature from thermal infrared imagery.",
    "lulc": "LULC class codes: 1=Water, 2=Trees, 7=Built Area, 8=Bare Ground.",
    "roads": "Road density = total road length / AOI area.",
    "waterline": "Proximity to active water bodies is the primary fluvial flood exposure indicator.",
}

_LAYER_METRIC_GUIDE: dict[str, dict[str, str]] = {
    "dem":  {"Flood": "High Susceptibility = bottom 33rd percentile (lowest terrain = highest flood risk)."},
    "twi":  {"Flood": "High Susceptibility = TWI > 10 (strong water convergence = elevated flood risk)."},
    "ndvi": {"Heat":  "High Susceptibility = NDVI < 0.2 (sparse/bare = heat hotspot)."},
    "ndbi": {"Heat":  "High Susceptibility = NDBI > 0.2 (dense built-up = heat accumulation)."},
    "lst":  {"Heat":  "High Susceptibility = LST > 40°C (active thermal hotspot)."},
    "lulc": {"Heat":  "Heat-Contributing: Built Area (7) + Bare Ground (8)."},
    "impervious": {"Flood": "% impervious > 60% = critical flood risk from surface runoff."},
    "roads":      {"Flood": "High road density = high impervious cover = increased surface runoff."},
    "waterline":  {"Flood": "Dense waterway networks = compound flooding risk."},
}

_ROAD_COLORS: dict[str, str] = {
    "trunk": "#E60000", "secondary": "#FF7800", "tertiary": "#FFD700",
    "residential": "#4DA6FF", "service": "#80B3FF", "track": "#A0A0A0",
    "footway": "#B5651D", "cycleway": "#00CC44", "path": "#996633",
    "unclassified": "#CCCCCC",
}
_ROAD_DEFAULT_COLOR = "#AAAAAA"

_WATER_COLORS: dict[str, str] = {
    "StreamRiver": "#1565C0", "CanalDitch": "#0288D1",
    "reservoir": "#006064", "pond": "#00ACC1", "basin": "#4DD0E1",
}
_WATER_DEFAULT_COLOR = "#29B6F6"

_CONCLUSION_TEMPLATE = (
    "Each of these layers contributes to the accuracy and reliability of the climate risk "
    "assessment. By integrating multi-variate parameters, Resilience360™ is able to produce "
    "detailed and localised risk assessments for {hazard_types}."
)
_HAZARD_LABEL: dict[str, str] = {
    "Flood": "flood risk", "Heat": "heat risk", "Both": "flood and heat risk",
}


# ── Helper utilities ──────────────────────────────────────────────────────────

def _is_null(v) -> bool:
    if v is None:
        return True
    try:
        import pandas as pd
        return bool(pd.isna(v))
    except (TypeError, ValueError, ImportError):
        return False


def _find_tif(directory: Path, key: str) -> Path | None:
    for variant in [key, key.upper(), key.lower(), key.title()]:
        matches = sorted(directory.glob(f"*_{variant}.tif"))
        if matches:
            return matches[0]
    return None


def _find_geojson(directory: Path) -> Path | None:
    matches = sorted(directory.glob("*.geojson"))
    return matches[0] if matches else None


# ── Percentage formatting helpers ────────────────────────────────────────────
#
# Product round-2 ask: no decimal percentages anywhere in LLM output. Render
# whole numbers, and for sub-1% non-zero values render "<1%" rather than 0%
# (which would read as "none of the site" when there really is something).

def _fmt_pct(pct: float | None) -> str:
    """Render a percentage as a whole number. Sub-1% non-zero values → '<1%'."""
    if pct is None or pct <= 0:
        return "0%"
    if pct < 1:
        return "<1%"
    return f"{round(pct)}%"


def _strip_markdown_emphasis(text: str | None) -> str:
    """Remove Markdown bold/italic markers from LLM output. The downstream
    renderer (Word doc / HTML) doesn't process Markdown, so `**ELEVATED**`
    shows up as literal asterisks. This belt-and-braces strip ensures clean
    plain-text output even when Claude ignores the prompt rule."""
    if not text:
        return ""
    # **bold** → bold  (longest first so we don't half-match)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__",    r"\1", text)
    # *italic* / _italic_ → italic (single markers; avoid matching across newlines)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_([^_\n]+?)_(?!_)",      r"\1", text)
    return text


def _round_pcts_recursive(obj):
    """Walk a dict/list and round every `pct` field to a whole number.
    Sub-1% non-zero values become 0.5 — a sentinel the prompt rule renders
    as '<1%' in the model's prose. Pure copy; original obj is not mutated."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "pct" and isinstance(v, (int, float)):
                if v <= 0:
                    out[k] = 0
                elif v < 1:
                    out[k] = 0.5  # rendered as "<1%" in prose per prompt rule
                else:
                    out[k] = round(v)
            elif isinstance(v, (dict, list)):
                out[k] = _round_pcts_recursive(v)
            else:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [_round_pcts_recursive(item) for item in obj]
    return obj


def _compute_stats(data: np.ndarray) -> dict:
    valid = data[~np.isnan(data)]
    if valid.size == 0:
        return {}
    return {
        "min": round(float(np.min(valid)), 4), "max": round(float(np.max(valid)), 4),
        "mean": round(float(np.mean(valid)), 4), "std": round(float(np.std(valid)), 4),
        "p5":  round(float(np.percentile(valid,  5)), 4),
        "p25": round(float(np.percentile(valid, 25)), 4),
        "p50": round(float(np.percentile(valid, 50)), 4),
        "p75": round(float(np.percentile(valid, 75)), 4),
        "p95": round(float(np.percentile(valid, 95)), 4),
        "valid_pixels": int(valid.size),
    }


# ── Susceptibility computation ────────────────────────────────────────────────

def _susc_bins(data: np.ndarray, bins: list) -> dict:
    valid = data[~np.isnan(data)]
    if valid.size == 0:
        return {}
    result = {}
    for label, lo, hi in bins:
        if lo is None:
            mask = valid < hi
        elif hi is None:
            mask = valid >= lo
        else:
            mask = (valid >= lo) & (valid < hi)
        px  = valid[mask]
        pct = round(100.0 * px.size / valid.size, 1)
        result[label] = {
            "range": f"{px.min():.3f} – {px.max():.3f}" if px.size else "N/A",
            "pct": pct, "count": int(px.size),
        }
    return result


def _susc_percentile(data: np.ndarray, mode: str = "normal") -> dict:
    valid = data[~np.isnan(data)]
    if valid.size == 0:
        return {}
    p33 = float(np.percentile(valid, 33))
    p67 = float(np.percentile(valid, 67))
    if mode == "normal":
        classes = [
            ("Low Susceptibility",      None, p33),
            ("Moderate Susceptibility", p33,  p67),
            ("High Susceptibility",     p67,  None),
        ]
    else:
        classes = [
            ("High Susceptibility",     None, p33),
            ("Moderate Susceptibility", p33,  p67),
            ("Low Susceptibility",      p67,  None),
        ]
    return _susc_bins(data, classes)


def _susc_lulc(data: np.ndarray) -> dict:
    int_data = np.round(data[~np.isnan(data)]).astype(int)
    total = int_data.size
    result = {}
    for code, (label, _) in _LULC_CLASSES.items():
        cnt = int(np.sum(int_data == code))
        if cnt:
            result[label] = {"count": cnt, "pct": round(100.0 * cnt / total, 1)}
    return result


# ── Severity signal ───────────────────────────────────────────────────────────

def _severity_signal(layer_key: str, susc: dict, hazard: str = "") -> tuple:
    dominant_class, dominant_pct, high_pct = "N/A", 0.0, 0.0
    if layer_key == "lulc":
        risk_classes = (
            {"Built Area", "Bare Ground", "Flooded Vegetation", "Water"}
            if "Flood" in hazard
            else {"Built Area", "Bare Ground"}
        )
        high_pct = sum(float(v.get("pct", 0)) for k, v in susc.items() if k in risk_classes)
        for cls, data in susc.items():
            p = float(data.get("pct", 0))
            if p > dominant_pct:
                dominant_pct, dominant_class = p, cls
    else:
        for cls, data in susc.items():
            p = float(data.get("pct", 0))
            if p > dominant_pct:
                dominant_pct, dominant_class = p, cls
            if "High" in cls:
                high_pct = max(high_pct, p)

    severity = (
        "CRITICAL" if high_pct > 60 else
        "ELEVATED" if high_pct > 30 else
        "MODERATE" if high_pct > 0  else
        "LOW"
    )
    return severity, high_pct, dominant_class, dominant_pct


def _overall_hazard_profile(risk_counts: dict) -> tuple[str, str]:
    """Given building-level risk counts (set by ara_exposure), return:
      - profile:  multi-line summary string ready to drop into the prompt
      - severity: single sentence-case word: 'critical' | 'elevated' | 'moderate' | 'low'

    Used by the Appendix A prompts to ground per-layer paragraphs in the
    SITE's overall hazard risk, so Claude doesn't write alarmist text for a
    single layer when the overall picture is benign.

    Pattern mirrors `_buildings_finding` which already formats risk_counts
    into a one-line distribution string for Phase B.
    """
    total = sum(risk_counts.values()) if risk_counts else 0
    if total == 0:
        return ("No building-level risk data available for this site.", "moderate")

    non_zero = {k: v for k, v in risk_counts.items() if v > 0}
    lines = [f"{total} buildings assessed."]
    for cls, n in sorted(non_zero.items(), key=lambda x: -x[1]):
        lines.append(f"  {n} ({_fmt_pct(100.0 * n / total)}) at {cls.lower()} risk")

    high_or_above = risk_counts.get("High", 0) + risk_counts.get("Very High", 0)
    high_pct      = 100.0 * high_or_above / total
    if   high_pct >= 30: severity = "elevated"
    elif high_pct >= 5:  severity = "moderate"
    else:                severity = "low"

    return ("\n".join(lines), severity)


# ── Claude API calls ──────────────────────────────────────────────────────────

def _ask_claude_raster(
    layer_name: str, hazard: str, stats: dict, susc: dict,
    site_info: dict, layer_key: str = "",
) -> str | None:
    try:
        import anthropic
        client = anthropic.Anthropic()

        site_name = site_info.get("site_name", "the assessed site")
        city      = site_info.get("city", "")
        country   = site_info.get("country", "")
        location  = ", ".join(p for p in [city, country] if p) or "the assessed location"

        severity, high_pct, dominant_class, dominant_pct = _severity_signal(layer_key, susc, hazard)
        severity_lc = severity.lower()

        # Overall site risk for this hazard — drives prompt calibration AND
        # the Sentence 4 gate (only fire prescriptive interventions when the
        # OVERALL site risk is elevated/critical, not just this single layer).
        is_heat = "heat" in hazard.lower()
        overall         = site_info.get("heat_overall" if is_heat else "flood_overall", "moderate")
        overall_profile = site_info.get("heat_profile" if is_heat else "flood_profile",
                                       "No building-level risk data available for this site.")

        guide_by_hazard = _LAYER_METRIC_GUIDE.get(layer_key, {})
        metric_guide = (
            guide_by_hazard.get(hazard)
            or next(iter(guide_by_hazard.values()), None)
            or _LAYER_EXTRA_CONTEXT.get(layer_key, "")
        )
        sentence_4_rule = (
            "Sentence 4: State the priority intervention needed to manage this risk. "
            "Be specific and prescriptive — name the action and the part of the site it applies to "
            "(e.g., 'Improve drainage along the southern low-elevation strip'). "
            "Do NOT use phrases like 'the site team should consider', 'we recommend', or "
            "'consider evaluating'. Write the intervention as a direct, actionable instruction."
            if overall in ("critical", "elevated")
            else "Sentence 4: Omit — stop after Sentence 3."
        )

        susc_for_prompt = _round_pcts_recursive(susc)

        prompt = f"""You are writing the Appendix A "Impact on Results" section of a climate risk assessment report for a business stakeholder.

AUDIENCE: Business stakeholder making decisions about the site. NOT a GIS or climate-science expert. Familiar with "flood" and "heat risk" at a general level. Does NOT know terms like NDVI, TWI, NDBI, evapotranspiration, albedo, pluvial, or fluvial.

SITE: {site_name}
LOCATION: {location}
HAZARD: {hazard}
DATA LAYER: {layer_name}
OVERALL SITE {hazard.upper()} RISK: {overall} (authoritative — calibrate your paragraph to this, not just to this single layer's metric)
THIS LAYER'S RISK SIGNAL: {severity_lc} — High-Susceptibility class covers {_fmt_pct(high_pct)} of the site
DOMINANT CLASS: {dominant_class} ({_fmt_pct(dominant_pct)} of site area)

--- OVERALL SITE {hazard.upper()} RISK PROFILE (building-level, authoritative) ---
{overall_profile}

--- RASTER STATISTICS ---
{json.dumps(stats, indent=2)}

--- SUSCEPTIBILITY CLASS DISTRIBUTION (pct values already rounded to whole numbers; 0.5 means '<1%') ---
{json.dumps(susc_for_prompt, indent=2)}

--- LAYER METRIC GUIDE (internal — do not echo terminology to the reader) ---
{metric_guide}

Write a paragraph of exactly 3–4 sentences (60–90 words).
Sentence 1: Open with this layer's risk signal ({severity_lc}) and state the dominant susceptibility class with its exact %.
Sentence 2: Explain in everyday terms what this measurement tells us about the site.
Sentence 3: State the practical implication for {hazard.lower()} risk at {site_name} in plain language — calibrated to the OVERALL site risk ({overall}), not just this layer's metric.
{sentence_4_rule}

Rules:
- Plain English; no specialist jargon.
- If a technical term is unavoidable, briefly define it inline (e.g., "TWI, a measure of how much water tends to collect in this area").
- Every number cited must come from the data above.
- Express ALL percentages as whole numbers (e.g., 26%, not 25.9%). For sub-1% non-zero values write "<1%". Never write decimal percentages.
- Avoid informal phrasing ('actually', 'pretty', 'kind of', 'a bit') and contractions ('don't', 'can't', 'we'd'). Professional, accessible prose only.
- Render severity words ('low', 'moderate', 'elevated', 'critical') in lowercase / sentence case in prose. Never write them in all caps.
- Calibrate your paragraph to the OVERALL SITE risk shown above — not to this single layer's metric in isolation. Do NOT write conclusions stronger or more alarming than the overall site risk justifies. If the layer's metric looks concerning but the overall risk is low or moderate, explain WHY in plain language: other factors (drainage, vegetation, terrain, distance from water, building stock) keep the overall risk in check.
- Do NOT draw a standalone hazard verdict from one layer's metric. The susceptibility class and overall site risk are authoritative — use the metric to explain WHY, not to override the verdict.
- Avoid vague qualifiers like 'moderate', 'some', 'a few' unless you immediately back them with a specific number, percentage, or area within the site. Prefer concrete description over hedging.
- Tone: clear, accessible, professional. Like explaining to an executive, not a peer scientist.
- Plain text only. No Markdown formatting — do NOT wrap any word in `**bold**`, `*italic*`, `__underline__`, or `# headings`. The downstream renderer does not process Markdown and will show the asterisks literally.
- No bullet points. Output only the paragraph."""

        logger.info("    Calling Claude for impact text: %s", layer_name)
        msg = client.messages.create(
            model="claude-opus-4-6", max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return _strip_markdown_emphasis(msg.content[0].text.strip())
    except Exception as exc:
        logger.warning("    Claude call failed for %s: %s", layer_name, exc)
        return None


def _ask_claude_vector(
    layer_name: str, hazard: str, type_stats: dict,
    site_info: dict, layer_key: str = "",
) -> str | None:
    try:
        import anthropic
        client = anthropic.Anthropic()

        site_name = site_info.get("site_name", "the assessed site")
        city      = site_info.get("city", "")
        country   = site_info.get("country", "")
        location  = ", ".join(p for p in [city, country] if p) or "the assessed location"

        by_type    = type_stats.get("by_type", {})
        dom_type   = max(by_type, key=lambda k: by_type[k].get("count", 0)) if by_type else "N/A"
        dom_pct    = by_type.get(dom_type, {}).get("pct", 0.0)
        total_feat = type_stats.get("total_features", 0)

        # Overall site risk — same plumbing as the raster prompt. Vector
        # layers like roads serve both flood AND heat; we surface BOTH
        # overalls so Claude can reference whichever is relevant.
        flood_overall   = site_info.get("flood_overall", "moderate")
        heat_overall    = site_info.get("heat_overall",  "moderate")
        flood_profile   = site_info.get("flood_profile",
                                       "No building-level risk data available for this site.")
        heat_profile    = site_info.get("heat_profile",
                                       "No building-level risk data available for this site.")
        # For Sentence 4 gating, use whichever overall the hazard string names;
        # roads' hazard is "Flood and Heat" — gate on the worse of the two.
        h_lower = hazard.lower()
        if "flood" in h_lower and "heat" in h_lower:
            severity_rank = {"low": 0, "moderate": 1, "elevated": 2, "critical": 3}
            primary_overall = max((flood_overall, heat_overall), key=lambda s: severity_rank.get(s, 0))
        elif "heat" in h_lower:
            primary_overall = heat_overall
        else:
            primary_overall = flood_overall

        guide_by_hazard = _LAYER_METRIC_GUIDE.get(layer_key, {})
        metric_guide = (
            guide_by_hazard.get(hazard)
            or next(iter(guide_by_hazard.values()), None)
            or _LAYER_EXTRA_CONTEXT.get(layer_key, "")
        )

        sentence_4_rule = (
            "Sentence 4: State the priority intervention needed to manage this risk. "
            "Be specific and prescriptive — name the action and the part of the site it applies to "
            "(e.g., 'Reinforce the southern access road against flood damage'). "
            "Do NOT use phrases like 'the site team should consider', 'we recommend', or "
            "'consider evaluating'. Write the intervention as a direct, actionable instruction."
            if primary_overall in ("critical", "elevated")
            else "Sentence 4: Omit — stop after Sentence 3."
        )

        type_stats_for_prompt = _round_pcts_recursive(type_stats)

        prompt = f"""You are writing the Appendix A "Impact on Results" section of a climate risk assessment report for a business stakeholder.

AUDIENCE: Business stakeholder making decisions about the site. NOT a GIS or climate-science expert. Familiar with "flood" and "heat risk" at a general level. Does NOT know terms like NDVI, TWI, NDBI, evapotranspiration, albedo, pluvial, or fluvial.

SITE: {site_name}
LOCATION: {location}
HAZARD: {hazard}
DATA LAYER: {layer_name}
OVERALL SITE FLOOD RISK: {flood_overall}
OVERALL SITE HEAT RISK:  {heat_overall}
(Authoritative — calibrate your paragraph to these, not just to this layer's metric.)
DOMINANT FEATURE TYPE: {dom_type} ({_fmt_pct(dom_pct)} of {total_feat} total features)

--- OVERALL SITE FLOOD RISK PROFILE (building-level, authoritative) ---
{flood_profile}

--- OVERALL SITE HEAT RISK PROFILE (building-level, authoritative) ---
{heat_profile}

--- FEATURE STATISTICS (pct values already rounded to whole numbers; 0.5 means '<1%') ---
{json.dumps(type_stats_for_prompt, indent=2)}

--- LAYER METRIC GUIDE (internal — do not echo terminology to the reader) ---
{metric_guide}

Write a paragraph of exactly 3–4 sentences (60–90 words).
Sentence 1: State the dominant feature type and its share, in plain language.
Sentence 2: Explain in everyday terms what this tells us about the site.
Sentence 3: State the practical implication for {hazard.lower()} risk at {site_name} in plain language — calibrated to the OVERALL site risk above, not just to this layer's metric.
{sentence_4_rule}

Rules:
- Plain English; no specialist jargon.
- If a technical term is unavoidable, briefly define it inline.
- Every number cited must come from the data above.
- Express ALL percentages as whole numbers (e.g., 26%, not 25.9%). For sub-1% non-zero values write "<1%". Never write decimal percentages.
- Avoid informal phrasing ('actually', 'pretty', 'kind of', 'a bit') and contractions ('don't', 'can't', 'we'd'). Professional, accessible prose only.
- Render severity words ('low', 'moderate', 'elevated', 'critical') in lowercase / sentence case in prose. Never write them in all caps.
- Calibrate your paragraph to the OVERALL SITE risk shown above — not to this single layer's metric in isolation. Do NOT write conclusions stronger or more alarming than the overall site risk justifies. If the layer's metric looks concerning but the overall risk is low or moderate, explain WHY in plain language: other factors (drainage, vegetation, terrain, distance from water, building stock) keep the overall risk in check.
- Do NOT draw a standalone hazard verdict from one layer's metric. The dominant feature type and overall site risk are authoritative — use the layer's stats to explain WHY, not to override the verdict.
- Avoid vague qualifiers like 'moderate', 'some', 'a few' unless you immediately back them with a specific number, percentage, or area within the site. Prefer concrete description over hedging.
- Tone: clear, accessible, professional. Like explaining to an executive, not a peer scientist.
- Plain text only. No Markdown formatting — do NOT wrap any word in `**bold**`, `*italic*`, `__underline__`, or `# headings`. The downstream renderer does not process Markdown and will show the asterisks literally.
- No bullet points. Output only the paragraph."""

        logger.info("    Calling Claude for impact text: %s", layer_name)
        msg = client.messages.create(
            model="claude-opus-4-6", max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return _strip_markdown_emphasis(msg.content[0].text.strip())
    except Exception as exc:
        logger.warning("    Claude call failed for %s: %s", layer_name, exc)
        return None


# ── Reprojection ──────────────────────────────────────────────────────────────

def _to_3857(data: np.ndarray, transform, crs) -> tuple:
    from rasterio.transform import array_bounds
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    src_bounds = array_bounds(data.shape[0], data.shape[1], transform)
    dst_crs = "EPSG:3857"
    dst_t, dst_w, dst_h = calculate_default_transform(
        crs, dst_crs, data.shape[1], data.shape[0], *src_bounds
    )
    dst = np.full((dst_h, dst_w), np.nan, dtype=np.float32)
    reproject(
        source=data.astype(np.float32), destination=dst,
        src_transform=transform, src_crs=crs,
        dst_transform=dst_t, dst_crs=dst_crs,
        resampling=Resampling.bilinear, src_nodata=np.nan, dst_nodata=np.nan,
    )
    bounds = (dst_t.c, dst_t.f + dst_t.e * dst_h, dst_t.c + dst_t.a * dst_w, dst_t.f)
    return dst, bounds


def _add_basemap(ax, bounds) -> None:
    try:
        import contextily as ctx_lib
        ctx_lib.add_basemap(ax, crs="EPSG:3857",
                            source=ctx_lib.providers.Esri.WorldImagery, zoom="auto")
    except Exception as exc:
        logger.debug("Basemap fetch skipped: %s", exc)


# ── PNG generation ────────────────────────────────────────────────────────────

def _png_continuous(data, bounds, title, fig_ref, cmap_name, cbar_label, out_path) -> bool:
    valid = data[~np.isnan(data)]
    if valid.size == 0:
        return False
    vmin = float(np.percentile(valid, 2))
    vmax = float(np.percentile(valid, 98))
    west, south, east, north = bounds

    fig, ax = plt.subplots(figsize=(8, 7))
    ax.set_xlim(west, east); ax.set_ylim(south, north)
    _add_basemap(ax, bounds)
    im = ax.imshow(np.ma.masked_invalid(data), extent=[west, east, south, north],
                   cmap=cmap_name, vmin=vmin, vmax=vmax, alpha=0.70, origin="upper", aspect="auto")
    cbar = plt.colorbar(im, ax=ax, shrink=0.55, pad=0.02)
    cbar.set_label(cbar_label, fontsize=9)
    ax.set_title(title, fontweight="bold", fontsize=10)
    ax.set_axis_off(); plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight"); plt.close(fig)
    return True


def _png_lulc(data, bounds, title, fig_ref, out_path) -> bool:
    int_data = np.round(data).astype(np.float64)
    present  = [c for c in _LULC_CLASSES if np.any(int_data == c)]
    if not present:
        return False
    west, south, east, north = bounds
    rgba = np.zeros((*data.shape, 4), dtype=np.float32)
    for code in present:
        _, hex_c = _LULC_CLASSES[code]
        r, g, b, _ = mcolors.to_rgba(hex_c)
        mask = int_data == code
        rgba[mask, 0] = r; rgba[mask, 1] = g; rgba[mask, 2] = b; rgba[mask, 3] = 0.85

    fig, ax = plt.subplots(figsize=(8, 7))
    ax.set_xlim(west, east); ax.set_ylim(south, north)
    _add_basemap(ax, bounds)
    ax.imshow(rgba, extent=[west, east, south, north], origin="upper", aspect="auto")
    patches = [mpatches.Patch(color=_LULC_CLASSES[c][1], label=_LULC_CLASSES[c][0]) for c in present]
    ax.legend(handles=patches, loc="lower right", fontsize=7, framealpha=0.85,
              title="LULC Classes", title_fontsize=8)
    ax.set_title(title, fontweight="bold", fontsize=10)
    ax.set_axis_off(); plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight"); plt.close(fig)
    return True


def _png_roads(gdf_4326, out_path, title, fig_ref) -> bool:
    try:
        gdf = gdf_4326.to_crs(3857)
        b   = gdf.total_bounds
        px  = (b[2] - b[0]) * 0.05; py = (b[3] - b[1]) * 0.05

        fig, ax = plt.subplots(figsize=(8, 7))
        ax.set_xlim(b[0] - px, b[2] + px); ax.set_ylim(b[1] - py, b[3] + py)
        _add_basemap(ax, (b[0] - px, b[1] - py, b[2] + px, b[3] + py))

        legend_patches = []
        if "highway" in gdf.columns:
            for htype, grp in gdf.groupby("highway"):
                color = _ROAD_COLORS.get(str(htype), _ROAD_DEFAULT_COLOR)
                grp.plot(ax=ax, color=color, linewidth=0.8, alpha=0.85)
                legend_patches.append(mpatches.Patch(color=color, label=str(htype).capitalize()))
        else:
            gdf.plot(ax=ax, color="#4DA6FF", linewidth=0.8, alpha=0.85)
        if legend_patches:
            ax.legend(handles=legend_patches, loc="lower right", fontsize=7,
                      framealpha=0.85, title="Road Type", title_fontsize=8)
        ax.set_title(title, fontweight="bold", fontsize=10)
        ax.set_axis_off(); plt.tight_layout()
        plt.savefig(out_path, dpi=150, bbox_inches="tight"); plt.close(fig)
        return True
    except Exception as exc:
        logger.error("PNG failed for roads: %s", exc)
        return False


def _png_waterways(gdf_4326, out_path, title, fig_ref) -> bool:
    try:
        gdf = gdf_4326.to_crs(3857).copy()
        b   = gdf.total_bounds
        px  = (b[2] - b[0]) * 0.05; py = (b[3] - b[1]) * 0.05

        fig, ax = plt.subplots(figsize=(8, 7))
        ax.set_xlim(b[0] - px, b[2] + px); ax.set_ylim(b[1] - py, b[3] + py)
        _add_basemap(ax, (b[0] - px, b[1] - py, b[2] + px, b[3] + py))

        ftype_col = next((c for c in gdf.columns if "FType" in c), None)
        water_col = "water" if "water" in gdf.columns else None

        if ftype_col or water_col:
            def _label(r):
                ft = r[ftype_col] if ftype_col and not _is_null(r.get(ftype_col)) else None
                wt = r[water_col] if water_col and not _is_null(r.get(water_col)) else None
                return str(ft or wt or "Other")
            gdf["_wtype"] = gdf.apply(_label, axis=1)
        else:
            gdf["_wtype"] = "Water"

        legend_patches = []
        for wtype, grp in gdf.groupby("_wtype"):
            color = _WATER_COLORS.get(str(wtype), _WATER_DEFAULT_COLOR)
            grp.plot(ax=ax, color=color, alpha=0.75, linewidth=0.5)
            legend_patches.append(mpatches.Patch(color=color, label=str(wtype)))
        if legend_patches:
            ax.legend(handles=legend_patches[:8], loc="lower right", fontsize=7,
                      framealpha=0.85, title="Water Type", title_fontsize=8)
        ax.set_title(title, fontweight="bold", fontsize=10)
        ax.set_axis_off(); plt.tight_layout()
        plt.savefig(out_path, dpi=150, bbox_inches="tight"); plt.close(fig)
        return True
    except Exception as exc:
        logger.error("PNG failed for waterways: %s", exc)
        return False


# ── Feature stats ─────────────────────────────────────────────────────────────

def _compute_road_stats(gdf) -> dict:
    total = len(gdf)
    by_type: dict = {}
    if "highway" in gdf.columns:
        for htype, grp in gdf.groupby("highway"):
            cnt = len(grp)
            entry: dict = {"count": cnt, "pct": round(100.0 * cnt / total, 1)}
            try:
                entry["length_km"] = round(grp.to_crs(3857).length.sum() / 1000.0, 2)
            except Exception:
                pass
            by_type[str(htype)] = entry
    return {"total_features": total, "by_type": by_type}


def _compute_water_stats(gdf) -> dict:
    total = len(gdf)
    ftype_col = next((c for c in gdf.columns if "FType" in c), None)
    water_col  = "water" if "water" in gdf.columns else None

    def _label(row):
        ft = row[ftype_col] if ftype_col and not _is_null(row[ftype_col]) else None
        wt = row[water_col] if water_col and not _is_null(row[water_col]) else None
        return str(ft or wt or "Other")

    labels = gdf.apply(_label, axis=1)
    by_type: dict = {}
    for lbl, grp in labels.groupby(labels):
        cnt = len(grp)
        by_type[str(lbl)] = {"count": cnt, "pct": round(100.0 * cnt / total, 1)}
    return {"total_features": total, "by_type": by_type}


# ── Core layer processors ─────────────────────────────────────────────────────

def _process_raster_layer(
    key: str, hazard: str, display_name: str, fig_ref: str, out_png: str,
    cmap_name: str, cbar_label: str, mode,
    cog_dir: Path, assets_dir: Path, azure_base: str, site_info: dict,
) -> dict | None:
    import rasterio

    # "Both" layers (e.g. NDVI, LULC) exist in whichever COG subfolder is
    # present.  Try Flood first, then Heat.  The effective_hazard is derived
    # from risk_for so the Claude prompt uses the right perspective.
    if hazard == "Both":
        tif = _find_tif(cog_dir / "Flood", key) or _find_tif(cog_dir / "Heat", key)
        effective_hazard = site_info.get("risk_for", "Flood") or "Flood"
        if effective_hazard not in ("Flood", "Heat", "Both"):
            effective_hazard = "Flood"
    else:
        tif = _find_tif(cog_dir / hazard, key)
        effective_hazard = hazard

    if tif is None:
        logger.warning("No TIF for layer '%s' in %s", key, cog_dir / hazard)
        return None

    try:
        with rasterio.open(tif) as src:
            data_raw  = src.read(1).astype(np.float64)
            nd        = src.nodata
            transform = src.transform
            crs       = src.crs
    except Exception as exc:
        logger.error("Cannot read %s: %s", tif.name, exc)
        return None

    if nd is not None:
        data_raw[data_raw == nd] = np.nan
    data_raw[data_raw <= -9000] = np.nan

    if mode == "lst_auto":
        valid_check = data_raw[~np.isnan(data_raw)]
        if valid_check.size and np.nanmean(valid_check) > 100:
            data_raw -= 273.15
            logger.info("  %s: converted Kelvin → Celsius", key)

    try:
        data_3857, bounds = _to_3857(data_raw, transform, crs)
    except Exception as exc:
        logger.error("Reproject failed for %s: %s", key, exc)
        return None

    out_path = assets_dir / out_png
    ok = False
    try:
        if mode == "categorical":
            ok = _png_lulc(data_3857, bounds, display_name, fig_ref, out_path)
        else:
            ok = _png_continuous(data_3857, bounds, display_name, fig_ref,
                                 cmap_name, cbar_label, out_path)
    except Exception as exc:
        logger.error("PNG failed for %s: %s", key, exc)

    susc: dict = {}
    try:
        if mode == "categorical":
            susc = _susc_lulc(data_raw)
        elif isinstance(mode, list):
            susc = _susc_bins(data_raw, mode)
        elif mode == "lst_auto":
            susc = _susc_bins(data_raw, _LST_BINS)
        elif mode == "percentile_inverse":
            susc = _susc_percentile(data_raw, "inverse")
        elif mode == "percentile_normal":
            susc = _susc_percentile(data_raw, "normal")
    except Exception as exc:
        logger.error("Susceptibility failed for %s: %s", key, exc)

    stats  = _compute_stats(data_raw)
    impact = _ask_claude_raster(display_name, effective_hazard, stats, susc, site_info, key)
    if impact is None:
        impact = _LAYER_IMPACT_FALLBACK.get(key, "")

    azure_url = None
    if ok:
        res = save_asset(local_path=out_path, blob_name=f"{azure_base}/assets/{out_png}",
                         content_type="image/png")
        azure_url = res.get("azure")

    url = azure_url or (str(out_path) if ok else None)
    return {
        "key": key, "hazard": hazard, "name": display_name, "figure_ref": fig_ref,
        "map_local": str(out_path) if ok else None, "map_azure_url": azure_url, "map_url": url,
        "description": _LAYER_DESCRIPTION.get(key, ""), "impact": impact,
        "susceptibility_distribution": susc, "raster_stats": stats,
    }


def _process_vector_layer(
    key: str, subdir_name: str, display_name: str, fig_ref: str, out_png: str,
    cog_dir: Path, assets_dir: Path, azure_base: str, site_info: dict,
) -> dict | None:
    try:
        import geopandas as gpd
    except ImportError:
        logger.error("geopandas not installed — skipping vector layer '%s'", key)
        return None

    geojson_path = _find_geojson(cog_dir / subdir_name)
    if geojson_path is None:
        logger.warning("No GeoJSON in %s — skipping '%s'", cog_dir / subdir_name, key)
        return None

    try:
        gdf = gpd.read_file(geojson_path)
    except Exception as exc:
        logger.error("Cannot read %s: %s", geojson_path.name, exc)
        return None

    if gdf.empty:
        return None

    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)

    if key == "roads":
        type_stats   = _compute_road_stats(gdf)
        hazard_label = "Flood and Heat"
    else:
        type_stats   = _compute_water_stats(gdf)
        hazard_label = "Flood"

    out_path = assets_dir / out_png
    ok = False
    try:
        ok = _png_roads(gdf, out_path, display_name, fig_ref) if key == "roads" \
             else _png_waterways(gdf, out_path, display_name, fig_ref)
    except Exception as exc:
        logger.error("PNG generation failed for %s: %s", key, exc)

    impact = _ask_claude_vector(display_name, hazard_label, type_stats, site_info, key)
    if impact is None:
        impact = _LAYER_IMPACT_FALLBACK.get(key, "")

    azure_url = None
    if ok:
        res = save_asset(local_path=out_path, blob_name=f"{azure_base}/assets/{out_png}",
                         content_type="image/png")
        azure_url = res.get("azure")

    url = azure_url or (str(out_path) if ok else None)
    return {
        "key": key, "hazard": hazard_label, "name": display_name, "figure_ref": fig_ref,
        "map_local": str(out_path) if ok else None, "map_azure_url": azure_url, "map_url": url,
        "description": _LAYER_DESCRIPTION.get(key, ""), "impact": impact,
        "feature_stats": type_stats,
    }


# ── Phase A entry point ───────────────────────────────────────────────────────

def _run_appendices(ctx: dict) -> dict:
    """
    Equivalent to 7_appendices.py run().
    Processes all raster + vector layers, saves 7_appendices.json, stores
    appendix_map_paths / appendix_layer_urls / appendix_stats /
    appendix_layer_impacts / appendix_layers_json in context.
    """
    assets   = ctx["assets_dir"]
    base     = ctx["azure_base_path"]
    risk_for = ctx.get("risk_for", "Both")
    cog_dir  = config.COG_DIR

    # Derive the site's OVERALL hazard risk from the building counts that
    # ara_exposure stashed in ctx. Used to calibrate the per-layer LLM
    # paragraphs so they don't go more alarmist than the overall verdict.
    flood_counts = ctx.get("flood_risk_counts", {}) or {}
    heat_counts  = ctx.get("heat_risk_counts",  {}) or {}
    flood_profile, flood_overall = _overall_hazard_profile(flood_counts)
    heat_profile,  heat_overall  = _overall_hazard_profile(heat_counts)

    site_info = {
        "site_name":     ctx.get("site_name", ctx.get("area_name", "the assessed site")),
        "city":          ctx.get("city", ""),
        "country":       ctx.get("country", ""),
        "risk_for":      risk_for,
        "flood_profile": flood_profile,
        "flood_overall": flood_overall,   # 'critical' | 'elevated' | 'moderate' | 'low'
        "heat_profile":  heat_profile,
        "heat_overall":  heat_overall,
    }

    layers_json:    list[dict]      = []
    map_paths:      dict[str, Path] = {}
    layer_urls:     dict[str, str]  = {}
    stats:          dict[str, dict] = {}
    layer_impacts:  dict[str, str]  = {}

    for (key, hazard, display_name, fig_ref, out_png, cmap, cbar, mode) in _LAYER_CATALOGUE:
        if hazard == "Flood" and risk_for not in ("Flood", "Both"):
            continue
        if hazard == "Heat" and risk_for not in ("Heat", "Both"):
            continue
        # "Both" layers (NDVI, LULC) run for any risk_for — skipped only when
        # neither flood nor heat is applicable (shouldn't happen in practice).
        if hazard == "Both" and risk_for not in ("Flood", "Heat", "Both"):
            continue
        logger.info("  Appendix raster layer: %s (%s)", key, hazard)
        result = _process_raster_layer(
            key, hazard, display_name, fig_ref, out_png, cmap, cbar, mode,
            cog_dir, assets, base, site_info,
        )
        if result is None:
            continue
        layers_json.append(result)
        if result["map_local"]:
            map_paths[key] = Path(result["map_local"])
        if result.get("map_url"):
            layer_urls[key] = result["map_url"]
        stats[key]         = result.get("susceptibility_distribution", {})
        layer_impacts[key] = result["impact"]

    for (key, subdir_name, display_name, fig_ref, out_png) in _GEOJSON_CATALOGUE:
        logger.info("  Appendix vector layer: %s", key)
        result = _process_vector_layer(
            key, subdir_name, display_name, fig_ref, out_png,
            cog_dir, assets, base, site_info,
        )
        if result is None:
            continue
        layers_json.append(result)
        if result["map_local"]:
            map_paths[key] = Path(result["map_local"])
        if result.get("map_url"):
            layer_urls[key] = result["map_url"]
        stats[key]         = result.get("feature_stats", {})
        layer_impacts[key] = result["impact"]

    # Save 7_appendices.json for compatibility with any downstream readers
    payload = {
        "section": "7. Appendices",
        "appendix_a": {
            "title":      "Appendix A: Risk Assessment Data",
            "layers":     layers_json,
            "conclusion": _CONCLUSION_TEMPLATE.format(
                hazard_types=_HAZARD_LABEL.get(risk_for, risk_for.lower())
            ),
        },
    }
    save_asset(
        local_path   = Path(ctx["output_dir"]) / "7_appendices.json",
        blob_name    = f"{base}/7_appendices.json",
        content_type = "application/json",
        data         = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
    )

    ctx["appendix_map_paths"]    = map_paths
    ctx["appendix_layer_urls"]   = layer_urls
    ctx["appendix_stats"]        = stats
    ctx["appendix_layer_impacts"] = layer_impacts
    ctx["appendix_layers_json"]  = layers_json

    logger.info("[Phase A] Appendices complete: %d layers", len(layers_json))
    return ctx


# ═════════════════════════════════════════════════════════════════════════════
# PHASE B — Risk findings  (from 9_risk_findings.py)
# ═════════════════════════════════════════════════════════════════════════════

_FLOOD_LAYERS = ["dem", "twi", "waterline"]
_HEAT_LAYERS  = ["ndvi", "ndbi", "lst"]


def _call_claude_findings(prompt: str) -> str:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        msg = client.messages.create(
            model="claude-opus-4-6", max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as exc:
        logger.warning("Claude findings call failed: %s", exc)
        return ""


def _pct(count: int, total: int) -> str:
    """Whole-number percentage; '<1%' for sub-1% non-zero. Consistent with the
    Appendix A prompt formatting so Section 5.4 Risk Insights bullets match."""
    if total == 0:
        return "0%"
    return _fmt_pct(100.0 * count / total)


def _buildings_finding(hazard: str, risk_counts: dict, total: int, site_name: str) -> str:
    non_zero    = {k: v for k, v in risk_counts.items() if v > 0}
    counts_text = ", ".join(
        f"{v} buildings ({_pct(v, total)}) at {k} risk"
        for k, v in sorted(non_zero.items(), key=lambda x: -x[1])
    )
    prompt = f"""You are writing a one-line risk finding for a formal asset resilience report.

HAZARD: {hazard}
SITE: {site_name}
TOTAL BUILDINGS: {total}
RISK DISTRIBUTION: {counts_text}

Write exactly ONE sentence (max 25 words). State the count and percentage of buildings at each non-zero risk level, starting with the most significant.
Output only the sentence."""
    result = _call_claude_findings(prompt)
    return result or counts_text


def _layer_finding(layer: dict, site_name: str) -> str:
    key        = layer.get("key", "")
    name       = layer.get("name", key)
    hazard     = layer.get("hazard", "")
    susc       = layer.get("susceptibility_distribution", {})
    stats      = layer.get("raster_stats", {})
    feat_stats = layer.get("feature_stats", {})

    data_section = (
        f"FEATURE STATISTICS:\n{json.dumps(feat_stats, indent=2)}"
        if feat_stats
        else f"SUSCEPTIBILITY DISTRIBUTION:\n{json.dumps(susc, indent=2)}\n"
             f"RASTER STATS: {json.dumps(stats, indent=2)}"
    )
    prompt = f"""You are writing a one-line risk finding for a formal asset resilience report.

HAZARD: {hazard}
SITE: {site_name}
DATA LAYER: {name}

{data_section}

Write exactly ONE sentence (max 30 words) about {name}'s {hazard.lower()} risk implication at {site_name}.
Cite one specific number or percentage from the data above.
Professional formal tone. Output only the sentence."""
    result = _call_claude_findings(prompt)
    return result or f"{name} data indicates {hazard.lower()} risk exposure at {site_name}."


def _ssp_finding(
    hazard: str, geojson_path, total: int, site_name: str,
    classify_today_fn, classify_ssp_fn,
) -> str:
    if geojson_path is None or not Path(geojson_path).exists():
        return f"Long-term SSP 8.5 projections indicate increased {hazard.lower()} risk at {site_name}."
    try:
        from core.geojson_utils import build_ssp_counts, load_geojson
        gj_data = load_geojson(geojson_path)
        if not gj_data:
            raise ValueError("empty geojson")

        today_counts, cols, per_col = build_ssp_counts(
            gj_data, classify_today_fn, classify_ssp_fn, config.SSP_HORIZONS
        )
        long_8_5_key = next((c for c in cols if "Long" in c[0] and "8.5" in c[2]), None)
        if long_8_5_key is None:
            raise ValueError("Long-term SSP 8.5 column not found")

        long_counts = dict(per_col[long_8_5_key])
        counts_text = ", ".join(
            f"{v} buildings ({_pct(v, total)}) at {k}"
            for k, v in long_counts.items() if v > 0
        )
        prompt = f"""You are writing a one-line risk finding for a formal asset resilience report.

HAZARD: {hazard}
SITE: {site_name}
TOTAL BUILDINGS: {total}
LONG-TERM SSP 8.5 (2081–2100) COUNTS: {counts_text}

Write exactly ONE sentence (max 30 words) about the long-term SSP 8.5 {hazard.lower()} projection.
Output only the sentence."""
        result = _call_claude_findings(prompt)
        return result or f"In long-term SSP 8.5 projections, increased {hazard.lower()} risk is expected at {site_name}."
    except Exception as exc:
        logger.warning("SSP finding failed for %s: %s", hazard, exc)
        return f"Long-term SSP 8.5 projections indicate increased {hazard.lower()} risk at {site_name}."


def _build_findings(
    hazard: str, risk_counts: dict, total: int, site_name: str,
    layers_by_key: dict, layer_keys: list, geojson_path,
    classify_today_fn, classify_ssp_fn,
) -> list[str]:
    findings: list[str] = []

    logger.info("  [%s] Point 1 — buildings", hazard)
    findings.append(_buildings_finding(hazard, risk_counts, total, site_name))

    for i, key in enumerate(layer_keys, start=2):
        layer = layers_by_key.get(key)
        if layer:
            logger.info("  [%s] Point %d — %s", hazard, i, key)
            findings.append(_layer_finding(layer, site_name))
        else:
            logger.warning("  [%s] Layer '%s' not found in appendix data", hazard, key)
            findings.append(f"{key.upper()} data not available for this report.")

    logger.info("  [%s] Point 5 — SSP 8.5 long-term", hazard)
    findings.append(_ssp_finding(hazard, geojson_path, total, site_name,
                                 classify_today_fn, classify_ssp_fn))
    return findings


# ── Phase B entry point ───────────────────────────────────────────────────────

def _run_risk_findings(ctx: dict) -> dict:
    """Equivalent to 9_risk_findings.py run(). Uses appendix data already in context."""
    from core.classifiers import classify_current_flood, classify_current_heat
    from core.classifiers import classify_flood_ssp, classify_heat_ssp

    risk_for  = ctx.get("risk_for", "Both")
    site_name = ctx.get("site_name", "the assessed site")

    layers_json  = ctx.get("appendix_layers_json", [])
    layers_by_key = {layer.get("key", ""): layer for layer in layers_json}

    flood_counts = ctx.get("flood_risk_counts", {})
    heat_counts  = ctx.get("heat_risk_counts", {})
    total = (
        sum(flood_counts.values()) or sum(heat_counts.values()) or
        ctx.get("total_buildings", 0)
    )

    findings: dict[str, list[str]] = {"flood": [], "heat": []}

    if risk_for in ("Flood", "Both"):
        logger.info("Generating Flood risk findings...")
        findings["flood"] = _build_findings(
            hazard            = "Flood",
            risk_counts       = flood_counts,
            total             = total,
            site_name         = site_name,
            layers_by_key     = layers_by_key,
            layer_keys        = _FLOOD_LAYERS,
            geojson_path      = ctx.get("flood_geojson_path"),
            classify_today_fn = classify_current_flood,
            classify_ssp_fn   = classify_flood_ssp,
        )

    if risk_for in ("Heat", "Both"):
        logger.info("Generating Heat risk findings...")
        findings["heat"] = _build_findings(
            hazard            = "Heat",
            risk_counts       = heat_counts,
            total             = total,
            site_name         = site_name,
            layers_by_key     = layers_by_key,
            layer_keys        = _HEAT_LAYERS,
            geojson_path      = ctx.get("heat_geojson_path"),
            classify_today_fn = classify_current_heat,
            classify_ssp_fn   = classify_heat_ssp,
        )

    ctx["risk_findings"] = findings
    return ctx


# ═════════════════════════════════════════════════════════════════════════════
# Steps 3 → 5 helpers
# ═════════════════════════════════════════════════════════════════════════════

def _find_placeholders(content: str) -> list[str]:
    seen, order = set(), []
    for m in _PLACEHOLDER_RE.finditer(content):
        key = m.group(1)
        if key not in seen:
            seen.add(key)
            order.append(key)
    return order


def _replace_placeholders(content: str, value_map: dict) -> str:
    def _sub(m: re.Match) -> str:
        val = value_map.get(m.group(1))
        if val is None:
            return m.group(0)
        return json.dumps(val)[1:-1]
    return _PLACEHOLDER_RE.sub(_sub, content)


def _build_section(doc: dict, input_config: dict, context: dict, derived: dict) -> str:
    content      = json.dumps(doc, ensure_ascii=False)
    placeholders = _find_placeholders(content)
    logger.info("[Step 3] Placeholders found: %s", placeholders)

    value_map: dict = {}
    for key in placeholders:
        if key in derived:
            value_map[key] = derived[key]
        elif key in input_config:
            value_map[key] = str(input_config[key])
        elif key in context:
            value_map[key] = str(context[key])
        else:
            logger.warning("[Step 4] No value for {{%s}} — left unresolved", key)
            value_map[key] = f"{{{{{key}}}}}"

    logger.info("[Step 4] Value map keys: %s", list(value_map.keys()))
    resolved = _replace_placeholders(content, value_map)
    logger.info("[Step 5] Placeholders replaced.")
    return resolved


# ═════════════════════════════════════════════════════════════════════════════
# Pipeline entry point
# ═════════════════════════════════════════════════════════════════════════════

def run(context: dict) -> dict:
    ctx = dict(context)

    raw = ctx.get("section_content", "")
    doc: dict = (
        json.loads(json.dumps(raw)) if isinstance(raw, (dict, list))
        else json.loads(raw)
    )
    input_config = ctx.get("input_config", {})
    risk_for     = input_config.get("risk_for", ctx.get("risk_for", "Both"))
    ctx["risk_for"] = risk_for

    # Phase A — appendix layer processing (generates maps + stats + impact texts)
    logger.info("[Phase A] Running appendix layer processing")
    ctx = _run_appendices(ctx)

    # Phase B — risk findings (uses appendix layer data already in context)
    logger.info("[Phase B] Running risk findings generation")
    ctx = _run_risk_findings(ctx)

    # Steps 3–5 — resolve placeholders
    findings       = ctx.get("risk_findings", {})
    flood_findings = findings.get("flood", [])
    heat_findings  = findings.get("heat",  [])

    derived: dict[str, str] = {}
    if risk_for in ("Flood", "Both"):
        for i, ph in enumerate(["FLOOD_DETECTIVE_1", "FLOOD_DETECTIVE_2", "FLOOD_DETECTIVE_3"]):
            derived[ph] = flood_findings[i] if i < len(flood_findings) else ""
    if risk_for in ("Heat", "Both"):
        for i, ph in enumerate(["HEAT_DETECTIVE_1", "HEAT_DETECTIVE_2", "HEAT_DETECTIVE_3"]):
            derived[ph] = heat_findings[i] if i < len(heat_findings) else ""

    logger.info("[Step 4] Derived placeholders: %s", list(derived.keys()))

    resolved_content = _build_section(doc, input_config, ctx, derived)

    # Step 6
    ctx["resolved_content"] = resolved_content
    logger.info("[Step 6] resolved_content stored.")
    save_section_output(resolved_content, Path(__file__).stem, ctx)
    return ctx
