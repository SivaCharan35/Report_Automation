"""
Module: ara_historical_charts — Historical Trend Charts (Self-contained)

Owns Steps 3–6 of the pipeline workflow.

  Step 3 — Detect historical folders and input files
  Step 4 — Generate charts
  Step 5 — Build metadata
  Step 6 — Store in context

"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

from core.geojson_utils import load_json
from core.storage import save_asset

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

FLOOD_COLOR = "#1f77b4"
HEAT_COLOR  = "#d62728"
_LOOKBACK_YEARS = 20


# ─────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────

def _trim(annual, n=_LOOKBACK_YEARS):
    if not annual:
        return annual
    cutoff = max(annual) - n + 1
    return {y: v for y, v in annual.items() if y >= cutoff}


def _plot_annual(years, vals, color, title, ylabel, out):
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(years, vals, color=color, marker="o")
    ax.fill_between(years, vals, alpha=0.2, color=color)
    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.set_xlabel("Year")
    ax.set_ylabel(ylabel)
    ax.xaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    ax.grid(True, axis="y", alpha=0.4)

    plt.tight_layout()
    plt.savefig(out, dpi=150)
    plt.close(fig)


# ─────────────────────────────────────────────────────────────
# Heatwave logic
# ─────────────────────────────────────────────────────────────

def _parse_daily(t2m):
    daily = []
    for d, t in t2m.items():
        try:
            daily.append((datetime.strptime(str(d), "%Y%m%d"), float(t)))
        except:
            pass
    return sorted(daily)


def _heatwave_flags_india(daily):
    flags = [False] * len(daily)

    for i, (_, t) in enumerate(daily):
        if t >= 45:
            flags[i] = True

    i = 0
    while i < len(daily):
        if daily[i][1] >= 40:
            j = i
            while j < len(daily) and daily[j][1] >= 40:
                if j > i and (daily[j][0] - daily[j-1][0]).days != 1:
                    break
                j += 1
            if j - i >= 2:
                for k in range(i, j):
                    flags[k] = True
            i = j
        else:
            i += 1
    return flags


def _monthly_counts(daily, flags):
    counts = defaultdict(int)
    for (dt, _), f in zip(daily, flags):
        if f:
            counts[(dt.year, dt.month)] += 1
    return counts


# ─────────────────────────────────────────────────────────────
# Chart functions
# ─────────────────────────────────────────────────────────────

def plot_runoff(src, out, **_):
    data = load_json(src)
    if not data:
        return False

    annual = defaultdict(float)
    for f in data.get("features", []):
        yr = f["properties"].get("year")
        val = f["properties"].get("runoff_mm", 0)
        if yr:
            annual[int(yr)] += float(val)

    annual = _trim(annual)
    if not annual:
        return False

    years = sorted(annual)
    _plot_annual(years, [annual[y] for y in years], FLOOD_COLOR,
                 "Annual Runoff", "Runoff (mm)", out)
    return True


def plot_heatwave_days(src, out, country="", **_):
    data = load_json(src)
    try:
        t2m = data["properties"]["parameter"]["T2M_MAX"]
    except:
        return False

    daily = _parse_daily(t2m)
    if not daily:
        return False

    flags = _heatwave_flags_india(daily)
    monthly = _monthly_counts(daily, flags)

    annual = defaultdict(int)
    for (yr, _), c in monthly.items():
        annual[yr] += c

    annual = _trim(annual)
    if not annual:
        return False

    years = sorted(annual)
    _plot_annual(years, [annual[y] for y in years], HEAT_COLOR,
                 "Annual Heatwave Days", "Days", out)
    return True


# ─────────────────────────────────────────────────────────────
# Detection
# ─────────────────────────────────────────────────────────────

def _match(folder, keywords):
    name = folder.name.lower()
    return all(k in name for k in keywords)


def _find_file(folder, patterns):
    for p in patterns:
        files = list(folder.glob(p))
        if files:
            return files[0]
    return None


def _detect(hist_dir, mapping):
    detected = []
    subdirs = [d for d in hist_dir.iterdir() if d.is_dir()]

    for keywords, fn, out, cap, globs in mapping:
        folder = next((d for d in subdirs if _match(d, keywords)), None)
        if not folder:
            continue

        src = _find_file(folder, globs)
        if not src:
            continue

        detected.append((fn, src, out, cap))

    return detected


# ─────────────────────────────────────────────────────────────
# Mapping
# ─────────────────────────────────────────────────────────────

_FLOOD_MAP = [
    (["runoff"], plot_runoff,
     "runoff.png", "Annual Runoff", ["*.geojson"]),
]

_HEAT_MAP = [
    (["heat"], plot_heatwave_days,
     "heatwave.png", "Heatwave Days", ["*.json"]),
]


# ─────────────────────────────────────────────────────────────
# Pipeline entry
# ─────────────────────────────────────────────────────────────

def run(context: dict) -> dict:
    ctx = dict(context)

    assets = ctx["assets_dir"]
    base   = ctx["azure_base_path"]

    flood = []
    heat  = []

    if ctx.get("hist_flood_dir"):
        detected = _detect(ctx["hist_flood_dir"], _FLOOD_MAP)
        for fn, src, name, cap in detected:
            out = assets / name
            if fn(src, out):
                save_asset(out, f"{base}/assets/{name}", "image/png")
                flood.append((out, cap))

    if ctx.get("hist_heat_dir"):
        detected = _detect(ctx["hist_heat_dir"], _HEAT_MAP)
        for fn, src, name, cap in detected:
            out = assets / name
            if fn(src, out):
                save_asset(out, f"{base}/assets/{name}", "image/png")
                heat.append((out, cap))

    ctx["historical_charts"] = {
        "flood": flood,
        "heat": heat
    }

    logger.info("Charts generated: flood=%d heat=%d", len(flood), len(heat))
    return ctx