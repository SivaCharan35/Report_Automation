# HHMD Pipeline Flow + API Save-Back Plan
*Generated: 2026-05-21*

> Scope: this document covers the active pipeline driven by `app_poc_v2.py` and the `ara_*` modules in `scripts/`. It deliberately **excludes** the `0. 0. EDMP/` folder and the legacy `z. junk - old/` numbered scripts.

---

## 1. Big picture — what this thing does

1. **Fetch** the report template + input_config from the live Backend API (or fall back to `api_response.json`).
2. **Dispatch** each section returned by the API to the matching `ara_*` module.
3. Each `ara_*` module **resolves placeholders** (`{{HAZARD_TYPES}}`, `{{FLOOD_SCORE_1}}`, `{{FLOOD_RISK_MAP_IMAGE}}`, …) by running GeoJSON/raster math, calling Claude where needed, generating PNGs, uploading them to Azure Blob Storage, and writing the resolved value back into the section's `jsonContent` tree.
4. Each module **persists** its resolved `jsonContent` to `Report_Data/Report_<timestamp>/<module_stem>.json` and (if `UPLOAD_AZURE=True`) to Azure under the same key.
5. **Today the loop stops there.** The resolved JSON sits on disk and in blob storage. **It is not pushed back to the BE** so the live report rendered in the platform still contains the un-resolved `{{...}}` placeholders. That's the gap this document is about — see §5.

---

## 2. Entry point — `app_poc_v2.py`

```
app_poc_v2.py
   │
   ├── Step 1  _load_pipeline_config()
   │     GET https://kub-dev.resilience360.ai/api/reports/templates-meta
   │           ?report_type=Resilience assessment&area=<area>
   │     ─ falls back to ./api_response.json on any failure / when --offline
   │     ─ normalises   input_config["risk_for"]: "Flood only" → "Flood"
   │     ─ injects      section["module"] = _MODULE_MAP[section["name"]]
   │
   ├── Build the base context
   │     report_id      = YYYYMMDD_HHMMSS
   │     output_dir     = Report_Data/Report_<report_id>
   │     assets_dir     = output_dir/assets
   │     azure_base_path= reports/Report_<report_id>
   │     + every input_config key flattened into the context dict
   │
   └── Step 2  for section in sections:
                  if section["module"] is None       → skip
                  if section["jsonContent"] is empty → skip
                  else                               → mod.run(context)
```

`_MODULE_MAP` (lines 53–66) is the name → module routing table. **Today's mapping:**

| API `section.name`           | `ara_*` module             |
|------------------------------|----------------------------|
| `Contents and Introduction`  | `ara_intro`                |
| `Overview`                   | `ara_overview`             |
| `Exposure`                   | `ara_exposure`             |
| `Analytics`                  | `ara_analytics`            |
| `Header`                     | `ara_analytics`            |
| `SSP Scenario Analysis`      | `ara_ssp_scenario`         |
| `Impact Scale`               | `ara_impact_scale`         |
| `Historical Trends`          | `ara_historical`           |
| `Influencing Factors`        | `ara_influencing_factors`  |
| `Risk Insights`              | `ara_risk_insights`        |
| `Parametric`                 | `ara_parametric`           |
| `Conclusion & Appendix`      | `ara_conclusion_appendix`  |

**CLI overrides:**
```bash
python app_poc_v2.py                    # live API, default area = "Shell Norco Refinery"
python app_poc_v2.py --area "Alagiyanallur"
python app_poc_v2.py --api-url <url>    # explicit full URL
python app_poc_v2.py --offline          # skip API, use api_response.json
python app_poc_v2.py --local-only       # skip Azure upload
```

---

## 3. The shared "context dict"

The single `context` dict is mutated as it walks down the section list. Every `ara_*.run(ctx)` reads keys it needs and writes its outputs back in. Important keys, in roughly the order they appear:

| Key                          | Set by              | Used by                                                |
|------------------------------|---------------------|--------------------------------------------------------|
| `report_id`                  | `app_poc_v2`        | path helpers                                           |
| `output_dir` / `assets_dir`  | `app_poc_v2`        | every module's `save_section_output()` / `save_asset()` |
| `azure_base_path`            | `app_poc_v2`        | every module's `save_asset()`                          |
| `input_config` + flat keys (`area`, `city`, `state`, `country`, `risk_for`, `heatwave_threshold`, …) | `app_poc_v2` | every module (placeholder resolution) |
| `section_name`, `section_content` | `_dispatch()` | the current module only — overwritten each iteration   |
| `flood_geojson_path`, `heat_geojson_path` | `ara_overview` (auto-detected from `Input_Files/`) | `ara_exposure`, `ara_ssp_scenario`, `ara_risk_insights` |
| `total_buildings`, `aoi_area` | `ara_overview`     | `ara_risk_insights`                                    |
| `flood_risk_counts`, `heat_risk_counts` | `ara_exposure` | `ara_influencing_factors`, `ara_parametric`, `ara_risk_insights` |
| `flood_risk_map_path`, `heat_risk_map_path` | `ara_exposure` | downstream image-URL placeholders                |
| `FLOOD_SCORE_1..5`, `HEAT_SCORE_1..5` (flat) | `ara_exposure` | `ara_influencing_factors` (so it can read counts without re-loading the GeoJSON) |
| `appendix_layers_json`, `appendix_layer_urls`, `appendix_stats`, `appendix_layer_impacts`, `appendix_map_paths` | `ara_risk_insights` (Phase A) | `ara_parametric` |
| `risk_findings` `{"flood": [..5..], "heat": [..5..]}` | `ara_risk_insights` (Phase B) | downstream `FLOOD_DETECTIVE_*` / `HEAT_DETECTIVE_*` placeholders |
| `resolved_content`           | every module        | the *current* module's `save_section_output()` only — overwritten next iteration |

**Ordering matters.** `ara_exposure` must run before `ara_influencing_factors`. `ara_risk_insights` must run before `ara_parametric` (the latter is a thin reader of the former's context). Today the order is dictated by whatever order the BE returns sections in.

---

## 4. Per-module breakdown

Every `ara_*.run(ctx)` follows the same 4-step recipe (defined as Steps 3–6 in the module docstrings):

- **Step 3** — find every `{{PLACEHOLDER}}` token in `section_content`
- **Step 4** — compute / fetch / generate every value (this is where the heavy lifting lives)
- **Step 5** — substitute placeholders inside the JSON-serialised content
- **Step 6** — store `resolved_content` in context and call `save_section_output(...)` → writes `Report_Data/Report_<id>/<module>.json` + uploads to Azure

| # | Module                       | Heavy work in Step 4                                                                                                             | Placeholders resolved                                                                                                                                                                                                                                                                                                                |
|---|------------------------------|----------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 1 | `ara_intro`                  | Pure string substitution.                                                                                                        | `AREA_COVERED_FULL`, `HAZARD_TYPES`                                                                                                                                                                                                                                                                                                  |
| 2 | `ara_overview`               | Auto-detect flood/heat GeoJSONs in `Input_Files/`; count features; bounding-box area in sq mi.                                   | `TOTAL_BUILDINGS`, `TOTAL_AREA` (+ inherits intro placeholders)                                                                                                                                                                                                                                                                       |
| 3 | `ara_exposure`               | `compute_risk_counts(...)` → 5-class counts per hazard. Generate `flood_risk_map.png` / `heat_risk_map.png` (matplotlib + contextily Esri basemap). | `FLOOD_RISK_MAP_IMAGE`, `HEAT_RISK_MAP_IMAGE`, `FLOOD_SCORE_1..5`, `HEAT_SCORE_1..5`, `FLOOD_SUMMARY`, `HEAT_SUMMARY`                                                                                                                                                                                                |
| 4 | `ara_analytics`              | None — section is just a heading.                                                                                                | (no placeholders)                                                                                                                                                                                                                                                                                                                    |
| 5 | `ara_ssp_scenario`           | `build_ssp_counts(...)` → 9 SSP columns × 5 risk levels per hazard. **Direct AST mutation** into table nodes at indexes 5 (flood) and 7 (heat). | `FLOOD_SSP_SUMMARY`, `HEAT_SSP_SUMMARY` (+ table cells)                                                                                                                                                                                                                                                                              |
| 6 | `ara_impact_scale`           | None — static 5×3 impact text. AST injection into the section's `table` node (looked up by `type`, not index).                   | (no placeholders — table cells injected)                                                                                                                                                                                                                                                                                              |
| 7 | `ara_historical`             | Walks `Historical_Plots/FLOOD/*` and `Historical_Plots/HEAT/*` by keyword match. Generates 8 annual time-series PNGs (20-yr lookback) via the bundled `plot_*` fns. WMO vs IMD heatwave logic switched on `country`. | `FLOOD_HIST_GRAPH_1..4`, `HEAT_HIST_GRAPH_1..4`                                                                                                                                                                                                                                                          |
| 8 | `ara_influencing_factors`    | Reads raster pairs from `COG/Flood/` & `COG/Heat/`: NDVI×TWI, NDBI×DEM, LST×NDVI. Jenks-2 → binary classification → 4-colour RGBA risk map. Reprojects each output to EPSG:3857 + Esri basemap. AST injection into colour-class tables at node indexes 10, 17, 27. | `FLOOD_NDVI_TWI_MAP`, `FLOOD_NDBI_DEM_MAP`, `HEAT_LST_NDVI_MAP`, `FLOOD_NDVI_TWI_SUMMARY`, `FLOOD_NDBI_DEM_SUMMARY`, `HEAT_LST_NDVI_SUMMARY`                                                                                                                              |
| 9 | `ara_risk_insights`          | **Phase A** = the old `7_appendices` logic: 7 raster layers + 2 vector layers → PNGs + susceptibility distributions + Claude-written impact paragraphs. Persists everything (including `appendix_layers_json` etc.) into context. Also writes a legacy `7_appendices.json`. **Phase B** = the old `9_risk_findings` logic: 5 Claude-written bullets per hazard from the Phase A data. | `FLOOD_DETECTIVE_1..3`, `HEAT_DETECTIVE_1..3`                                                                                                                            |
| 10 | `ara_parametric`            | Pure reader — pulls map URLs, susceptibility %, and impact texts from `appendix_*` keys put there by `ara_risk_insights`. No raster work.                | `APPX_*_MAP` (9), `APPX_*_IMPACT_TEXT` (8), `APPX_*_LOW/MOD/HIGH` (15 susc + 9 prone), … (~40 placeholders total)                                                                                                                                                       |
| 11 | `ara_conclusion_appendix`   | Single string substitution.                                                                                                       | `HAZARD_TYPES`                                                                                                                                                                                                                                                                                                                       |

### Two ways content gets into the section

- **`{{PLACEHOLDER}}` substitution** — the default. The module does `json.dumps(node)`, regex-replaces tokens, then re-parses.
- **Direct AST mutation** — used when the section has empty cells without `{{...}}` markers. Used by `ara_impact_scale`, `ara_ssp_scenario`, and `ara_influencing_factors`. These modules walk the parsed JSON tree, find the relevant `table` node (sometimes by fixed index, sometimes by node type), and write text into specific cells.

### Where outputs go

- **`Report_Data/Report_<id>/<module>.json`** — the fully resolved `jsonContent` of one section. This is the shape you would later POST/PUT back to the BE.
- **`Report_Data/Report_<id>/assets/*.png` and `*.tif`** — every map / chart generated during Step 4. Same files get uploaded to Azure when `UPLOAD_AZURE=True`, and the *Azure URL* (not the local path) is the value that ends up substituted into the placeholder, so the resolved JSON points at the cloud asset.

### `core/` shared utilities used by all modules

- `core/classifiers.py` — risk-score → label (`Very Low`…`Very High`); separate functions for current vs RCP vs SSP and per hazard.
- `core/geojson_utils.py` — `load_geojson`, `compute_risk_counts`, `compute_aoi_sqmiles`, `build_ssp_counts`.
- `core/chart_utils.py` — `FLOOD_COLOR`, `HEAT_COLOR`, `score_to_rgba`, `set_chart_style`.
- `core/storage.py` — `save_asset()` (local + Azure) and `save_section_output()` (parses resolved JSON, promotes `attrs.src` → top-level `src` on image nodes, then writes `<module>.json`).
- `azure_utils.py` — thin wrappers around `azure-storage-blob`; only imported when `UPLOAD_AZURE=True`.

### `config.py`

| Flag             | Default | Meaning                                                  |
|------------------|---------|----------------------------------------------------------|
| `SAVE_LOCAL`     | `True`  | Write outputs to `Report_Data/<run_id>/`.                |
| `UPLOAD_AZURE`   | `True`  | Upload all outputs to Azure Blob.                        |
| `GENERATE_WORD`  | `True`  | **Unused in `app_poc_v2.py` flow** — leftover from the older `app.py` that called `word_report.py`. The new pipeline produces section JSONs only, no `.docx`. |

`SSP_HORIZONS` defines the 3 horizons × 3 SSP scenarios used by `ara_ssp_scenario` and the SSP-finding logic in `ara_risk_insights`.

### Inputs the pipeline reads at runtime

- `Input_Files/*_flood.geojson` and `*_heat.geojson` — per-site, house-level features with `risk_score` + `SSP_2040/2060/2100.SSP_Score_2.6/4.5/8.5` properties.
- `COG/Flood/*.tif` — DEM, TWI, NDVI/NDBI (today), LULC, slope, geomorphology vector.
- `COG/Heat/*.tif` — LST, NDVI, NDBI, LULC.
- `COG/roads/*.geojson`, `COG/waterline/*.geojson` — appendix vector layers.
- `Historical_Plots/FLOOD/<subdir>/*.json` — precipitation weekly, rainfall days above threshold, max rainfall weekly, runoff geojson.
- `Historical_Plots/HEAT/<subdir>/*.json` — heat wave days, heat index, LST, max temp weekly.

A note on the current input state in this repo: `Input_Files/` and `Historical_Plots/` have been refreshed to **Alagiyanallur** (`shell_final_house_level_4326_Flood/Heat.geojson` + `Alagiyanallur_9.6207_78.0118_*.json`), but `COG/Flood/` and `COG/Heat/` still hold **Shell Norco** rasters (`shell_dem.tif`, `shell_TWI.tif`, `20260128_shell_lst.tif`, …), and `COG/roads/` / `COG/waterline/` still hold `shell_AOI.geojson`. So a live run today will *match* TIFs to filename keywords like `dem`, `twi`, `lst`, `ndvi` regardless of site — i.e. you'll get Shell Norco rasters layered onto Alagiyanallur points if you don't swap the COGs.

### What a completed run looks like on disk

```
Report_Data/Report_20260427_204225/
   ara_intro.json
   ara_overview.json
   ara_analytics.json
   ara_exposure.json
   ara_ssp_scenario.json
   ara_impact_scale.json
   ara_historical.json
   ara_influencing_factors.json
   ara_risk_insights.json
   ara_parametric.json
   ara_conclusion_appendix.json
   7_appendices.json                  # legacy artifact written by ara_risk_insights
   assets/
      flood_risk_map.png  heat_risk_map.png
      fig_5_1a_rainfall_days.png … fig_5_4b_heat_index.png
      flood_if_ndvi_twi.png  flood_if_ndbi_dem.png  heat_if_lst_ndvi.png
      app_dem.png  app_twi.png  app_ndvi.png  app_ndbi.png  app_lst.png  app_lulc.png  app_impervious.png
      app_roads.png  app_waterline.png
```

Each `ara_*.json` file is **exactly** the shape the BE wants in a section's `jsonContent` field. That's the key insight for §5.

---

## 5. What's new in the API — `Export Collection.postman_collection 6.json` vs `api_response.json`

### TL;DR

The previous integration was a one-shot **read-only template fetch**. The new API is a full **stateful report-document system** with persisted per-section content, version history, exclude flags, image hosting, and publish actions. Our pipeline currently consumes only the equivalent of *endpoint 101* and never writes anything back.

### Endpoint inventory in the new collection

All endpoints live under `{{base_url}}` and use Bearer-token auth.

| # | Method | URL                                                                                          | Purpose                                                                                                  |
|---|--------|----------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------|
| 1 | POST   | `/api/reports/{report_id}/initialize-sections`                                               | Bootstraps a report — pulls section templates and inserts them into `user_report_sections`. Idempotent (409 on re-run). Body: `{"custom_sections_count": N}`. |
| 2 | GET    | `/api/reports/{report_id}/sections?with_content=true`                                        | Hierarchical list of all sections + their `jsonContent` + per-section metadata.                          |
| 3 | POST   | `/api/reports/{report_id}/sections/{section_id}/exclude`                                     | Toggle `is_excluded` on a single section.                                                                |
| 4 | PUT    | `/api/reports/{report_id}/alias`                                                             | Rename the report.                                                                                       |
| 5 | GET    | `/api/reports/{report_id}/sections/{section_id}`                                             | Fetch one section's current `jsonContent`.                                                               |
| 6 | **PUT**| **`/api/reports/{report_id}/sections/{section_id}`**                                         | **Save resolved content.** Body: `{"jsonContent": { ... }}`. Returns `{IsSuccess, doc_id, version}`. Bumps the section version. *This is the endpoint we will use to push pipeline output back.* |
| 7 | PUT    | `/api/reports/{report_id}/sections/{section_id}/rename`                                      | Change a (sub-)section's title.                                                                          |
| 8 | GET    | `/api/reports/{report_id}/sections/{section_id}/history`                                     | Full revision history of a section.                                                                      |
| 9 | POST   | `/save_and_generate_actions`                                                                 | Save the **Resilience Actions** list (the matrix-of-interventions content the BE renders downstream). Body has `report_id` + `actions[]` array with `climate_type`, `category`, `timeline`, `responsible_authority_Sector`, `relevance`, etc. |
| 10 | GET   | `/api/reports/{report_id}/preview`                                                           | Renders the assembled report `jsonContent`.                                                              |
| 11 | POST  | `/api/v3/publish_report_auto`                                                                | Publishes the report (multipart form: `file`, `doc_id`).                                                 |
| 100 | POST | `/api/v1/report_docs/{report_id}/images`                                                     | Image upload (multipart form: `image`). Returns a blob URL we can stitch into `jsonContent`.             |
| 101 | GET  | `/api/reports/templates-meta?report_type=...&area=...&flag_update=false`                     | **The endpoint we currently call.** Has a new `flag_update` query param — set to `false` to read without bumping the template version.                                                              |
| 102 | POST | `/api/reports/master-sections`                                                               | Save master content (admin-side template editing — not part of our pipeline).                            |

### What `api_response.json` (= endpoint 101) looks like vs what endpoints 2/5 return

- `api_response.json` is a **template** payload: `{input_config, sections: [{name, jsonContent, module}, …]}`. Sections are a **flat list** keyed by `name` ("Contents and Introduction", "Overview", "SSP Scenario Analysis", …). No `report_id`, no `section_id`, no version metadata. `jsonContent` still contains `{{PLACEHOLDER}}` tokens.
- Endpoint 2 returns a **report instance** payload: `{report_id, sections: [{section_header, section_title, sequence_number, subsections: [...]}, ...]}`. Each `section_header` has `doc_id` (= `section_id`), `section_title`, optional `sub_section`, `sequence_number`, `sub_section_number`, `is_excluded`, `is_latest`, `version`, `updated_at`, `updated_by`, plus `jsonContent` which (after we save) will hold the **resolved** content.

### The section hierarchy in the new API

From the saved example response for `5d5fac08-...` the report has 8 top-level sections; two of them have sub-sections:

```
seq 1: Content and Introduction               doc=8daeddd9-…
seq 2: Executive Summary                      doc=0504843d-…
seq 3: Hazard Risk Assessment (Header)        doc=9836324d-…
       └── 3.1  Overview                      doc=d5ebcdf3-…
       └── 3.2  SSP Scenario analysis         doc=0b898792-…
seq 4: Vulnerability Assessment               doc=fac93c64-…
seq 5: Impact Scale                           doc=c481e5ac-…
seq 6: Historical Trends                      doc=aaf4d4ef-…
seq 7: Conclusion and Recommendations         doc=61d2705d-…
seq 8: Appendices (Header)                    doc=c3f79701-…
       └── 8.1  Detailed Data Layers          doc=388f7125-…
       └── 8.2  Shared Socioeconomic Pathways doc=9e4a16eb-…
```

This is a **rename + regroup** compared to the flat names in `api_response.json`. Provisional re-mapping to our `ara_*` modules:

| New `section_title` / `sub_section`              | doc_id seen in example | Likely module                                       |
|--------------------------------------------------|------------------------|-----------------------------------------------------|
| `Content and Introduction`                       | `8daeddd9-…`           | `ara_intro`                                         |
| `Executive Summary`                              | `0504843d-…`           | new — partially covered by `ara_intro` today        |
| `Hazard Risk Assessment` / Header                | `9836324d-…`           | `ara_analytics`                                     |
| `Hazard Risk Assessment` / `Overview`            | `d5ebcdf3-…`           | `ara_overview` + `ara_exposure`                     |
| `Hazard Risk Assessment` / `SSP Scenario analysis` | `0b898792-…`         | `ara_ssp_scenario`                                  |
| `Vulnerability Assessment`                       | `fac93c64-…`           | `ara_influencing_factors` + `ara_risk_insights`     |
| `Impact Scale`                                   | `c481e5ac-…`           | `ara_impact_scale`                                  |
| `Historical Trends`                              | `aaf4d4ef-…`           | `ara_historical`                                    |
| `Conclusion and Recommendations`                 | `61d2705d-…`           | `ara_conclusion_appendix` (Conclusion half)         |
| `Appendices` / Header                            | `c3f79701-…`           | (header — no module needed)                         |
| `Appendices` / `Detailed Data Layers`            | `388f7125-…`           | `ara_parametric`                                    |
| `Appendices` / `Shared Socioeconomic Pathways`   | `9e4a16eb-…`           | `ara_conclusion_appendix` (Appendix B half)         |

> The `_MODULE_MAP` in `app_poc_v2.py` (lines 53–66) was written against the **old** flat section names. It needs an update to match the new `section_title` / `sub_section` pairs. Until then, sections with new titles will fall through with `module = None` and get skipped — i.e. the pipeline will silently do nothing for them.

### Other notable additions

- **Version tracking** — every PUT to endpoint 6 returns an incremented `version` and (per endpoint 8) we can fetch the full edit history. Good for diffing pipeline runs.
- **Exclude flag** — `is_excluded` lets the BE/user hide a section without deleting it. We should respect this on read: don't bother running a module if its section is excluded.
- **Image upload endpoint** — `POST /api/v1/report_docs/{report_id}/images`. Today our pipeline uploads PNGs straight to Azure and embeds the blob URL into `jsonContent`. With endpoint 100 the BE wants to mediate that step (so it can keep a record of which images belong to which report). Worth considering switching to it later; not blocking.
- **Resilience Actions (endpoint 9)** — a brand-new domain. The previous pipeline never touched the per-action mitigation matrix. We don't generate this data today; right now this endpoint is consumed by humans / a different service.
- **`flag_update=false`** on the templates-meta call — the new param means "give me the latest template, but don't mark it as 'consumed' / bump any update counters". Safer for our offline runs. Worth adding to `_build_api_url()` so we stay on the read-only side.

---

## 6. Saving section results back to the BE — proposed approach

The user's framing is exactly right:

> "First we run the pipeline, get the results of all the sections, second we will save the results to the live API, placeholders/sections."

So the work splits cleanly into **two phases**:

### Phase 1 — already exists: generate the resolved JSONs

When the pipeline finishes, `Report_Data/Report_<id>/` already holds one file per module:

```
Report_Data/Report_<id>/ara_intro.json
Report_Data/Report_<id>/ara_overview.json
…
Report_Data/Report_<id>/ara_conclusion_appendix.json
```

Each file is the exact `jsonContent` object that endpoint 6 expects (verified by spot-checking `ara_intro.json` against the Section Edit Content PUT body shape — same top-level `content[]` array of paragraph/table nodes). No transformation needed.

### Phase 2 — the new piece: push them back

A new module — call it `scripts/ara_publish.py` or `core/api_client.py` — that runs **after** the section loop and does this for each completed module:

1. **Resolve the target `section_id`.** We don't know `doc_id`s when we write `ara_<name>.json`. Two options:
   - (preferred) On every pipeline run, also call endpoint 2 (`GET /sections`) and build a `{(section_title, sub_section): doc_id}` lookup. Cache it in the run's `output_dir` as `_sections_index.json` for traceability.
   - Or pass `report_id` + a manual `module → doc_id` table via CLI / env var when iterating. Brittle but fine for a first pass.
2. **Skip excluded sections.** If the section's `is_excluded == true` in the lookup, log + skip.
3. **PUT** `/api/reports/{report_id}/sections/{doc_id}` with body `{"jsonContent": <contents of ara_<name>.json>}`. Reuse the same Bearer token / SSL-off pattern already in `_fetch_api()`.
4. **Log the returned `version`** to `Report_Data/Report_<id>/_publish_log.json` so we can audit which sections were pushed and at what version. On failure (non-200), log the response body and continue to the next section — don't crash the whole publish step on one bad section.
5. **(Optional)** call endpoint 10 (`GET /preview`) at the end to verify the BE renders the assembled doc with the resolved content. Save the response to `_preview.json` for offline inspection.

### Where this hooks into the existing code

Smallest possible diff:

- Add a `--publish` / `--no-publish` CLI flag to `app_poc_v2.py` (default to *not* publishing — pipeline today is dry-run by default).
- After the `for section in sections:` loop in `run_pipeline()`, if `--publish` is set, call the new publisher with `report_id` + `output_dir`.
- The publisher reads every `<output_dir>/ara_*.json`, loads it, wraps it in `{"jsonContent": ...}`, and PUTs.
- The `report_id` itself has to come from somewhere — likely a new CLI arg `--report-id <uuid>` (the BE/user creates the report shell and gives us its ID).

A larger-but-cleaner alternative: stop calling endpoint 101 entirely. Instead, given a `report_id`, call **endpoints 1 (initialize) → 2 (list sections with content) → dispatch each section to its module (using `doc_id` as the routing key + `section_title`/`sub_section` for module selection) → 6 (PUT resolved content per section)**. That puts the pipeline fully on the new report-instance model and makes `api_response.json` / endpoint 101 a legacy backup. It's the right end-state but a bigger lift than the publish-only approach above.

### Things that need a decision before coding

1. **`_MODULE_MAP` reshape.** The new section titles don't match today's mapping. Need to decide which `ara_*` module owns which new section/sub-section (see provisional table in §5). The `Overview` sub-section in particular maps to *two* modules today (`ara_overview` + `ara_exposure`), which means either merging them or pushing both modules' resolved content into the same `doc_id` (last writer wins).
2. **One PUT per section vs one merged PUT.** If multiple modules contribute to the same `doc_id` (`Overview` case above), the *second* module's PUT must reuse the first module's resolved output as its input, not the original API template. Either compose them in memory before publishing, or have the second module re-fetch via endpoint 5 before resolving.
3. **`Executive Summary`** — new section in the new layout, no module today. Decide whether to autogenerate it (Claude-from-context) or leave it untouched (don't PUT) so the platform's default text remains.
4. **Auth.** Postman uses `{{jwt_token}}` — we need to know whether the same long-lived token works for headless pipeline runs, or whether we need a service account / refresh flow.
5. **Idempotency.** Endpoint 6 bumps `version` on every PUT. Re-running the pipeline against the same `report_id` will keep growing the history. That's probably fine, but worth confirming with BE that there's no rate-limit / lock on rapid successive saves.

### Suggested order of work

1. Quick rename pass on `_MODULE_MAP` to match the new `section_title` / `sub_section` strings, run against endpoint 2 to see who is unmapped and who is duplicated.
2. Write `core/api_client.py` with `list_sections(report_id)` and `put_section(report_id, doc_id, json_content)` — thin wrappers around the same `urllib` pattern already in `app_poc_v2._fetch_api()`. Authenticate with a `RES360_API_TOKEN` env var.
3. Write `scripts/ara_publish.py` (or a small function in `app_poc_v2.py`) that loops `output_dir/ara_*.json` and calls `put_section()`. Wire it behind a `--publish --report-id <uuid>` CLI combo. Write `_publish_log.json` to `output_dir`.
4. Resolve the `Overview` / two-module collision (decision needed — see point 2 above).
5. Switch the read path from endpoint 101 → endpoint 2, so pipeline-input and pipeline-output both live on the report-instance model and the API is the single source of truth for which sections exist. This is the final step that lets us delete `api_response.json`.

---

## 7. Quick reference — files you'll touch when implementing this

| File / area                       | Why                                                                                       |
|-----------------------------------|-------------------------------------------------------------------------------------------|
| `app_poc_v2.py`                   | Add `--publish` + `--report-id`; rename `_MODULE_MAP` keys to new section titles.         |
| `core/api_client.py` *(new)*      | Encapsulate all live-API calls — `list_sections`, `get_section`, `put_section`.           |
| `scripts/ara_publish.py` *(new)*  | Post-pipeline publisher — reads `output_dir/ara_*.json` and PUTs to endpoint 6.           |
| `core/storage.py`                 | Already does what we need on the write side; no change unless we want to also persist the publisher's response next to each `ara_*.json`. |
| `config.py`                       | Add `RES360_API_TOKEN` (from `.env`), `RES360_BASE_URL`, optional `RES360_PUBLISH=False` default flag. |
| `.env`                            | Add the JWT / service-account token.                                                      |
| `Report_Data/Report_<id>/_sections_index.json` *(new artefact)* | Lookup `section_title`/`sub_section` → `doc_id` captured at the start of each run.        |
| `Report_Data/Report_<id>/_publish_log.json` *(new artefact)*    | Per-section PUT result + version + any error responses.                                   |

Nothing else in the pipeline needs to change — the heavy `ara_*` modules already produce the right shape.

---

## 8. Deep-dive — the section-model change, and what to ask the BE to keep

This section unpacks the §5 bullet *"The section model has changed too"* into something we can hand to the BE team. Our position is: **do not change the scripts**. Instead, ask the BE to keep the existing shape on the read side (or expose a compatibility view), so the pipeline can keep ingesting the same payload it does today.

### 8.1 What our code reads, exactly

The dispatcher contract is two `dict.get()` calls in [app_poc_v2.py:139-142](app_poc_v2.py#L139-L142) and [app_poc_v2.py:166-168](app_poc_v2.py#L166-L168):

```python
# Loading
for s in raw_sections:
    section["module"] = _MODULE_MAP.get(s.get("name", ""))

# Dispatching
section_name = section.get("name", module_name)
json_content = section.get("jsonContent") or ""
```

So every section the pipeline accepts today must satisfy three rules:

1. The list of sections is a **flat list** (no nesting, no `subsections[]`).
2. Each section is a dict with a top-level **`"name"`** string field, and that string is one of the 12 keys in `_MODULE_MAP` (see §2).
3. Each section has a top-level **`"jsonContent"`** field whose structure is identical to what we already produce in `Report_Data/Report_<id>/ara_*.json`.

No other field is consumed by the dispatcher. The modules themselves never read `name` again — they only read `jsonContent` (renamed to `section_content` in context).

### 8.2 What the new API delivers in endpoint 2 (`GET /sections?with_content=true`)

Confirmed from the saved example in `Export Collection.postman_collection 6.json`:

```json
{
  "IsSuccess": true,
  "report_id": "5d5fac08-...",
  "status": "success",
  "sections": [
    {
      "section_header": {                                     ← wrapper that didn't exist before
        "area": "Alagiyanallur",
        "doc_id": "8daeddd9-...",                             ← new — section identifier (used for PUT)
        "section_id": "8daeddd9-...",
        "section_title": "Content and Introduction",          ← renamed from "name"
        "sub_section": "",                                    ← new — disambiguates sub-sections
        "sub_section_number": 0,
        "sequence_number": 1,
        "is_excluded": false,                                 ← new — sections can be hidden
        "is_editable": false,
        "is_latest": true,
        "version": 1,
        "updated_at": "...",
        "updated_by": "...",
        "report_id": "5d5fac08-...",
        "jsonContent": { ... }                                ← moved one level deeper
      },
      "section_title": "Content and Introduction",
      "sequence_number": 1,
      "subsections": []                                       ← new — child sections live here
    },
    {
      "section_header": { ..., "section_title": "Hazard Risk Assessment", "sub_section": "Header" },
      "section_title": "Hazard Risk Assessment",
      "subsections": [
        {                                                     ← subsections are flat dicts, NOT wrapped in section_header
          "doc_id": "d5ebcdf3-...",
          "section_title": "Hazard Risk Assessment",
          "sub_section": "Overview",
          "sub_section_number": 1,
          "sequence_number": 3,
          "is_excluded": false,
          ...
        },
        ...
      ]
    }
  ]
}
```

Differences vs the old `templates-meta` payload (`api_response.json`) that matter to us:

| Concern                  | Old payload                           | New payload                                                                       |
|--------------------------|---------------------------------------|-----------------------------------------------------------------------------------|
| Section list shape       | Flat                                  | 2-level: top-level + `subsections[]`                                              |
| Section name field       | `section.name`                        | `section.section_header.section_title` (top-level) or `subsection.section_title` + `subsection.sub_section` (sub-level) |
| Where `jsonContent` sits | `section.jsonContent`                 | `section.section_header.jsonContent` (top-level) / `subsection.jsonContent` (sub-level) |
| Renames in name strings  | `Contents and Introduction`           | `Content and Introduction` (no `s`)                                               |
|                          | `SSP Scenario Analysis`               | `SSP Scenario analysis` (lowercase `a`)                                           |
|                          | `Overview` (flat)                     | `Hazard Risk Assessment` + `sub_section = Overview`                               |
|                          | `Exposure`                            | (gone — merged into `Overview` sub-section)                                       |
|                          | `Analytics` / `Header`                | `Hazard Risk Assessment` + `sub_section = Header`                                 |
|                          | `Influencing Factors`, `Risk Insights`| (gone — merged into `Vulnerability Assessment`)                                   |
|                          | `Parametric`                          | `Appendices` + `sub_section = Detailed Data Layers`                               |
|                          | `Conclusion & Appendix`               | split into `Conclusion and Recommendations` + `Appendices` + `sub_section = Shared Socioeconomic Pathways` |
|                          | (none)                                | new: `Executive Summary`                                                          |
| Excluded sections        | Not represented                       | `is_excluded` flag — we would need to skip them                                   |
| Versioning               | None                                  | `version`, `is_latest`, `updated_at`, `updated_by` — informational, not blocking  |

### 8.3 Do the `ara_*` modules themselves need changing?

**No — almost all are insulated.** Modules receive `section_content` via the context dict (it's set by `_dispatch()` *after* extracting `jsonContent`). They never look at `name` or `doc_id`. As long as the orchestrator hands each module the correct `jsonContent` object, the module is happy.

**Two modules are sensitive to the *inner* structure of `jsonContent`** (not the wrapper around it):

- [scripts/ara_ssp_scenario.py:62-63](scripts/ara_ssp_scenario.py#L62-L63) — hardcodes `_FLOOD_TABLE_IDX = 5` and `_HEAT_TABLE_IDX = 7` (the SSP table positions inside the section's `content[]` array).
- [scripts/ara_influencing_factors.py:382-386](scripts/ara_influencing_factors.py#L382-L386) — hardcodes node indexes `10`, `17`, `27` for the three colour-class tables.

These will break iff the BE reshuffles the **paragraphs/tables inside a section's `jsonContent`**. That's a different concern from the wrapper/naming change — see ask #4 in §8.5.

### 8.4 If we *did* adopt the new shape — what would have to change

For the record (so we know exactly what we're avoiding):

1. **`app_poc_v2._load_pipeline_config()`** — replace the flat loop with a flattener that walks `payload["sections"]` AND each section's `subsections[]`, and unwraps `section_header` for top-level entries. Roughly:
   ```python
   def _flatten(raw):
       out = []
       for s in raw:
           hdr = s.get("section_header", {})
           out.append({
               "doc_id":      hdr.get("doc_id"),
               "name":        hdr.get("section_title", ""),
               "sub_section": hdr.get("sub_section", ""),
               "jsonContent": hdr.get("jsonContent"),
               "is_excluded": hdr.get("is_excluded", False),
           })
           for sub in s.get("subsections", []):
               out.append({
                   "doc_id":      sub.get("doc_id"),
                   "name":        sub.get("section_title", ""),
                   "sub_section": sub.get("sub_section", ""),
                   "jsonContent": sub.get("jsonContent"),
                   "is_excluded": sub.get("is_excluded", False),
               })
       return out
   ```
2. **`_MODULE_MAP`** — rebuild keys as `(section_title, sub_section)` tuples and re-route to handle the renames and the new merged sections. E.g.:
   ```python
   _MODULE_MAP = {
       ("Content and Introduction", ""):                          "ara_intro",
       ("Executive Summary", ""):                                 None,   # decide later
       ("Hazard Risk Assessment", "Header"):                      "ara_analytics",
       ("Hazard Risk Assessment", "Overview"):                    "ara_overview_exposure_combined",
       ("Hazard Risk Assessment", "SSP Scenario analysis"):       "ara_ssp_scenario",
       ("Vulnerability Assessment", ""):                          "ara_influencing_factors_plus_insights",
       ("Impact Scale", ""):                                      "ara_impact_scale",
       ("Historical Trends", ""):                                 "ara_historical",
       ("Conclusion and Recommendations", ""):                    "ara_conclusion",
       ("Appendices", "Header"):                                  None,
       ("Appendices", "Detailed Data Layers"):                    "ara_parametric",
       ("Appendices", "Shared Socioeconomic Pathways"):           "ara_ssp_appendix",
   }
   ```
3. **Module merges** — because `Overview` + `Exposure` collapsed to a single new sub-section, we'd have to either chain `ara_overview.run()` then `ara_exposure.run()` against the *same* `jsonContent` (each module's output becomes the next module's input), or merge the two modules into one. Same problem for `Influencing Factors` + `Risk Insights` → `Vulnerability Assessment`, and for `Conclusion` + `Appendix B` splitting across two new sections.
4. **`is_excluded` handling** — add a skip in `run_pipeline()`'s section loop.
5. **`Executive Summary`** — brand-new section with no module today; either skip or wire up a new module.

That's a meaningful refactor — touches the orchestrator, the module-map, and potentially merges/splits existing `ara_*` modules. **We want to avoid all of this.**

### 8.5 What to ask the BE to keep — so we don't change anything

Concrete asks, ordered by how much they save us:

1. **Keep the read endpoint's section shape flat** (or expose a parallel "legacy" view, e.g. `?shape=flat`):
   - top-level field is `"name"` (string)
   - top-level field is `"jsonContent"` (object)
   - no `section_header` wrapping
   - no `subsections[]` nesting — every section, including what they now call sub-sections, appears as its own entry in the flat list
2. **Keep the `name` strings identical to what `_MODULE_MAP` in `app_poc_v2.py` already expects** — i.e. all 12 keys listed in §2 of this document. In particular:
   - `Contents and Introduction` (with the `s` — not `Content and Introduction`)
   - `SSP Scenario Analysis` (capital `A`)
   - `Overview` and `Exposure` as **separate** sections, not merged into a single sub-section
   - `Influencing Factors` and `Risk Insights` as **separate** sections
   - `Parametric` as its own top-level section (not nested under `Appendices`)
   - `Conclusion & Appendix` as **one** section, not split into two
   - Keep `Analytics` / `Header` exactly as today
3. **Do not introduce new required sections that block the pipeline.** If `Executive Summary` is going to be in the new template, that's fine — as long as it's optional (we can ignore it or send empty content). Just don't make it required for the report to render.
4. **Keep the inner structure of every section's `jsonContent` unchanged.** Specifically:
   - For `SSP Scenario Analysis`: the flood SSP table must stay at `content[5]` and the heat SSP table at `content[7]`.
   - For `Influencing Factors`: the three colour-class tables must stay at `content[10]`, `content[17]`, `content[27]`.
   - For `Impact Scale`: the section must contain exactly one `type: "table"` node (we look it up by type, so position can move, but a second table would confuse the lookup).
5. **Keep `input_config` at the top of the payload** with the same keys (`area`, `city`, `state`, `country`, `client`, `risk_for`, `heatwave_threshold`, `region`, `site_location`, `site_name`). If `risk_for` is being canonicalised to `Flood` / `Heat` / `Both` on the BE side that's actually good — the temporary `" only"` normaliser at [app_poc_v2.py:135](app_poc_v2.py#L135) can be deleted.
6. **It is OK for the new fields to *coexist*** alongside the old ones — `doc_id`, `section_id`, `version`, `is_excluded`, `updated_at`, etc. can all be added without breaking us, because we only read `name` and `jsonContent`. We will actually *need* `doc_id` on the section objects later, when we wire up the save-back path (§6), so please keep that field present. Same for `is_excluded` — once we wire it up we'll respect it.

### 8.6 Minimum the BE *must* expose (if they refuse to keep the legacy shape)

If the BE cannot keep the flat shape on the read side, the bare minimum we need on each section in order to wire up the save-back **without a major refactor** is:

- `doc_id` (string) — to PUT against
- a stable, predictable string that maps 1:1 to one of the 12 `_MODULE_MAP` keys (could be a brand-new field, e.g. `"legacy_name"` or `"module_key"`)
- `jsonContent` (object) — same shape as today
- `is_excluded` (bool) — so we know to skip

With those four fields we can write a tiny adapter that flattens whatever shape they send into the dict `app_poc_v2._dispatch()` already understands — no changes to any `ara_*` module.

### 8.7 One-paragraph version (to paste into Slack / email)

> Our pipeline today reads `sections[].name` and `sections[].jsonContent` from a flat list. The new `/sections` response wraps content inside `section_header`, nests sub-sections under `subsections[]`, and renames the section labels (e.g. `Contents and Introduction` → `Content and Introduction`, `Overview` + `Exposure` collapsed into one). All of that would force us to refactor the orchestrator and merge several modules. Can you either (a) keep the old flat shape and original `name` strings on the read endpoint, or (b) add a compatibility view (`?shape=flat`) that returns the legacy structure? The new fields (`doc_id`, `version`, `is_excluded`, `subsections`) can stay alongside — we just need `name` and `jsonContent` to remain at the top level of each section, and we'll need `doc_id` exposed for when we start PUTting resolved content back.

---

## 9. Blockers to clarify with BE *before* tomorrow's demo

We're shipping a happy-path demo, so this is the short list — only the things that, if unanswered, stop the demo dead.

1. **Give us a working `report_id` + JWT.** BE creates a dev report in their system, shares the `report_id` and a long-lived token. We paste the token into `.env` and pass `--report-id` to the pipeline.
2. **Is that test report already `initialize-sections`'d?** If yes, we go straight to `GET /sections` + `PUT /sections/{doc_id}`. If no, we (or they) call endpoint 1 once.
3. **Will our existing Azure blob URLs render in the BE preview?** Today every PNG (risk maps, charts, layer maps) lives at `https://resscore.blob.core.windows.net/...` and we embed that URL into `jsonContent.src`. If the BE renderer accepts external URLs → no image-related changes. If it doesn't → we have to POST every PNG to endpoint 100 (`/report_docs/{report_id}/images`) and use the URL it returns instead.
4. **Where is `input_config` (`risk_for`, `area`, `city`, `country`, …) in the new model?** Endpoint 2 doesn't expose it at the top level. If BE doesn't have an answer by tomorrow, we hardcode it for the demo and move on.

Everything else (auth refresh, optimistic locking, `is_excluded` handling, history retention, error retries, prod SSL, publish flow, resilience actions, AST-index stability across templates) — out of scope for the demo. Park and revisit after.

---

## 10. Minimum script changes for the happy-path demo

Three changes, all in `app_poc_v2.py`. The 11 `ara_*` modules stay untouched.

1. **Flattener** — turn the new `section_header` + `subsections[]` response into the flat dict shape `_dispatch()` already understands. ~15 lines.
2. **Update `_MODULE_MAP`** — switch keys from old flat strings (`"Overview"`, `"Exposure"`, …) to new `(section_title, sub_section)` tuples (`("Hazard Risk Assessment", "Overview")`, …). For merged sections (`Overview` now owns what `ara_overview` + `ara_exposure` produced), **pick one module** for the demo — accept losing the other module's data this round.
3. **PUT loop at the end of `run_pipeline()`** — walk `Report_Data/Report_<id>/ara_*.json`, look up the `doc_id` from the `list_sections` response captured at start of run, `PUT /sections/{doc_id}` with body `{"jsonContent": <file contents>}`. Skip on `is_excluded=true`. Log results to `_publish_log.json`. ~20 lines.

Plus a `--report-id <uuid>` CLI arg and an `RES360_API_TOKEN` entry in `.env`.

**Knowingly accepting for the demo (fix post-demo):**

- `ara_ssp_scenario` and `ara_influencing_factors` hardcode AST indexes (5, 7 / 10, 17, 27). If the new template has reshuffled those tables, those sections will look broken. We'll find out at runtime — accept it.
- `Executive Summary` is a new section with no module. Leave alone, don't PUT to it. The BE template's default text stays.
- Chained dispatch (running two modules into one section) — skip; pick one module per merged section as noted above.
- No retries, no conflict detection, no SSL verification flip — happy path only.
