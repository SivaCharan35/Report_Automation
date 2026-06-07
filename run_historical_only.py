"""
Standalone runner — generates only the 8 historical chart PNGs and prints
their blob storage URLs.

Usage
─────
    python run_historical_only.py
    python run_historical_only.py --country India
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("azure").setLevel(logging.WARNING)

import config
from scripts.ara_historical import (
    _FLOOD_MAP,
    _HEAT_MAP,
    _scan_and_plot,
)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--country", default="India",
                   help="Used for heatwave classification (default: India)")
    p.add_argument("--out-tag", default=None,
                   help="Optional sub-folder tag under Report_Data (default: timestamp)")
    args = p.parse_args()

    run_id     = args.out_tag or datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = config.REPORT_DATA_DIR / f"Historical_Only_{run_id}"
    assets_dir = output_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    azure_base = f"reports/Historical_Only_{run_id}"

    flood_dir = config.HIST_PLOTS_DIR / "FLOOD"
    heat_dir  = config.HIST_PLOTS_DIR / "HEAT"

    chart_urls: dict[str, str] = {}

    if flood_dir.exists():
        chart_urls.update(_scan_and_plot(
            flood_dir, _FLOOD_MAP, assets_dir, azure_base, {}
        ))
    else:
        print(f"FLOOD dir not found: {flood_dir}", file=sys.stderr)

    if heat_dir.exists():
        chart_urls.update(_scan_and_plot(
            heat_dir, _HEAT_MAP, assets_dir, azure_base, {"country": args.country}
        ))
    else:
        print(f"HEAT dir not found: {heat_dir}", file=sys.stderr)

    # Print results in a clean ordered table
    print()
    print("=" * 100)
    print(f"  Historical Charts — {len(chart_urls)} URL(s) generated")
    print("=" * 100)

    ordered_keys = [k for _, _, _, k, _ in (_FLOOD_MAP + _HEAT_MAP)]
    file_map = {k: f for _, _, f, k, _ in (_FLOOD_MAP + _HEAT_MAP)}

    for i, key in enumerate(ordered_keys, 1):
        url = chart_urls.get(key, "(NOT GENERATED)")
        png = file_map[key]
        print(f"\n  {i}. {key}")
        print(f"     File : {png}")
        print(f"     URL  : {url}")

    print()
    print("=" * 100)
    print(f"  Local output dir : {output_dir}")
    print("=" * 100)


if __name__ == "__main__":
    main()
