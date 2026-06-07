"""
Hybrid storage layer.
Every pipeline output is routed through save_asset(), which respects the
SAVE_LOCAL and UPLOAD_AZURE flags in config.py.

Usage pattern in any pipeline module:

    save_asset(
        local_path=ctx["output_dir"] / "my_output.json",
        blob_name=f"{ctx['azure_base_path']}/my_output.json",
        content_type="application/json",
        data=my_bytes,          # optional — omit if file already on disk
    )
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import config

logger = logging.getLogger(__name__)


def save_asset(
    *,
    local_path: Path,
    blob_name: str,
    content_type: str = "application/octet-stream",
    data: bytes | None = None,
) -> dict[str, str | None]:
    """
    Persist an asset according to the pipeline flags.

    - If data is provided it is written to local_path (text/JSON/binary blobs).
    - If data is None, local_path must already exist on disk (e.g. a PNG saved
      by matplotlib), and only the Azure upload step is performed.

    Returns {"local": path_str | None, "azure": url | None}.
    """
    result: dict[str, str | None] = {"local": None, "azure": None}

    # ── Local ─────────────────────────────────────────────────────────────────
    if config.SAVE_LOCAL:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if data is not None:
            local_path.write_bytes(data)
        result["local"] = str(local_path)
        logger.debug("Saved locally: %s", local_path)

    # ── Azure ─────────────────────────────────────────────────────────────────
    if config.UPLOAD_AZURE:
        if not config.AZURE_CONNECTION_STRING:
            logger.warning(
                "UPLOAD_AZURE=True but AZURE_CONNECTION_STRING is empty — skipping."
            )
        else:
            try:
                from azure_utils import upload_bytes_to_azure, upload_file_to_azure

                if data is not None:
                    url = upload_bytes_to_azure(data, blob_name, content_type)
                else:
                    url = upload_file_to_azure(local_path, blob_name)
                result["azure"] = url
            except Exception as exc:
                logger.error("Azure upload failed for %s: %s", blob_name, exc)

    return result


def _swap_url_title_to_src(obj) -> None:
    """Recursively, for image nodes whose `attrs.title` holds a URL different
    from `attrs.src`, copy `title` into `src` so our generated URL becomes
    the rendered one.

    Required because the BE Parametric template stores the `{{APPX_*_MAP}}`
    placeholders in `attrs.title` instead of `attrs.src`. After our pipeline
    resolves the placeholder, our generated `app_*.png` URL sits in `title`
    while `src` still holds a hardcoded BE-stored URL. The renderer reads
    `src`, so without this swap the wrong image renders.

    Scoped to image nodes AND guarded by URL-shape detection on `title` —
    sections whose `title` is a normal tooltip string are untouched. After
    this runs, `_promote_image_src` propagates the new `src` to the
    top-level `src` field that the renderer ultimately reads.
    """
    if isinstance(obj, dict):
        if obj.get("type") == "image":
            attrs = obj.get("attrs")
            if isinstance(attrs, dict):
                title = attrs.get("title")
                src   = attrs.get("src")
                if (isinstance(title, str)
                        and title.startswith(("http://", "https://"))
                        and title != src):
                    attrs["src"]   = title
                    attrs["title"] = None  # don't leak the URL as an alt-text tooltip
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _swap_url_title_to_src(v)
    elif isinstance(obj, list):
        for v in obj:
            _swap_url_title_to_src(v)


def _promote_image_src(obj) -> None:
    """Recursively copy attrs.src → top-level src for image nodes.

    The API template stores image paths inside attrs.src with the top-level
    src set to null. After placeholder replacement the resolved URL sits in
    attrs.src, but the frontend reads the top-level src field to render the
    image. This walk ensures both fields hold the same value.
    """
    if isinstance(obj, dict):
        if obj.get("type") == "image":
            attrs = obj.get("attrs")
            if isinstance(attrs, dict):
                url = attrs.get("src")
                if url and not obj.get("src"):
                    obj["src"] = url
        for v in obj.values():
            _promote_image_src(v)
    elif isinstance(obj, list):
        for v in obj:
            _promote_image_src(v)


def save_section_output(
    content: str,
    stem: str,
    ctx: dict,
) -> dict[str, str | None]:
    """
    Save a module's resolved content to {output_dir}/{stem}.json.

    Called at Step 6 by every ara_* module.  Wraps save_asset() so modules
    don't need to build paths themselves.

    content is the placeholder-resolved jsonContent string produced by the
    module.  It is parsed back to a Python object and re-serialised with
    indent=2 so the output file is readable JSON.  If parsing fails (e.g. a
    placeholder value broke the JSON structure) the raw string is wrapped in
    {"resolved_content": "..."} so the file is still valid JSON.

    Args:
        content : resolved placeholder string (Step 6 output)
        stem    : filename stem, e.g. "ara_intro"  → ara_intro.json
        ctx     : pipeline context (must contain output_dir + azure_base_path)
    """
    output_dir = ctx.get("output_dir")
    azure_base = ctx.get("azure_base_path", "")

    if output_dir is None:
        logger.warning("save_section_output: 'output_dir' missing from context — skipping.")
        return {"local": None, "azure": None}

    # Parse the resolved JSON string back to a Python object for pretty output.
    # Fall back to a plain wrapper if the string is not valid JSON.
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        payload = {"resolved_content": content}

    # First: where the BE template stored the placeholder in `attrs.title`
    # (e.g. Parametric section) — move our resolved URL out of `title` and
    # into `attrs.src` so the renderer picks it up. Guarded by URL-shape
    # detection; tooltip-only `title` strings are untouched.
    _swap_url_title_to_src(payload)

    # Then: copy attrs.src → top-level src for image nodes.
    # The API template stores image URLs inside attrs.src (src is null at the
    # top level). After placeholder replacement the URL ends up in attrs.src,
    # but the frontend renderer reads the top-level src field — so we copy it.
    _promote_image_src(payload)

    data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")

    return save_asset(
        local_path   = Path(output_dir) / f"{stem}.json",
        blob_name    = f"{azure_base}/{stem}.json",
        content_type = "application/json",
        data         = data,
    )
