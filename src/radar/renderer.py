"""Renderowanie danych radarowych do statycznych obrazów i overlayów Leaflet."""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
import numpy as np
from PIL import Image
from pyproj import Transformer

from .palette import RadarPalette


class RadarRenderer:
    """Renderuje dane radarowe do PNG — statycznego lub do Leaflet imageOverlay."""

    def __init__(self, palette: RadarPalette | None = None):
        self._palette = palette or RadarPalette()

    def render_static(self, radar_data: dict, output_file: str,
                      gdf_borders, gdf_regions=None,
                      extent=None, dpi: int = 100,
                      width: int = 20, height: int = 20,
                      style: str = "noaa"):
        """Generuje statyczny PNG z siatką, granicami i legendą."""
        from mpl_toolkits.axes_grid1 import make_axes_locatable

        fig, ax = plt.subplots(figsize=(width, height), dpi=dpi)
        ax.set_facecolor("#A8A8A8")

        cmap, norm, label = self._palette.get(radar_data["quantity"], style=style)
        data = radar_data["radar_data"]["dataset1"]

        ax.pcolormesh(
            radar_data["lon_mesh"], radar_data["lat_mesh"], data,
            cmap=cmap, norm=norm, shading="flat",
        )
        ax.grid(True, color="white", linewidth=0.5, alpha=0.4, linestyle="-")

        gdf_borders.plot(ax=ax, edgecolor="#111111", facecolor="none", linewidth=0.9)
        if gdf_regions is not None:
            gdf_regions.plot(ax=ax, edgecolor="#555555", facecolor="none", linewidth=0.3)

        x_min = float(radar_data["lon_mesh"].min())
        x_max = float(radar_data["lon_mesh"].max())
        y_min = float(radar_data["lat_mesh"].min())
        y_max = float(radar_data["lat_mesh"].max())

        if extent:
            ax.set_xlim(extent[0], extent[2])
            ax.set_ylim(extent[1], extent[3])
        else:
            ax.set_xlim(x_min, x_max)
            ax.set_ylim(y_min, y_max)

        sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
        sm.set_array([])
        divider = make_axes_locatable(ax)
        cax = divider.append_axes("right", size="3%", pad=0.12)
        cbar = plt.colorbar(sm, cax=cax)
        cbar.set_label(label, rotation=90, labelpad=8, fontsize=11, fontweight="bold")

        dt = radar_data["start_date"]
        ax.set_title(
            f"{radar_data['quantity']}  {radar_data['product']}  "
            f"| {dt.strftime('%d-%m-%Y')} {dt.strftime('%H:%M')} UTC",
            fontsize=13, fontweight="bold",
        )

        plt.savefig(output_file, bbox_inches="tight", pad_inches=0.15)
        plt.close()

    def render_overlay(self, radar_data: dict, output_png: str,
                       dataset_key: str = "dataset1",
                       dpi: int = 150, size: int = 10,
                       style: str = "imgw") -> dict:
        """
        Generuje przezroczysty PNG w projekcji EPSG:3857 do L.imageOverlay w Leaflet.

        Używa Figure + FigureCanvasAgg (bez globalnego plt) — thread-safe.
        radar_data musi być zdekodowany z projection="EPSG:3857".

        Zwraca: {bounds: [[lat_sw, lon_sw], [lat_ne, lon_ne]], timestamp, quantity, ...}
        """
        cmap, norm, _ = self._palette.get(radar_data["quantity"], style=style)
        data = radar_data["radar_data"][dataset_key]

        x_mesh = radar_data["lon_mesh"]
        y_mesh = radar_data["lat_mesh"]
        x_min, x_max = float(x_mesh.min()), float(x_mesh.max())
        y_min, y_max = float(y_mesh.min()), float(y_mesh.max())

        to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
        lon_sw, lat_sw = to_4326.transform(x_min, y_min)
        lon_ne, lat_ne = to_4326.transform(x_max, y_max)

        # Figure + FigureCanvasAgg — thread-safe, nie modyfikuje globalnego stanu plt
        fig = Figure(figsize=(size, size), dpi=dpi, frameon=False)
        canvas = FigureCanvasAgg(fig)
        ax = fig.add_axes([0, 0, 1, 1])
        fig.patch.set_alpha(0)
        ax.patch.set_alpha(0)

        ax.pcolormesh(x_mesh, y_mesh, data, cmap=cmap, norm=norm, shading="flat")
        ax.set_aspect("auto")
        ax.set_xlim(x_min, x_max)
        ax.set_ylim(y_min, y_max)
        ax.axis("off")

        canvas.draw()
        buf_w, buf_h = canvas.get_width_height()
        buf = np.frombuffer(canvas.buffer_rgba(), dtype=np.uint8).reshape(buf_h, buf_w, 4)
        Image.fromarray(buf, "RGBA").save(output_png, "PNG")

        return {
            "bounds":    [[lat_sw, lon_sw], [lat_ne, lon_ne]],
            "timestamp": radar_data["start_date"].strftime("%Y-%m-%dT%H:%M:%S"),
            "quantity":  radar_data["quantity"],
            "product":   radar_data["product"],
            "system":    radar_data["system"],
        }
