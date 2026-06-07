"""
Module: ara_impact_scale — Impact Scale
Owns Steps 3–6 of the pipeline workflow.

Maps to the "Impact Scale" section of the API response.  The section contains
a table whose data cells are empty in the API content — this module populates
them with the static IMPACT_DATA definitions (same data as scripts/4_impact.py).

  Step 3 → finds zero placeholders
  Step 4 → injects IMPACT_DATA text into the 5 × 3 empty table cells
  Step 5 → content returned unchanged (no placeholders)
  Step 6 → resolved_content stored + written to disk

Table layout (node index 3 in section_content)
───────────────────────────────────────────────
  row[0]   : header  — Risk Level | Flood Impact | Heatwave Impact | Operational Impact
  row[1–5] : data    — Very Low / Low / Moderate / High / Very High
              cell[1] = flood_impact
              cell[2] = heat_impact
              cell[3] = operational_impact

Context keys consumed
─────────────────────
  section_content : str  — raw jsonContent from the API
  input_config    : dict — full input_config dict from the API

Context keys produced
─────────────────────
  resolved_content : str — section_content with table cells populated
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from core.storage import save_section_output

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

# ── Static impact definitions (mirrors scripts/4_impact.py) ──────────────────
IMPACT_DATA: list[dict] = [
    {
        "risk_level": "Very Low",
        "flood_impact": (
            "Minimal surface water accumulation; no impact on process units, "
            "electrical systems, or internal access routes."
        ),
        "heat_impact": (
            "Negligible heat impact; indoor temperatures remain within safe "
            "operating limits for control rooms and equipment."
        ),
        "operational_impact": (
            "Normal operations maintained; no impact on production, staffing, "
            "or maintenance activities."
        ),
    },
    {
        "risk_level": "Low",
        "flood_impact": (
            "Minor surface water pooling (1–3 hours) in non-critical paved areas; "
            "limited impact on internal vehicle movement."
        ),
        "heat_impact": (
            "Elevated temperatures causing mild indoor heat stress; increased "
            "HVAC demand in control and support buildings."
        ),
        "operational_impact": (
            "Minor operational adjustments; increased cooling loads; enhanced "
            "monitoring of outdoor maintenance activities."
        ),
    },
    {
        "risk_level": "Moderate",
        "flood_impact": (
            "Localized flooding around access roads, utility corridors, or "
            "non-critical operational zones (3–8 hours); temporary access constraints."
        ),
        "heat_impact": (
            "Prolonged high temperatures (30–32 °C for 3–5 days); reduced "
            "workforce efficiency and sustained cooling demand."
        ),
        "operational_impact": (
            "Partial operational slowdown; rescheduling of non-essential field work; "
            "reliance on remote monitoring and inventory buffers."
        ),
    },
    {
        "risk_level": "High",
        "flood_impact": (
            "Severe flooding (8–16 hours) impacting substations, electrical rooms, "
            "or internal access routes to process units."
        ),
        "heat_impact": (
            "Extreme heat (>32–35 °C); increased risk of equipment overheating "
            "and strain on cooling systems."
        ),
        "operational_impact": (
            "Major operational disruption; controlled slowdown or temporary shutdown "
            "of affected units; emergency response protocols activated."
        ),
    },
    {
        "risk_level": "Very High",
        "flood_impact": (
            "Widespread inundation (>24 hours) affecting utilities, foundations, "
            "and multiple operational systems."
        ),
        "heat_impact": (
            "Extreme heat (>35 °C); unsafe conditions for continuous operation "
            "without risk to equipment and personnel."
        ),
        "operational_impact": (
            "Controlled shutdown and transition to safe mode; non-essential personnel "
            "stood down; phased recovery and restart procedures initiated."
        ),
    },
]

# Column index → IMPACT_DATA field
_COL_FIELDS = ["flood_impact", "heat_impact", "operational_impact"]


# ─────────────────────────────────────────────────────────────────────────────
# Table injection
# ─────────────────────────────────────────────────────────────────────────────

def _inject_impact_data(table_node: dict) -> None:
    """Write IMPACT_DATA text into the empty data cells of the table AST node."""
    rows = table_node["content"]
    # row[0] is the header; data rows start at index 1
    for ri, entry in enumerate(IMPACT_DATA):
        row = rows[ri + 1]
        cells = row["content"]
        for ci, field in enumerate(_COL_FIELDS):
            # cells[0] is the risk-level label; text columns start at index 1
            para = cells[ci + 1]["content"][0]
            para["content"] = [{"type": "text", "text": entry[field]}]


# ─────────────────────────────────────────────────────────────────────────────
# Steps 3 → 5
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


def _build(doc: dict, input_config: dict, context: dict) -> str:
    content = json.dumps(doc, ensure_ascii=False)

    placeholders = _find_placeholders(content)
    logger.info("[Step 3] Placeholders found: %s", placeholders)

    value_map: dict = {}
    for key in placeholders:
        if key in input_config:
            value_map[key] = str(input_config[key])
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
    doc: dict = json.loads(json.dumps(raw)) if isinstance(raw, (dict, list)) else json.loads(raw)

    input_config = ctx.get("input_config", {})

    # Step 4 — inject static impact text into the empty table cells.
    # Find the table node by type instead of relying on a fixed index,
    # so the module stays robust when the API reorders section content.
    nodes = doc.get("content", [])
    table_node = next(
        (n for n in nodes if isinstance(n, dict) and n.get("type") == "table"),
        None,
    )
    if table_node is not None and "content" in table_node:
        _inject_impact_data(table_node)
        logger.info("[Step 4] Impact table cells populated.")
    else:
        logger.warning("[Step 4] Table node not found in section content.")

    # Steps 3–5
    resolved_content = _build(doc, input_config, ctx)

    # Step 6
    ctx["resolved_content"] = resolved_content
    logger.info("[Step 6] resolved_content stored.")

    save_section_output(resolved_content, Path(__file__).stem, ctx)
    return ctx
