"""
Module: ara_title — Report Title
Owns Steps 3–6 of the pipeline workflow.

  Step 3 — Find all {{PLACEHOLDER}} tokens in section_content
  Step 4 — Compute values for each placeholder
  Step 5 — Replace placeholders with resolved values
  Step 6 — Store resolved_content + write to disk

Placeholders resolved
─────────────────────
  AREA_COVERED_FULL : "<city>, <state>, <country>"
  PREPARED_FOR      : input_config["client"]
  Report_Category   : static "Resilience Assessment"
  HAZARD_TYPES      : "Flood and Heat Assessment" | "Flood Assessment" | "Heat Assessment"

Context keys consumed
─────────────────────
  section_content : str   — raw jsonContent from the API (with {{placeholders}})
  input_config    : dict  — full input_config dict from the API

Context keys produced
─────────────────────
  resolved_content : str  — section_content with all placeholders substituted
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from core.storage import save_section_output

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


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
) -> dict:
    """
    Map every placeholder to a value.

    Priority: input_config → derived → context → leave unresolved.
    """
    city    = input_config.get("city", "")
    state   = input_config.get("state", "")
    country = input_config.get("country", "")
    risk    = input_config.get("risk_for", "")
    client  = input_config.get("client", "")

    derived: dict = {
        "AREA_COVERED_FULL": ", ".join(p for p in [city, state, country] if p),
        "PREPARED_FOR":      client,
        "Report_Category":   "Resilience Assessment",
        "HAZARD_TYPES": {
            "Both":  "Flood and Heat Assessment",
            "Flood": "Flood Assessment",
            "Heat":  "Heat Assessment",
        }.get(risk, "Climate Risk Assessment"),
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

def _build(content: str, input_config: dict, context: dict) -> str:
    placeholders = _find_placeholders(content)
    logger.info("[Step 3] Placeholders found: %s", placeholders)

    value_map = _compute_values(placeholders, input_config, context)
    logger.info("[Step 4] Value map: %s", value_map)

    resolved = _replace_placeholders(content, value_map)
    logger.info("[Step 5] Placeholders replaced.")

    return resolved


# ─────────────────────────────────────────────────────────────────────────────
# AST pre-pass — merge split placeholders
#
# The BE template sometimes splits a placeholder across two (or more)
# adjacent text/run nodes, e.g.
#     run { content: [text "{{HAZARD_TYPES"] },
#     run { content: [text "}}"] }
# Our regex `\{\{(\w+)\}\}` operates on the JSON-serialised string and can
# never match across those boundaries (JSON commas / quotes sit between
# them). This walker scans content arrays, finds runs of adjacent children
# whose combined leaf-text forms a complete {{PLACEHOLDER}}, and rewrites
# them: the full placeholder text is set on the FIRST child's deepest text
# leaf and the follower children are deleted. Marks/styling of the first
# child are preserved.
# ─────────────────────────────────────────────────────────────────────────────

def _node_text(node) -> str:
    """Recursively concatenate all text content inside `node`."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "") or ""
    children = node.get("content")
    if isinstance(children, list):
        return "".join(_node_text(c) for c in children)
    return ""


def _replace_first_text(node, new_text: str) -> bool:
    """Walk into the FIRST text leaf in `node` and set its text. Returns True
    iff a text leaf was found."""
    if not isinstance(node, dict):
        return False
    if node.get("type") == "text":
        node["text"] = new_text
        return True
    children = node.get("content")
    if isinstance(children, list):
        for child in children:
            if _replace_first_text(child, new_text):
                return True
    return False


def _merge_split_placeholders_in_children(children: list) -> None:
    """In-place: scan a content list for consecutive items whose combined
    text contains a complete {{PLACEHOLDER}}. When found, collapse them
    into the first item (which gets the full placeholder text) and delete
    the rest."""
    i = 0
    while i < len(children):
        text_i = _node_text(children[i])
        if "{{" in text_i and "}}" not in text_i:
            combined = text_i
            j = i + 1
            while j < len(children):
                next_text = _node_text(children[j])
                combined += next_text
                if "}}" in next_text:
                    if _PLACEHOLDER_RE.search(combined):
                        if _replace_first_text(children[i], combined):
                            del children[i + 1 : j + 1]
                    break
                j += 1
        i += 1


def _merge_split_placeholders(node) -> None:
    """Recursively walk and merge split placeholders in every content list."""
    if isinstance(node, dict):
        children = node.get("content")
        if isinstance(children, list):
            _merge_split_placeholders_in_children(children)
            for child in children:
                _merge_split_placeholders(child)
    elif isinstance(node, list):
        for item in node:
            _merge_split_placeholders(item)


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(context: dict) -> dict:
    ctx = dict(context)

    raw = ctx.get("section_content", "")
    # Merge any split placeholders BEFORE serialising for regex substitution.
    if isinstance(raw, (dict, list)):
        raw = json.loads(json.dumps(raw))   # deep copy so we don't mutate the caller's dict
        _merge_split_placeholders(raw)

    content = (
        json.dumps(raw, ensure_ascii=False)
        if isinstance(raw, (dict, list))
        else str(raw)
    )
    input_config = ctx.get("input_config", {})

    # Steps 3–6
    resolved_content = _build(content, input_config, ctx)

    # Step 6 — store in context and persist to disk
    ctx["resolved_content"] = resolved_content
    logger.info("[Step 6] resolved_content stored.")

    save_section_output(resolved_content, Path(__file__).stem, ctx)

    return ctx
