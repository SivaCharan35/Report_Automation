"""
Module: ara_exposure — Exposure
Owns Steps 3–6 of the pipeline workflow.

  Step 3 — Find all {{PLACEHOLDER}} tokens in section_content
  Step 4 — Compute values (risk counts + map generation from GeoJSON)
  Step 5 — Replace placeholders with resolved values
  Step 6 — Store resolved_content + write to disk

Placeholders resolved
─────────────────────
  FLOOD_RISK_MAP_IMAGE : path to generated flood risk map PNG
  FLOOD_SCORE_1        : count of buildings at Very Low flood risk
  FLOOD_SCORE_2        : count of buildings at Low flood risk
  FLOOD_SCORE_3        : count of buildings at Moderate flood risk
  FLOOD_SCORE_4        : count of buildings at High flood risk
  FLOOD_SCORE_5        : count of buildings at Very High flood risk
  FLOOD_SUMMARY        : one-line text summary of flood risk distribution
  HEAT_RISK_MAP_IMAGE  : path to generated heat risk map PNG
  HEAT_SCORE_1–5       : same structure as FLOOD_SCORE_1–5 but for heat
  HEAT_SUMMARY         : one-line text summary of heat risk distribution

Context keys consumed
─────────────────────
  section_content      : str        — raw jsonContent from the API
  input_config         : dict       — full input_config from the API
  input_files_dir      : Path       — directory containing *.geojson input files
  assets_dir           : Path       — output directory for generated PNGs
  azure_base_path      : str        — Azure blob prefix
  flood_geojson_path   : Path|None  — set by ara_overview if it ran first
  heat_geojson_path    : Path|None  — set by ara_overview if it ran first

Context keys produced
─────────────────────
  resolved_content     : str
  flood_risk_counts    : dict[str, int]
  heat_risk_counts     : dict[str, int]
  flood_risk_map_path  : Path|None
  heat_risk_map_path   : Path|None
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt

import geopandas as gpd
import contextily as cx

from core.chart_utils import score_to_rgba
from core.classifiers import RISK_ORDER, classify_current_flood, classify_current_heat
from core.geojson_utils import compute_risk_counts, load_geojson
from core.storage import save_asset, save_section_output

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

# FLOOD_SCORE_N / HEAT_SCORE_N index → RISK_ORDER label
_SCORE_INDEX = {
    "1": "Very Low",
    "2": "Low",
    "3": "Moderate",
    "4": "High",
    "5": "Very High",
}


# ─────────────────────────────────────────────────────────────────────────────
# GeoJSON auto-detection  (same pattern as scripts/0_input.py)
# ─────────────────────────────────────────────────────────────────────────────

def _detect_geojson(directory: Path, keyword: str) -> Path | None:
    if not directory.exists():
        logger.warning("Input directory not found: %s", directory)
        return None
    for f in sorted(directory.glob("*.geojson")):
        if keyword.lower() in f.name.lower():
            logger.info("Auto-detected %s GeoJSON: %s", keyword, f.name)
            return f
    logger.warning("No %s GeoJSON found in %s", keyword, directory)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Risk map generation  (same logic as scripts/3_risk_assessment.py)
# ─────────────────────────────────────────────────────────────────────────────

def _generate_risk_map(geojson_path: Path, out_path: Path, title: str) -> None:
    gdf     = gpd.read_file(geojson_path)
    gdf["facecolor"] = gdf["risk_score"].apply(score_to_rgba)
    gdf_web = gdf.to_crs(epsg=3857)

    fig, ax = plt.subplots(figsize=(14, 12))
    gdf_web.plot(ax=ax, color=list(gdf_web["facecolor"]),
                 edgecolor="black", linewidth=0.5)
    cx.add_basemap(ax, source=cx.providers.Esri.WorldImagery)

    legend_meta = [
        (5, "Very High", (235/255,  52/255,  52/255)),
        (4, "High",      (235/255, 143/255,  52/255)),
        (3, "Moderate",  (235/255, 183/255,  52/255)),
        (2, "Low",       (235/255, 235/255,  52/255)),
        (1, "Very Low",  ( 76/255, 235/255,  52/255)),
    ]
    present = {int(round(s)) for s in gdf["risk_score"].dropna()}
    patches = [
        mpatches.Patch(color=c, label=lbl)
        for score, lbl, c in legend_meta if score in present
    ]
    ax.legend(handles=patches, loc="upper right", fontsize=9,
              title="Risk Level", title_fontsize=10, framealpha=0.9)
    ax.set_title(title, fontsize=15, fontweight="bold")
    ax.axis("off")
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.info("Risk map saved: %s", out_path.name)


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Find all placeholders
# ─────────────────────────────────────────────────────────────────────────────

def _find_placeholders(content: str) -> list[str]:
    seen, order = set(), []
    for m in _PLACEHOLDER_RE.finditer(content):
        key = m.group(1)
        if key not in seen:
            seen.add(key)
            order.append(key)
    return order


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Compute values for each placeholder
# ─────────────────────────────────────────────────────────────────────────────

def _make_summary(hazard: str, counts: dict[str, int]) -> str:
    """Build a one-line risk summary string from risk counts."""
    total = sum(counts.values())
    high  = counts.get("High", 0) + counts.get("Very High", 0)
    pct   = round(high / total * 100) if total else 0
    return (
        f"{total} buildings assessed for {hazard} risk. "
        f"{high} ({pct}%) at High or Very High risk."
    )


def _compute_values(
    placeholders: list[str],
    input_config: dict,
    context: dict,
    flood_counts: dict[str, int],
    heat_counts:  dict[str, int],
    flood_map_path: str | None,
    heat_map_path:  str | None,
) -> dict:
    """
    Map every placeholder to a value.

    Priority: input_config → derived → context → leave unresolved.
    """
    derived: dict = {}

    # FLOOD_SCORE_1–5
    for idx, label in _SCORE_INDEX.items():
        derived[f"FLOOD_SCORE_{idx}"] = str(flood_counts.get(label, 0))
        derived[f"HEAT_SCORE_{idx}"]  = str(heat_counts.get(label, 0))

    derived["FLOOD_SUMMARY"] = _make_summary("flood", flood_counts) if flood_counts else ""
    derived["HEAT_SUMMARY"]  = _make_summary("heat",  heat_counts)  if heat_counts  else ""

    derived["FLOOD_RISK_MAP_IMAGE"] = flood_map_path or ""
    derived["HEAT_RISK_MAP_IMAGE"]  = heat_map_path  or ""

    value_map: dict = {}
    for key in placeholders:
        if key in input_config:
            value_map[key] = str(input_config[key])
        elif key in derived:
            value_map[key] = derived[key]
        elif key in context:
            value_map[key] = str(context[key])
        else:
            logger.warning("[Step 4] No value for {{%s}} — left unresolved", key)
            value_map[key] = f"{{{{{key}}}}}"

    return value_map


# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — Replace placeholders
# ─────────────────────────────────────────────────────────────────────────────

def _replace_placeholders(content: str, value_map: dict) -> str:
    def _sub(m: re.Match) -> str:
        val = value_map.get(m.group(1))
        if val is None:
            return m.group(0)
        return json.dumps(val)[1:-1]
    return _PLACEHOLDER_RE.sub(_sub, content)


# ─────────────────────────────────────────────────────────────────────────────
# Steps 3 → 6 builder
# ─────────────────────────────────────────────────────────────────────────────

def _build(
    content: str,
    input_config: dict,
    context: dict,
    flood_counts: dict[str, int],
    heat_counts:  dict[str, int],
    flood_map_path: str | None,
    heat_map_path:  str | None,
) -> str:
    placeholders = _find_placeholders(content)
    logger.info("[Step 3] Placeholders found: %s", placeholders)

    value_map = _compute_values(
        placeholders, input_config, context,
        flood_counts, heat_counts, flood_map_path, heat_map_path,
    )
    logger.info("[Step 4] Value map: %s", value_map)

    resolved = _replace_placeholders(content, value_map)
    logger.info("[Step 5] Placeholders replaced.")

    return resolved


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(context: dict) -> dict:
    ctx = dict(context)

    raw = ctx.get("section_content", "")
    content = (
        json.dumps(raw, ensure_ascii=False)
        if isinstance(raw, (dict, list))
        else str(raw)
    )
    input_config = ctx.get("input_config", {})
    risk_for     = input_config.get("risk_for", "Both")

    assets          = Path(ctx["assets_dir"])
    azure_base      = ctx.get("azure_base_path", "")
    input_files_dir = Path(ctx.get("input_files_dir", "Input_Files"))

    # ── Resolve GeoJSON paths (context first → auto-detect) ───────────────────
    flood_path = ctx.get("flood_geojson_path") or _detect_geojson(input_files_dir, "flood")
    heat_path  = ctx.get("heat_geojson_path")  or _detect_geojson(input_files_dir, "heat")

    ctx["flood_geojson_path"] = flood_path
    ctx["heat_geojson_path"]  = heat_path

    # ── Compute risk counts + generate maps ───────────────────────────────────
    flood_counts:   dict[str, int] = {}
    heat_counts:    dict[str, int] = {}
    flood_map_path: str | None     = None   # Azure URL or local path string
    heat_map_path:  str | None     = None   # Azure URL or local path string

    site_name = (
        input_config.get("site_name")
        or input_config.get("area")
        or input_config.get("area_name", "Site")
    )

    if risk_for in ("Flood", "Both") and flood_path:
        flood_data = load_geojson(flood_path)
        if flood_data:
            flood_counts = compute_risk_counts(flood_data, classify_current_flood)
            logger.info("[Step 4] Flood risk counts: %s", flood_counts)

            out = assets / "flood_risk_map.png"
            _generate_risk_map(flood_path, out, f"Flood Risk Map — {site_name}")
            saved = save_asset(
                local_path   = out,
                blob_name    = f"{azure_base}/assets/flood_risk_map.png",
                content_type = "image/png",
            )
            # Prefer Azure URL for the placeholder (renderer fetches it server-side).
            # Fall back to local path when running without Azure.
            flood_map_path = saved.get("azure") or saved.get("local") or str(out)

    if risk_for in ("Heat", "Both") and heat_path:
        heat_data = load_geojson(heat_path)
        if heat_data:
            heat_counts = compute_risk_counts(heat_data, classify_current_heat)
            logger.info("[Step 4] Heat risk counts: %s", heat_counts)

            out = assets / "heat_risk_map.png"
            _generate_risk_map(heat_path, out, f"Heat Risk Map — {site_name}")
            saved = save_asset(
                local_path   = out,
                blob_name    = f"{azure_base}/assets/heat_risk_map.png",
                content_type = "image/png",
            )
            heat_map_path = saved.get("azure") or saved.get("local") or str(out)

    # Persist in context for downstream modules
    ctx["flood_risk_counts"]   = flood_counts
    ctx["heat_risk_counts"]    = heat_counts
    ctx["flood_risk_map_path"] = flood_map_path
    ctx["heat_risk_map_path"]  = heat_map_path
    # Store flat score keys so ara_influencing_factors and other modules can read them
    for idx, label in _SCORE_INDEX.items():
        ctx[f"FLOOD_SCORE_{idx}"] = str(flood_counts.get(label, 0))
        ctx[f"HEAT_SCORE_{idx}"]  = str(heat_counts.get(label, 0))

    # Steps 3–6
    resolved_content = _build(
        content, input_config, ctx,
        flood_counts, heat_counts, flood_map_path, heat_map_path,
    )

    # Step 6
    ctx["resolved_content"] = resolved_content
    logger.info("[Step 6] resolved_content stored.")

    save_section_output(resolved_content, Path(__file__).stem, ctx)

    return ctx
