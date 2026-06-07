"""
GeoJSON / JSON loading and statistical utilities.
All functions are pure — they take paths/dicts and return results with no side-effects.
"""

from __future__ import annotations

import json
import math
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from core.classifiers import RISK_LABELS, RISK_ORDER

logger = logging.getLogger(__name__)


def load_geojson(path: Path) -> dict | None:
    if not path.exists():
        logger.warning("GeoJSON not found: %s", path)
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_json(path: Path) -> Any | None:
    if not path.exists():
        logger.warning("JSON not found: %s", path)
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def compute_risk_counts(
    geojson_data: dict,
    classify_fn: Callable[[float], str],
) -> dict[str, int]:
    """Return {risk_label: count} for all 5 risk levels."""
    counts: dict[str, int] = {v: 0 for v in RISK_LABELS.values()}
    for feat in geojson_data["features"]:
        label = classify_fn(feat["properties"].get("risk_score", 0))
        counts[label] = counts.get(label, 0) + 1
    return counts


def compute_aoi_area(geojson_data: dict, country: str = "") -> tuple[float, str]:
    """
    Return (value, unit_label) for the AOI bounding-box area.

    India  → square kilometres ("sq km")
    Others → square miles      ("sq mile")

    The lat/lon → km math is identical in both branches; only the final
    conversion factor and unit label differ.
    """
    lons, lats = [], []
    for feat in geojson_data["features"]:
        geom = feat["geometry"]
        if geom["type"] == "Polygon":
            rings = geom["coordinates"]
        elif geom["type"] == "MultiPolygon":
            rings = [r for poly in geom["coordinates"] for r in poly]
        else:
            continue
        for ring in rings:
            for lon, lat, *_ in ring:
                lons.append(lon)
                lats.append(lat)
    if not lons:
        return 0.0, ("sq km" if country.strip().lower() == "india" else "sq mile")

    mid_lat = sum(lats) / len(lats)
    lat_km  = (max(lats) - min(lats)) * 111.0
    lon_km  = (max(lons) - min(lons)) * 111.0 * math.cos(math.radians(mid_lat))
    area_sqkm = lat_km * lon_km

    if country.strip().lower() == "india":
        return round(area_sqkm, 1), "sq km"
    return round(area_sqkm / 2.59, 1), "sq mile"


def compute_aoi_sqmiles(geojson_data: dict) -> float:
    """Backward-compatible wrapper — always returns square miles."""
    value, _ = compute_aoi_area(geojson_data, country="")
    return value


def build_ssp_counts(
    geojson_data: dict,
    classify_today_fn: Callable[[float], str],
    classify_ssp_fn: Callable[[float], str],
    ssp_horizons: list[tuple],
) -> tuple[dict[str, int], list[tuple], dict[tuple, dict[str, int]]]:
    """
    Compute today + projected SSP risk counts.

    Returns:
        today_counts  — {risk_label: count}
        cols          — ordered list of (horizon_label, prop_key, sub_key) tuples
        per_col       — {col_tuple: {risk_label: count}}
    """
    today_counts: dict[str, int] = defaultdict(int)
    cols: list[tuple] = []
    for hlabel, prop_key, sub_keys in ssp_horizons:
        for sk in sub_keys:
            cols.append((hlabel, prop_key, sk))

    per_col: dict[tuple, dict] = {col: defaultdict(int) for col in cols}

    for feat in geojson_data["features"]:
        props = feat["properties"]
        today_counts[classify_today_fn(props.get("risk_score", 0))] += 1
        for col in cols:
            _, prop_key, sk = col
            score = props.get(prop_key, {}).get(sk, 0) if isinstance(props.get(prop_key), dict) else 0
            per_col[col][classify_ssp_fn(score)] += 1

    return (
        dict(today_counts),
        cols,
        {k: dict(v) for k, v in per_col.items()},
    )
