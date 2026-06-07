"""
Module: ara_conclusion_appendix — Conclusion & Appendix (Section 7)
Owns Steps 3–6 of the pipeline workflow.

The section contains the Conclusion paragraph and Appendix B (SSP pathways).
Most content is already static in the API template.  Only one placeholder
requires resolution.

  Step 3 — Find {{HAZARD_TYPES}}
  Step 4 — Resolve from risk_for context value
  Step 5 — Replace placeholder
  Step 6 — Store resolved_content + write to disk

Placeholders resolved
─────────────────────
  HAZARD_TYPES : display label for the hazards covered
                 "Flood"  → "flood risk"
                 "Heat"   → "heat risk"
                 "Both"   → "flood and heat risk"

Context keys consumed
─────────────────────
  section_content : str   — raw jsonContent from the API
  input_config    : dict  — full input_config from the API
  risk_for        : str

Context keys produced
─────────────────────
  resolved_content : str
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from core.storage import save_section_output

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

_HAZARD_DISPLAY: dict[str, str] = {
    "Flood": "flood risk",
    "Heat":  "heat risk",
    "Both":  "flood and heat risk",
}


# ─────────────────────────────────────────────────────────────────────────────
# Steps 3 → 5 helpers
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


def _build(doc: dict, input_config: dict, context: dict, derived: dict) -> str:
    content = json.dumps(doc, ensure_ascii=False)
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
    doc: dict = (
        json.loads(json.dumps(raw)) if isinstance(raw, (dict, list))
        else json.loads(raw)
    )
    input_config = ctx.get("input_config", {})
    risk_for     = input_config.get("risk_for", ctx.get("risk_for", "Both"))

    # Step 4 — resolve HAZARD_TYPES
    derived: dict[str, str] = {
        "HAZARD_TYPES": _HAZARD_DISPLAY.get(risk_for, risk_for.lower()),
    }
    logger.info("[Step 4] HAZARD_TYPES = %r", derived["HAZARD_TYPES"])

    # Steps 3–5
    resolved_content = _build(doc, input_config, ctx, derived)

    # Step 6
    ctx["resolved_content"] = resolved_content
    logger.info("[Step 6] resolved_content stored.")
    save_section_output(resolved_content, Path(__file__).stem, ctx)
    return ctx
