"""
Pipeline configuration.
Edit the three flags at the top to control pipeline behaviour.
All other values are derived or loaded from the environment.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Behaviour flags ───────────────────────────────────────────────────────────
SAVE_LOCAL    : bool = True   # Write all outputs to Report_Data/<run_id>/
UPLOAD_AZURE  : bool = True   # Upload outputs to Azure Blob Storage
GENERATE_WORD : bool = True   # Produce final_report.docx (local only)

# ── Azure ─────────────────────────────────────────────────────────────────────
AZURE_CONNECTION_STRING : str = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
AZURE_CONTAINER_NAME    : str = os.getenv("AZURE_CONTAINER_NAME", "reports")

# ── Base directories (always relative to this file, not the CWD) ─────────────
BASE_DIR        : Path = Path(__file__).parent
INPUT_FILES_DIR : Path = BASE_DIR / "Input_Files"
HIST_PLOTS_DIR  : Path = BASE_DIR / "Historical_Plots"
REPORT_DATA_DIR : Path = BASE_DIR / "Report_Data"
COG_DIR         : Path = BASE_DIR / "COG"

# ── SSP horizon definitions (shared across scripts + word assembler) ──────────
SSP_HORIZONS: list[tuple] = [
    ("Near [2040]", "SSP_2040", ["SSP_Score_2.6", "SSP_Score_4.5", "SSP_Score_8.5"]),
    ("Mid [2060]",  "SSP_2060", ["SSP_Score_2.6", "SSP_Score_4.5", "SSP_Score_8.5"]),
    ("Long [2100]", "SSP_2100", ["SSP_Score_2.6", "SSP_Score_4.5", "SSP_Score_8.5"]),
]
