"""
Module: ara_influencing_factors — Influencing Factors
Owns Steps 3–6 of the pipeline workflow.

All raster-processing logic is copied directly from
z. junk - old/6_influencing_factors.py — no numbered scripts are imported.

  Step 3 — Find all {{PLACEHOLDER}} tokens in section_content
  Step 4 — Process raster pairs (NDVI×TWI, NDBI×DEM, LST×NDVI), generate
            map PNGs, upload to Azure; inject colour-class table cells by
            direct AST mutation
  Step 5 — Replace placeholders with resolved values
  Step 6 — Store resolved_content + write to disk

Placeholders resolved
─────────────────────
  FLOOD_SCORE_1–5      : building counts per risk level (from ara_exposure context)
  HEAT_SCORE_1–5       : building counts per risk level (from ara_exposure context)
  FLOOD_NDVI_TWI_MAP   : Azure URL of NDVI×TWI risk map PNG
  FLOOD_NDBI_DEM_MAP   : Azure URL of NDBI×DEM risk map PNG
  HEAT_LST_NDVI_MAP    : Azure URL of LST×NDVI risk map PNG
  FLOOD_NDVI_TWI_SUMMARY : arrow text for NDVI×TWI pair
  FLOOD_NDBI_DEM_SUMMARY : arrow text for NDBI×DEM pair
  HEAT_LST_NDVI_SUMMARY  : arrow text for LST×NDVI pair

Colour-class table injection (no {{}} tokens — direct AST mutation)
────────────────────────────────────────────────────────────────────
  Node [10] — NDVI×TWI colour class table
  Node [17] — NDBI×DEM colour class table
  Node [27] — LST×NDVI colour class table

Context keys consumed
─────────────────────
  section_content    : str
  input_config       : dict
  assets_dir         : Path
  azure_base_path    : str
  risk_for           : str
  site_name          : str
  flood_risk_counts  : dict  — set by ara_exposure
  heat_risk_counts   : dict  — set by ara_exposure
  FLOOD_SCORE_1–5    : str   — set by ara_exposure (flat in context)
  HEAT_SCORE_1–5     : str   — set by ara_exposure (flat in context)

Context keys produced
─────────────────────
  resolved_content : str
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
import numpy as np
import rasterio
from rasterio.crs import CRS as rioCRS
from rasterio.transform import array_bounds
from rasterio.warp import Resampling, calculate_default_transform, reproject

import contextily as cx
import config
from core.storage import save_asset, save_section_output

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


# ═════════════════════════════════════════════════════════════════════════════
# Raster-processing logic  (from 6_influencing_factors.py)
# ═════════════════════════════════════════════════════════════════════════════

_RISK_RGBA: dict[str, tuple] = {
    "Very Low": ( 76/255, 235/255,  52/255, 0.75),
    "Low":      (235/255, 235/255,  52/255, 0.75),
    "High":     (235/255, 143/255,  52/255, 0.75),
    "Very High":(235/255,  52/255,  52/255, 0.75),
}

_PAIR_DEFS: dict[str, list[dict]] = {
    "Flood": [
        {
            "name":       "ndvi_twi",
            "output":     "flood_if_ndvi_twi.png",
            "title":      "Flood Influencing Factor — NDVI × TWI",
            "key_a":      "ndvi",
            "key_b":      "twi",
            "risk_table": {(0,0):"Low", (0,1):"Very High", (1,0):"Very Low", (1,1):"High"},
        },
        {
            "name":       "ndbi_dem",
            "output":     "flood_if_ndbi_dem.png",
            "title":      "Flood Influencing Factor — NDBI × DEM",
            "key_a":      "ndbi",
            "key_b":      "dem",
            "risk_table": {(0,0):"Low", (0,1):"Very Low", (1,0):"Very High", (1,1):"High"},
        },
    ],
    "Heat": [
        {
            "name":       "lst_ndvi",
            "output":     "heat_if_lst_ndvi.png",
            "title":      "Heat Influencing Factor — LST × NDVI",
            "key_a":      "lst",
            "key_b":      "ndvi",
            "risk_table": {(0,0):"Low", (0,1):"Very Low", (1,0):"Very High", (1,1):"High"},
        },
    ],
}

_FLOOD_RISK_INTERP: dict[str, str] = {
    "Very Low":  "",
    "Low":       "Low TWI values with sparse vegetation (low NDVI), suggesting limited "
                 "water retention capacity but reduced runoff buffering.",
    "Moderate":  "Localized zones of elevated TWI, partially offset by high NDVI.",
    "High":      "High TWI values combined with moderate distance from the river channel, "
                 "leading to significant water accumulation potential.",
    "Very High": "Immediate proximity to the river, where fluvial flooding dominates "
                 "despite vegetative cover (high NDVI) and relatively low TWI.",
}

_HEAT_RISK_INTERP: dict[str, str] = {
    "Very Low":  "Low LST values with sparse vegetation (low NDVI), suggesting limited "
                 "latent heat build-up.",
    "Low":       "Localized zones of elevated LST, partially offset by high NDVI.",
    "Moderate":  "High LST values combined with increased presence of built area (high NDBI).",
    "High":      "High NDBI values (low density built area and barren land) with reduced "
                 "green cover become hotspots for LST hotspot formation.",
    "Very High": "Very high levels of NDBI (indicating high density-built area) and very "
                 "low levels of NDVI (barren land) indicate heightened levels of heat "
                 "stress and impacts from increased LST.",
}

_COLOUR_CLASS: dict[str, dict] = {
    "ndvi_twi": {
        "col_a_name": "NDVI (Vegetation)",
        "col_b_name": "TWI (Wetness)",
        "rows": [
            {"color_hex": "EBEB34", "col_a": "Low",  "col_b": "Low",
             "interpretation": "Sparse vegetation with low water accumulation; areas with "
                               "limited green cover and good drainage, indicating minimal flood risk."},
            {"color_hex": "EB3434", "col_a": "High", "col_b": "Low",
             "interpretation": "Sparse vegetation with high water accumulation; bare or less "
                               "vegetated land in low-lying areas where water tends to collect, "
                               "indicating high flood vulnerability."},
            {"color_hex": "4CEB34", "col_a": "Low",  "col_b": "High",
             "interpretation": "Dense vegetation with low water accumulation; well-vegetated "
                               "areas on slopes or elevated terrain with good drainage and "
                               "minimal waterlogging."},
            {"color_hex": "EB8F34", "col_a": "High", "col_b": "High",
             "interpretation": "Dense vegetation with high water accumulation; vegetated areas "
                               "in depressions where water naturally accumulates, indicating "
                               "moderate to high flood susceptibility."},
        ],
    },
    "ndbi_dem": {
        "col_a_name": "Built-up (NDBI)",
        "col_b_name": "Elevation (DEM)",
        "rows": [
            {"color_hex": "EBEB34", "col_a": "Low",  "col_b": "Low",
             "interpretation": "Semi-open, low-lying areas with moderate flood susceptibility."},
            {"color_hex": "EB3434", "col_a": "High", "col_b": "Low",
             "interpretation": "Dense built-up in low elevation; critical flood-prone pockets "
                               "due to poor drainage."},
            {"color_hex": "4CEB34", "col_a": "Low",  "col_b": "High",
             "interpretation": "Elevated but less urbanised; relatively safe zones with "
                               "natural drainage."},
            {"color_hex": "EB8F34", "col_a": "High", "col_b": "High",
             "interpretation": "High built-up on elevated land; less flood risk locally but "
                               "contributes to downstream runoff."},
        ],
    },
    "lst_ndvi": {
        "col_a_name": "LST (Temperature)",
        "col_b_name": "NDVI (Vegetation)",
        "rows": [
            {"color_hex": "EBEB34", "col_a": "Low",  "col_b": "Low",
             "interpretation": "Areas with low density vegetation, indicating moist ground "
                               "resulting in low LST values."},
            {"color_hex": "4CEB34", "col_a": "Low",  "col_b": "High",
             "interpretation": "Areas with high density vegetation, indicating higher levels "
                               "of moisture and low levels of LST."},
            {"color_hex": "EB3434", "col_a": "High", "col_b": "Low",
             "interpretation": "Likely barren areas with low levels of vegetation and high "
                               "levels of surface temperature."},
            {"color_hex": "EB8F34", "col_a": "High", "col_b": "High",
             "interpretation": "Built areas with high surface temperatures along with vegetation."},
        ],
    },
}

_ARROW_TEXT: dict[str, str] = {
    "ndvi_twi": (
        "Areas with high TWI values show greater water accumulation potential. "
        "Zones combining low NDVI (reduced vegetation) with high TWI face the highest "
        "flood exposure, as limited vegetation does little to mitigate water retention "
        "and surface runoff."
    ),
    "ndbi_dem": (
        "Low-elevation areas with high built-up density show elevated flood vulnerability "
        "due to impervious surfaces and reduced drainage capacity. Locations at lower "
        "elevations are at increased risk of waterlogging if drainage is overwhelmed."
    ),
    "lst_ndvi": (
        "Areas with high land surface temperature and low vegetation coverage represent "
        "urban heat islands and face the highest heat stress. Dense built-up zones with "
        "minimal tree cover experience the most intense surface heating, while "
        "well-vegetated areas show significantly reduced thermal exposure."
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Raster helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_tif(keyword: str, search_dirs: list) -> Path | None:
    kw = keyword.lower()
    for d in search_dirs:
        if not Path(d).is_dir():
            continue
        for p in sorted(Path(d).glob("*.tif")):
            if kw in p.stem.lower():
                return p
    return None


def _read_classify(tif_path: Path):
    try:
        with rasterio.open(tif_path) as src:
            data      = src.read(1).astype(np.float64)
            nd        = src.nodata
            transform = src.transform
            crs       = src.crs

        if nd is not None:
            data[data == nd] = np.nan
        data[data <= -9000]   = np.nan
        data[data <= -3.0e4]  = np.nan

        valid = data[~np.isnan(data)]
        if valid.size == 0:
            logger.warning("  All nodata in %s — skipping", tif_path.name)
            return None

        _JENKS_MIN_PX = 10
        _JENKS_MAX_PX = 100_000
        threshold     = None
        method        = "jenks"

        if valid.size >= _JENKS_MIN_PX:
            try:
                import jenkspy
                sample = valid
                if valid.size > _JENKS_MAX_PX:
                    rng    = np.random.default_rng(seed=42)
                    sample = rng.choice(valid, size=_JENKS_MAX_PX, replace=False)
                breaks    = jenkspy.jenks_breaks(sample.tolist(), n_classes=2)
                threshold = float(breaks[1])
            except Exception as jenks_exc:
                logger.warning("  Jenks failed for %s (%s) — falling back to median",
                               tif_path.name, jenks_exc)

        if threshold is None:
            threshold = float(np.nanmedian(valid))
            method    = "median"

        binary = np.where(
            np.isnan(data), 255,
            np.where(data > threshold, 1, 0),
        ).astype(np.uint8)

        logger.info("  %-40s  threshold=%.4f (%s)  valid_px=%d",
                    tif_path.name, threshold, method, valid.size)
        return binary, transform, crs

    except Exception as exc:
        logger.warning("  Failed to read %s: %s", tif_path.name, exc)
        return None


def _align_to(arr_src, t_src, crs_src, arr_ref, t_ref, crs_ref) -> np.ndarray:
    h, w = arr_ref.shape
    out  = np.full((h, w), 255, dtype=np.uint8)
    reproject(
        source=arr_src, destination=out,
        src_transform=t_src, src_crs=crs_src,
        dst_transform=t_ref, dst_crs=crs_ref,
        resampling=Resampling.nearest,
        src_nodata=255, dst_nodata=255,
    )
    return out


def _build_rgba(binary_a, binary_b, risk_table) -> np.ndarray:
    h, w = binary_a.shape
    rgba = np.zeros((h, w, 4), dtype=np.float32)
    for (va, vb), risk_level in risk_table.items():
        color = _RISK_RGBA[risk_level]
        mask  = (binary_a == va) & (binary_b == vb)
        rgba[mask] = color
    nd_mask = (binary_a == 255) | (binary_b == 255)
    rgba[nd_mask, 3] = 0.0
    return rgba


def _reproject_rgba(rgba, src_transform, src_crs):
    h, w    = rgba.shape[:2]
    dst_crs = rioCRS.from_epsg(3857)
    dst_transform, dst_w, dst_h = calculate_default_transform(
        src_crs, dst_crs, w, h, *array_bounds(h, w, src_transform),
    )
    out = np.zeros((dst_h, dst_w, 4), dtype=np.float32)
    for band in range(4):
        reproject(
            source=rgba[:, :, band], destination=out[:, :, band],
            src_transform=src_transform, src_crs=src_crs,
            dst_transform=dst_transform, dst_crs=dst_crs,
            resampling=Resampling.nearest,
        )
    left, bottom, right, top = array_bounds(dst_h, dst_w, dst_transform)
    return out, dst_transform, left, right, bottom, top


def _generate_if_map(binary_a, binary_b, transform, crs,
                     risk_table, title, out_path) -> None:
    rgba = _build_rgba(binary_a, binary_b, risk_table)
    if rgba[:, :, 3].mean() < 0.05:
        logger.warning("  < 5%% valid pixels for '%s' — map skipped", title)
        return

    reproj, _dt, left, right, bottom, top = _reproject_rgba(rgba, transform, crs)
    reproj = np.clip(reproj, 0.0, 1.0)

    fig, ax = plt.subplots(figsize=(14, 12))
    ax.set_xlim(left, right)
    ax.set_ylim(bottom, top)
    cx.add_basemap(ax, source=cx.providers.Esri.WorldImagery)
    ax.imshow(reproj, extent=[left, right, bottom, top],
              origin="upper", aspect="auto", zorder=2, interpolation="nearest")

    present = list(dict.fromkeys(risk_table.values()))
    order   = ["Very High", "High", "Low", "Very Low"]
    patches = [mpatches.Patch(color=_RISK_RGBA[lv][:3], label=lv)
               for lv in order if lv in present]
    ax.legend(handles=patches, loc="upper right", fontsize=9,
              title="Risk Level", title_fontsize=10, framealpha=0.9)
    ax.set_title(title, fontsize=14, fontweight="bold")
    ax.axis("off")
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.info("Influencing factor map saved: %s", out_path.name)


def _save_class_tiff(binary_a, binary_b, transform, crs, out_path) -> None:
    h, w    = binary_a.shape
    classes = np.where(
        (binary_a == 255) | (binary_b == 255), 255,
        binary_a * 2 + binary_b,
    ).astype(np.uint8)
    with rasterio.open(
        out_path, "w", driver="GTiff",
        height=h, width=w, count=1, dtype=np.uint8,
        crs=crs, transform=transform, nodata=255,
    ) as dst:
        dst.write(classes, 1)
    logger.info("Class GeoTIFF saved: %s", out_path.name)


# ─────────────────────────────────────────────────────────────────────────────
# Colour-class table injection (direct AST mutation — no {{ }} tokens)
# ─────────────────────────────────────────────────────────────────────────────

# Old hardcoded indexes [10, 17, 27]. BE template now has more tables in this
# section, which shifts positions across versions. We can't reliably map by
# index without inspecting content, so we degrade gracefully: only inject if
# the node at the expected index is *actually* a table. If positions move,
# the colour-class cells stay empty but the rest of the section still
# resolves (maps + summary placeholders).
_COLOUR_TABLE_NODES: dict[str, int] = {
    "ndvi_twi": 10,
    "ndbi_dem": 17,
    "lst_ndvi": 27,
}


def _inject_colour_table(table_node: dict, pair_name: str) -> None:
    """Colour the first cell of each data row with the row's hex; clear any
    text inside that cell so the swatch shows as a solid colour with no
    visible hex string. Cells 1+ are left untouched (uncoloured)."""
    cc        = _COLOUR_CLASS.get(pair_name, {})
    rows_data = cc.get("rows", [])
    rows      = table_node.get("content", [])
    for ri, row_data in enumerate(rows_data):
        if ri + 1 >= len(rows):
            break
        cells = rows[ri + 1].get("content", [])
        if not cells:
            continue
        cell0 = cells[0]

        # 1) Set the colour via cell attributes (both wrappers used by the
        #    template — top-level "background" and the WordprocessingML
        #    "tableCellProperties.shading.fill").
        hex_code = row_data["color_hex"]
        attrs = cell0.setdefault("attrs", {})
        attrs["background"] = {"color": hex_code}
        cell_props = attrs.setdefault("tableCellProperties", {}) or {}
        attrs["tableCellProperties"] = cell_props
        cell_props["shading"] = {"fill": hex_code, "color": "auto", "val": "clear"}

        # 2) Clear the cell's paragraph text so the hex string no longer
        #    renders inside the swatch.
        first_cell_content = cell0.get("content")
        if first_cell_content:
            para = first_cell_content[0]
            if isinstance(para, dict):
                para["content"] = []


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Process one raster pair
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Missing-pair subsection pruner
#
# Each Influencing Factors "pair" (ndvi_twi, ndbi_dem, lst_ndvi) has its own
# subsection in the section content, introduced by a paragraph whose text
# starts with "Influence of …". If a pair fails to process upstream (missing
# raster), its MAP placeholder never lands in `derived`. Today the section
# leaves the subtitle + blank image. This pruner removes the entire pair
# subsection in that case.
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


def _is_pair_heading(node) -> bool:
    """Pair subsections start with a paragraph whose first text begins with
    'Influence of'. StyleIds vary (Heading4 vs NoSpacing) across pairs, so
    we anchor on text content rather than style."""
    if not isinstance(node, dict) or node.get("type") != "paragraph":
        return False
    text = _node_text(node).strip().lower()
    return text.startswith("influence of")


def _prune_missing_pairs(doc: dict, derived: dict) -> list[str]:
    """In-place: drop pair subsections whose MAP placeholder didn't resolve
    (i.e., the pair processing failed). Returns the list of pair names whose
    subsections were removed (for logging)."""
    content = doc.get("content")
    if not isinstance(content, list):
        return []

    starts = [i for i, n in enumerate(content) if _is_pair_heading(n)]
    if not starts:
        return []

    ranges = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(content)
        ranges.append((s, e))

    # Reverse map: placeholder name → pair key (covers MAP + SUMMARY)
    ph_to_pair: dict[str, str] = {}
    for pair, ph in _MAP_PLACEHOLDER.items():
        ph_to_pair[ph] = pair
    for pair, ph in _SUMMARY_PLACEHOLDER.items():
        ph_to_pair[ph] = pair

    missing_pairs = {p for p, ph in _MAP_PLACEHOLDER.items() if not derived.get(ph)}

    to_remove: list[int] = []
    pairs_removed: set[str] = set()
    for start, end in ranges:
        range_text = "".join(_node_text(content[j]) for j in range(start, end))
        pairs_in_range = {
            pair for ph, pair in ph_to_pair.items()
            if "{{" + ph + "}}" in range_text
        }
        if not pairs_in_range:
            continue
        if pairs_in_range.issubset(missing_pairs):
            to_remove.extend(range(start, end))
            pairs_removed.update(pairs_in_range)

    for i in sorted(to_remove, reverse=True):
        del content[i]
    return sorted(pairs_removed)


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Process one raster pair (continued)
# ─────────────────────────────────────────────────────────────────────────────

_MAP_PLACEHOLDER: dict[str, str] = {
    "ndvi_twi": "FLOOD_NDVI_TWI_MAP",
    "ndbi_dem": "FLOOD_NDBI_DEM_MAP",
    "lst_ndvi": "HEAT_LST_NDVI_MAP",
}
_SUMMARY_PLACEHOLDER: dict[str, str] = {
    "ndvi_twi": "FLOOD_NDVI_TWI_SUMMARY",
    "ndbi_dem": "FLOOD_NDBI_DEM_SUMMARY",
    "lst_ndvi": "HEAT_LST_NDVI_SUMMARY",
}


def _process_pair(pair: dict, search_dirs: list, ctx: dict) -> dict | None:
    assets = Path(ctx["assets_dir"])
    base   = ctx.get("azure_base_path", "")
    site   = ctx.get("site_name", ctx.get("area", "Site"))

    path_a = _find_tif(pair["key_a"], search_dirs)
    path_b = _find_tif(pair["key_b"], search_dirs)

    if path_a is None:
        logger.warning("[Step 4] '%s' raster not found — skipping %s",
                       pair["key_a"], pair["name"])
        return None
    if path_b is None:
        logger.warning("[Step 4] '%s' raster not found — skipping %s",
                       pair["key_b"], pair["name"])
        return None

    res_a = _read_classify(path_a)
    res_b = _read_classify(path_b)
    if res_a is None or res_b is None:
        return None

    bin_a, t_a, crs_a = res_a
    bin_b, t_b, crs_b = res_b
    bin_b_aligned = _align_to(bin_b, t_b, crs_b, bin_a, t_a, crs_a)

    out_png = assets / pair["output"]
    out_tif = assets / pair["output"].replace(".png", ".tif")

    _generate_if_map(
        bin_a, bin_b_aligned, t_a, crs_a,
        pair["risk_table"],
        f"{pair['title']} — {site}",
        out_png,
    )
    _save_class_tiff(bin_a, bin_b_aligned, t_a, crs_a, out_tif)

    azure_url  = None
    local_path = None

    if out_png.exists():
        result = save_asset(
            local_path   = out_png,
            blob_name    = f"{base}/assets/{pair['output']}",
            content_type = "image/png",
        )
        azure_url  = result.get("azure")
        local_path = result.get("local")

    if out_tif.exists():
        save_asset(
            local_path   = out_tif,
            blob_name    = f"{base}/assets/{out_tif.name}",
            content_type = "image/tiff",
        )

    url = azure_url or local_path or str(out_png)
    return {
        "url":     url,
        "summary": _ARROW_TEXT.get(pair["name"], ""),
    }


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


def _build(doc: dict, input_config: dict, context: dict, derived: dict) -> str:
    content      = json.dumps(doc, ensure_ascii=False)
    placeholders = _find_placeholders(content)
    logger.info("[Step 3] Placeholders found: %s", placeholders)

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
    doc: dict = (
        json.loads(json.dumps(raw)) if isinstance(raw, (dict, list))
        else json.loads(raw)
    )

    input_config = ctx.get("input_config", {})
    risk_for     = input_config.get("risk_for", ctx.get("risk_for", "Both"))

    flood_cog = config.COG_DIR / "Flood"
    heat_cog  = config.COG_DIR / "Heat"

    nodes   = doc.get("content", [])
    derived: dict[str, str] = {}

    # ── Flood pairs ───────────────────────────────────────────────────────────
    if risk_for in ("Flood", "Both"):
        for pair in _PAIR_DEFS.get("Flood", []):
            result = _process_pair(pair, [flood_cog, heat_cog], ctx)
            if result:
                derived[_MAP_PLACEHOLDER[pair["name"]]]     = result["url"]
                derived[_SUMMARY_PLACEHOLDER[pair["name"]]] = result["summary"]

                ni = _COLOUR_TABLE_NODES.get(pair["name"])
                if (ni is not None and ni < len(nodes)
                        and isinstance(nodes[ni], dict)
                        and nodes[ni].get("type") == "table"
                        and "content" in nodes[ni]):
                    _inject_colour_table(nodes[ni], pair["name"])
                    logger.info("[Step 4] Colour table injected for %s", pair["name"])
                else:
                    logger.warning(
                        "[Step 4] Colour table for %s not found at expected "
                        "index %s — skipping inject (template may have moved).",
                        pair["name"], ni,
                    )

    # ── Heat pairs ────────────────────────────────────────────────────────────
    if risk_for in ("Heat", "Both"):
        for pair in _PAIR_DEFS.get("Heat", []):
            result = _process_pair(pair, [heat_cog, flood_cog], ctx)
            if result:
                derived[_MAP_PLACEHOLDER[pair["name"]]]     = result["url"]
                derived[_SUMMARY_PLACEHOLDER[pair["name"]]] = result["summary"]

                ni = _COLOUR_TABLE_NODES.get(pair["name"])
                if (ni is not None and ni < len(nodes)
                        and isinstance(nodes[ni], dict)
                        and nodes[ni].get("type") == "table"
                        and "content" in nodes[ni]):
                    _inject_colour_table(nodes[ni], pair["name"])
                    logger.info("[Step 4] Colour table injected for %s", pair["name"])
                else:
                    logger.warning(
                        "[Step 4] Colour table for %s not found at expected "
                        "index %s — skipping inject (template may have moved).",
                        pair["name"], ni,
                    )

    logger.info("[Step 4] Derived placeholders: %s", list(derived.keys()))

    # Drop pair subsections that failed to process (no MAP url in `derived`).
    # Done BEFORE placeholder substitution so the literal {{...}} text still
    # identifies which subsection belongs to which pair.
    removed_pairs = _prune_missing_pairs(doc, derived)
    if removed_pairs:
        logger.info("[Step 4] Pruned subsections for missing pairs: %s", removed_pairs)

    # Steps 3–5
    resolved_content = _build(doc, input_config, ctx, derived)

    # Step 6
    ctx["resolved_content"] = resolved_content
    logger.info("[Step 6] resolved_content stored.")
    save_section_output(resolved_content, Path(__file__).stem, ctx)
    return ctx
