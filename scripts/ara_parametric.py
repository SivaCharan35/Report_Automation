"""
Module: ara_parametric — Parametric (Section 6)
Owns Steps 3–6 of the pipeline workflow.

ara_risk_insights runs before this module and stores all appendix layer data
(maps, susceptibility stats, impact texts) in the pipeline context.
This module reads that data from context — no heavy processing here.

  Step 3 — Find all {{PLACEHOLDER}} tokens in section_content
  Step 4 — Read appendix data from context; compute flood/heat prone %
            from exposure counts
  Step 5 — Replace placeholders
  Step 6 — Store resolved_content + write to disk

Placeholders resolved
─────────────────────
  Map URLs:
    APPX_BUILT_ENV_MAP        APPX_ELEVATION_MAP    APPX_TWI_MAP
    APPX_NDVI_MAP             APPX_NDBI_MAP         APPX_BUILT_LAYERS_MAP
    APPX_ROAD_MAP             APPX_LST_MAP          APPX_WATER_MAP

  Susceptibility cells (Low / Moderate / High) — Sarita style "(min - max) (N%)":
    APPX_ELEVATION_LOW/MOD/HIGH   APPX_TWI_LOW/MOD/HIGH
    APPX_NDVI_LOW/MOD/HIGH        APPX_NDBI_LOW/MOD/HIGH
    APPX_LST_LOW/MOD/HIGH

  Flood / heat prone % (aggregated from exposure risk counts — text or % only):
    APPX_FLOOD_PRONE_LOW/MOD/HIGH
    APPX_FLOOD_PRONE2_LOW/MOD/HIGH  (same values, reused in two tables)
    APPX_HEAT_PRONE_LOW/MOD/HIGH

  Impact texts:
    APPX_BUILT_ENV_IMPACT_TEXT    APPX_DEM_IMPACT_TEXT
    APPX_NDVI_IMPACT_TEXT         APPX_NDBI_IMPACT_TEXT
    APPX_BUILT_LAYERS_IMPACT_TEXT APPX_ROAD_IMPACT_TEXT
    APPX_LST_IMPACT_TEXT          APPX_WATER_IMPACT_TEXT

Context keys consumed
─────────────────────
  section_content        : str
  input_config           : dict
  risk_for               : str
  flood_risk_counts      : dict   — set by ara_exposure
  heat_risk_counts       : dict   — set by ara_exposure
  appendix_layer_urls    : dict   — set by ara_risk_insights
  appendix_stats         : dict   — set by ara_risk_insights
  appendix_layer_impacts : dict   — set by ara_risk_insights

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

# ── Map placeholder → layer key ───────────────────────────────────────────────
_MAP_PH: dict[str, str] = {
    "APPX_BUILT_ENV_MAP":    "built_env",
    "APPX_ELEVATION_MAP":    "dem",
    "APPX_TWI_MAP":          "twi",
    "APPX_NDVI_MAP":         "ndvi",
    "APPX_NDBI_MAP":         "ndbi",
    "APPX_BUILT_LAYERS_MAP": "impervious",
    "APPX_LULC_MAP":         "lulc",
    "APPX_ROAD_MAP":         "roads",
    "APPX_LST_MAP":          "lst",
    "APPX_WATER_MAP":        "waterline",
}

# ── Impact text placeholder → layer key ──────────────────────────────────────
_IMPACT_PH: dict[str, str] = {
    "APPX_BUILT_ENV_IMPACT_TEXT":    "built_env",
    "APPX_DEM_IMPACT_TEXT":          "dem",
    "APPX_NDVI_IMPACT_TEXT":         "ndvi",
    "APPX_NDBI_IMPACT_TEXT":         "ndbi",
    "APPX_BUILT_LAYERS_IMPACT_TEXT": "impervious",
    "APPX_LULC_IMPACT_TEXT":         "lulc",
    "APPX_ROAD_IMPACT_TEXT":         "roads",
    "APPX_LST_IMPACT_TEXT":          "lst",
    "APPX_WATER_IMPACT_TEXT":        "waterline",
}

# ── Description placeholder → layer key (static copy from appendix layers) ───
_DESC_PH: dict[str, str] = {
    "APPX_BUILT_ENV_DESCRIPTION":    "built_env",
    "APPX_ELEVATION_DESCRIPTION":    "dem",
    "APPX_DEM_DESCRIPTION":          "dem",
    "APPX_TWI_DESCRIPTION":          "twi",
    "APPX_NDVI_DESCRIPTION":         "ndvi",
    "APPX_NDBI_DESCRIPTION":         "ndbi",
    "APPX_BUILT_LAYERS_DESCRIPTION": "impervious",
    "APPX_IMPERVIOUS_DESCRIPTION":   "impervious",
    "APPX_LULC_DESCRIPTION":         "lulc",
    "APPX_ROAD_DESCRIPTION":         "roads",
    "APPX_LST_DESCRIPTION":          "lst",
    "APPX_WATER_DESCRIPTION":        "waterline",
}

# ── Susceptibility % placeholder → (layer_key, class_label) ──────────────────
_SUSC_PH: dict[str, tuple[str, str]] = {
    "APPX_ELEVATION_LOW":  ("dem",  "Low Susceptibility"),
    "APPX_ELEVATION_MOD":  ("dem",  "Moderate Susceptibility"),
    "APPX_ELEVATION_HIGH": ("dem",  "High Susceptibility"),
    "APPX_TWI_LOW":        ("twi",  "Low Susceptibility"),
    "APPX_TWI_MOD":        ("twi",  "Moderate Susceptibility"),
    "APPX_TWI_HIGH":       ("twi",  "High Susceptibility"),
    "APPX_NDVI_LOW":       ("ndvi", "Low Susceptibility"),
    "APPX_NDVI_MOD":       ("ndvi", "Moderate Susceptibility"),
    "APPX_NDVI_HIGH":      ("ndvi", "High Susceptibility"),
    "APPX_NDBI_LOW":       ("ndbi", "Low Susceptibility"),
    "APPX_NDBI_MOD":       ("ndbi", "Moderate Susceptibility"),
    "APPX_NDBI_HIGH":      ("ndbi", "High Susceptibility"),
    "APPX_LST_LOW":        ("lst",  "Low Susceptibility"),
    "APPX_LST_MOD":        ("lst",  "Moderate Susceptibility"),
    "APPX_LST_HIGH":       ("lst",  "High Susceptibility"),
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _pct(count: int, total: int) -> str:
    if total == 0:
        return "0.0%"
    return f"{100.0 * count / total:.1f}%"


def _prone_pcts(risk_counts: dict) -> tuple[str, str, str]:
    """Aggregate 5-class risk counts into Low / Moderate / High %."""
    total = sum(risk_counts.values()) or 1
    low  = risk_counts.get("Very Low", 0) + risk_counts.get("Low", 0)
    mod  = risk_counts.get("Moderate", 0)
    high = risk_counts.get("High", 0) + risk_counts.get("Very High", 0)
    return _pct(low, total), _pct(mod, total), _pct(high, total)


def _susc_pct(appendix_stats: dict, layer_key: str, class_label: str) -> str:
    """Return '(min - max) (N%)' for one susceptibility class — Sarita table style.

    Uses the same 3-bin break logic already computed in ara_risk_insights
    (pixel range within each Low/Moderate/High bin + share of AOI).
    """
    layer_stats = appendix_stats.get(layer_key, {})
    entry = layer_stats.get(class_label)
    if entry is None:
        entry = next(
            (v for k, v in layer_stats.items() if k.startswith(class_label)),
            None,
        )
    if entry is None:
        return "—"
    pct = entry.get("pct", 0.0)
    pct_str = f"{int(round(pct))}%"
    rng = (entry.get("range") or "").strip()
    if not rng or rng == "N/A":
        return f"({pct_str})"
    # Normalise en-dash / commas to " - " for a consistent cell string.
    rng = rng.replace("–", " - ").replace(",", " - ")
    rng = " - ".join(part.strip() for part in rng.split(" - ") if part.strip())
    return f"({rng}) ({pct_str})"


# Reverse lookup: every placeholder we know about → its parent layer key.
# Used by the missing-layer subsection pruner below.
_PH_TO_LAYER: dict[str, str] = {**_MAP_PH, **_IMPACT_PH, **_DESC_PH}


# ─────────────────────────────────────────────────────────────────────────────
# Missing-layer subsection pruner
#
# When a data layer fails to load upstream (no TIF / no GeoJSON), its map URL
# never lands in `appendix_layer_urls`. Today the placeholders for that layer
# resolve to "" — leaving a subtitle + blank image in the rendered report.
#
# This pruner walks the section's `content[]` BEFORE placeholder substitution
# and removes any subsection (delimited by paragraphs with attrs.styleId ==
# "Heading3") whose layer placeholders ALL belong to missing layers. Mixed
# subsections (one missing layer + one present) are left untouched.
# ─────────────────────────────────────────────────────────────────────────────

def _node_text(node) -> str:
    """Recursively concatenate all text inside `node`."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "") or ""
    children = node.get("content")
    if isinstance(children, list):
        return "".join(_node_text(c) for c in children)
    return ""


def _is_subsection_heading(node) -> bool:
    """Parametric subsections are delimited by paragraphs with styleId='Heading3'."""
    if not isinstance(node, dict) or node.get("type") != "paragraph":
        return False
    attrs = node.get("attrs") or {}
    return attrs.get("styleId") == "Heading3"


def _prune_missing_subsections(doc: dict, layer_urls: dict) -> list[str]:
    """In-place: drop subsections whose only layer placeholders refer to
    missing layers. Returns the list of layer keys whose subsections were
    actually removed (for logging)."""
    content = doc.get("content")
    if not isinstance(content, list):
        return []

    starts = [i for i, n in enumerate(content) if _is_subsection_heading(n)]
    if not starts:
        return []

    ranges = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(content)
        ranges.append((s, e))

    to_remove: list[int] = []
    layers_removed: set[str] = set()

    for start, end in ranges:
        range_text = "".join(_node_text(content[j]) for j in range(start, end))
        layers_in_range = {
            layer for ph, layer in _PH_TO_LAYER.items()
            if "{{" + ph + "}}" in range_text
        }
        if not layers_in_range:
            continue  # subsection has no layer placeholders — leave alone
        if all(not layer_urls.get(l) for l in layers_in_range):
            to_remove.extend(range(start, end))
            layers_removed.update(layers_in_range)

    for i in sorted(to_remove, reverse=True):
        del content[i]
    return sorted(layers_removed)


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
            return m.group(0)          # leave unresolved placeholder as-is
        # JSON-escape: substitution happens inside a serialised JSON string,
        # so newlines / tabs / backslashes in the value must be escaped.
        return json.dumps(val)[1:-1]   # strip surrounding quotes
    return _PLACEHOLDER_RE.sub(_sub, content)


def _build(doc: dict, input_config: dict, context: dict, derived: dict) -> str:
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

    # Step 4 — read appendix data from context (set by ara_risk_insights)
    layer_urls             = ctx.get("appendix_layer_urls", {})
    appendix_stats         = ctx.get("appendix_stats", {})
    appendix_layer_impacts = ctx.get("appendix_layer_impacts", {})

    if not layer_urls:
        logger.warning(
            "[Step 4] appendix_layer_urls not in context — "
            "map URLs will be empty (did ara_risk_insights run?)"
        )

    derived: dict[str, str] = {}

    # HAZARD_TYPES — same mapping as ara_intro / ara_overview. The Parametric
    # section template contains this placeholder; without an entry here it
    # falls through and renders literally as `{{HAZARD_TYPES}}`.
    derived["HAZARD_TYPES"] = {
        "Both":  "Flood and Heat Assessment",
        "Flood": "Flood Assessment",
        "Heat":  "Heat Assessment",
    }.get(input_config.get("risk_for", ""), "Climate Risk Assessment")

    # Map URLs
    for ph, layer_key in _MAP_PH.items():
        derived[ph] = layer_urls.get(layer_key, "")

    # Impact texts
    for ph, layer_key in _IMPACT_PH.items():
        derived[ph] = appendix_layer_impacts.get(layer_key, "")

    # Static layer descriptions (from appendix layers JSON or hard-coded fallbacks)
    layers_by_key = {
        layer.get("key", ""): layer
        for layer in (ctx.get("appendix_layers_json") or [])
        if isinstance(layer, dict)
    }
    for ph, layer_key in _DESC_PH.items():
        desc = ""
        layer = layers_by_key.get(layer_key)
        if layer:
            desc = layer.get("description") or ""
        if not desc:
            # Fallback: import descriptions from risk_insights catalogue
            try:
                from scripts.ara_risk_insights import _LAYER_DESCRIPTION
                desc = _LAYER_DESCRIPTION.get(layer_key, "")
            except Exception:
                desc = ""
        derived[ph] = desc

    # Susceptibility percentages
    for ph, (layer_key, class_label) in _SUSC_PH.items():
        derived[ph] = _susc_pct(appendix_stats, layer_key, class_label)

    # Flood / heat prone percentages from exposure risk counts
    flood_counts = ctx.get("flood_risk_counts", {})
    heat_counts  = ctx.get("heat_risk_counts",  {})

    f_low, f_mod, f_high = _prone_pcts(flood_counts)
    h_low, h_mod, h_high = _prone_pcts(heat_counts)

    derived["APPX_FLOOD_PRONE_LOW"]   = f_low
    derived["APPX_FLOOD_PRONE_MOD"]   = f_mod
    derived["APPX_FLOOD_PRONE_HIGH"]  = f_high
    derived["APPX_FLOOD_PRONE2_LOW"]  = f_low
    derived["APPX_FLOOD_PRONE2_MOD"]  = f_mod
    derived["APPX_FLOOD_PRONE2_HIGH"] = f_high
    derived["APPX_HEAT_PRONE_LOW"]    = h_low
    derived["APPX_HEAT_PRONE_MOD"]    = h_mod
    derived["APPX_HEAT_PRONE_HIGH"]   = h_high

    logger.info("[Step 4] Derived placeholders: %d", len(derived))

    # Drop subsections that belong entirely to missing layers (Bug 5).
    # Done BEFORE placeholder substitution so the literal {{...}} text is
    # still present in the content (we use it to identify which layers a
    # subsection references).
    removed = _prune_missing_subsections(doc, layer_urls)
    if removed:
        logger.info("[Step 4] Pruned subsections for missing layers: %s", removed)

    # Steps 3–5
    resolved_content = _build(doc, input_config, ctx, derived)

    # Step 6
    ctx["resolved_content"] = resolved_content
    logger.info("[Step 6] resolved_content stored.")
    save_section_output(resolved_content, Path(__file__).stem, ctx)
    return ctx
