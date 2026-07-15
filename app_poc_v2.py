"""
Asset Resilience Assessment — POC Orchestrator v2 
==================================================
Extends app_poc.py to dispatch the new ara_* modules.

Step 1 — Fetch sections + input_config from the live BE API. 
           Falls back to api_response.json on the disk if the API 
           is unreachable or returns an error. 
Step 2 — Dispatch each section to its ara_* module. 

Usage
─────
    python app_poc_v2.py                          # live API, default area
    python app_poc_v2.py --area "Shell Norco"     # live API, different area
    python app_poc_v2.py --api-url <url>          # explicit API URL
    python app_poc_v2.py --local-only             # skip Azure upload
    python app_poc_v2.py --offline                # force static api_response.json
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("azure").setLevel(logging.WARNING)
logger = logging.getLogger("app_poc_v2")

import config  # noqa: E402

# ── Live API ──────────────────────────────────────────────────────────────────
_API_BASE_URL    = "https://kub-dev.resilience360.ai/api/reports/templates-meta"
_DEFAULT_AREA    = "Shell Norco Refinery"
_DEFAULT_REPORT_TYPE = "Resilience assessment" 

# ── Fallback static file ──────────────────────────────────────────────────────
_API_RESPONSE_PATH = Path(__file__).parent / "api_response.json"

# ── Section name → ara_* module (only implemented modules listed) ─────────────
_MODULE_MAP: dict[str, str] = {
    "Contents and Introduction": "ara_intro",
    "Overview":                  "ara_overview",
    "Exposure":                  "ara_exposure",
    "Analytics":                 "ara_analytics",   # live API name
    "Header":                    "ara_analytics",   # static file name
    "SSP Scenario Analysis":     "ara_ssp_scenario",
    "Impact Scale":              "ara_impact_scale",
    "Historical Trends":         "ara_historical",
    "Influencing Factors":       "ara_influencing_factors",
    "Risk Insights":             "ara_risk_insights",
    "Parametric":                "ara_parametric",
    "Conclusion & Appendix":     "ara_conclusion_appendix",
}


# ═════════════════════════════════════════════════════════════════════════════
# Step 1 — Fetch pipeline config from the live BE API
# ═════════════════════════════════════════════════════════════════════════════

def _build_api_url(api_url: str | None, area: str) -> str:
    """Return the full API URL, inserting query params if only the base was given."""
    if api_url:
        return api_url
    params = urllib.parse.urlencode({
        "report_type": _DEFAULT_REPORT_TYPE,
        "area":        area,
    })
    return f"{_API_BASE_URL}?{params}"


def _fetch_api(url: str, timeout: int = 15) -> dict:
    """HTTP GET the API and return the parsed JSON payload."""
    logger.info("[Step 1] Calling live API: %s", url)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    # Dev server uses a self-signed cert — disable verification
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        if resp.status != 200:
            raise RuntimeError(f"API returned HTTP {resp.status}")
        return json.loads(resp.read().decode())


def _load_from_file() -> dict:
    """Load the saved api_response.json as a fallback."""
    if not _API_RESPONSE_PATH.exists():
        logger.error("Fallback file not found: %s", _API_RESPONSE_PATH)
        sys.exit(1)
    logger.info("[Step 1] Loading fallback file: %s", _API_RESPONSE_PATH)
    with open(_API_RESPONSE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_pipeline_config(
    api_url: str | None,
    area: str,
    offline: bool,
) -> tuple[dict, list[dict], dict]:
    """
    Fetch config from the live API; fall back to api_response.json on failure.
    Injects ara_* module names from _MODULE_MAP into each section.

    Returns
    -------
    input_config     : dict
    sections         : list[dict]  — each item has "module" + "jsonContent"
    section_metadata : dict        — { module_stem: {section_id, name, ...} }
                       Copy of every section field except `jsonContent`, keyed by
                       module name. Used as the publish-step payload (see
                       scripts/save_to_api.py). The API already returns
                       `section_id`, `module`, `report_id`, `section_title`,
                       `sub_section`, etc. — we just thread them through.
    """
    payload: dict | None = None

    if False :
        url = _build_api_url(api_url, area)
        try:
            payload = _fetch_api(url)
            logger.info("[Step 1] Live API response received.")
        except (urllib.error.URLError, RuntimeError, json.JSONDecodeError, OSError) as exc:
            logger.warning("[Step 1] Live API unreachable (%s) — falling back to file.", exc)

    if payload is None:
        payload = _load_from_file()

    input_config: dict       = payload.get("input_config") or {}
    raw_sections: list[dict] = payload.get("sections")     or []

    # TEMPORARY — normalise "Flood only" / "Heat only" → "Flood" / "Heat"
    # Remove once backend sends the canonical value ("Flood" / "Heat" / "Both").
    input_config["risk_for"] = input_config.get("risk_for", "Both").replace(" only", "")

    # Override module names with the ara_* implementations + capture per-section
    # metadata for the publish step.
    sections:         list[dict]      = []
    section_metadata: dict[str, dict] = {}

    for s in raw_sections:
        section = dict(s)
        # Prefer the API's own `module` field (templates-meta returns it).
        # Fall back to _MODULE_MAP keyed by `name` for backward compatibility.
        mod = s.get("module") or _MODULE_MAP.get(s.get("name", ""))
        section["module"] = mod  # None → skipped by the dispatcher
        sections.append(section)

        if mod:
            # Keep every API field except jsonContent — the publish step rebuilds
            # the payload from this metadata + the resolved jsonContent on disk.
            section_metadata[mod] = {k: v for k, v in s.items() if k != "jsonContent"}

    logger.info("[Step 1] input_config keys : %s", list(input_config.keys()))
    logger.info(
        "[Step 1] sections received : %d  (%d with a module)",
        len(sections),
        sum(1 for s in sections if s.get("module")),
    )
    return input_config, sections, section_metadata


# ═════════════════════════════════════════════════════════════════════════════
# Step 2 — Dispatch each section to its ara_* module
# ═════════════════════════════════════════════════════════════════════════════

def _dispatch(section: dict, input_config: dict, context: dict) -> dict:
    """
    Load section["module"] and call its run().

    Injects into context:
      section_name    — display name
      section_content — raw jsonContent (placeholders not yet resolved)
      input_config    — full input_config dict from the API
    """
    module_name  = section["module"]
    section_name = section.get("name", module_name)
    json_content = section.get("jsonContent") or ""

    logger.info("[Step 2] ▶  %-35s → %s", section_name, module_name)

    context["section_name"]    = section_name
    context["section_content"] = json_content
    context["input_config"]    = input_config

    mod     = importlib.import_module(f"scripts.{module_name}")
    context = mod.run(context)
    return context


# ═════════════════════════════════════════════════════════════════════════════
# Pipeline runner
# ═════════════════════════════════════════════════════════════════════════════

def run_pipeline(
    api_url: str | None = None,
    area:    str        = _DEFAULT_AREA,
    offline: bool       = False,
) -> dict:
    # Step 1
    input_config, sections, section_metadata = _load_pipeline_config(api_url, area, offline)

    # Base context
    report_id  = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = config.REPORT_DATA_DIR / f"Report_{report_id}"
    assets_dir = output_dir / "assets"

    if config.SAVE_LOCAL:
        assets_dir.mkdir(parents=True, exist_ok=True)
        # Persist the per-section payload metadata so the publish step
        # (scripts/save_to_api.py) can re-build POST bodies without hitting
        # templates-meta a second time. Also lets us re-publish a previous run
        # standalone: `python -m scripts.save_to_api Report_Data/Report_<id>`
        (output_dir / "_section_metadata.json").write_text(
            json.dumps(section_metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        # Also persist the API's input_config — master-sections requires `area`
        # (and possibly more fields later) in the POST body, which lives here.
        (output_dir / "_input_config.json").write_text(
            json.dumps(input_config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    context: dict = {
        "report_id":        report_id,
        "output_dir":       output_dir,
        "assets_dir":       assets_dir,
        "azure_base_path":  f"reports/Report_{report_id}",
        "input_files_dir":  config.INPUT_FILES_DIR,
        "hist_plots_dir":   config.HIST_PLOTS_DIR,
        "section_metadata": section_metadata,
    }
    # Seed flat input_config values so modules can read them directly
    context.update(input_config)

    # Step 2 — dispatch
    logger.info("=" * 55)
    logger.info("  Pipeline v2 — %d section(s)", len(sections))
    logger.info("=" * 55)

    for section in sections:
        module_name = section.get("module")
        if not module_name:
            logger.info(
                "[Step 2] —  %-35s  (no module yet, skipped)",
                section.get("name", "?"),
            )
            continue
        _jc = section.get("jsonContent")
        if not _jc or (isinstance(_jc, str) and not _jc.strip()):
            logger.info(
                "[Step 2] —  %-35s  (empty jsonContent, skipped)",
                section.get("name", "?"),
            )
            continue
        context = _dispatch(section, input_config, context)

    # ── Step 3 — Publish resolved section JSONs to the live BE ───────────────
    # Skipped automatically if `--local-only` (UPLOAD_AZURE=False) is used.
    if config.UPLOAD_AZURE:
        try:
            from scripts.save_to_api import publish_all
            publish_all(output_dir)
        except ImportError as exc:
            logger.warning(
                "Publish step skipped — missing dependency (%s). "
                "Install with: pip install requests",
                exc,
            )
        except Exception:
            logger.exception(
                "Publish step failed (pipeline outputs are still on disk in %s).",
                output_dir,
            )
    else:
        logger.info("Publish step skipped (--local-only / UPLOAD_AZURE=False).")

    logger.info("=" * 55)
    logger.info("  Pipeline v2 complete — Report ID: %s", report_id)
    logger.info("  Output dir : %s", output_dir)
    logger.info("=" * 55)
    return context


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Asset Resilience Assessment — POC v2")
    p.add_argument(
        "--api-url", default=None, metavar="URL",
        help="Full BE API URL (overrides --area if given)",
    )
    p.add_argument(
        "--area", default=_DEFAULT_AREA, metavar="AREA",
        help=f"Area name passed to the API (default: {_DEFAULT_AREA})",
    )
    p.add_argument(
        "--local-only", action="store_true",
        help="Skip Azure upload — write local files only",
    )
    p.add_argument(
        "--offline", action="store_true",
        help="Skip live API call and use api_response.json directly",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.local_only:
        config.UPLOAD_AZURE = False
    try:
        run_pipeline(
            api_url = args.api_url,
            area    = args.area,
            offline = args.offline,
        )
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:
        logger.exception("Pipeline v2 failed.")
        sys.exit(1)
