"""
Module: save_to_api
Final pipeline step — push every resolved section JSON from the latest run 
to the live BE `master-sections` endpoint.

How it works
────────────
1. The pipeline (`app_poc_v2._load_pipeline_config`) fetches `templates-meta`
   from the BE. Every section in that response already includes the full
   payload schema we need to POST back: `section_id`, `module`, `report_id`,
   `name`, `section_title`, `sub_section`, etc.
2. `app_poc_v2.run_pipeline()` strips the `jsonContent` field from each
   section, keyed by module name, and writes the result to
   `Report_Data/Report_<id>/_section_metadata.json`.
3. After every section has been resolved and `ara_*.json` files are on disk,
   `publish_all(output_dir)` reads `_section_metadata.json`, walks each
   `ara_*.json`, and POSTs:
       payload = metadata[<module>]  +  {"jsonContent": <contents of ara_*.json>}
4. A per-section audit trail is written to `<output_dir>/_publish_log.json`.

Usage (automatic)
─────────────────
    Called at the end of app_poc_v2.run_pipeline() — no manual step needed.

Usage (standalone, e.g. to retry a previous run)
────────────────────────────────────────────────
    python -m scripts.save_to_api                                # newest Report_*
    python -m scripts.save_to_api Report_Data/Report_20260428_004251

Environment overrides (optional)
────────────────────────────────
    RES360_MASTER_SECTIONS_URL  override endpoint URL (default: live dev)
    RES360_API_TOKEN            only used if auth is required (header wired
                                but commented out — matches current dev usage)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import requests
import urllib3

logger = logging.getLogger(__name__)

# Dev server uses a self-signed cert. Mirror the same `verify=False` posture
# that app_poc_v2._fetch_api() already uses for the read side (ssl.CERT_NONE).
# When the BE moves to a properly trusted prod cert, flip RES360_VERIFY_SSL=true
# in .env and remove this warning suppression.
_VERIFY_SSL = os.getenv("RES360_VERIFY_SSL", "false").lower() == "true"
if not _VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────────────────────────────────────

ENDPOINT = os.getenv(
    "RES360_MASTER_SECTIONS_URL",
    "https://kub-dev.resilience360.ai/api/reports/master-sections",
)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────────────────────

def save_master_section(
    payload: dict,
    json_content: dict,
    timeout: int = 30,
) -> dict:
    """
    POST a single section's resolved jsonContent to master-sections.

    Returns:
        {"ok": bool, "status": int | None, "body": dict | str}
    """
    request_payload = dict(payload)
    request_payload["jsonContent"] = json_content

    headers = {"Content-Type": "application/json"}
    # If/when BE requires auth, uncomment:
    # token = os.getenv("RES360_API_TOKEN")
    # if token:
    #     headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.post(
            ENDPOINT,
            headers=headers,
            json=request_payload,
            timeout=timeout,
            verify=_VERIFY_SSL,    # dev self-signed cert → False
        )
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}
        return {"ok": resp.ok, "status": resp.status_code, "body": body}
    except requests.RequestException as exc:
        return {"ok": False, "status": None, "body": {"error": str(exc)}}


# ─────────────────────────────────────────────────────────────────────────────
# Metadata lookup
# ─────────────────────────────────────────────────────────────────────────────

def _load_section_metadata(output_dir: Path) -> dict:
    """Load `_section_metadata.json` produced by the pipeline."""
    path = output_dir / "_section_metadata.json"
    if not path.exists():
        logger.error(
            "[publish] _section_metadata.json not found in %s. "
            "Run the pipeline first — it writes this file automatically.", 
            output_dir,
        )
        return {}
    try:
        # utf-8-sig silently strips a BOM if present (PowerShell's
        # Out-File -Encoding utf8 writes one on Windows by default).
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        logger.error("[publish] _section_metadata.json is malformed: %s", exc)
        return {}


def _load_input_config(output_dir: Path) -> dict:
    """
    Load `_input_config.json` produced by the pipeline.

    Provides `area`, `city`, `state`, `country`, `risk_for`, etc. — fields
    the BE requires in the POST body but that aren't part of per-section
    metadata. Falls back to env var RES360_AREA if the file is missing
    (useful when re-publishing an old run that pre-dates this change).
    """
    path = output_dir / "_input_config.json"
    if path.exists():
        try:
            # utf-8-sig silently strips a BOM if present.
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            logger.warning("[publish] _input_config.json malformed: %s", exc)

    area = os.getenv("RES360_AREA")
    if area:
        logger.info("[publish] _input_config.json missing — using RES360_AREA=%s", area)
        return {"area": area}

    logger.warning(
        "[publish] _input_config.json not found in %s and RES360_AREA not set. "
        "BE may reject POSTs requiring `area`.",
        output_dir,
    )
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# Bulk publish for one pipeline run
# ─────────────────────────────────────────────────────────────────────────────

def publish_all(output_dir: Path) -> dict:
    """
    Push every `Report_Data/Report_<id>/ara_*.json` to master-sections.

    Returns:
        Audit log (also written to `<output_dir>/_publish_log.json`).
    """
    output_dir = Path(output_dir)
    if not output_dir.exists():
        logger.error("[publish] output_dir does not exist: %s", output_dir)
        return {}

    section_metadata = _load_section_metadata(output_dir)
    if not section_metadata:
        return {}

    input_config = _load_input_config(output_dir)
    # Fields from input_config that BE requires in the section POST body.
    # Add more here if BE responds with "Missing required field: 'X'".
    _BODY_INJECT_KEYS = ("area",)

    logger.info("=" * 55)
    logger.info("  Step 3 — Publishing section JSONs to BE")
    logger.info("  Endpoint  : %s", ENDPOINT)
    logger.info("  Source    : %s", output_dir)
    logger.info("  Sections  : %d (from _section_metadata.json)", len(section_metadata))
    logger.info("  Area      : %s", input_config.get("area", "<not set>"))
    logger.info("=" * 55)

    log: dict = {
        "endpoint":   ENDPOINT,
        "output_dir": str(output_dir),
        "results":    [],
    }

    json_files = sorted(output_dir.glob("ara_*.json"))
    if not json_files:
        logger.warning("[publish] no ara_*.json files found in %s", output_dir)

    for json_path in json_files:
        stem  = json_path.stem
        meta  = section_metadata.get(stem)
        entry = {"module": stem, "file": json_path.name}

        if meta is None:
            entry.update({"action": "skipped", "reason": "no metadata for this module"})
            log["results"].append(entry)
            logger.warning("[publish] skip %s — not in _section_metadata.json", stem)
            continue

        try:
            json_content = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            entry.update({"action": "failed", "reason": f"cannot read JSON: {exc}"})
            log["results"].append(entry)
            logger.error("[publish] %s — failed to load: %s", stem, exc)
            continue

        # Build the full payload: per-section metadata + required input_config
        # fields (e.g. `area`). per-section values win on conflict.
        payload = {k: input_config[k] for k in _BODY_INJECT_KEYS if k in input_config}
        payload.update(meta)

        result = save_master_section(payload, json_content)
        entry.update({
            "action":     "ok" if result["ok"] else "failed",
            "status":     result["status"],
            "section_id": meta.get("section_id"),
            "report_id":  meta.get("report_id"),
            "response":   result["body"],
        })
        log["results"].append(entry)

        if result["ok"]:
            logger.info("[publish] ✓ %-30s status=%s", stem, result["status"]) 
        else:
            logger.error(
                "[publish] ✗ %-30s status=%s body=%s",
                stem, result["status"], result["body"],
            )

    log_path = output_dir / "_publish_log.json"
    log_path.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")

    ok_count   = sum(1 for r in log["results"] if r.get("action") == "ok")
    skip_count = sum(1 for r in log["results"] if r.get("action") == "skipped")
    fail_count = sum(1 for r in log["results"] if r.get("action") == "failed")
    logger.info(
        "[publish] Done — %d ok, %d skipped, %d failed.  Log: %s",
        ok_count, skip_count, fail_count, log_path,
    )

    return log


# ─────────────────────────────────────────────────────────────────────────────
# Standalone CLI entry
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    import config  # noqa: E402

    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        runs = sorted(config.REPORT_DATA_DIR.glob("Report_*"), key=lambda p: p.stat().st_mtime)
        if not runs:
            logger.error("No Report_* directories found in %s", config.REPORT_DATA_DIR)
            sys.exit(1)
        target = runs[-1]
        logger.info("Auto-selected newest run: %s", target.name)

    publish_all(target)
