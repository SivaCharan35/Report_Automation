"""
Shared map chrome helpers for report PNGs.
Adds north arrow, scale bar, lat/long labels, optional logo, and off-map legends.

Patterns borrowed from the reference map generator (lst_map_appv10.py) —
not a Streamlit runtime dependency.
"""

from __future__ import annotations

import logging
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

_ASSETS_DIR = Path(__file__).parent / "map_assets"
_NORTH_ARROW = _ASSETS_DIR / "north_arrow.png"
_LOGO = _ASSETS_DIR / "resilience_logo.jpg"


def _load_rgba(path: Path) -> np.ndarray | None:
    if not path.exists():
        logger.warning("Map asset missing: %s", path)
        return None
    try:
        return np.array(Image.open(path).convert("RGBA"))
    except Exception as exc:
        logger.warning("Failed to load map asset %s: %s", path, exc)
        return None


def add_north_arrow(ax, size: float = 0.10, margin: float = 0.01) -> None:
    """Place north-arrow PNG in the top-right corner of the axes."""
    img = _load_rgba(_NORTH_ARROW)
    if img is None:
        return
    try:
        ins = ax.inset_axes([1 - size - margin, 1 - size - margin, size, size])
        ins.imshow(img)
        ins.set_facecolor("none")
        ins.patch.set_visible(False)
        ins.axis("off")
        for spine in ins.spines.values():
            spine.set_visible(False)
    except Exception as exc:
        logger.debug("North arrow skipped: %s", exc)


def add_scalebar(ax) -> None:
    """Add a metre scale bar (Web Mercator axes assumed)."""
    try:
        from matplotlib_scalebar.scalebar import ScaleBar

        ax.add_artist(
            ScaleBar(
                1,
                "m",
                location="lower right",
                frameon=True,
                color="black",
                box_color="white",
                box_alpha=0.75,
                font_properties={"size": 8},
            )
        )
    except ImportError:
        logger.warning("matplotlib-scalebar not installed — scale bar skipped")
    except Exception as exc:
        logger.debug("Scale bar skipped: %s", exc)


def add_latlon_labels(ax, bounds) -> None:
    """
    Format Web Mercator tick labels as WGS84 lon/lat degrees.
    `bounds` = (west, south, east, north) in EPSG:3857.
    """
    try:
        from pyproj import Transformer

        west, south, east, north = bounds
        to_wgs = Transformer.from_crs(3857, 4326, always_xy=True)
        cx = (west + east) / 2
        cy = (south + north) / 2

        def _fmt_x(x, _pos):
            lon, _ = to_wgs.transform(x, cy)
            return f"{lon:.2f}°"

        def _fmt_y(y, _pos):
            _, lat = to_wgs.transform(cx, y)
            return f"{lat:.2f}°"

        ax.xaxis.set_major_formatter(mticker.FuncFormatter(_fmt_x))
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(_fmt_y))
        ax.xaxis.set_major_locator(mticker.MaxNLocator(5))
        ax.yaxis.set_major_locator(mticker.MaxNLocator(5))
        ax.tick_params(labelsize=7, direction="in", top=True, right=True)
        ax.grid(True, color="white", linewidth=0.35, linestyle="--", alpha=0.55)
        for spine in ax.spines.values():
            spine.set_visible(True)
            spine.set_linewidth(1.2)
    except Exception as exc:
        logger.debug("Lat/lon labels skipped: %s", exc)


# ── Layout / export (shared by all report map PNGs) ───────────────────────────
MAP_LEFT        = 0.02
MAP_BOTTOM      = 0.06
MAP_TOP         = 0.95
MAP_RIGHT_PAD   = 0.005
LEGEND_WIDTH    = 0.15
LEGEND_GAP      = 0.008
SAVE_PAD_INCHES = 0.01
# Fraction of data width/height added on EACH side so basemap shows uniformly
# around the overlay (data stays the same size; view expands outward).
# 0.07 = 7% of data extent on each side (user-selected; also try 0.08).
DATA_VIEW_PAD   = 0.07
# Logo sits on the figure canvas (bottom-right), outside the map axes.
LOGO_LEFT       = 0.88
LOGO_BOTTOM     = 0.008
LOGO_WIDTH      = 0.10
LOGO_HEIGHT     = 0.055


def expand_view_bounds(bounds, pad_frac: float | None = None) -> tuple[float, float, float, float]:
    """
    Expand (west, south, east, north) by pad_frac of the data extent on each side.

    Overlay geometry / raster extent is unchanged; only the map view (basemap +
    axes limits) grows so satellite context is uniform on all four sides.
    """
    frac = DATA_VIEW_PAD if pad_frac is None else float(pad_frac)
    west, south, east, north = bounds
    dx = (east - west) * frac
    dy = (north - south) * frac
    return (west - dx, south - dy, east + dx, north + dy)


def save_map_figure(fig, out_path, dpi: int = 150) -> None:
    """Save with minimal outer padding — use for all report map PNGs."""
    fig.savefig(
        out_path,
        dpi=dpi,
        bbox_inches="tight",
        pad_inches=SAVE_PAD_INCHES,
        facecolor="white",
    )
    plt.close(fig)


def _map_axes_rect(legend_width_frac: float) -> list[float]:
    """Main map axes [left, bottom, width, height] in figure coordinates."""
    width = 1.0 - MAP_LEFT - LEGEND_GAP - legend_width_frac - MAP_RIGHT_PAD
    return [MAP_LEFT, MAP_BOTTOM, width, MAP_TOP - MAP_BOTTOM]


def expand_map_axes(ax, *, with_legend: bool = False) -> None:
    """Use nearly the full figure for the map axes (no side legend, or before legend placement)."""
    if with_legend:
        ax.set_position(_map_axes_rect(LEGEND_WIDTH))
    else:
        width = 1.0 - MAP_LEFT - MAP_RIGHT_PAD
        ax.set_position([MAP_LEFT, MAP_BOTTOM, width, MAP_TOP - MAP_BOTTOM])


def _legend_width_for_image(fig, img: np.ndarray, max_width_frac: float = 0.28) -> float:
    """Legend panel width from image aspect — keep natural proportions (no stretch)."""
    h, w = img.shape[:2]
    if h <= 0 or w <= 0:
        return LEGEND_WIDTH
    # Prefer a readable width; height will follow image aspect (legends are ~2:1 wide).
    return min(max(0.22, LEGEND_WIDTH), max_width_frac)


def add_logo(fig) -> None:
    """Place Resilience logo at figure bottom-right (outside the map), matching report style."""
    img = _load_rgba(_LOGO)
    if img is None:
        return
    try:
        logo_ax = fig.add_axes([LOGO_LEFT, LOGO_BOTTOM, LOGO_WIDTH, LOGO_HEIGHT])
        logo_ax.imshow(img)
        logo_ax.axis("off")
        logo_ax.set_facecolor("none")
        logo_ax.patch.set_visible(False)
    except Exception as exc:
        logger.debug("Logo skipped: %s", exc)


def apply_map_chrome(fig, ax, bounds, *, logo: bool = True) -> None:
    """North arrow + scale + lat/lon (+ optional logo). Call instead of axis('off').

    Keeps view limits fixed (no datalim expansion) so basemap padding stays
    uniform on all four sides. Axes position is resized to match the view
    aspect instead of changing data limits.
    """
    try:
        west, south, east, north = bounds
        ax.set_xlim(west, east)
        ax.set_ylim(south, north)
        # Match axes box to view aspect so equal-scale map doesn't eat T/B or L/R pad
        pos = ax.get_position()
        fig_w, fig_h = fig.get_size_inches()
        view_aspect = (east - west) / max(north - south, 1e-9)  # data units (x/y)
        # Available box in inches
        box_w_in = pos.width * fig_w
        box_h_in = pos.height * fig_h
        target_h_in = box_w_in / view_aspect
        if target_h_in <= box_h_in:
            new_h = target_h_in / fig_h
            new_w = pos.width
        else:
            new_w = (box_h_in * view_aspect) / fig_w
            new_h = pos.height
        x0 = pos.x0 + (pos.width - new_w) / 2.0
        y0 = pos.y0 + (pos.height - new_h) / 2.0
        ax.set_position([x0, y0, new_w, new_h])
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlim(west, east)
        ax.set_ylim(south, north)
    except Exception:
        pass
    add_latlon_labels(ax, bounds)
    add_scalebar(ax)
    add_north_arrow(ax)
    if logo:
        add_logo(fig)


def place_legend_right(fig, ax, handles, title: str = "", fontsize: int = 7) -> None:
    """
    Draw legend in a tight panel to the right of the map (not overlaid on imagery).
    """
    if not handles:
        return
    try:
        rect = _map_axes_rect(LEGEND_WIDTH)
        ax.set_position(rect)
        leg_left = rect[0] + rect[2] + LEGEND_GAP
        leg_ax = fig.add_axes([leg_left, MAP_BOTTOM, LEGEND_WIDTH, rect[3]])
        leg_ax.axis("off")
        leg_ax.legend(
            handles=handles,
            loc="center left",
            fontsize=fontsize,
            frameon=True,
            framealpha=0.95,
            title=title or None,
            title_fontsize=max(fontsize, 8),
            borderaxespad=0.2,
            labelspacing=0.35,
            handletextpad=0.5,
        )
    except Exception as exc:
        logger.debug("Right legend failed (%s) — falling back to upper-right", exc)
        ax.legend(
            handles=handles,
            loc="upper right",
            fontsize=fontsize,
            framealpha=0.9,
            title=title or None,
        )


def _legend_rgba_on_white(path: Path) -> np.ndarray | None:
    """Load an IF legend PNG and composite onto white.

    Official legends store black labels as opaque black (alpha=255) over a
    transparent background (alpha=0). Compositing onto white makes labels
    readable on report maps.
    """
    if not path.exists():
        logger.warning("Legend image missing: %s", path)
        return None
    try:
        rgba = np.array(Image.open(path).convert("RGBA"), dtype=np.float32)
        rgb = rgba[:, :, :3]
        alpha = rgba[:, :, 3:4] / 255.0
        out = (rgb * alpha + 255.0 * (1.0 - alpha)).astype(np.uint8)
        # Crop to non-white content with a small pad
        mask = out.mean(axis=2) < 250
        ys, xs = np.where(mask)
        if len(xs) == 0:
            return out
        pad = 8
        y0 = max(0, int(ys.min()) - pad)
        y1 = min(out.shape[0], int(ys.max()) + pad + 1)
        x0 = max(0, int(xs.min()) - pad)
        x1 = min(out.shape[1], int(xs.max()) + pad + 1)
        return out[y0:y1, x0:x1]
    except Exception as exc:
        logger.warning("Failed to load legend image %s: %s", path, exc)
        return None


def place_legend_image_right(fig, ax, legend_path: Path, width_frac: float | None = None) -> None:
    """
    Place a legend PNG in a panel to the right of the map (not overlaid).
    Used for Influencing Factors bivariate legends.

    Panel size follows the PNG aspect (~2:1 wide) so the diamond is not stretched.
    """
    img = _legend_rgba_on_white(Path(legend_path))
    if img is None:
        return
    try:
        img_h, img_w = img.shape[:2]
        leg_w = width_frac if width_frac is not None else _legend_width_for_image(fig, img)
        # Map gets remaining width; legend height follows image aspect (not full map height).
        map_w = 1.0 - MAP_LEFT - LEGEND_GAP - leg_w - MAP_RIGHT_PAD
        map_h = MAP_TOP - MAP_BOTTOM
        ax.set_position([MAP_LEFT, MAP_BOTTOM, map_w, map_h])

        fig_w, fig_h = fig.get_size_inches()
        leg_h = (leg_w * fig_w) * (img_h / max(img_w, 1)) / fig_h
        leg_h = min(leg_h, map_h)
        leg_left = MAP_LEFT + map_w + LEGEND_GAP
        leg_bottom = MAP_BOTTOM + (map_h - leg_h) / 2.0
        leg_ax = fig.add_axes([leg_left, leg_bottom, leg_w, leg_h])
        leg_ax.imshow(img, aspect="equal", interpolation="nearest")
        leg_ax.axis("off")
        leg_ax.set_facecolor("white")
    except Exception as exc:
        logger.warning("Right legend image failed (%s)", exc)


def finish_and_save(fig, ax, bounds, out_path, *, handles=None, legend_title: str = "",
                    logo: bool = True, dpi: int = 150) -> None:
    """Apply chrome, optional right legend, and save PNG."""
    if handles:
        place_legend_right(fig, ax, handles, title=legend_title)
    apply_map_chrome(fig, ax, bounds, logo=logo)
    save_map_figure(fig, out_path, dpi=dpi)
