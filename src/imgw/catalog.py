"""Katalog produktow radarowych IMGW — mapuje konfiguracje na sciezki API."""

import json
from pathlib import Path

_IMGW_PATH_BASE = "/Oper/Polrad/Produkty/HVD"


class ProductCatalog:
    """Wczytuje config/radar_config.json i generuje sciezki IMGW dla wszystkich produktow."""

    def __init__(self, config_path: str | Path):
        with open(config_path, encoding="utf-8") as f:
            self._cfg = json.load(f)

    @property
    def config(self) -> dict:
        return self._cfg

    def all_paths(self) -> list[dict]:
        """
        Zwraca liste slownikow {path, key_prefix, label_base} dla wszystkich produktow.
        """
        paths = []

        for radar in self._cfg["radars"]:
            for product in self._cfg["radar_products"]:
                paths.append({
                    "path":       f"{_IMGW_PATH_BASE}/HVD_{radar}_{product}",
                    "key_prefix": f"{radar.upper()}_{product}",
                    "label_base": (
                        f"{self._station_name(radar)} – "
                        f"{self._cfg['product_labels'].get(product, product)}"
                    ),
                })

        for compo in self._cfg["compo_products"]:
            short = compo.split(".")[0]
            paths.append({
                "path":       f"{_IMGW_PATH_BASE}/HVD_COMPO_{compo}",
                "key_prefix": f"COMPO_{short}",
                "label_base": self._cfg["product_labels"].get(compo, compo),
            })

        return paths

    def select_path(self, key_prefix: str) -> dict | None:
        """Zwraca wpis dla konkretnego key_prefix lub None."""
        return next((p for p in self.all_paths() if p["key_prefix"] == key_prefix), None)

    def _station_name(self, radar_id: str) -> str:
        for st in self._cfg.get("radar_stations", []):
            if st["id"] == radar_id:
                return st["name"]
        return radar_id.upper()
