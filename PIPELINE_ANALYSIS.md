# HHMD Pipeline Analysis
*Generated: 2026-04-21*

---

## What This System Does

**HHMD** (Hazard & Heat Map Dashboard) is a **climate risk report generator** for flood and heat hazards. Given a site's GeoJSON data, it runs an 8-stage pipeline and produces a professional Word document with maps, charts, tables, and LLM-generated text.

**Built for:** Resilience AI Solutions  
**Clients so far:** Alagiyanallur (India), Shell Norco (Louisiana, USA)

---

## Pipeline Flow

```
app.py  →  run_pipeline()
            │
            ├── Stage 0  [scripts/0_input.py]       Collect area/client/hazard details
            ├── Stage 1  [scripts/1_intro.py]        Generate executive summary text
            ├── Stage 2  [scripts/2_methodology.py]  Generate methodology text
            ├── Stage 3  [scripts/3_risk_assessment.py] Risk maps + SSP projection tables
            ├── Stage 4  [scripts/4_impact.py]       Static 5-level impact definitions
            ├── Stage 5  [scripts/5_historical.py]   Historical trend charts (rainfall, temp)
            ├── Stage 6  [scripts/6_influencing_factors.py]  Geospatial factor maps
            ├── Stage 7  [scripts/7_appendices.py]   Layer maps + LLM impact text (Claude API)
            │
            └── word_report.py  →  final_report.docx
```

All stages share a single **context dict** that grows at each step. Each stage reads inputs from context and writes its outputs back in.

---

## Directory Structure

```
0. HHMD/
├── app.py                      # Entry point, orchestrates all stages
├── config.py                   # Flags (SAVE_LOCAL, UPLOAD_AZURE, GENERATE_WORD), paths
├── word_report.py              # Final Word document builder (~1,258 lines)
├── azure_utils.py              # Azure Blob Storage upload helpers
├── requirements.txt
├── .env                        # ⚠️ Contains live API keys and Azure credentials
├── .env.example                # Safe template with empty values
│
├── core/                       # Shared utility modules
│   ├── classifiers.py          # Score → risk label (flood/heat, current/RCP/SSP)
│   ├── chart_utils.py          # Colour constants, matplotlib style helpers
│   ├── geojson_utils.py        # Load GeoJSON, compute risk counts, AOI area
│   ├── storage.py              # save_asset() — write locally and/or upload to Azure
│   └── word_utils.py           # Word building blocks (headings, tables, images)
│
├── scripts/                    # One file per pipeline stage
├── COG/                        # Input rasters (GeoTIFF) and vector layers
│   ├── Flood/                  # DEM, TWI, NDVI, LULC, HYSOG, Slope, Geomorphology
│   ├── Heat/                   # LST, NDVI, NDBI, LULC
│   ├── roads/                  # shell_AOI.geojson  ⚠️ see data mismatch below
│   └── waterline/              # shell_AOI.geojson  ⚠️ see data mismatch below
│
├── Input_Files/                # Per-site GeoJSONs with house-level risk scores
│   └── Alagiyanallur_*_flood.geojson / *_heat.geojson
│
├── Historical_Plots/           # Pre-computed time-series JSON files
│   ├── FLOOD/                  # Precipitation, max rainfall, runoff, rain-days
│   └── HEAT/                   # Heatwave days, heat index, LST, max temp
│
└── Report_Data/                # All generated outputs (timestamped subdirs)
```

---

## What Each Stage Produces

| Stage | Script | Key Outputs |
|-------|--------|-------------|
| 0 | 0_input.py | Context populated: area_name, city, state, country, client, risk_for, GeoJSON paths, hist dirs |
| 1 | 1_intro.py | `1_intro.txt` — executive summary |
| 2 | 2_methodology.py | `2_methodology.txt` — methodology section |
| 3 | 3_risk_assessment.py | `flood_risk_map.png`, `heat_risk_map.png`, `flood_ssp_table.png`, `heat_ssp_table.png`, `3_risk.json` |
| 4 | 4_impact.py | `4_impact.json` — static 5-level impact scale |
| 5 | 5_historical.py | `fig_5_1a` through `fig_5_4b` PNGs (8 charts total) |
| 6 | 6_influencing_factors.py | `flood_if_ndvi_twi.png`, `flood_if_ndbi_dem.png`, `heat_if_lst_ndvi.png`, `6_influencing_factors.json` |
| 7 | 7_appendices.py | `app_dem.png`, `app_twi.png`, `app_ndvi.png`, `app_ndbi.png`, `app_lst.png`, `app_lulc.png`, `app_roads.png`, `app_waterline.png`, `8_appendices.json` |
| Word | word_report.py | `final_report.docx` |

---

## Configuration Flags (`config.py`)

| Flag | Default | Effect |
|------|---------|--------|
| `SAVE_LOCAL` | `True` | Write all outputs to `Report_Data/` |
| `UPLOAD_AZURE` | `True` | Upload all outputs to Azure Blob Storage |
| `GENERATE_WORD` | `True` | Run `word_report.py` to build the `.docx` |

**CLI overrides:**
```bash
python app.py --no-word       # Skip Word generation
python app.py --no-azure      # Disable Azure upload
python app.py --local-only    # Same as --no-azure
```

---

## SSP Climate Projections

The pipeline supports three IPCC shared socioeconomic pathways across three horizons:

| Horizon | Key | SSP Scenarios |
|---------|-----|---------------|
| Near [2040] | SSP_2040 | SSP 2.6, SSP 4.5, SSP 8.5 |
| Mid [2060] | SSP_2060 | SSP 2.6, SSP 4.5, SSP 8.5 |
| Long [2100] | SSP_2100 | SSP 2.6, SSP 4.5, SSP 8.5 |

These scores must be pre-computed and embedded in the input GeoJSONs as properties.

---

## External Dependencies

| Service | Used for | Credential |
|---------|----------|-----------|
| Anthropic Claude API | LLM impact text in appendices | `ANTHROPIC_API_KEY` |
| Azure Blob Storage | Cloud output upload | `AZURE_STORAGE_CONNECTION_STRING` |
| contextily (OpenStreetMap tiles) | Satellite basemaps on maps | Internet access required |
| Google Gemini API | Unused/optional | `GEMINI_API_KEY` |
| OpenAI API | Unused/optional | `OPENAI_API_KEY` |

---

---

## MAJOR ISSUES & LOOPHOLES

### CRITICAL

---

#### 1. `.env` File Has No `.gitignore` Protection

**The `.env` file is sitting in the project root with live credentials and no `.gitignore` file exists.**

If this project is ever pushed to GitHub, all secrets are immediately exposed:
- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY`
- `OPENAI_API_KEY`
- `AZURE_STORAGE_CONNECTION_STRING` (full account key — grants read/write to all blobs in the storage account)

**Fix:** Create `.gitignore` immediately:
```
.env
__pycache__/
*.pyc
Report_Data/
```

---

#### 2. Data Mismatch — Roads/Waterline Are Shell Norco, Not Alagiyanallur

`COG/roads/shell_AOI.geojson` and `COG/waterline/shell_AOI.geojson` are named after and contain the **Shell Norco (Louisiana) site**. But the current input GeoJSONs and COG rasters are for **Alagiyanallur, India**.

When you run the pipeline for Alagiyanallur, the `app_roads.png` and `app_waterline.png` maps will silently show Shell's road/waterline network overlaid on or near wrong coordinates. The report includes this map in the appendix without any warning.

**Fix:** Replace `COG/roads/` and `COG/waterline/` contents with Alagiyanallur-specific vector data before running.

---

#### 3. Historical Data Is for Shell Norco, Not Alagiyanallur

All files in `Historical_Plots/` are named `Shell_30.0068_-90.3812_*` — these are coordinates for Norco, Louisiana (30.00°N, 90.38°W).

Running the pipeline for Alagiyanallur will produce charts labeled with Alagiyanallur's name but showing **Shell Norco's climate history** — a different continent, climate zone, and baseline entirely.

**Fix:** Replace `Historical_Plots/FLOOD/` and `Historical_Plots/HEAT/` with Alagiyanallur-specific JSON data before running.

---

#### 4. Missing `NDBI` Raster in `COG/Flood/`

Stage 6 (`6_influencing_factors.py`) computes a **NDBI × DEM** combined risk map for flood analysis, requiring both `NDBI` and `DEM` rasters from `COG/Flood/`.

Currently `COG/Flood/` contains:
```
Alagianallur_LULC.tif
Alagianallur_NDVI.tif
Alagianallur_TWI.tif
Alagianallur_dem.tif
Geomorphology_Alagianallur.geojson
HYSOG_Alagianallur.tif
Slope_Alagianallur.tif
```

**`Alagianallur_NDBI.tif` is missing from `COG/Flood/`.** Only `COG/Heat/` has it.

Result: Stage 6 will either silently skip the NDBI×DEM map or throw an error, leaving `flood_if_ndbi_dem.png` absent from the report. Confirmed by inspecting the latest run (`Report_20260420_221956/assets/`) — `flood_if_ndbi_dem.png` is not there.

**Fix:** Copy or compute `Alagianallur_NDBI.tif` into `COG/Flood/`.

---

#### 5. Missing Impervious Raster in `COG/Flood/`

Stage 7 (`7_appendices.py`) generates `app_impervious.png` for the flood appendix, requiring an impervious surface raster. No `*Impervious*` or `*impervious*` `.tif` file exists anywhere in `COG/Flood/`.

This silently produces a missing appendix map in the report.

**Fix:** Add an impervious surface raster (e.g., `Alagianallur_Impervious.tif`) to `COG/Flood/`.

---

### MEDIUM

---

#### 6. Stage 7 Output is Named `8_appendices.json` (Numbering Off by One)

The script is `scripts/7_appendices.py` but it saves its JSON as `8_appendices.json`. This is a naming inconsistency across every run. Not a functional bug but causes confusion when navigating output folders.

---

#### 7. No `5_historical.json` Written

Stage 5 saves chart PNG files but does **not write a structured JSON summary** (like every other stage does). The context key `historical_charts` exists in memory but is never persisted. If word generation fails after this stage, no JSON backup of the historical stage exists.

---

#### 8. Latest Run (`Report_20260420_221956`) Is Incomplete

The most recent run is missing:
- `app_dem.png`, `app_twi.png`, `app_ndvi.png`, `app_ndbi.png`, `app_lst.png`, `app_lulc.png` — entire appendix stage failed
- `flood_if_ndbi_dem.png` — NDBI raster missing (see issue #4)
- `heat_if_lst_ndvi.png` — heat influencing factor map missing
- `8_appendices.json` — appendix stage failed
- `final_report.docx` — Word generation did not complete

This run produced a partial output with no final document.

---

#### 9. LLM Failures Are Silent

If the Anthropic Claude API call fails in Stage 7, the pipeline falls back to static placeholder text without any visible warning in the console or in the final Word document. Users won't know which sections are LLM-generated vs. hardcoded fallback.

---

#### 10. Azure Upload Failures Are Swallowed

If Azure upload fails (network issue, expired key, wrong container name), the pipeline logs a warning but continues. The user may believe the report was uploaded when it was not.

---

#### 11. No Input Validation on User Prompts

In Stage 0, user-typed strings (area name, city, client, etc.) are directly interpolated into report paragraphs and Word document content with no sanitization. A careless entry (e.g., quotes, unicode symbols) could corrupt document formatting or cause unexpected rendering in the Word output.

---

### LOW

---

#### 12. `GEMINI_API_KEY` and `OPENAI_API_KEY` Are Present but Unused

These are stored in `.env` and loaded by `config.py` but no current pipeline stage uses them. Storing unused credentials increases the blast radius if `.env` is exposed.

---

#### 13. No Retry Logic for contextily Tile Downloads

Stage 3 and Stage 6 fetch satellite basemap tiles from OpenStreetMap via `contextily`. If the network is slow or tile server is rate-limiting, the map renders with a blank/partial basemap, silently. There is no retry or fallback.

---

---

## Required Files to Run the Pipeline

To run for **Alagiyanallur** (the current input GeoJSONs), you need:

### Input GeoJSONs (present ✓)
```
Input_Files/Alagiyanallur_final_house_level_4326_flood.geojson  ✓
Input_Files/Alagiyanallur_final_house_level_4326_heat.geojson   ✓
```

### COG Rasters — Flood (partially present ⚠️)
```
COG/Flood/Alagianallur_dem.tif         ✓
COG/Flood/Alagianallur_TWI.tif         ✓
COG/Flood/Alagianallur_NDVI.tif        ✓
COG/Flood/Alagianallur_LULC.tif        ✓
COG/Flood/Alagianallur_NDBI.tif        ✗  MISSING — needed for flood influencing factors
COG/Flood/Alagianallur_Impervious.tif  ✗  MISSING — needed for appendix map
COG/Flood/HYSOG_Alagianallur.tif       ✓
COG/Flood/Slope_Alagianallur.tif       ✓
COG/Flood/Geomorphology_Alagianallur.geojson ✓
```

### COG Rasters — Heat (present ✓)
```
COG/Heat/Alagianallur_LST.tif          ✓
COG/Heat/Alagianallur_NDVI.tif         ✓
COG/Heat/Alagianallur_NDBI.tif         ✓
COG/Heat/Alagianallur_LULC.tif         ✓
```

### COG Vectors — Infrastructure (WRONG DATA ⚠️)
```
COG/roads/shell_AOI.geojson     ⚠️  Shell Norco data — replace with Alagiyanallur roads
COG/waterline/shell_AOI.geojson ⚠️  Shell Norco data — replace with Alagiyanallur waterlines
```

### Historical Plots (WRONG DATA ⚠️)
```
Historical_Plots/FLOOD/Precipitation_weekly/     ⚠️  Contains Shell_30.0068_-90.3812_* files
Historical_Plots/FLOOD/maximum_rainfall_weekly/  ⚠️  Contains Shell_30.0068_-90.3812_* files
Historical_Plots/FLOOD/rainfall_days_above_threshold/ ⚠️  Contains Shell data
Historical_Plots/FLOOD/output_geojson_runoff/    ⚠️  Contains Shell_RUNOFF.geojson
Historical_Plots/HEAT/Heat wave days/            ⚠️  Contains Shell_30.0068_-90.3812.json
Historical_Plots/HEAT/hi/                        ⚠️  Contains Shell data
Historical_Plots/HEAT/lst/                       ⚠️  Contains Shell_LST.json
Historical_Plots/HEAT/max temp weekly/           ⚠️  Contains Shell data
```
All historical JSON files need to be replaced with Alagiyanallur-specific data.

### Environment Variables (present but unprotected ⚠️)
```
.env   ⚠️  Present with live keys — no .gitignore exists
```

---

## Summary Table

| Issue | Severity | Status |
|-------|----------|--------|
| No .gitignore (live secrets exposed) | Critical | Not fixed |
| Roads/waterline data is Shell Norco, not Alagiyanallur | Critical | Not fixed |
| Historical climate data is Shell Norco, not Alagiyanallur | Critical | Not fixed |
| `Alagianallur_NDBI.tif` missing from `COG/Flood/` | Critical | Not fixed |
| Impervious raster missing from `COG/Flood/` | Critical | Not fixed |
| Latest run incomplete — no final_report.docx | High | Not fixed |
| Appendix output named `8_` instead of `7_` | Low | Cosmetic |
| No `5_historical.json` persisted | Low | Minor |
| Silent LLM fallback with no user warning | Medium | Not fixed |
| Silent Azure upload failure | Medium | Not fixed |
| No user input sanitization | Medium | Not fixed |
| Unused API keys (Gemini, OpenAI) in .env | Low | Not fixed |
| No retry on contextily tile downloads | Low | Not fixed |
