"""Dekodowanie plików HDF5 IMGW do słownika danych radarowych."""

import h5py
import numpy as np
from datetime import datetime
from pyproj import Proj, Transformer


class RadarDecoder:
    """Dekoduje pojedynczy plik HDF5 z IMGW do słownika gotowego do renderowania."""

    # Domyślna projekcja wyjściowa — EPSG:3857 wymagane przez RadarRenderer (web overlay)
    DEFAULT_PROJECTION = "EPSG:3857"

    def decode(self, file_path: str, projection: str | None = None) -> dict:
        """
        Wczytuje plik HDF5 i zwraca słownik:
          radar_data  : {dataset_name: np.ndarray}  — wartości fizyczne (dBZ, mm/h …)
          lon_mesh    : np.ndarray (xsize+1, ysize+1) — siatka X w docelowej projekcji
          lat_mesh    : np.ndarray                    — siatka Y w docelowej projekcji
          quantity    : str   (np. "DBZH")
          product     : str   (np. "MAX")
          system      : str   (np. "POLCOMP" | "POLRAD")
          start_date  : datetime
        """
        proj = projection or self.DEFAULT_PROJECTION

        with h5py.File(file_path, "r") as f:
            datasets = [k for k in f.keys() if "dataset" in k.lower()]
            first_ds = datasets[0]

            what_ds = f[f"{first_ds}/what"]
            where   = f.get("where")
            if where is None or "xsize" not in where.attrs:
                where = f[f"{first_ds}/where"]

            quantity = what_ds.attrs["quantity"].decode()
            product  = what_ds.attrs["product"].decode()
            system   = f["how"].attrs["system"].decode()

            startdate  = what_ds.attrs["startdate"].decode()
            starttime  = what_ds.attrs["starttime"].decode()
            start_date = datetime.strptime(f"{startdate}{starttime}", "%Y%m%d%H%M%S")

            lon_mesh, lat_mesh, lonlat_to_radar = self._build_mesh(where, proj)

            radar_data = {
                ds: self._decode_array(f[f"/{ds}/data1/data"], what_ds)
                for ds in datasets
            }

        return {
            "radar_data":     radar_data,
            "lon_mesh":       lon_mesh,
            "lat_mesh":       lat_mesh,
            "lonlat_to_radar": lonlat_to_radar,
            "quantity":       quantity,
            "product":        product,
            "system":         system,
            "start_date":     start_date,
        }

    # ── prywatne ──────────────────────────────────────────────────────────────

    @staticmethod
    def _decode_array(dset, what_ds) -> np.ndarray:
        """Przelicza surowe piksele na wartości fizyczne; nodata/undetect → NaN."""
        data     = dset[:].astype(float)
        nodata   = what_ds.attrs["nodata"]
        undetect = what_ds.attrs["undetect"]
        gain     = what_ds.attrs["gain"]
        offset   = what_ds.attrs["offset"]

        data[data == nodata]   = np.nan
        data[data == undetect] = np.nan
        data = data * gain + offset
        return np.flipud(data)

    @staticmethod
    def _build_mesh(where, output_projection: str):
        """
        Buduje siatkę współrzędnych metodą interpolacji dwuliniowej z narożników HDF5.
        Zwraca (lon_mesh, lat_mesh, lonlat_to_radar_transformer).
        """
        UL = where.attrs["UL_lon"], where.attrs["UL_lat"]
        UR = where.attrs["UR_lon"], where.attrs["UR_lat"]
        LL = where.attrs["LL_lon"], where.attrs["LL_lat"]
        LR = where.attrs["LR_lon"], where.attrs["LR_lat"]

        xsize = int(where.attrs["xsize"])
        ysize = int(where.attrs["ysize"])

        projdef = where.attrs["projdef"].decode()
        # Zamiana przestarzałego +ellps=sphere na równoważne parametry pyproj
        projdef = projdef.replace("+ellps=sphere", "+R=6378137 +nadgrids=@null +no_defs")

        lonlat_to_radar = Transformer.from_proj(Proj("EPSG:4326"), Proj(projdef), always_xy=True)
        radar_to_out    = Transformer.from_proj(Proj(projdef), Proj(output_projection), always_xy=True)

        corners = {
            "LL": lonlat_to_radar.transform(*LL),
            "LR": lonlat_to_radar.transform(*LR),
            "UL": lonlat_to_radar.transform(*UL),
            "UR": lonlat_to_radar.transform(*UR),
        }

        u = np.linspace(0, 1, xsize + 1)
        v = np.linspace(0, 1, ysize + 1)
        U, V = np.meshgrid(u, v)

        # Interpolacja dwuliniowa w układzie radaru
        X = (1-U)*(1-V)*corners["LL"][0] + U*(1-V)*corners["LR"][0] \
          + (1-U)*V    *corners["UL"][0]  + U*V    *corners["UR"][0]
        Y = (1-U)*(1-V)*corners["LL"][1] + U*(1-V)*corners["LR"][1] \
          + (1-U)*V    *corners["UL"][1]  + U*V    *corners["UR"][1]

        lon_mesh, lat_mesh = radar_to_out.transform(X, Y)
        return lon_mesh, lat_mesh, lonlat_to_radar
