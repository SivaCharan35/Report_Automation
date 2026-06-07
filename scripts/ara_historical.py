"""
Module: ara_historical — Historical Trends
Owns Steps 3–6 of the pipeline workflow.

  Step 3 — Find all {{PLACEHOLDER}} tokens in section_content
  Step 4 — Generate up to 4 chart PNGs per hazard, upload to Azure
  Step 5 — Replace placeholders with Azure URLs
  Step 6 — Store resolved_content + write to disk

Chart functions and helpers are taken directly from scripts/5_historical.py.
module5_charts.py is no longer used — this module is self-contained.

Placeholders resolved
─────────────────────
  FLOOD_HIST_GRAPH_1 : Azure URL — fig 5.1(a) rainfall days above threshold
  FLOOD_HIST_GRAPH_2 : Azure URL — fig 5.1(b) max rainfall weekly
  FLOOD_HIST_GRAPH_3 : Azure URL — fig 5.2(a) runoff
  FLOOD_HIST_GRAPH_4 : Azure URL — fig 5.2(b) precipitation weekly
  HEAT_HIST_GRAPH_1  : Azure URL — fig 5.3(a) heatwave days
  HEAT_HIST_GRAPH_2  : Azure URL — fig 5.3(b) max air temperature
  HEAT_HIST_GRAPH_3  : Azure URL — fig 5.4(a) land surface temperature
  HEAT_HIST_GRAPH_4  : Azure URL — fig 5.4(b) heat index

Context keys consumed
─────────────────────
  section_content : str        — raw jsonContent from the API
  input_config    : dict       — full input_config from the API
  assets_dir      : Path       — output directory for generated PNGs
  azure_base_path : str        — Azure blob prefix
  risk_for        : str        — "Flood" | "Heat" | "Both"
  country         : str        — used for heatwave classification (India vs WMO)

Context keys produced
─────────────────────
  resolved_content : str
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

import config
from core.chart_utils import FLOOD_COLOR, HEAT_COLOR, set_chart_style
from core.geojson_utils import load_json
from core.storage import save_asset, save_section_output

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")
_LOOKBACK_YEARS = 20


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers  (from 5_historical.py)
# ─────────────────────────────────────────────────────────────────────────────

def _trim(annual: dict, n: int = _LOOKBACK_YEARS) -> dict:
    if not annual:
        return annual
    cutoff = max(annual) - n + 1
    return {y: v for y, v in annual.items() if y >= cutoff}


def _plot_annual(years, vals, color, title, ylabel, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(years, vals, color=color, linewidth=1.5, alpha=0.85,
            marker="o", markersize=4)
    ax.fill_between(years, vals, alpha=0.2, color=color)
    ax.set_title(title, fontweight="bold", fontsize=11)
    ax.set_xlabel("Year", fontsize=9)
    ax.set_ylabel(ylabel, fontsize=9)
    ax.xaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    ax.grid(True, axis="y", alpha=0.5)
    plt.tight_layout()
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ─────────────────────────────────────────────────────────────────────────────
# Heatwave logic  (from 5_historical.py)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_daily(t2m: dict) -> list:
    daily = []
    for datestr, temp in t2m.items():
        try:
            daily.append((datetime.strptime(str(datestr), "%Y%m%d"), float(temp)))
        except (ValueError, TypeError):
            pass
    daily.sort()
    return daily


def _heatwave_flags_india(daily: list) -> list:
    n = len(daily)
    flags = [False] * n
    for i, (_, temp) in enumerate(daily):
        if temp >= 45.0:
            flags[i] = True
    i = 0
    while i < n:
        if daily[i][1] >= 40.0:
            j = i + 1
            while j < n and daily[j][1] >= 40.0:
                if (daily[j][0] - daily[j - 1][0]).days != 1:
                    break
                j += 1
            if j - i >= 2:
                for k in range(i, j):
                    flags[k] = True
            i = j
        else:
            i += 1
    return flags


def _heatwave_flags_wmo(daily: list) -> list:
    n = len(daily)
    clim: dict = defaultdict(list)
    for dt, temp in daily:
        clim[(dt.month, dt.day)].append(temp)
    clim_mean = {k: sum(v) / len(v) for k, v in clim.items()}
    anomalies = [temp - clim_mean.get((dt.month, dt.day), temp) for dt, temp in daily]
    flags = [False] * n
    i = 0
    while i < n:
        if anomalies[i] >= 5.0:
            j = i + 1
            while j < n and anomalies[j] >= 5.0:
                if (daily[j][0] - daily[j - 1][0]).days != 1:
                    break
                j += 1
            if j - i >= 5:
                for k in range(i, j):
                    flags[k] = True
            i = j
        else:
            i += 1
    return flags


def _monthly_heatwave_counts(daily: list, flags: list) -> dict:
    counts: dict = defaultdict(int)
    for (dt, _), is_hw in zip(daily, flags):
        if is_hw:
            counts[(dt.year, dt.month)] += 1
    return dict(counts)


# ─────────────────────────────────────────────────────────────────────────────
# Chart functions  (from 5_historical.py)
# ─────────────────────────────────────────────────────────────────────────────

def plot_rainfall_days_above_threshold(src: Path, out: Path, **_) -> bool:
    data = load_json(src)
    if not data:
        return False
    set_chart_style()
    annual: dict = defaultdict(float)
    for month_key, year_dict in data.items():
        for yr, val in year_dict.items():
            try:
                annual[int(yr)] += float(val)
            except (ValueError, TypeError):
                pass
    annual = _trim(annual)
    if not annual:
        return False
    years = sorted(annual)
    _plot_annual(years, [annual[y] for y in years], FLOOD_COLOR,
                 "Annual Rainfall Days Above Threshold", "Number of Days", out)
    return True


def plot_maximum_rainfall_weekly(src: Path, out: Path, **_) -> bool:
    data = load_json(src)
    if not data:
        return False
    weekly = data.get("weekly_maximum_rainfall", {})
    if not weekly:
        return False
    set_chart_style()
    annual: dict = defaultdict(float)
    for key, val in weekly.items():
        try:
            yr = int(key.split("-W")[0])
            annual[yr] = max(annual[yr], float(val))
        except Exception:
            pass
    annual = _trim(annual)
    if not annual:
        return False
    years = sorted(annual)
    _plot_annual(years, [annual[y] for y in years], FLOOD_COLOR,
                 "Annual Maximum Rainfall", "Rainfall (mm)", out)
    return True


def plot_runoff(src: Path, out: Path, **_) -> bool:
    data = load_json(src)
    if not data:
        return False
    set_chart_style()
    annual: dict = defaultdict(float)
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        yr = props.get("year")
        if yr is not None:
            try:
                annual[int(yr)] += float(props.get("runoff_mm", 0))
            except (ValueError, TypeError):
                pass
    annual = _trim(annual)
    if not annual:
        return False
    years = sorted(annual)
    _plot_annual(years, [annual[y] for y in years], FLOOD_COLOR,
                 "Annual Total Runoff", "Runoff (mm)", out)
    return True


def plot_precipitation_weekly(src: Path, out: Path, **_) -> bool:
    data = load_json(src)
    if not data or not isinstance(data, list):
        return False
    set_chart_style()
    annual: dict = defaultdict(float)
    for item in data:
        try:
            yr = int(item["start_date"][:4])
            annual[yr] += float(item["rainfall"])
        except Exception:
            pass
    annual = _trim(annual)
    if not annual:
        return False
    years = sorted(annual)
    _plot_annual(years, [annual[y] for y in years], FLOOD_COLOR,
                 "Annual Total Precipitation", "Rainfall (mm)", out)
    return True


def plot_heatwave_days(src: Path, out: Path, country: str = "", **_) -> bool:
    data = load_json(src)
    if not data:
        return False
    try:
        t2m = data["properties"]["parameter"]["T2M_MAX"]
    except (KeyError, TypeError):
        return False
    daily = _parse_daily(t2m)
    if not daily:
        return False
    is_india = country.strip().lower() == "india"
    if is_india:
        flags    = _heatwave_flags_india(daily)
        subtitle = "IMD: spell ≥40°C (2+ days) or single day ≥45°C"
    else:
        flags    = _heatwave_flags_wmo(daily)
        subtitle = "WMO: anomaly ≥+5°C for 5+ consecutive days"
    monthly = _monthly_heatwave_counts(daily, flags)
    annual: dict = defaultdict(int)
    for (yr, _), count in monthly.items():
        annual[yr] += count
    annual = _trim(annual)
    if not annual:
        return False
    years = sorted(annual)
    set_chart_style()
    _plot_annual(years, [annual[y] for y in years], HEAT_COLOR,
                 f"Annual Heatwave Days\n{subtitle}", "Heatwave Days", out)
    return True


def plot_max_temp_weekly(src: Path, out: Path, **_) -> bool:
    data = load_json(src)
    if not data:
        return False
    try:
        t2m = data["properties"]["parameter"]["T2M_MAX"]
    except (KeyError, TypeError):
        return False
    set_chart_style()
    annual: dict = defaultdict(float)
    for datestr, temp in t2m.items():
        try:
            yr = int(str(datestr)[:4])
            annual[yr] = max(annual[yr], float(temp))
        except (ValueError, TypeError):
            pass
    annual = _trim(annual)
    if not annual:
        return False
    years = sorted(annual)
    _plot_annual(years, [annual[y] for y in years], HEAT_COLOR,
                 "Annual Maximum Air Temperature", "Temperature (°C)", out)
    return True


def plot_lst(src: Path, out: Path, **_) -> bool:
    data = load_json(src)
    if not data or not isinstance(data, list):
        return False
    set_chart_style()
    annual: dict = defaultdict(list)
    for item in data:
        try:
            yr = int(str(item["date"])[:4])
            annual[yr].append(float(item["LST_C"]))
        except Exception:
            pass
    annual_mean = {y: sum(v) / len(v) for y, v in annual.items() if v}
    annual_mean = _trim(annual_mean)
    if not annual_mean:
        return False
    years = sorted(annual_mean)
    _plot_annual(years, [annual_mean[y] for y in years], HEAT_COLOR,
                 "Annual Mean Land Surface Temperature (LST)", "LST (°C)", out)
    return True


def plot_heat_index(src: Path, out: Path, **_) -> bool:
    data = load_json(src)
    if not data or not isinstance(data, list):
        return False
    set_chart_style()
    annual: dict = defaultdict(float)
    for item in data:
        try:
            yr = int(item["date"][:4])
            annual[yr] = max(annual[yr], float(item["HI"]))
        except Exception:
            pass
    annual = _trim(annual)
    if not annual:
        return False
    years = sorted(annual)
    _plot_annual(years, [annual[y] for y in years], HEAT_COLOR,
                 "Annual Maximum Heat Index", "Heat Index (°C)", out)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Chart map  (keywords → plot fn → output name → placeholder key → file globs)
# ─────────────────────────────────────────────────────────────────────────────

_FLOOD_MAP: list[tuple] = [
    (["rainfall", "threshold"],           plot_rainfall_days_above_threshold,
     "fig_5_1a_rainfall_days.png",        "FLOOD_HIST_GRAPH_1", ["*.json"]),
    (["maximum", "rainfall", "weekly"],   plot_maximum_rainfall_weekly,
     "fig_5_1b_max_rainfall_weekly.png",  "FLOOD_HIST_GRAPH_2", ["*.json"]),
    (["runoff"],                           plot_runoff,
     "fig_5_2a_runoff.png",               "FLOOD_HIST_GRAPH_3", ["*.geojson", "*.json"]),
    (["precipitation", "weekly"],          plot_precipitation_weekly,
     "fig_5_2b_precipitation_weekly.png", "FLOOD_HIST_GRAPH_4", ["*.json"]),
]

_HEAT_MAP: list[tuple] = [
    (["heat", "wave"],                    plot_heatwave_days,
     "fig_5_3a_heatwave_days.png",        "HEAT_HIST_GRAPH_1", ["*.json"]),
    (["max", "temp"],                     plot_max_temp_weekly,
     "fig_5_3b_max_temp_weekly.png",      "HEAT_HIST_GRAPH_2", ["*.json"]),
    (["lst"],                             plot_lst,
     "fig_5_4a_lst.png",                  "HEAT_HIST_GRAPH_3", ["*.json"]),
    (["hi"],                              plot_heat_index,
     "fig_5_4b_heat_index.png",           "HEAT_HIST_GRAPH_4", ["*.json"]),
]


# ─────────────────────────────────────────────────────────────────────────────
# Detection + generation
# ─────────────────────────────────────────────────────────────────────────────

def _match(folder: Path, keywords: list) -> bool:
    name = folder.name.lower().replace(" ", "_").replace("-", "_")
    return all(kw in name for kw in keywords)


def _first_file(directory: Path, globs: list) -> Path | None:
    for pattern in globs:
        matches = sorted(directory.glob(pattern))
        if matches:
            return matches[0]
    return None


def _scan_and_plot(
    hist_dir: Path,
    chart_map: list,
    assets_dir: Path,
    azure_base: str,
    extra_kwargs: dict,
) -> dict[str, str]:
    """Return {placeholder_key: azure_or_local_url} for every chart generated."""
    result: dict[str, str] = {}
    subdirs = [d for d in hist_dir.iterdir() if d.is_dir()]

    for keywords, fn, out_name, ph_key, globs in chart_map:
        folder = next((d for d in subdirs if _match(d, keywords)), None)
        if folder is None:
            logger.warning("[Step 4] No folder matching %s in %s", keywords, hist_dir)
            continue

        src = _first_file(folder, globs)
        if src is None:
            logger.warning("[Step 4] No matching file (%s) in %s", globs, folder)
            continue

        out = assets_dir / out_name
        try:
            ok = fn(src, out, **extra_kwargs)
        except Exception as exc:
            logger.error("[Step 4] Chart %s failed: %s", out_name, exc)
            continue

        if not ok:
            logger.warning("[Step 4] Chart fn returned False for %s", out_name)
            continue

        saved = save_asset(
            local_path   = out,
            blob_name    = f"{azure_base}/assets/{out_name}",
            content_type = "image/png",
        )
        url = saved.get("azure") or saved.get("local") or str(out)
        result[ph_key] = url
        logger.info("[Step 4] %s → %s", ph_key, out_name)

    return result


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


def _build(content: str, input_config: dict, context: dict,
           chart_urls: dict[str, str]) -> str:
    placeholders = _find_placeholders(content)
    logger.info("[Step 3] Placeholders found: %s", placeholders)

    value_map: dict = {}
    for key in placeholders:
        if key in input_config:
            value_map[key] = str(input_config[key])
        elif key in chart_urls:
            value_map[key] = chart_urls[key]
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
    content = (
        json.dumps(raw, ensure_ascii=False)
        if isinstance(raw, (dict, list))
        else str(raw)
    )
    input_config = ctx.get("input_config", {})
    risk_for     = input_config.get("risk_for", ctx.get("risk_for", "Both"))
    country      = input_config.get("country",  ctx.get("country", ""))
    assets       = Path(ctx["assets_dir"])
    azure_base   = ctx.get("azure_base_path", "")

    # Resolve historical plot directories from config
    hist_flood_dir = ctx.get("hist_flood_dir") or (config.HIST_PLOTS_DIR / "FLOOD")
    hist_heat_dir  = ctx.get("hist_heat_dir")  or (config.HIST_PLOTS_DIR / "HEAT")

    chart_urls: dict[str, str] = {}

    # Step 4 — generate charts + upload
    if risk_for in ("Flood", "Both") and hist_flood_dir.exists():
        logger.info("[Step 4] Scanning flood historical plots: %s", hist_flood_dir)
        chart_urls.update(_scan_and_plot(
            hist_flood_dir, _FLOOD_MAP, assets, azure_base, {}
        ))
    elif risk_for in ("Flood", "Both"):
        logger.warning("[Step 4] Flood historical dir not found: %s", hist_flood_dir)

    if risk_for in ("Heat", "Both") and hist_heat_dir.exists():
        logger.info("[Step 4] Scanning heat historical plots: %s", hist_heat_dir)
        chart_urls.update(_scan_and_plot(
            hist_heat_dir, _HEAT_MAP, assets, azure_base, {"country": country}
        ))
    elif risk_for in ("Heat", "Both"):
        logger.warning("[Step 4] Heat historical dir not found: %s", hist_heat_dir)

    logger.info("[Step 4] Chart URLs resolved: %d", len(chart_urls))

    # Steps 3–5
    resolved_content = _build(content, input_config, ctx, chart_urls)

    # Step 6
    ctx["resolved_content"] = resolved_content
    logger.info("[Step 6] resolved_content stored.")

    save_section_output(resolved_content, Path(__file__).stem, ctx)
    return ctx
