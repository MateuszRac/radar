#!/usr/bin/env python
"""
Prognoza STEPS – kompozyt DPSRI IMGW
======================================
Pobiera ostatnie skany kompozytu DPSRI (natężenie opadu mm/h) z API IMGW,
szacuje pole ruchu metodą Lucas-Kanade i oblicza stochastyczną prognozę STEPS
(ensemble mean) z krokiem 5 minut do +60 minut.
Wynik zapisuje jako PNG z granicami województw.

Uruchomienie:
    uv run python src/pysteps/steps_nowcast_imgw.py

Wymagane pakiety (poza standardowymi zależnościami projektu):
    pysteps, geopandas, cartopy (opcjonalnie, do granic województw)
"""

import gc
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import h5py
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import geopandas as gpd
from PIL import Image
from pyproj import CRS, Proj, Transformer

# Jeśli projekt jest zainstalowany przez `uv sync`, import działa bez sys.path.
# Fallback dla uruchomienia bez instalacji: dodajemy src/ NA KOŃCU sys.path,
# żeby zainstalowany pakiet `pysteps` nie był zasłonięty przez src/pysteps/.
try:
    from imgw.client import ImgwClient
    from radar.palette import RadarPalette
    from telegram.telegram import notify_precip_alerts
except ImportError:
    sys.path.append(str(Path(__file__).resolve().parents[1]))  # → src/
    from imgw.client import ImgwClient
    from radar.palette import RadarPalette
    from telegram.telegram import notify_precip_alerts

from pysteps import nowcasts
from pysteps.motion.lucaskanade import dense_lucaskanade
from pysteps.postprocessing.ensemblestats import excprob
from pysteps.utils import transformation

# ── Konfiguracja logowania ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Parametry prognozy ───────────────────────────────────────────────────────
TIMESTEP      = 5       # krok czasowy [min]
N_FRAMES      = int(60/TIMESTEP)       # liczba skanów wejściowych (min. 3 do STEPS)
N_LEADTIMES   = int(120/TIMESTEP)      # kroków prognozy: 12 × 5 min = 60 min
N_ENS_MEMBERS = 50      # liczba członków ensemblu
SEED          = 42

# Próg natężenia opadu uznawanego za „opad" — zgodny z dolną granicą palety RATE
# (0.01 mm/h). Poniżej tej wartości = sucho. Wcześniej 0.1 mm/h kasowało cały
# lekki opad (znikał w prognozie, choć był w obserwacji). dBR: 10·log10(0.01)=-20.
PRECIP_THR_MM  = 0.01     # próg w mm/h (transformacja wejściowa, prog odwrotny)
PRECIP_THR_DBR = -20.0    # ten sam próg w dBR (precip_thr dla S-PROG/STEPS)
ZEROVALUE_DBR  = -25.0    # wartość „sucho" w dBR (kilka dB poniżej progu)

COMPO_PATH  = "/Oper/Polrad/Produkty/HVD/HVD_COMPO_DPSRI.comp.sri"
WWW_DATA_DIR = Path("www/data")   # katalog dla overlayów Leaflet

# Lokalny cache surowych plików radarowych HDF5 — trzymamy ostatnie skany,
# żeby nie pobierać ich ponownie przy każdym uruchomieniu.
RADAR_CACHE_DIR  = Path("data/imgw/polrad")
RADAR_CACHE_KEEP = N_FRAMES        # ile najnowszych plików zostawiamy w cache

# ── Paleta kolorów opadów ────────────────────────────────────────────────────
_RAIN_BOUNDS = [0.1, 0.3, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0]
_RAIN_COLORS = [
    "#C8FAFF", "#96E6FF", "#64CAFF",
    "#1E8CFF", "#0050C8", "#00A000",
    "#00DC00", "#FFFF00", "#FF6400",
    "#FF0000", "#C800C8",
]
RAIN_CMAP = mcolors.ListedColormap(_RAIN_COLORS)
RAIN_NORM = mcolors.BoundaryNorm(_RAIN_BOUNDS, RAIN_CMAP.N)
RAIN_CMAP.set_under("#FFFFFF00")   # przezroczysty dla wartości < 0.1 mm/h
RAIN_CMAP.set_bad("#FFFFFF00")

# Paleta natężenia opadu RATE z radar/palette.py — wspólne źródło kolorów dla
# overlayów obs/det. Tablice kolorów i progów MUSZĄ być zgodne z www/js/config.js
# (paleta + wartości) oraz www/point_data.php (odczyt punktowy).
RATE_CMAP, RATE_NORM, _RATE_LABEL = RadarPalette().get("RATE", style="imgw")

# ── Palety i progi prawdopodobieństwa (wykresy zbiorcze) ─────────────────────
PROB_THRESHOLDS = [
    (0.1,  "P(R > 0.1 mm/h) – jakikolwiek opad",   plt.cm.Blues),
    (10.0, "P(R > 10 mm/h) – intensywny opad",      plt.cm.Reds),
]
PROB_NORM  = mcolors.Normalize(vmin=0.0, vmax=1.0)
PROB_TICKS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]

# ── Dyskretne palety dla overlayów Leaflet (9 kroków po 10%) ─────────────────
# Kolory = ColorBrewer Blues/Reds 9-class; identyczne tablice są w index.php.
_PROB_BOUNDS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01]

_PROB01_COLORS = [          # Blues (P > 0.1 mm/h)
    "#deebf7", "#c6dbef", "#9ecae1", "#6baed6", "#4292c6",
    "#2171b5", "#08519c", "#08306b", "#041e42",
]
PROB01_CMAP = mcolors.ListedColormap(_PROB01_COLORS)
PROB01_NORM = mcolors.BoundaryNorm(_PROB_BOUNDS, PROB01_CMAP.N)
PROB01_CMAP.set_under("#FFFFFF00")
PROB01_CMAP.set_bad("#FFFFFF00")

_PROB10_COLORS = [          # Reds (P > 10 mm/h)
    "#fee0d2", "#fcbba1", "#fc9272", "#fb6a4a", "#ef3b2c",
    "#cb181d", "#a50f15", "#67000d", "#3f0007",
]
PROB10_CMAP = mcolors.ListedColormap(_PROB10_COLORS)
PROB10_NORM = mcolors.BoundaryNorm(_PROB_BOUNDS, PROB10_CMAP.N)
PROB10_CMAP.set_under("#FFFFFF00")
PROB10_CMAP.set_bad("#FFFFFF00")


# ── Funkcje overlayów Leaflet ────────────────────────────────────────────────

def compute_epsg3857_mesh(info: dict) -> tuple[np.ndarray, np.ndarray, dict]:
    """
    Przelicza siatkę radaru z natywnej projekcji do EPSG:3857.
    Zwraca (X_3857, Y_3857, bounds_wgs84) gdzie bounds = {sw:[lat,lon], ne:[lat,lon]}.
    """
    to_3857 = Transformer.from_proj(Proj(info["projdef"]), Proj("EPSG:3857"), always_xy=True)
    to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

    xs = np.linspace(info["x1"], info["x2"], info["xsize"] + 1)
    ys = np.linspace(info["y1"], info["y2"], info["ysize"] + 1)
    X_n, Y_n = np.meshgrid(xs, ys)
    X_3857, Y_3857 = to_3857.transform(X_n, Y_n)

    x_min, x_max = float(X_3857.min()), float(X_3857.max())
    y_min, y_max = float(Y_3857.min()), float(Y_3857.max())
    lon_sw, lat_sw = to_4326.transform(x_min, y_min)
    lon_ne, lat_ne = to_4326.transform(x_max, y_max)

    bounds = {
        "sw": [round(lat_sw, 6), round(lon_sw, 6)],
        "ne": [round(lat_ne, 6), round(lon_ne, 6)],
    }
    return X_3857, Y_3857, bounds


def build_warp_index(
    info: dict,
    X_3857: np.ndarray,
    Y_3857: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, int, int]:
    """
    Buduje (RAZ na przebieg) mapę indeksów nearest-neighbor reprojekcji
    natywna siatka radaru → regularny raster EPSG:3857.

    Dla każdego piksela wyjściowego (regularny w 3857, góra = północ) wyznacza,
    z której komórki natywnej siatki pobrać wartość. Dzięki temu kosztowna
    reprojekcja liczona jest tylko raz, a render każdej klatki to czyste
    indeksowanie numpy (bez matplotlib / pcolormesh — to było wąskie gardło,
    zwłaszcza na słabszym CPU jak Raspberry Pi).

    Zwraca (flat_idx, valid, W, H):
      flat_idx – (H, W) int: indeks do ``data.ravel()`` (poza domeną = 0)
      valid    – (H, W) bool: czy piksel mieści się w domenie radaru
      W, H     – wymiary wyjściowego rastra (H = ysize, czyli natywna rozdzielczość)
    """
    x1, x2, y1, y2 = info["x1"], info["x2"], info["y1"], info["y2"]
    xsize, ysize = info["xsize"], info["ysize"]

    x_min, x_max = float(X_3857.min()), float(X_3857.max())
    y_min, y_max = float(Y_3857.min()), float(Y_3857.max())

    H = ysize
    W = int(round(H * (x_max - x_min) / (y_max - y_min)))

    to_native = Transformer.from_proj(Proj("EPSG:3857"), Proj(info["projdef"]), always_xy=True)
    xo = np.linspace(x_min, x_max, W)
    yo = np.linspace(y_max, y_min, H)      # góra rastra = północ
    Xo, Yo = np.meshgrid(xo, yo)
    Xn, Yn = to_native.transform(Xo, Yo)

    # Współrzędne natywne → indeks komórki (oś X: W→E, oś Y: S→N — jak `data`).
    j = np.floor((Xn - x1) / (x2 - x1) * xsize).astype(np.intp)
    i = np.floor((Yn - y1) / (y2 - y1) * ysize).astype(np.intp)
    valid = (j >= 0) & (j < xsize) & (i >= 0) & (i < ysize)
    np.clip(i, 0, ysize - 1, out=i)
    np.clip(j, 0, xsize - 1, out=j)

    flat_idx = i * xsize + j
    flat_idx[~valid] = 0
    return flat_idx, valid, W, H


def render_overlay_png(
    data: np.ndarray,
    flat_idx: np.ndarray,
    valid: np.ndarray,
    output_path: Path,
    cmap,
    norm,
) -> None:
    """
    Renderuje transparentny PNG w EPSG:3857 gotowy do L.imageOverlay, używając
    gotowej mapy indeksów z :func:`build_warp_index` (bez matplotlib figure).

    Wartości NaN i < progu (set_under) → alpha = 0 (przezroczyste), identycznie
    jak przy dawnym pcolormesh — ``ScalarMappable.to_rgba`` respektuje
    set_under / set_bad palety.
    """
    vals = data.ravel()[flat_idx]          # (H, W) — wartości z natywnej siatki
    vals[~valid] = np.nan                  # poza domeną radaru → przezroczyste
    # NaN trzeba ZAMASKOWAĆ: to_rgba na surowej tablicy z BoundaryNorm renderuje
    # NaN jako kolor (alpha=255), dopiero masked array uruchamia set_bad (alpha=0).
    vals = np.ma.masked_invalid(vals)
    rgba = plt.cm.ScalarMappable(norm=norm, cmap=cmap).to_rgba(vals, bytes=True)
    Image.fromarray(rgba, "RGBA").save(str(output_path), "PNG")


# ── Funkcje pomocnicze ───────────────────────────────────────────────────────

def read_compo_h5(filepath: str) -> dict:
    """
    Wczytuje plik HDF5 kompozytu (CAPPI / DPSRI / SRI).

    Zwraca słownik z:
        data      – macierz w jednostkach fizycznych (np. mm/h), flip do osi Y↑
        projdef   – proj4 natywnej projekcji radaru
        xsize / ysize – wymiary siatki
        x1/y1/x2/y2  – obwiednia w metrach (projekcja natywna)
        timestamp – czas skanu (datetime UTC)
    """
    with h5py.File(filepath, "r") as f:
        datasets = sorted(k for k in f.keys() if "dataset" in k.lower())
        ds = datasets[0]

        what  = f[f"{ds}/what"]
        where = f.get("where")
        if where is None or "xsize" not in where.attrs:
            where = f[f"{ds}/where"]

        gain     = float(what.attrs["gain"])
        offset   = float(what.attrs["offset"])
        nodata   = float(what.attrs["nodata"])
        undetect = float(what.attrs["undetect"])

        raw = f[f"/{ds}/data1/data"][:].astype(np.float32)
        raw[raw == nodata]   = np.nan
        raw[raw == undetect] = np.nan
        data = np.flipud(raw * gain + offset)          # oś Y: południe→północ

        projdef = where.attrs["projdef"].decode().replace(
            "+ellps=sphere", "+R=6378137 +nadgrids=@null +no_defs"
        )
        xsize = int(where.attrs["xsize"])
        ysize = int(where.attrs["ysize"])

        to_native = Transformer.from_proj(
            Proj("EPSG:4326"), Proj(projdef), always_xy=True
        )

        def _corner(key):
            return to_native.transform(
                float(where.attrs[f"{key}_lon"]),
                float(where.attrs[f"{key}_lat"]),
            )

        corners = {k: _corner(k) for k in ("UL", "UR", "LL", "LR")}
        xs = [v[0] for v in corners.values()]
        ys = [v[1] for v in corners.values()]
        x1, x2 = min(xs), max(xs)
        y1, y2 = min(ys), max(ys)

        startdate = what.attrs["startdate"].decode()
        starttime = what.attrs["starttime"].decode()
        ts = datetime.strptime(f"{startdate}{starttime}", "%Y%m%d%H%M%S")

    return {
        "data": data.astype(np.float64),
        "projdef": projdef,
        "xsize": xsize, "ysize": ysize,
        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        "timestamp": ts,
    }


def build_pysteps_metadata(info: dict, timestamps: list[datetime]) -> dict:
    """Buduje słownik metadanych wymagany przez pysteps."""
    xpixelsize = (info["x2"] - info["x1"]) / info["xsize"]
    ypixelsize = (info["y2"] - info["y1"]) / info["ysize"]
    return {
        "projection":  info["projdef"],
        "x1": info["x1"], "x2": info["x2"],
        "y1": info["y1"], "y2": info["y2"],
        "xpixelsize":  xpixelsize,
        "ypixelsize":  ypixelsize,
        "yorigin":     "lower",
        "unit":        "mm/h",
        "transform":   None,
        "zerovalue":   0.0,
        "threshold":   0.1,
        "timestamps":  timestamps,
        "shape":       (info["ysize"], info["xsize"]),
    }


def load_voivodeships(native_crs_proj4: str) -> gpd.GeoDataFrame | None:
    """
    Ładuje granice województw i reprojekcjonuje je do natywnego układu radaru.
    Próbuje kolejno: cartopy → URL naturalearth.
    """
    gdf = None

    # Próba 1: cartopy (pobiera i cachuje naturalearth automatycznie)
    try:
        import cartopy.io.shapereader as shpreader
        shp = shpreader.natural_earth(
            resolution="10m", category="cultural",
            name="admin_1_states_provinces"
        )
        gdf = gpd.read_file(shp)
        gdf = gdf[gdf["admin"] == "Poland"].copy()
        log.info("Załadowano %d województw (cartopy naturalearth)", len(gdf))
    except Exception as e:
        log.warning("cartopy niedostępne lub błąd: %s", e)

    # Próba 2: bezpośredni URL
    if gdf is None or gdf.empty:
        url = (
            "https://naciscdn.org/naturalearth/10m/cultural/"
            "ne_10m_admin_1_states_provinces.zip"
        )
        try:
            log.info("Pobieranie granic województw z URL...")
            tmp = gpd.read_file(url)
            gdf = tmp[tmp["admin"] == "Poland"].copy()
            log.info("Załadowano %d województw (URL)", len(gdf))
        except Exception as e:
            log.warning("Nie udało się pobrać granic: %s", e)
            return None

    if gdf.empty:
        log.warning("Brak województw w danych — pominięto granice")
        return None

    try:
        target_crs = CRS.from_proj4(native_crs_proj4)
        return gdf.to_crs(target_crs)
    except Exception as e:
        log.warning("Błąd reprojekcji granic: %s", e)
        return None


# ── Główna logika ────────────────────────────────────────────────────────────

def prune_radar_cache(cache_dir: Path = RADAR_CACHE_DIR, keep: int = RADAR_CACHE_KEEP) -> int:
    """
    Zostawia ``keep`` najnowszych plików w cache radarowym, usuwa starsze.
    Nazwy plików IMGW zaczynają się od 14-cyfrowego znacznika czasu, więc
    sortowanie po nazwie malejąco = od najnowszego do najstarszego.
    """
    cache_dir = Path(cache_dir)
    if not cache_dir.exists():
        return 0
    files = sorted((f for f in cache_dir.iterdir() if f.is_file()),
                   key=lambda f: f.name, reverse=True)
    removed = 0
    for f in files[keep:]:
        try:
            f.unlink()
            removed += 1
            log.debug("Cache: usunięto stary plik %s", f.name)
        except OSError as e:
            log.warning("Cache: nie udało się usunąć %s: %s", f.name, e)
    if removed:
        log.info("Cache radarowy: usunięto %d starych plików (zostaje %d).",
                 removed, min(len(files), keep))
    return removed


def fetch_radar_scans(
    client: ImgwClient,
    n_frames: int = N_FRAMES,
    cache_dir: Path = RADAR_CACHE_DIR,
    keep: int = RADAR_CACHE_KEEP,
) -> list[tuple[datetime, str]]:
    """
    Zwraca posortowaną listę ``(timestamp, ścieżka)`` ostatnich ``n_frames``
    skanów kompozytu DPSRI, korzystając z lokalnego cache w ``cache_dir``.

    Pobiera tylko pliki, których nie ma jeszcze w cache, a na końcu przycina
    cache do ``keep`` najnowszych plików.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    log.info("Pobieranie listy plików COMPO_DPSRI...")
    df = client.get_file_list(COMPO_PATH)
    if df is None or df.empty:
        log.error("API IMGW nie zwróciło żadnych plików dla: %s", COMPO_PATH)
        sys.exit(1)

    df = df.dropna(subset=["timestamp"]).sort_values("timestamp").tail(n_frames)
    log.info("Wybrano %d skanów: %s → %s", len(df),
             df["timestamp"].iloc[0].strftime("%Y-%m-%d %H:%M"),
             df["timestamp"].iloc[-1].strftime("%Y-%m-%d %H:%M"))

    def _cached(row) -> bool:
        local = cache_dir / row["filename"]
        return local.exists() and local.stat().st_size > 0

    to_download = [row for _, row in df.iterrows() if not _cached(row)]
    n_cached = len(df) - len(to_download)
    log.info("Cache radarowy: %d/%d z dysku, %d do pobrania.",
             n_cached, len(df), len(to_download))

    if to_download:
        def _download(row):
            ok = client.download_file(row["url"], str(cache_dir / row["filename"]))
            return row["filename"], ok

        with ThreadPoolExecutor(max_workers=len(to_download)) as ex:
            for fname, ok in ex.map(_download, to_download):
                log.info("  %s %s", "↓" if ok else "✗", fname)

    # Zbuduj rekordy z plików obecnych w cache
    records = [
        (row["timestamp"], str(cache_dir / row["filename"]))
        for _, row in df.iterrows()
        if _cached(row)
    ]
    if len(records) < 3:
        log.error("Za mało danych (min. 3 skany), dostępnych %d", len(records))
        sys.exit(1)
    records.sort(key=lambda x: x[0])

    prune_radar_cache(cache_dir, keep)
    return records


def compute_linda(R_obs: np.ndarray, V: np.ndarray, kmperpixel: float) -> np.ndarray | None:
    """
    Deterministyczna prognoza LINDA (Lagrangian INtegro-Difference equation model
    with Autoregression). Działa na natężeniu opadu w jednostkach liniowych
    (mm/h, NIE dB) — inaczej niż S-PROG/STEPS.

    feature_method="shitomasi" → detektor cech OpenCV (cv2.goodFeaturesToTrack);
    paczka opencv jest już zależnością projektu, więc nie wymaga scikit-image
    (potrzebnego dla domyślnego "blob").

    Zwraca (N_LEADTIMES, ysize, xsize) mm/h, albo ``None`` gdy LINDA się nie
    powiedzie (np. brak cech przy znikomym opadzie) — wtedy produkt jest pomijany.
    """
    R_lin = np.nan_to_num(R_obs, nan=0.0)
    R_lin[R_lin < 0] = 0.0
    log.info("Obliczanie LINDA (deterministyczna, feature=shitomasi)...")
    try:
        linda_fct = nowcasts.get_method("linda")
        R_linda = linda_fct(
            R_lin[-3:, :, :],
            V,
            N_LEADTIMES,
            feature_method="shitomasi",
            add_perturbations=False,
            kmperpixel=kmperpixel,
            timestep=TIMESTEP,
            num_workers=os.cpu_count() or 1,
        )
        # float32 wystarcza dla natężenia opadu — o połowę mniej RAM (ważne na RPi).
        return np.maximum(np.nan_to_num(np.asarray(R_linda, dtype=np.float32), nan=0.0), 0.0)
    except Exception as e:
        log.warning("LINDA nie powiodła się (%s) — produkt 'linda' pominięty.", e)
        return None


def compute_anvil(R_obs: np.ndarray, V: np.ndarray) -> np.ndarray | None:
    """
    Deterministyczna prognoza ANVIL (Autoregressive Nowcasting using VIL).

    ANVIL dopasowuje model autoregresyjny AR(2) w przestrzennie zdekomponowanym
    polu, dzięki czemu lepiej oddaje rozwój i zanik komórek niż czysta
    ekstrapolacja. Działa na natężeniu opadu w jednostkach liniowych (mm/h, NIE
    dB) — podobnie jak LINDA.

    ``rainrate=None`` jest **istotne**: pole wejściowe karmimy wprost jako mm/h i
    NIE włączamy konwersji R(VIL). Konwersja R=a·VIL+b ma zaszyty próg ``VIL > 10``
    (kalibrowany pod jednostki VIL, kg/m²); podanie naszego mm/h jako „VIL"
    zerowałoby cały opad ≤ 10 mm/h, zostawiając tylko duże obszary ulewy. Bez
    konwersji wynik jest w jednostkach wejścia (mm/h), a ``apply_rainrate_mask``
    zeruje jedynie piksele < 0.1 mm/h (jak próg opadu w S-PROG).

    Wymaga co najmniej ``ar_order + 2`` = 4 pól wejściowych (model liczy pochodne
    czasowe). Zwraca (N_LEADTIMES, ysize, xsize) mm/h, albo ``None`` gdy ANVIL się
    nie powiedzie — wtedy produkt ``anvil`` jest pomijany.
    """
    R_lin = np.nan_to_num(R_obs, nan=0.0)
    R_lin[R_lin < 0] = 0.0
    log.info("Obliczanie ANVIL (deterministyczna, AR-2)...")
    try:
        anvil_fct = nowcasts.get_method("anvil")
        R_anvil = anvil_fct(
            R_lin[-4:, :, :],
            V,
            N_LEADTIMES,
            rainrate=None,
            n_cascade_levels=4,
            ar_order=2,
            ar_window_radius=25,
            fft_method="numpy",
            apply_rainrate_mask=True,
            num_workers=os.cpu_count() or 1,
        )
        # float32 wystarcza dla natężenia opadu — o połowę mniej RAM (ważne na RPi).
        return np.maximum(np.nan_to_num(np.asarray(R_anvil, dtype=np.float32), nan=0.0), 0.0)
    except Exception as e:
        log.warning("ANVIL nie powiodła się (%s) — produkt 'anvil' pominięty.", e)
        return None


def main(ensemble: bool = False, linda: bool = True, anvil: bool = False,
         sprog: bool = True) -> None:
    """
    Uruchamia pipeline prognozy i renderuje overlaye Leaflet.

    Parametry
    ---------
    ensemble:
        Gdy ``False`` (domyślnie) nie liczy stochastycznego ensemblu STEPS. Gdy
        ``True`` uruchamia ensemble STEPS i generuje overlaye ``mean`` oraz
        prawdopodobieństwa ``prob01`` / ``prob10`` (wolniej).
    linda:
        Gdy ``True`` (domyślnie) dodatkowo liczy deterministyczną prognozę LINDA
        (overlaye ``linda``). Gdy ``False`` pomija LINDA (szybciej).
    anvil:
        Gdy ``True`` dodatkowo liczy deterministyczną prognozę ANVIL
        (overlaye ``anvil``). Domyślnie ``False``.
    sprog:
        Gdy ``True`` (domyślnie) liczy deterministyczną prognozę S-PROG
        (overlaye ``det``). Gdy ``False`` pomija S-PROG — pozwala np. wygenerować
        wyłącznie ANVIL (``--no-sprog --anvil``).

    Musi pozostać włączona co najmniej jedna metoda (S-PROG / LINDA / ANVIL /
    ensemble), inaczej nie powstałby żaden produkt.
    """
    if not (sprog or linda or anvil or ensemble):
        log.error("Wyłączono wszystkie metody — włącz co najmniej jedną "
                  "(S-PROG / LINDA / ANVIL / ensemble).")
        sys.exit(1)
    # 1-2. Lista + pobranie skanów DPSRI (z cache) ───────────────────────────
    client = ImgwClient()
    records = fetch_radar_scans(client)

    # 3. Wczytaj HDF5 → macierze mm/h ────────────────────────────────────────
    log.info("Dekodowanie plików HDF5...")
    frames = [read_compo_h5(path) for _, path in records]
    info       = frames[0]          # geometria siatki (wspólna dla wszystkich)
    timestamps = [f["timestamp"] for f in frames]

    # 4. Budowanie macierzy 3D – dane już w mm/h, ujemne wartości → NaN ──────
    R_obs = np.stack([f["data"] for f in frames], axis=0).astype(np.float64)
    R_obs[R_obs < 0] = np.nan
    # shape: (N_FRAMES, ysize, xsize)

    metadata = build_pysteps_metadata(info, timestamps)

    # 5. Transformacja logarytmiczna dBR (wymagana przez STEPS) ──────────────
    R_dBR, meta_dBR = transformation.dB_transform(
        R_obs.copy(), metadata.copy(),
        threshold=PRECIP_THR_MM, zerovalue=ZEROVALUE_DBR
    )
    R_dBR[~np.isfinite(R_dBR)] = ZEROVALUE_DBR

    # 6. Pole ruchu – Lucas-Kanade ────────────────────────────────────────────
    log.info("Estymacja pola ruchu (Lucas-Kanade)...")
    V = dense_lucaskanade(R_dBR)

    kmperpixel = (info["x2"] - info["x1"]) / info["xsize"] / 1000.0
    log.info(
        "Rozdzielczość siatki: %.2f km/px  |  Siatka: %d × %d",
        kmperpixel, info["xsize"], info["ysize"],
    )

    # 7. S-PROG – prognoza deterministyczna (opcjonalna) ──────────────────────
    # Ekstrapolacja po polu ruchu V z progresywnym wygładzaniem nieprzewidywalnych
    # małych skal — "najlepsza pojedyncza prognoza", bez szumu stochastycznego.
    R_det: np.ndarray | None = None
    if sprog:
        log.info("Obliczanie S-PROG (deterministyczna)...")
        sprog_method = nowcasts.get_method("sprog")
        R_det = sprog_method(
            R_dBR[-3:, :, :],
            V,
            N_LEADTIMES,
            n_cascade_levels=10,
            precip_thr=PRECIP_THR_DBR,
        )
        R_det = transformation.dB_transform(R_det, threshold=PRECIP_THR_DBR, inverse=True)[0]
        R_det = R_det.astype(np.float32)   # float32 — o połowę mniej RAM
        # R_det shape: (N_LEADTIMES, ysize, xsize)

    # 7b. LINDA – druga deterministyczna metoda (opcjonalnie) ─────────────────
    R_linda = compute_linda(R_obs, V, kmperpixel) if linda else None

    # 7c. ANVIL – trzecia deterministyczna metoda (opcjonalnie) ───────────────
    R_anvil = compute_anvil(R_obs, V) if anvil else None

    # 8. STEPS – ensemble stochastyczny (opcjonalnie: mean + prawdopodobieństwa)
    R_mean: np.ndarray | None = None
    P_all:  np.ndarray | None = None

    if ensemble:
        n_workers = os.cpu_count() or 1
        log.info(
            "Obliczanie STEPS: %d kroków × %d min, %d członków ensemblu (%d wątków)...",
            N_LEADTIMES, TIMESTEP, N_ENS_MEMBERS, n_workers,
        )
        steps = nowcasts.get_method("steps")
        R_f = steps(
            R_dBR[-3:, :, :],           # ostatnie 3 skany do inicjalizacji
            V,
            N_LEADTIMES,
            N_ENS_MEMBERS,
            n_cascade_levels=4,
            precip_thr=PRECIP_THR_DBR,
            kmperpixel=kmperpixel,
            timestep=TIMESTEP,
            noise_method="nonparametric",
            vel_pert_method="bps",
            mask_method="incremental",
            num_workers=n_workers,      # równoległe generowanie członków ensemblu
            seed=SEED,
        )
        # R_f shape: (N_ENS_MEMBERS, N_LEADTIMES, ysize, xsize)
        R_f = transformation.dB_transform(R_f, threshold=PRECIP_THR_DBR, inverse=True)[0]
        R_mean = np.nanmean(R_f, axis=0).astype(np.float32)   # (N_LEADTIMES, ysize, xsize)

        log.info("Obliczanie prawdopodobieństw przekroczenia progów...")
        P_all = np.stack([
            np.stack([excprob(R_f[:, i, :, :], thr) for i in range(N_LEADTIMES)])
            for thr, _, _ in PROB_THRESHOLDS
        ])
    else:
        log.info("Pomijanie ensemblu STEPS — tylko prognoza deterministyczna (S-PROG).")

    log.info("Prognoza gotowa. Przygotowywanie overlayów...")

    # 9. Granice województw ───────────────────────────────────────────────────
    """Ładuje granice województw i reprojekcjonuje je do natywnego układu radaru.
    gdf_voi = load_voivodeships(info["projdef"])

    # 10. Siatka współrzędnych do pcolormesh ──────────────────────────────────
    xs = np.linspace(info["x1"], info["x2"], info["xsize"] + 1)
    ys = np.linspace(info["y1"], info["y2"], info["ysize"] + 1)
    X, Y = np.meshgrid(xs, ys)

    # 11. Wykres 4 × 3 (12 kroków: +5 … +60 min) ─────────────────────────────
    fig, axes = plt.subplots(
        3, 4, figsize=(24, 18), dpi=120,
        constrained_layout=True,
        facecolor="#1C1C1C",
    )

    last_ts   = timestamps[-1]
    base_bg   = "#2A2A2A"

    for i, ax in enumerate(axes.flat):
        lead_min = (i + 1) * TIMESTEP
        fc_str   = (last_ts + timedelta(minutes=lead_min)).strftime("%H:%M UTC")

        ax.set_facecolor(base_bg)
        ax.pcolormesh(
            X, Y, R_mean[i],
            cmap=RAIN_CMAP, norm=RAIN_NORM,
            shading="flat", rasterized=True,
        )

        if gdf_voi is not None:
            gdf_voi.boundary.plot(
                ax=ax, color="#CCCCCC", linewidth=0.6, zorder=5
            )

        ax.set_xlim(info["x1"], info["x2"])
        ax.set_ylim(info["y1"], info["y2"])
        ax.set_aspect("equal")
        ax.set_xticks([])
        ax.set_yticks([])

        ax.set_title(
            f"+{lead_min} min  ({fc_str})",
            color="white", fontsize=10, fontweight="bold", pad=4,
        )

    # Legenda ─────────────────────────────────────────────────────────────────
    sm = plt.cm.ScalarMappable(cmap=RAIN_CMAP, norm=RAIN_NORM)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=axes, fraction=0.018, pad=0.02, extend="max")
    cbar.set_label("Natężenie opadu [mm/h]", color="white",
                   fontsize=13, fontweight="bold", labelpad=10)
    cbar.ax.yaxis.set_tick_params(color="white")
    plt.setp(cbar.ax.yaxis.get_ticklabels(), color="white", fontsize=9)
    cbar.set_ticks(_RAIN_BOUNDS)
    cbar.set_ticklabels([str(b) for b in _RAIN_BOUNDS])

    # Tytuł główny ────────────────────────────────────────────────────────────
    last_ts_str = last_ts.strftime("%d-%m-%Y %H:%M UTC")
    fig.suptitle(
        f"STEPS – Stochastic Nowcast  |  Ensemble mean\n"
        f"Kompozyt DPSRI IMGW  ·  Ostatni skan: {last_ts_str}  ·  "
        f"Prognoza: +5 … +60 min  (krok {TIMESTEP} min)  ·  "
        f"{N_ENS_MEMBERS} członków ensemblu",
        color="white", fontsize=14, fontweight="bold",
        y=1.01,
    )
    fig.patch.set_facecolor("#1C1C1C")

    # Zapis ───────────────────────────────────────────────────────────────────
    out_path = Path("steps_nowcast_dpsri.png")
    plt.savefig(out_path, dpi=120, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    log.info("Zapisano: %s", out_path.resolve())

    # 12. Prawdopodobieństwo przekroczenia progów ──────────────────────────────
    log.info("Obliczanie prawdopodobieństw przekroczenia progów...")
    n_thr = len(PROB_THRESHOLDS)

    # shape wynikowa: (n_thr, N_LEADTIMES, ysize, xsize)
    P_all = np.stack([
        np.stack([excprob(R_f[:, i, :, :], thr) for i in range(N_LEADTIMES)])
        for thr, _, _ in PROB_THRESHOLDS
    ])

    # 13. Wykres prawdopodobieństw – n_thr sekcje po 4×3 każda ───────────────
    fig_p, axes_p = plt.subplots(
        n_thr * 3, 4,
        figsize=(24, n_thr * 14),
        dpi=120,
        constrained_layout=True,
        facecolor="#1C1C1C",
    )

    for t_idx, (thr, thr_label, prob_cmap) in enumerate(PROB_THRESHOLDS):
        for i in range(N_LEADTIMES):
            row = t_idx * 3 + i // 4
            col = i % 4
            ax  = axes_p[row, col]
            lead_min = (i + 1) * TIMESTEP
            fc_str   = (last_ts + timedelta(minutes=lead_min)).strftime("%H:%M UTC")

            ax.set_facecolor("#2A2A2A")
            ax.pcolormesh(
                X, Y, P_all[t_idx, i],
                cmap=prob_cmap, norm=PROB_NORM,
                shading="flat", rasterized=True,
            )

            if gdf_voi is not None:
                gdf_voi.boundary.plot(
                    ax=ax, color="#AAAAAA", linewidth=0.6, zorder=5
                )

            ax.set_xlim(info["x1"], info["x2"])
            ax.set_ylim(info["y1"], info["y2"])
            ax.set_aspect("equal")
            ax.set_xticks([])
            ax.set_yticks([])
            ax.set_title(
                f"+{lead_min} min  ({fc_str})",
                color="white", fontsize=10, fontweight="bold", pad=4,
            )

        # Pasek legendy osobny dla każdego progu
        sm = plt.cm.ScalarMappable(cmap=prob_cmap, norm=PROB_NORM)
        sm.set_array([])
        section_axes = axes_p[t_idx * 3 : t_idx * 3 + 3, :]
        cbar = fig_p.colorbar(sm, ax=section_axes, fraction=0.018, pad=0.02)
        cbar.set_label(
            f"Prawdopodobieństwo  [{thr_label.split('–')[0].strip()}]",
            color="white", fontsize=12, fontweight="bold", labelpad=10,
        )
        cbar.ax.yaxis.set_tick_params(color="white")
        plt.setp(cbar.ax.yaxis.get_ticklabels(), color="white", fontsize=9)
        cbar.set_ticks(PROB_TICKS)
        cbar.set_ticklabels([f"{int(v*100)}%" for v in PROB_TICKS])

        # Nagłówek sekcji
        axes_p[t_idx * 3, 0].annotate(
            thr_label,
            xy=(0, 1.08), xycoords="axes fraction",
            color="#FFD700", fontsize=12, fontweight="bold",
        )

    fig_p.suptitle(
        f"STEPS – Stochastic Nowcast  |  Prawdopodobieństwo przekroczenia progu\n"
        f"Kompozyt DPSRI IMGW  ·  Ostatni skan: {last_ts_str}  ·  "
        f"{N_ENS_MEMBERS} członków ensemblu",
        color="white", fontsize=14, fontweight="bold",
        y=1.005,
    )
    fig_p.patch.set_facecolor("#1C1C1C")

    out_prob = Path("steps_nowcast_dpsri_prob.png")
    plt.savefig(out_prob, dpi=120, bbox_inches="tight",
                facecolor=fig_p.get_facecolor())
    plt.close(fig_p)
    log.info("Zapisano: %s", out_prob.resolve())
    """

    last_ts = timestamps[-1]

    # Zwolnij duże tablice pośrednie niepotrzebne do renderu — niższy szczyt
    # pamięci (istotne na RPi). Render używa tylko frames + gotowych prognoz.
    del R_obs, R_dBR, V, metadata
    gc.collect()

    # 14. Overlaye Leaflet — typy: det (S-PROG) + linda (LINDA) + anvil (ANVIL) ─
    extra = []
    if R_linda is not None:
        extra.append(("linda", R_linda))
    if R_anvil is not None:
        extra.append(("anvil", R_anvil))
    generate_leaflet_overlays(frames, R_mean, R_det, P_all, info, last_ts,
                              extra_dets=extra)

    # 15. Alerty Telegram o silnym opadzie (STEPS / LINDA / ANVIL) ─────────────
    try:
        forecasts = {}
        if R_det is not None:
            forecasts["STEPS"] = R_det
        if R_linda is not None:
            forecasts["LINDA"] = R_linda
        if R_anvil is not None:
            forecasts["ANVIL"] = R_anvil
        notify_precip_alerts(forecasts, info, last_ts, timestep=TIMESTEP,
                             obs_field=frames[-1]["data"])
    except Exception as exc:
        log.warning("Powiadomienia Telegram pominięte: %s", exc)


def generate_leaflet_overlays(
    frames: list[dict],
    R_mean: np.ndarray | None,
    R_det: np.ndarray | None,
    P_all: np.ndarray | None,
    info: dict,
    last_ts: datetime,
    extra_dets: list[tuple[str, np.ndarray]] | None = None,
    meta: dict | None = None,
) -> None:
    """
    Generuje transparentne PNG w EPSG:3857 do www/data/ oraz bounds.json.

    Konwencja nazw:
      obs_YYYYMMDD_HHMM.png                   – obserwowany skan (mm/h)
      fcst_YYYYMMDD_HHMM_plus_NNN_mean.png    – ensemble mean (mm/h)
      fcst_YYYYMMDD_HHMM_plus_NNN_det.png     – S-PROG deterministyczna (mm/h)
      fcst_YYYYMMDD_HHMM_plus_NNN_prob01.png  – P(R > 0.1 mm/h)
      fcst_YYYYMMDD_HHMM_plus_NNN_prob10.png  – P(R > 10 mm/h)

    P_all shape: (2, N_LEADTIMES, ysize, xsize)
      P_all[0] = P > 0.1 mm/h,  P_all[1] = P > 10 mm/h

    extra_dets:
        Dodatkowe deterministyczne produkty (suffix, stack mm/h) renderowane tą
        samą paletą RATE — np. [("icon", R_det_blend)] dla wariantu z ICON-EU.
        Pozwala zapisać kilka „typów” prognozy obok siebie (np. det + icon).
    """
    WWW_DATA_DIR.mkdir(parents=True, exist_ok=True)

    X_3857, Y_3857, bounds = compute_epsg3857_mesh(info)
    # Reprojekcję liczymy RAZ jako mapę indeksów — render każdej klatki to potem
    # samo indeksowanie numpy (zob. build_warp_index / render_overlay_png).
    warp_idx, warp_valid, _W, _H = build_warp_index(info, X_3857, Y_3857)
    with open(WWW_DATA_DIR / "bounds.json", "w") as fh:
        json.dump(bounds, fh)

    # Manifest: pełny indeks wygenerowanych plików (front i PHP czytają to zamiast
    # globować katalog → atomowe przełączenie nowych danych na serwerze).
    manifest: dict = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bounds": bounds,
        "meta": meta,
        "obs": [],
        "fcst": {},
    }

    # Buduj listę zadań renderowania. Trzymamy TYLKO referencje do już istniejących
    # tablic (+ indeks klatki) — maskę liczymy leniwie w wątku renderu. Wcześniejsze
    # materializowanie ~80 kopii 800×800 naraz zjadało setki MB i ubijało RPi (OOM).
    base_dt  = last_ts.replace(second=0, microsecond=0)
    base_str = base_dt.strftime("%Y%m%d_%H%M")
    # Task = (źródło, idx|None, ścieżka, cmap, norm); idx=None → źródło jest 2D (obs).
    render_tasks: list[tuple[np.ndarray, int | None, Path, object, object]] = []

    for frame in frames:
        ts = frame["timestamp"].replace(second=0, microsecond=0)
        ts_str = ts.strftime("%Y%m%d_%H%M")
        name = f"obs_{ts_str}.png"
        render_tasks.append((frame["data"], None, WWW_DATA_DIR / name, RATE_CMAP, RATE_NORM))
        manifest["obs"].append({
            "file":  name,
            "time":  ts.strftime("%Y-%m-%dT%H:%M:00Z"),
            "label": ts.strftime("%H:%M UTC"),
        })

    overlay_specs = []
    if R_det is not None:
        overlay_specs.append(("det", R_det, RATE_CMAP, RATE_NORM))
    if R_mean is not None:
        overlay_specs.append(("mean", R_mean, RATE_CMAP, RATE_NORM))
    if P_all is not None:
        overlay_specs += [
            ("prob01", P_all[0],  PROB01_CMAP, PROB01_NORM),
            ("prob10", P_all[1],  PROB10_CMAP, PROB10_NORM),
        ]
    for suffix, stack in (extra_dets or []):
        overlay_specs.append((suffix, stack, RATE_CMAP, RATE_NORM))

    for suffix, stack, cmap, norm in overlay_specs:
        entries = []
        for i in range(N_LEADTIMES):
            lead_min = (i + 1) * TIMESTEP
            name = f"fcst_{base_str}_plus_{lead_min:03d}_{suffix}.png"
            render_tasks.append((stack, i, WWW_DATA_DIR / name, cmap, norm))
            valid = base_dt + timedelta(minutes=lead_min)
            entries.append({
                "file":     name,
                "time":     valid.strftime("%Y-%m-%dT%H:%M:00Z"),
                "label":    f"{valid.strftime('%H:%M')} UTC (+{lead_min}min)",
                "lead_min": lead_min,
            })
        manifest["fcst"][suffix] = entries

    # Renderowanie równoległe – operacje numpy/PIL zwalniają GIL, więc wątki
    # realnie przyspieszają (a nie ma już ciężkiego matplotlib pcolormesh).
    # Maskę liczymy tu (leniwie), żeby nie trzymać wszystkich kopii naraz.
    def _render(task):
        arr, idx, path, cmap, norm = task
        src = arr if idx is None else arr[idx]
        data = np.where(np.isfinite(src) & (src > 0), src, np.nan)
        render_overlay_png(data, warp_idx, warp_valid, path, cmap, norm)
        return path.name

    # Liczbę wątków renderu można ograniczyć przez STEPS_RENDER_WORKERS — na RPi
    # mniej wątków = niższy szczyt pamięci (każdy alokuje przejściowy bufor RGBA).
    _env_workers = os.getenv("STEPS_RENDER_WORKERS")
    if _env_workers and _env_workers.isdigit() and int(_env_workers) > 0:
        n_render = min(int(_env_workers), len(render_tasks))
    else:
        n_render = min(os.cpu_count() or 4, 4, len(render_tasks))
    log.info("Renderowanie %d PNG równolegle (%d wątki)...",
             len(render_tasks), n_render)

    with ThreadPoolExecutor(max_workers=n_render) as ex:
        for name in ex.map(_render, render_tasks):
            log.debug("  ✓ %s", name)

    # Manifest zapisujemy NA KOŃCU — po wyrenderowaniu wszystkich PNG.
    with open(WWW_DATA_DIR / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False)

    n_products = len(overlay_specs)
    log.info("Overlaye gotowe: %d obs + %d×%d fcst (+ manifest.json)  →  www/data/",
             len(frames), N_LEADTIMES, n_products)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Prognoza DPSRI IMGW (overlaye Leaflet). "
                    "Domyślnie tylko S-PROG (deterministyczna)."
    )
    parser.add_argument(
        "--ensemble", dest="ensemble", action="store_true",
        help="Dodatkowo policz ensemble STEPS: mean + prawdopodobieństwa (wolniej).",
    )
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--linda", dest="linda", action="store_true",
                     help="Generuj też prognozę LINDA (domyślnie włączone).")
    grp.add_argument("--no-linda", dest="linda", action="store_false",
                     help="Pomiń LINDA (szybciej).")
    parser.set_defaults(linda=True)
    parser.add_argument("--anvil", dest="anvil", action="store_true",
                        help="Generuj też prognozę ANVIL (AR-2; domyślnie wyłączone).")
    parser.set_defaults(anvil=False)
    parser.add_argument("--no-sprog", dest="sprog", action="store_false",
                        help="Pomiń S-PROG (produkt 'det') — np. tylko ANVIL: --no-sprog --anvil.")
    parser.set_defaults(sprog=True)
    args = parser.parse_args()

    main(ensemble=args.ensemble, linda=args.linda, anvil=args.anvil, sprog=args.sprog)
