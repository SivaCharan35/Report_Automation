"""
Module: ara_overview — Overview
Owns Steps 3–6 of the pipeline workflow.

  Step 3 — Find all {{PLACEHOLDER}} tokens in section_content
  Step 4 — Compute values (input_config + GeoJSON analysis)
  Step 5 — Replace placeholders with resolved values
  Step 6 — Store resolved_content + write to disk

Placeholders resolved
─────────────────────
  AREA_COVERED_FULL : "<area>, <city>, <state>, <country>"
  HAZARD_TYPES      : "Flood and Heat Assessment" | "Flood Assessment" | "Heat Assessment"
  TOTAL_BUILDINGS   : total building count from GeoJSON features
  TOTAL_AREA        : approximate bounding-box area in sq. miles

Context keys consumed
─────────────────────
  section_content    : str        — raw jsonContent from the API
  input_config       : dict       — full input_config from the API
  input_files_dir    : Path       — directory containing *.geojson input files
  flood_geojson_path : Path|None  — set by earlier stage if already detected
  heat_geojson_path  : Path|None  — set by earlier stage if already detected

Context keys produced
─────────────────────
  resolved_content   : str        — section_content with placeholders substituted
  flood_geojson_path : Path|None  — auto-detected if not already in context
  heat_geojson_path  : Path|None  — auto-detected if not already in context
  total_buildings    : int
  aoi_area           : float      — sq. miles
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from core.geojson_utils import compute_aoi_area, load_geojson
from core.storage import save_section_output

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


# ─────────────────────────────────────────────────────────────────────────────
# GeoJSON auto-detection  (same pattern as scripts/0_input.py)
# ─────────────────────────────────────────────────────────────────────────────

def _detect_geojson(directory: Path, keyword: str) -> Path | None:
    """Return first *.geojson in directory whose filename contains keyword."""
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

def _compute_values(
    placeholders: list[str],
    input_config: dict,
    context: dict,
    total_buildings: int,
    aoi_area: float,
    aoi_unit: str,
) -> dict:
    """
    Map every placeholder to a value.

    Priority: input_config → derived → context → leave unresolved.
    """
    city    = input_config.get("city", "")
    state   = input_config.get("state", "")
    country = input_config.get("country", "")
    risk    = input_config.get("risk_for", "")

    derived: dict = {
        "AREA_COVERED_FULL": ", ".join(p for p in [city, state, country] if p),
        "HAZARD_TYPES": {
            "Both":  "Flood and Heat Assessment",
            "Flood": "Flood Assessment",
            "Heat":  "Heat Assessment",
        }.get(risk, "Climate Risk Assessment"),
        "TOTAL_BUILDINGS": str(total_buildings),
        "TOTAL_AREA":      f"{aoi_area} {aoi_unit}",
    }

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
    total_buildings: int,
    aoi_area: float,
    aoi_unit: str,
) -> str:
    placeholders = _find_placeholders(content)
    logger.info("[Step 3] Placeholders found: %s", placeholders)

    value_map = _compute_values(
        placeholders, input_config, context, total_buildings, aoi_area, aoi_unit
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

    # ── Resolve GeoJSON paths (context → auto-detect) ─────────────────────────
    input_files_dir = Path(ctx.get("input_files_dir", "Input_Files"))

    flood_path = ctx.get("flood_geojson_path") or _detect_geojson(input_files_dir, "flood")
    heat_path  = ctx.get("heat_geojson_path")  or _detect_geojson(input_files_dir, "heat")

    # Store detected paths back into context for later modules
    ctx["flood_geojson_path"] = flood_path
    ctx["heat_geojson_path"]  = heat_path

    # ── Compute TOTAL_BUILDINGS and TOTAL_AREA from GeoJSON ───────────────────
    country         = input_config.get("country", "")
    total_buildings = 0
    aoi_area        = 0.0
    aoi_unit        = "sq km" if country.strip().lower() == "india" else "sq mile"

    if risk_for in ("Flood", "Both") and flood_path:
        flood_data = load_geojson(flood_path)
        if flood_data:
            total_buildings = len(flood_data["features"])
            aoi_area, aoi_unit = compute_aoi_area(flood_data, country)
            logger.info(
                "[Step 4] Flood GeoJSON: %d buildings, %.1f %s",
                total_buildings, aoi_area, aoi_unit,
            )

    if risk_for in ("Heat", "Both") and heat_path and total_buildings == 0:
        heat_data = load_geojson(heat_path)
        if heat_data:
            total_buildings = len(heat_data["features"])
            aoi_area, aoi_unit = compute_aoi_area(heat_data, country)
            logger.info(
                "[Step 4] Heat GeoJSON: %d buildings, %.1f %s",
                total_buildings, aoi_area, aoi_unit,
            )

    # Persist computed values in context for downstream modules
    ctx["total_buildings"] = total_buildings
    ctx["aoi_area"]        = aoi_area
    ctx["aoi_unit"]        = aoi_unit

    # Steps 3–6
    resolved_content = _build(content, input_config, ctx, total_buildings, aoi_area, aoi_unit)

    # Step 6
    ctx["resolved_content"] = resolved_content
    logger.info("[Step 6] resolved_content stored.")

    save_section_output(resolved_content, Path(__file__).stem, ctx)

    return ctx
