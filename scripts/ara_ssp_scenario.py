"""
Module: ara_ssp_scenario — SSP Scenario Analysis
Owns Steps 3–6 of the pipeline workflow.

  Step 3 — Find all {{PLACEHOLDER}} tokens in section_content
  Step 4 — Compute SSP counts from GeoJSON; inject counts directly into the
            two empty SSP tables in the document AST (nodes [5] and [7]);
            build summary strings
  Step 5 — Replace {{FLOOD_SSP_SUMMARY}} / {{HEAT_SSP_SUMMARY}} placeholders
  Step 6 — Store resolved_content + write to disk

Placeholders resolved
─────────────────────
  FLOOD_SSP_SUMMARY : one-line text summary of flood SSP 8.5 projections by 2100
  HEAT_SSP_SUMMARY  : one-line text summary of heat SSP 8.5 projections by 2100

Table cell injection (no {{}} tokens — direct AST mutation)
────────────────────────────────────────────────────────────
  Node [5] — Flood SSP table  (rows 2-6 × 9 SSP columns)
  Node [7] — Heat SSP table   (rows 2-6 × 9 SSP columns)

  Column order (left to right): Near 2.6 | Near 4.5 | Near 8.5 |
                                 Mid 2.6  | Mid 4.5  | Mid 8.5  |
                                 Long 2.6 | Long 4.5 | Long 8.5

Context keys consumed
─────────────────────
  section_content    : str        — raw jsonContent from the API
  input_config       : dict       — full input_config from the API
  input_files_dir    : Path       — directory containing *.geojson input files
  flood_geojson_path : Path|None  — set by ara_overview/ara_exposure if ran first
  heat_geojson_path  : Path|None  — set by ara_overview/ara_exposure if ran first

Context keys produced
─────────────────────
  resolved_content   : str
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import config
from core.classifiers import (
    RISK_ORDER,
    classify_current_flood,
    classify_current_heat,
    classify_flood_ssp,
    classify_heat_ssp,
)
from core.geojson_utils import build_ssp_counts, load_geojson
from core.storage import save_section_output

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

# Occurrence-index of each SSP table inside section_content["content"].
# We used to hardcode array positions (5 and 7) but BE reshuffles paragraphs
# above/between the tables across template versions, which moves the indexes
# and crashes the inject. Looking up by occurrence (0 = first table seen,
# 1 = second) is stable across any rearrangement of paragraphs.
_FLOOD_TABLE_OCCURRENCE = 0   # first table in the section = flood
_HEAT_TABLE_OCCURRENCE  = 1   # second table in the section = heat
# Data rows start after 2 header rows (Time Horizon + SSP sub-headers)
_DATA_ROW_OFFSET = 2


def _find_table_by_occurrence(nodes: list, n: int) -> dict | None:
    """Return the Nth (0-indexed) node where type == 'table'."""
    seen = 0
    for node in nodes:
        if isinstance(node, dict) and node.get("type") == "table":
            if seen == n:
                return node
            seen += 1
    return None


# ─────────────────────────────────────────────────────────────────────────────
# GeoJSON auto-detection
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
# SSP table injection
# ─────────────────────────────────────────────────────────────────────────────

def _inject_ssp_counts(
    table_node: dict,
    cols: list,
    per_col: dict,
) -> None:
    """Write SSP count values into the empty data cells of a table AST node."""
    rows = table_node["content"]
    for ri, risk_label in enumerate(RISK_ORDER):
        row = rows[ri + _DATA_ROW_OFFSET]
        cells = row["content"]
        for ci, col in enumerate(cols):
            count = per_col.get(col, {}).get(risk_label, 0)
            # cells[0] is the risk-label cell; data cells start at index 1
            para = cells[ci + 1]["content"][0]
            para["content"] = [{"type": "text", "text": str(count)}]


# ─────────────────────────────────────────────────────────────────────────────
# Summary string builder
# ─────────────────────────────────────────────────────────────────────────────

def _make_ssp_summary(hazard: str, cols: list, per_col: dict) -> str:
    """One-line summary using the worst-case SSP 8.5 Long Term column."""
    long_ssp85 = cols[-1]  # ("Long [2100]", "SSP_2100", "SSP_Score_8.5")
    counts = per_col.get(long_ssp85, {})
    total  = sum(counts.values())
    high   = counts.get("High", 0) + counts.get("Very High", 0)
    pct    = round(high / total * 100) if total else 0
    return (
        f"Under the worst-case SSP 8.5 scenario by 2100, "
        f"{high} buildings ({pct}%) are projected at High or Very High "
        f"{hazard} risk."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Steps 3 → 5 — placeholder find / compute / replace
# ─────────────────────────────────────────────────────────────────────────────

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


def _build(
    doc: dict,
    input_config: dict,
    context: dict,
    flood_summary: str,
    heat_summary: str,
) -> str:
    """Serialize doc to JSON string and replace the two summary placeholders."""
    content = json.dumps(doc, ensure_ascii=False)

    placeholders = _find_placeholders(content)
    logger.info("[Step 3] Placeholders found: %s", placeholders)

    derived = {
        "FLOOD_SSP_SUMMARY": flood_summary,
        "HEAT_SSP_SUMMARY":  heat_summary,
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
    # Work on the parsed JSON object so we can mutate the table nodes directly
    doc: dict = json.loads(json.dumps(raw)) if isinstance(raw, (dict, list)) else json.loads(raw)

    input_config    = ctx.get("input_config", {})
    risk_for        = input_config.get("risk_for", "Both")
    input_files_dir = Path(ctx.get("input_files_dir", "Input_Files"))

    flood_path = ctx.get("flood_geojson_path") or _detect_geojson(input_files_dir, "flood")
    heat_path  = ctx.get("heat_geojson_path")  or _detect_geojson(input_files_dir, "heat")

    nodes = doc.get("content", [])

    flood_summary = ""
    heat_summary  = ""

    if risk_for in ("Flood", "Both") and flood_path:
        flood_data = load_geojson(flood_path)
        if flood_data:
            today_counts, cols, per_col = build_ssp_counts(
                flood_data, classify_current_flood, classify_flood_ssp, config.SSP_HORIZONS
            )
            logger.info("[Step 4] Flood SSP cols: %d, per_col keys: %d", len(cols), len(per_col))
            flood_table = _find_table_by_occurrence(nodes, _FLOOD_TABLE_OCCURRENCE)
            if flood_table is not None and "content" in flood_table:
                _inject_ssp_counts(flood_table, cols, per_col)
                logger.info("[Step 4] Flood SSP table populated.")
            else:
                logger.warning(
                    "[Step 4] Flood SSP table not found in section content — "
                    "skipping table inject. Summary placeholder still resolved."
                )
            flood_summary = _make_ssp_summary("flood", cols, per_col)
            ctx["flood_ssp_per_col"] = {str(k): v for k, v in per_col.items()}

    if risk_for in ("Heat", "Both") and heat_path:
        heat_data = load_geojson(heat_path)
        if heat_data:
            today_counts, cols, per_col = build_ssp_counts(
                heat_data, classify_current_heat, classify_heat_ssp, config.SSP_HORIZONS
            )
            logger.info("[Step 4] Heat SSP cols: %d, per_col keys: %d", len(cols), len(per_col))
            heat_table = _find_table_by_occurrence(nodes, _HEAT_TABLE_OCCURRENCE)
            if heat_table is not None and "content" in heat_table:
                _inject_ssp_counts(heat_table, cols, per_col)
                logger.info("[Step 4] Heat SSP table populated.")
            else:
                logger.warning(
                    "[Step 4] Heat SSP table not found in section content — "
                    "skipping table inject. Summary placeholder still resolved."
                )
            heat_summary = _make_ssp_summary("heat", cols, per_col)
            ctx["heat_ssp_per_col"] = {str(k): v for k, v in per_col.items()}

    # Steps 3–5: serialize + replace text placeholders
    resolved_content = _build(doc, input_config, ctx, flood_summary, heat_summary)

    # Step 6
    ctx["resolved_content"] = resolved_content
    logger.info("[Step 6] resolved_content stored.")

    save_section_output(resolved_content, Path(__file__).stem, ctx)
    return ctx
