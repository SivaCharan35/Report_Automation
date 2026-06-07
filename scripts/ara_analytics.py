"""
Module: ara_analytics — Analytics Header
Owns Steps 3–6 of the pipeline workflow.

Maps to the "Header" section of the API response.  This section was created
by the API by mistake and contains only the "5. Analytics" page heading with
no dynamic content.

  Step 3 → finds zero placeholders
  Step 4 → empty value map (nothing to compute)
  Step 5 → content returned unchanged
  Step 6 → resolved_content stored + written to disk

Context keys consumed
─────────────────────
  section_content : str  — raw jsonContent from the API (the "5. Analytics" heading)
  input_config    : dict — full input_config dict from the API

Context keys produced
─────────────────────
  resolved_content : str — section_content unchanged (no placeholders)
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from core.storage import save_section_output

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


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


def _build(content: str, input_config: dict, context: dict) -> str:
    placeholders = _find_placeholders(content)
    logger.info("[Step 3] Placeholders found: %s", placeholders)

    # No placeholders expected — any future ones fall back to input_config/context
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


def run(context: dict) -> dict:
    ctx = dict(context)

    raw = ctx.get("section_content", "")
    content = (
        json.dumps(raw, ensure_ascii=False)
        if isinstance(raw, (dict, list))
        else str(raw)
    )
    input_config = ctx.get("input_config", {})

    resolved_content = _build(content, input_config, ctx)

    ctx["resolved_content"] = resolved_content
    logger.info("[Step 6] resolved_content stored.")

    save_section_output(resolved_content, Path(__file__).stem, ctx)
    return ctx
