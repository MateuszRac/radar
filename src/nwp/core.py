"""
Moduł NWP – narzędzia do pobierania i zarządzania danymi numerycznych
prognoz pogody (Numerical Weather Prediction) z publicznych serwerów.

Obsługiwane źródła:
    - DWD (Deutscher Wetterdienst) – serwer opendata
"""

from __future__ import annotations

import bz2
import re
from pathlib import Path
from typing import Iterable

import eccodes
from .helpers import clean_grib_dir
import numpy as np
import os
import pandas as pd
import requests
import xarray as xr
import yaml
from dotenv import load_dotenv


# Bazowy URL serwera NWP DWD
_DWD_NWP_BASE_URL = "https://opendata.dwd.de/weather/nwp/"

# Stała słoneczna [W/m²]
_SOLAR_CONST = 1361.0

# Domyślny współczynnik zamętnienia Linkego dla Polski (~49–55°N, kontynent)
# T_L ≈ 2.0–2.5: czyste powietrze po frontach, zima
# T_L ≈ 3.0–3.5: typowy słoneczny dzień, umiarkowana wilgotność
# T_L ≈ 4.0–5.0: letni, wilgotny, aerozole miejskie
_LINKE_TURBIDITY_PL = 3.0


def _etr_horizontal(
    forecast_time: pd.Series,
    lat_deg: pd.Series,
    lon_deg: pd.Series,
) -> np.ndarray:
    """
    Pozaatmosferyczne poziome natężenie promieniowania słonecznego (ETR) [W/m²].

    Dla wartości godzinowych (aswdir_s_hourly reprezentuje środek godziny)
    czas obliczeniowy przesuwa się o -30 min względem forecast_time.

    Zwraca tablicę ETR ≥ 0 (nocne wartości = 0).
    """
    t_mid = forecast_time - pd.Timedelta(minutes=30)
    doy   = t_mid.dt.dayofyear.values.astype(float)
    hour_utc = t_mid.dt.hour.values + t_mid.dt.minute.values / 60.0

    lat_r = np.radians(lat_deg.values.astype(float))
    lon_d = lon_deg.values.astype(float)

    B = 2 * np.pi * (doy - 1) / 365.0

    # Korekcja odległości Ziemia–Słońce (Iqbal 1983)
    E0 = (1.000110 + 0.034221 * np.cos(B) + 0.001280 * np.sin(B)
          + 0.000719 * np.cos(2 * B) + 0.000077 * np.sin(2 * B))

    # Deklinacja słoneczna [rad] (Spencer 1971)
    dec = (0.006918 - 0.399912 * np.cos(B) + 0.070257 * np.sin(B)
           - 0.006758 * np.cos(2 * B) + 0.000907 * np.sin(2 * B)
           - 0.002697 * np.cos(3 * B) + 0.001480 * np.sin(3 * B))

    # Równanie czasu [min]
    eot = 229.18 * (0.000075 + 0.001868 * np.cos(B) - 0.032077 * np.sin(B)
                    - 0.014615 * np.cos(2 * B) - 0.040890 * np.sin(2 * B))

    # Czas słoneczny [h]
    solar_time = hour_utc + lon_d / 15.0 + eot / 60.0

    # Kąt godzinny [rad] (0 = południe słoneczne)
    hour_angle = np.radians((solar_time - 12.0) * 15.0)

    # cos(kąt zenitalny)
    cos_z = (np.sin(lat_r) * np.sin(dec)
             + np.cos(lat_r) * np.cos(dec) * np.cos(hour_angle))

    return (_SOLAR_CONST * E0 * np.maximum(cos_z, 0.0))


def _clear_sky_direct_horizontal(
    forecast_time: pd.Series,
    lat_deg: pd.Series,
    lon_deg: pd.Series,
    T_L: float = _LINKE_TURBIDITY_PL,
) -> np.ndarray:
    """
    Poziome promieniowanie bezpośrednie przy bezchmurnym niebie [W/m²].

    Model Ineichen & Perez (2002) z masą powietrzną Kastena-Younga (1989).
    Uwzględnia:
      - korekcję odległości Ziemia–Słońce (E0)
      - masę optyczną atmosfery m (dokładna formuła, ważna przy niskim słońcu)
      - optyczną grubość Rayleigha δ_cda(m) (Kasten 1996)
      - łączną absorbcję i rozpraszanie przez T_L (turbidity Linkego):
          Rayleigh + aerozole + para wodna + ozon — w jednym parametrze

    T_L dla Polski (49–55°N):
        ~2.5  po chłodnym froncie, zima, czyste powietrze
        ~3.0  typowy jasny dzień, umiarkowana wilgotność  ← wartość domyślna
        ~3.5  letni, wyższa wilgotność i aerozole
        ~5.0  zadymiony, bardzo wilgotny

    Przy T_L = 3.0 promieniowanie przy pełnym czystym niebie wynosi typowo
    ~75–85% ETR (zależy od kąta słońca), więc indeks klarowności osiąga ~100%
    właśnie przy bezchmurnym niebie.
    """
    doy      = forecast_time.dt.dayofyear.values.astype(float)
    hour_utc = forecast_time.dt.hour.values + forecast_time.dt.minute.values / 60.0

    lat_r = np.radians(lat_deg.values.astype(float))
    lon_d = lon_deg.values.astype(float)

    B = 2 * np.pi * (doy - 1) / 365.0

    # Korekcja odległości Ziemia–Słońce
    E0 = (1.000110 + 0.034221 * np.cos(B) + 0.001280 * np.sin(B)
          + 0.000719 * np.cos(2 * B) + 0.000077 * np.sin(2 * B))

    # Deklinacja słoneczna [rad]
    dec = (0.006918 - 0.399912 * np.cos(B) + 0.070257 * np.sin(B)
           - 0.006758 * np.cos(2 * B) + 0.000907 * np.sin(2 * B)
           - 0.002697 * np.cos(3 * B) + 0.001480 * np.sin(3 * B))

    # Równanie czasu [min]
    eot = 229.18 * (0.000075 + 0.001868 * np.cos(B) - 0.032077 * np.sin(B)
                    - 0.014615 * np.cos(2 * B) - 0.040890 * np.sin(2 * B))

    solar_time = hour_utc + lon_d / 15.0 + eot / 60.0
    hour_angle = np.radians((solar_time - 12.0) * 15.0)

    cos_z = (np.sin(lat_r) * np.sin(dec)
             + np.cos(lat_r) * np.cos(dec) * np.cos(hour_angle))

    sun_up    = cos_z > 0.0
    cos_z_pos = np.maximum(cos_z, 1e-6)   # unikamy dzielenia przez zero

    # Masa powietrzna (Kasten-Young 1989) — poprawna aż do horyzontu
    zenith_deg = np.degrees(np.arccos(np.clip(cos_z, -1.0, 1.0)))
    m = 1.0 / (
        cos_z_pos
        + 0.50572 * np.maximum(96.07995 - zenith_deg, 0.01) ** (-1.6364)
    )
    m = np.clip(m, 1.0, 40.0)   # >40 ≈ słońce pod horyzontem, wynik i tak = 0

    # Optyczna grubość Rayleigha δ_cda(m) (Kasten 1996), ważna dla m ∈ [1, 20]
    m2, m3, m4 = m**2, m**3, m**4
    delta_cda = 1.0 / (
        6.6296 + 1.7513 * m - 0.1202 * m2 + 0.0065 * m3 - 0.00013 * m4
    )

    # DNI przy czystym niebie [W/m²]
    dni_cs = _SOLAR_CONST * E0 * np.exp(-0.8662 * T_L * m * delta_cda)

    # Poziome promieniowanie bezpośrednie = DNI × cos(θz); nocą = 0
    I_b_cs = np.where(sun_up, dni_cs * cos_z, 0.0)
    return np.maximum(I_b_cs, 0.0)


# Mapowanie angielskich skrótów miesięcy → numer miesiąca, niezależne od locale
_MONTH_MAP: dict[str, int] = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5,  "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# Klucze metadanych wyciągane z każdej wiadomości GRIB
_GRIB_META_KEYS = [
    "shortName", "name", "units", "typeOfLevel", "level",
    "validDate", "dataDate", "dataTime", "stepRange", "stepType",
    "paramId", "parameterCategory", "parameterNumber", "centre",
]

_STATS_GROUP_COLS = [
    "shortName", "name", "units", "typeOfLevel", "level",
    "forecast_time", "target_name",
]


def _parse_dwd_date(date_str: str) -> pd.Timestamp:
    """
    Parsuje datę z listingu DWD w formacie '15-May-2026 11:39:33'.

    Używa ręcznego mapowania miesięcy zamiast strptime/%b, które jest
    zależne od locale systemowego i zawodzi przy polskim locale.
    """
    m = re.fullmatch(
        r"(\d{2})-([A-Za-z]{3})-(\d{4})\s+(\d{2}):(\d{2}):(\d{2})",
        date_str,
    )
    if not m:
        return pd.NaT
    day, mon_str, year, hour, minute, second = m.groups()
    month = _MONTH_MAP.get(mon_str.capitalize())
    if month is None:
        return pd.NaT
    return pd.Timestamp(int(year), month, int(day), int(hour), int(minute), int(second))


def load_config(name: str = "icon") -> dict:
    """
    Wczytuje konfigurację z folderu ``config/`` w katalogu projektu.

    Ładuje ``.env``, bierze ``PROJECT_ROOT`` i szuka pliku
    ``${PROJECT_ROOT}/config/{name}.yaml``.  Jeśli podasz pełną ścieżkę
    absolutną, użyje jej bezpośrednio.

    Parameters
    ----------
    name : str
        Nazwa pliku konfiguracyjnego bez rozszerzenia (np. ``'icon'``)
        lub pełna ścieżka absolutna do pliku YAML.

    Returns
    -------
    dict
        Słownik z konfiguracją gotowy do przekazania do :meth:`NWP.icon_compute_stats`.

    Examples
    --------
    >>> cfg = load_config()           # → PROJECT_ROOT/config/icon.yaml
    >>> cfg = load_config("arome")    # → PROJECT_ROOT/config/arome.yaml
    """
    load_dotenv()
    path = Path(name)
    if not path.is_absolute():
        project_root = Path(os.environ.get("PROJECT_ROOT", "."))
        if not path.suffix:
            path = path.with_suffix(".yaml")
        path = project_root / "config" / path
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    return yaml.safe_load(os.path.expandvars(raw))


class NWP:
    """
    Narzędzia do obsługi danych NWP z publicznych archiwów meteorologicznych.

    Przykład użycia::

        nwp = NWP()

        # lista plików GRIB dla modelu ICON-EU, godzina inicjalizacji 09 UTC, parametr runoff_s
        df = nwp.dwd_list_files("icon-eu/grib/09/runoff_s/")

        # pobierz i rozpakuj pliki do wskazanego folderu
        df_downloaded = nwp.dwd_download(df, save_path="data/dwd/icon", unzip=True)

        # ekstrakcja wartości ensembla w punktach z konfiguracji
        cfg = load_config("config/icon.yaml")
        df_raw = nwp.icon_extract_points(
            grib_files=df_downloaded["grib_path"],
            grid_file=cfg["grid_file"],
            points=cfg["points"],
            n_neighbors=cfg["n_neighbors"],
        )
        df_stats = nwp.icon_compute_stats(df_raw, cfg)
    """

    # ------------------------------------------------------------------
    # DWD (Deutscher Wetterdienst)
    # ------------------------------------------------------------------

    def dwd_list_files(self, path: str) -> pd.DataFrame:
        """
        Listuje pliki dostępne w katalogu DWD opendata NWP.

        Łączy się z serwerem DWD i parsuje listing Apache'a pod podaną
        ścieżką. Zwraca wyłącznie pliki (pomija podkatalogi).

        Parameters
        ----------
        path : str
            Względna ścieżka pod bazowym URL DWD NWP, np.
            ``'icon-eu/grib/09/runoff_s/'``.
            Skutkuje zapytaniem do:
            ``https://opendata.dwd.de/weather/nwp/icon-eu/grib/09/runoff_s/``

        Returns
        -------
        pd.DataFrame
            DataFrame z kolumnami:

            - ``name``         : nazwa pliku
            - ``full_path``    : pełny URL do pliku
            - ``date_created`` : znacznik czasu ostatniej modyfikacji (UTC),
                                 lub ``NaT`` jeśli niedostępny

        Raises
        ------
        requests.HTTPError
            Gdy serwer zwróci kod błędu HTTP.
        requests.Timeout
            Gdy połączenie przekroczy limit czasu.
        """
        url = _DWD_NWP_BASE_URL.rstrip("/") + "/" + path.lstrip("/")

        response = requests.get(url, timeout=30)
        response.raise_for_status()

        records = []

        # Apache autoindex generuje linie w formacie:
        #   <a href="nazwa_pliku">nazwa_pliku</a>   YYYY-MM-DD HH:MM   ROZMIAR
        # Parsujemy każdą linię HTML przez wyrażenia regularne –
        # jest to prostsze i wystarczające dla standardowego listingu Apache'a.
        for line in response.text.splitlines():
            href_match = re.search(r'<a href="([^"?][^"]*)">', line)
            if not href_match:
                continue

            href = href_match.group(1)

            # Pomijamy wpisy katalogów (kończą się '/') oraz katalog nadrzędny
            if href.endswith("/"):
                continue

            full_url = url.rstrip("/") + "/" + href

            # Data ostatniej modyfikacji jest w tej samej linii, po znaczniku </a>
            # Format DWD: "15-May-2026 11:39:33"
            date_match = re.search(r"(\d{2}-[A-Za-z]{3}-\d{4}\s+\d{2}:\d{2}:\d{2})", line)
            date = _parse_dwd_date(date_match.group(1)) if date_match else pd.NaT

            records.append(
                {
                    "name": href,
                    "full_path": full_url,
                    "date_created": date,
                }
            )

        return pd.DataFrame(records, columns=["name", "full_path", "date_created"])

    def dwd_download(
        self,
        df: pd.DataFrame,
        save_path: str | Path,
        unzip: bool = False,
    ) -> pd.DataFrame:
        """
        Pobiera pliki GRIB z DataFrame'u zwróconego przez :meth:`dwd_list_files`.

        Pliki są strumieniowane w kawałkach (chunked streaming), aby uniknąć
        ładowania dużych binarnych plików GRIB do pamięci RAM.

        Parameters
        ----------
        df : pd.DataFrame
            DataFrame z :meth:`dwd_list_files`; musi zawierać kolumny
            ``name`` i ``full_path``.
        save_path : str lub Path
            Katalog docelowy dla pobieranych plików. Zostanie utworzony
            automatycznie jeśli nie istnieje.
        unzip : bool, opcjonalnie
            Jeśli ``True``, archiwa ``.bz2`` zostaną rozpakowane po pobraniu,
            a oryginalny plik skompresowany usunięty. Pliki nie będące archiwami
            ``.bz2`` są zachowywane bez zmian niezależnie od tej flagi.
            Domyślnie ``False``.

        Returns
        -------
        pd.DataFrame
            Kopia wejściowego DataFrame'u z dodatkową kolumną ``grib_path``
            zawierającą bezwzględną ścieżkę do zapisanego pliku GRIB
            (lub rozpakowanego, gdy ``unzip=True``).

        Raises
        ------
        requests.HTTPError
            Gdy pobieranie któregokolwiek pliku zakończy się błędem HTTP.
        requests.Timeout
            Gdy połączenie przekroczy limit czasu (120 s).
        """
        save_dir = Path(save_path)
        save_dir.mkdir(parents=True, exist_ok=True)

        result = df.copy()
        grib_paths: list[Path] = []
        total = len(df)

        for idx, (_, row) in enumerate(df.iterrows(), start=1):
            url: str = row["full_path"]
            filename: str = row["name"]
            dest = save_dir / filename

            #print(f"[{idx}/{total}] Pobieranie: {filename}")

            # Streaming pozwala obsłużyć duże pliki GRIB (>100 MB) bez
            # wczytywania całości do pamięci – zapisujemy w kawałkach 1 MB.
            with requests.get(url, stream=True, timeout=120) as response:
                response.raise_for_status()
                with open(dest, "wb") as fh:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        fh.write(chunk)

            if unzip and filename.endswith(".bz2"):
                # Usuwamy sufiks .bz2, zachowując .grib2 jako rozszerzenie docelowe
                grib_dest = dest.with_suffix("")
                #print(f"  Rozpakowywanie → {grib_dest.name}")
                with bz2.open(dest, "rb") as compressed, open(grib_dest, "wb") as out:
                    out.write(compressed.read())
                #print(f"  Usuwanie → {dest.name}")
                dest.unlink()  # usuń archiwum .bz2 po rozpakowaniu
                grib_paths.append(grib_dest.resolve())
            else:
                grib_paths.append(dest.resolve())

        result["grib_path"] = grib_paths
        return result

    # ------------------------------------------------------------------
    # ICON-EU-EPS – ekstrakcja punktowa i statystyki ensembla
    # ------------------------------------------------------------------

    def icon_extract_points(
        self,
        grib_files: Iterable[str | Path],
        grid_file: str | Path,
        points: list[dict],
        n_neighbors: int = 9,
    ) -> pd.DataFrame:
        """
        Czyta pliki GRIB2 ICON-EU-EPS i wyciąga wartości ensembla w zadanych punktach.

        Dla każdego komunikatu GRIB i każdego punktu znajduje ``n_neighbors``
        najbliższych węzłów siatki icosahedralnej i zapisuje ich wartości jako
        osobne wiersze (z kolumną ``neighbor_rank``).  Dzięki temu downstream
        możliwe jest obliczenie statystyk ensembla uwzględniających zarówno
        niepewność między członkami, jak i lokalną zmienność przestrzenną.

        Parameters
        ----------
        grib_files : iterable of str lub Path
            Lista ścieżek do plików GRIB2 (np. kolumna ``grib_path`` z
            :meth:`dwd_download`).
        grid_file : str lub Path
            Ścieżka do pliku NetCDF z geometrią siatki ICON (``clat``, ``clon``
            w radianach).
        points : list of dict
            Każdy słownik musi mieć klucze ``lat``, ``lon``, ``name``.
            Najwygodniej przekazać ``cfg["points"]`` z wczytanej konfiguracji.
        n_neighbors : int
            Liczba najbliższych węzłów siatki na punkt. Domyślnie 9 (sąsiedztwo 3×3).

        Returns
        -------
        pd.DataFrame
            Jeden wiersz na (plik GRIB, komunikat, punkt, sąsiad) z kolumnami:
            ``shortName``, ``name``, ``units``, ``typeOfLevel``, ``level``,
            ``dataDate``, ``dataTime``, ``stepRange``, ``paramId``,
            ``msg_number``, ``grib_file``, ``forecast_time``,
            ``target_name``, ``target_lat``, ``target_lon``,
            ``neighbor_rank``, ``lat_found``, ``lon_found``, ``dist_deg``,
            ``value``.
        """
        grid = xr.open_dataset(grid_file, engine="netcdf4")
        lats = grid["clat"].values
        lons = grid["clon"].values
        grid.close()
        if np.abs(lats).max() <= np.pi:
            lats = lats * (180.0 / np.pi)
            lons = lons * (180.0 / np.pi)

        # Oblicz indeksy sąsiadów raz dla każdego punktu – siatka jest niezmienna
        point_neighbors: list[dict] = []
        ranks = np.arange(n_neighbors)
        for point in points:
            dist = np.sqrt((lats - point["lat"]) ** 2 + (lons - point["lon"]) ** 2)
            idx = np.argpartition(dist, n_neighbors)[:n_neighbors]
            point_neighbors.append({
                "name":       point["name"],
                "target_lat": point["lat"],
                "target_lon": point["lon"],
                "indices":    idx,
                "lats_found": lats[idx],
                "lons_found": lons[idx],
                "dists":      dist[idx],
            })

        n_pts = len(point_neighbors)
        rows_per_msg = n_pts * n_neighbors
        # Stałe kolumny geometryczne (powtarzane dla każdej wiadomości GRIB)
        geo_target_name = np.repeat([p["name"]       for p in point_neighbors], n_neighbors)
        geo_target_lat  = np.repeat([p["target_lat"] for p in point_neighbors], n_neighbors)
        geo_target_lon  = np.repeat([p["target_lon"] for p in point_neighbors], n_neighbors)
        geo_neighbor_rank = np.tile(ranks, n_pts)
        geo_lat_found   = np.concatenate([p["lats_found"] for p in point_neighbors])
        geo_lon_found   = np.concatenate([p["lons_found"] for p in point_neighbors])
        geo_dist_deg    = np.concatenate([p["dists"]      for p in point_neighbors])

        file_chunks: list[pd.DataFrame] = []

        for grib_file in grib_files:
            msg_frames: list[pd.DataFrame] = []
            with open(grib_file, "rb") as f:
                msg_number = 0
                while True:
                    handle = eccodes.codes_grib_new_from_file(f)
                    if handle is None:
                        break
                    msg_number += 1
                    try:
                        values = eccodes.codes_get_array(handle, "values")
                        meta: dict = {"msg_number": msg_number, "grib_file": str(grib_file)}
                        for key in _GRIB_META_KEYS:
                            try:
                                val = eccodes.codes_get(handle, key)
                                # Normalizuj shortName do lowercase — DWD zwraca np. CAPE_ML, ASWDIR_S
                                if key == "shortName" and isinstance(val, str):
                                    val = val.lower()
                                meta[key] = val
                            except Exception:
                                meta[key] = None

                        block: dict = {k: [v] * rows_per_msg for k, v in meta.items()}
                        block["target_name"]   = geo_target_name
                        block["target_lat"]    = geo_target_lat
                        block["target_lon"]    = geo_target_lon
                        block["neighbor_rank"] = geo_neighbor_rank
                        block["lat_found"]     = geo_lat_found
                        block["lon_found"]     = geo_lon_found
                        block["dist_deg"]      = geo_dist_deg
                        block["value"] = np.concatenate(
                            [values[p["indices"]] for p in point_neighbors]
                        )
                        msg_frames.append(pd.DataFrame(block))

                    except Exception as e:
                        print(f"Pominięto msg {msg_number} w {grib_file}: {e}")
                    finally:
                        eccodes.codes_release(handle)

            # Scalaj wiadomości z jednego pliku od razu — zwalnia msg_frames z RAM
            if msg_frames:
                file_chunks.append(pd.concat(msg_frames, ignore_index=True))

        if not file_chunks:
            return pd.DataFrame()

        df = pd.concat(file_chunks, ignore_index=True)
        df["forecast_time"] = (
            pd.to_datetime(df["dataDate"], format="%Y%m%d")
            + pd.to_timedelta(pd.to_numeric(df["dataTime"]) / 100, unit="h")
            + pd.to_timedelta(
                df["stepRange"]
                    .astype(str)
                    .str.replace("m", "", regex=False)
                    .str.extract(r"(\d+)$")[0]
                    .astype(float),
                unit="h",
            )
        )
        df["forecast_time"] = pd.to_datetime(df["forecast_time"])
        return df

    def _derive_from_accumulated(
        self,
        df: pd.DataFrame,
        source_shortname: str,
        result_shortname: str,
        result_name: str,
        result_units: str,
        per_hour: bool = True,
    ) -> pd.DataFrame:
        """
        Oblicza wartości z kroku czasowego przez różnicowanie sumy akumulowanej.

        per_hour=True  → wynik w [jedn./h]  (np. mm/h dla opadu: diff / dt_s * 3600)
        per_hour=False → wynik w [jedn./s]  (np. W/m²: diff / dt_s)
        """
        df_src = df[df["shortName"] == source_shortname].copy().sort_values("forecast_time")
        group_cols = ["target_name", "neighbor_rank", "msg_number"]
        time_diff_s = (
            df_src.groupby(group_cols)["forecast_time"]
            .diff()
            .dt.total_seconds()
        )
        df_src["value"] = df_src.groupby(group_cols)["value"].diff() / time_diff_s
        if per_hour:
            df_src["value"] *= 3600
        df_src["shortName"] = result_shortname
        df_src["name"]      = result_name
        df_src["units"]     = result_units
        return df_src

    def _derive_flux_from_running_avg(
        self,
        df: pd.DataFrame,
        source_shortname: str,
        result_shortname: str,
        result_units: str = "W/m²",
    ) -> pd.DataFrame:
        """
        Wyciąga godzinowy strumień z narastającej średniej (np. aswdir_s).

        DWD publikuje aswdir_s jako średnią od startu modelu A(t) [W/m²].
        Aby odzyskać średni strumień w przedziale [t1, t2]:

            flux = (A(t2)·t2 − A(t1)·t1) / (t2 − t1)

        gdzie t to czas od startu modelu w sekundach.
        """
        df_src = df[df["shortName"] == source_shortname].copy()

        run_time_s = (
            pd.to_datetime(df_src["dataDate"].astype(str), format="%Y%m%d")
            + pd.to_timedelta(df_src["dataTime"].astype(float) / 100, unit="h")
        )
        df_src["_step_s"]   = (df_src["forecast_time"] - run_time_s).dt.total_seconds()
        df_src["_accum"]    = df_src["value"] * df_src["_step_s"]
        df_src = df_src.sort_values("forecast_time")

        group_cols = ["target_name", "neighbor_rank", "msg_number"]
        d_accum = df_src.groupby(group_cols)["_accum"].diff()
        d_step  = df_src.groupby(group_cols)["_step_s"].diff()

        df_src["value"]     = d_accum / d_step
        df_src["shortName"] = result_shortname
        df_src["name"]      = result_shortname
        df_src["units"]     = result_units
        return df_src.drop(columns=["_step_s", "_accum"])

    def _derive_solar_clearness(
        self,
        df: pd.DataFrame,
        source_shortname: str,
        result_shortname: str,
        T_L: float = _LINKE_TURBIDITY_PL,
        n_sub: int = 6,
    ) -> pd.DataFrame:
        """
        Indeks klarowności promieniowania bezpośredniego [%].

        kb = aswdir_s_hourly / I_b_cs_mean × 100

        I_b_cs_mean to średnie clear-sky (Ineichen-Perez, turbidity Linkego T_L)
        całkowane metodą trapezów (n_sub przedziałów) nad **rzeczywistym oknem
        czasowym** danego kroku — 1h dla pierwszych 48h prognozy, 6h dla dalszych.
        Szerokość okna wyznaczana automatycznie z różnic forecast_time wewnątrz
        każdego członka ensembla.

        Nocą (I_b_cs_mean = 0) wynik NaN.
        """
        df_src = df[df["shortName"] == source_shortname].copy()
        if df_src.empty:
            return df_src

        group_cols = ["target_name", "neighbor_rank", "msg_number"]
        df_src = df_src.sort_values(group_cols + ["forecast_time"])

        # Szerokość okna w sekundach (1h → 3600, 6h → 21600)
        dt_s = (
            df_src
            .groupby(group_cols)["forecast_time"]
            .diff()
            .dt.total_seconds()
            .bfill()          # pierwsza różnica w grupie → weź kolejną wartość
            .fillna(3600.0)   # fallback dla grup 1-elementowych
        )

        # Całkowanie trapezowe I_b_cs po oknie [forecast_time - dt, forecast_time]
        # n_sub przedziałów → n_sub+1 punktów równomiernie rozłożonych
        # Dla okna 1h i n_sub=6: punkty co 10 min
        # Dla okna 6h i n_sub=6: punkty co 1 h
        sub_vals = []
        for k in range(n_sub + 1):
            # frac=0 → start okna (forecast_time - dt), frac=1 → koniec (forecast_time)
            frac = k / n_sub
            t_k = df_src["forecast_time"] - pd.to_timedelta(dt_s * (1.0 - frac), unit="s")
            sub_vals.append(
                _clear_sky_direct_horizontal(
                    t_k, df_src["target_lat"], df_src["target_lon"], T_L
                )
            )

        sub_arr    = np.stack(sub_vals, axis=1)          # (N_wierszy, n_sub+1)
        # reguła trapezów: (f0 + f_n)/2 + f1 + … + f_{n-1}, całość / n_sub
        I_b_cs_mean = (
            (sub_arr[:, 0] + sub_arr[:, -1]) / 2.0
            + sub_arr[:, 1:-1].sum(axis=1)
        ) / n_sub

        pct = np.where(I_b_cs_mean > 50.0, df_src["value"].values / I_b_cs_mean * 100.0, np.nan)
        pct = np.clip(pct, 0.0, 100.0)

        df_src["value"]     = pct
        df_src["shortName"] = result_shortname
        df_src["name"]      = "Direct solar clearness index"
        df_src["units"]     = "%"
        return df_src

    def _derive_storm_probability(
        self,
        df: pd.DataFrame,
        precip_sn:  str   = "tp_hourly",
        cape_sn:    str   = "cape_ml",
        precip_thr: float = 0.1,
        cape_thr:   float = 250.0,
        result_sn:  str   = "thunder_prob",
    ) -> pd.DataFrame:
        """
        Wskaźnik burzy (0/1) na poziomie każdego członka ensembla.

        storm_member = 1  gdy  tp_hourly > precip_thr [mm/h]  AND  CAPE > cape_thr [J/kg]

        Po standardowej agregacji w icon_compute_stats:
            mean(storm_member)  =  prawdopodobieństwo burzy  ∈ [0, 1]
        """
        join_cols = ["forecast_time", "target_name", "neighbor_rank", "msg_number"]

        df_tp   = df[df["shortName"] == precip_sn].copy()
        df_cape = (
            df[df["shortName"] == cape_sn][join_cols + ["value"]]
            .rename(columns={"value": "_cape"})
        )

        if df_tp.empty or df_cape.empty:
            return pd.DataFrame()

        merged = df_tp.merge(df_cape, on=join_cols, how="inner")
        if merged.empty:
            return pd.DataFrame()

        merged["value"]     = ((merged["value"] > precip_thr) & (merged["_cape"] > cape_thr)).astype(float)
        merged["shortName"] = result_sn
        merged["name"]      = "Storm probability indicator"
        merged["units"]     = "1"
        return merged.drop(columns=["_cape"])

    def icon_download_grid(
        self,
        save_path: str | Path,
        config: dict | None = None,
        hour: str = "00",
    ) -> Path:
        """
        Pobiera siatkę ICON-EU-EPS (clat, clon) z DWD i zapisuje jako plik NetCDF4.

        Siatkę wystarczy pobrać **raz** — jest niezmienna w czasie.  Plik
        wynikowy jest następnie przekazywany do :meth:`icon_extract_points`
        jako ``grid_file``.

        DWD publikuje współrzędne jako osobne pliki GRIB2 w folderach
        ``clat/`` i ``clon/`` (parametry time-invariant).  Metoda pobiera je,
        odczytuje wartości przez eccodes i zapisuje do NetCDF4 z jednostką
        ``radian`` (kompatybilność z oryginalnym formatem pliku siatki ICON).

        Parameters
        ----------
        save_path : str lub Path
            Ścieżka do pliku wynikowego NetCDF4 (np. ``'tests/icon_grid.nc'``)
            lub do katalogu — wtedy plik zostanie nazwany ``icon_grid.nc``.
        config : dict, opcjonalnie
            Konfiguracja z :func:`load_config`; używa ``config["dwd"]["parent_path"]``.
            Jeśli ``None``, używa wartości domyślnej ``icon-eu-eps/grib/``.
        hour : str
            Godzina inicjalizacji, z której pobierany jest plik siatki.
            Dowolna z ``00``, ``06``, ``12``, ``18`` — siatka jest identyczna.

        Returns
        -------
        Path
            Bezwzględna ścieżka do zapisanego pliku NetCDF4.
        """
        import tempfile

        dwd_cfg: dict = (config or {}).get("dwd", {})
        parent_path: str = dwd_cfg.get("parent_path", "icon-eu-eps/grib/")

        dest = Path(save_path)
        if dest.suffix:
            grid_nc_path = dest
            dest.parent.mkdir(parents=True, exist_ok=True)
        else:
            dest.mkdir(parents=True, exist_ok=True)
            grid_nc_path = dest / "icon_grid.nc"

        coords: dict[str, np.ndarray] = {}
        for coord in ("clat", "clon"):
            df_files = self.dwd_list_files(f"{parent_path}{hour}/{coord}/")
            if df_files.empty:
                raise RuntimeError(
                    f"Nie znaleziono pliku siatki '{coord}' na serwerze DWD "
                    f"(ścieżka: {parent_path}{hour}/{coord}/)."
                )
            with tempfile.TemporaryDirectory() as tmpdir:
                df_dl = self.dwd_download(df_files.head(1), save_path=tmpdir, unzip=True)
                grib_path: Path = df_dl["grib_path"].iloc[0]
                with open(grib_path, "rb") as f:
                    handle = eccodes.codes_grib_new_from_file(f)
                    try:
                        values: np.ndarray = eccodes.codes_get_array(handle, "values").astype(np.float64)
                    finally:
                        eccodes.codes_release(handle)

            # ICON przechowuje clat/clon w radianach; GRIB2 może dekodować do stopni
            if np.abs(values).max() > np.pi:
                values = values * (np.pi / 180.0)
            coords[coord] = values

        grid_ds = xr.Dataset(
            {
                "clat": xr.DataArray(coords["clat"], dims=["ncells"], attrs={"units": "radian"}),
                "clon": xr.DataArray(coords["clon"], dims=["ncells"], attrs={"units": "radian"}),
            }
        )
        grid_ds.to_netcdf(str(grid_nc_path), engine="netcdf4")
        return grid_nc_path.resolve()

    def icon_list_runs(
        self,
        config: dict,
        hours: list[str] | None = None,
    ) -> pd.DataFrame:
        """
        Zwraca listę runów ICON-EU-EPS aktualnie dostępnych na serwerze DWD.

        Przydatne do synchronizacji z bazą danych: porównaj ``run_time``
        z tym co już masz i pobierz tylko brakujące runy przez :meth:`icon_fetch`.

        Parameters
        ----------
        config : dict
            Konfiguracja z :func:`load_config`.
        hours : list of str, opcjonalnie
            Godziny inicjalizacji do sprawdzenia.  Domyślnie z konfiguracji.

        Returns
        -------
        pd.DataFrame
            Posortowany malejąco po ``run_time``, z kolumnami:

            - ``run_time``    : pd.Timestamp – czas inicjalizacji runu (UTC)
            - ``hour``        : str – slot godzinowy na serwerze DWD (``'00'``…``'18'``)
            - ``file_count``  : int – liczba plików dostępnych dla sondy
            - ``is_complete`` : bool – ``True`` gdy ``file_count ≥ min_files_per_hour``
        """
        dwd_cfg: dict = config.get("dwd", {})
        parent_path: str = dwd_cfg.get("parent_path", "icon-eu-eps/grib/")
        if hours is None:
            hours = dwd_cfg.get("hours", ["00", "06", "12", "18"])
        min_files: int = dwd_cfg.get("min_files_per_hour", 25)

        params_cfg: dict = config.get("parameters", {})
        dwd_params = [key for key, pcfg in params_cfg.items() if not pcfg.get("derived")]
        if not dwd_params:
            raise ValueError("Brak parametrów do pobrania w konfiguracji.")

        probe = dwd_params[0]
        rows: list[pd.DataFrame] = []
        for hour in hours:
            df_files = self.dwd_list_files(f"{parent_path}{hour}/{probe}/")
            df_files["hour"] = hour
            rows.append(df_files)

        df_all = pd.concat(rows, ignore_index=True)
        # Wyciągnij czas inicjalizacji z nazwy pliku: YYYYMMDDHH (10 cyfr)
        df_all["run_time"] = pd.to_datetime(
            df_all["name"].str.extract(r"_(\d{10})_")[0],
            format="%Y%m%d%H",
            utc=True,
        )

        df_runs = (
            df_all.groupby(["run_time", "hour"])
            .size()
            .reset_index(name="file_count")
        )
        df_runs["is_complete"] = df_runs["file_count"] >= min_files
        return df_runs.sort_values("run_time", ascending=False).reset_index(drop=True)

    def icon_fetch(
        self,
        run_time: pd.Timestamp | str,
        config: dict,
        save_path: str | Path | None = None,
    ) -> pd.DataFrame:
        """
        Pobiera wszystkie skonfigurowane parametry dla wskazanego runu ICON-EU-EPS.

        Pobierany jest run odpowiadający ``hour = run_time.hour`` – DWD udostępnia
        tylko aktualny run dla każdego slotu godzinowego, więc ``run_time`` powinno
        pochodzić z :meth:`icon_list_runs`.

        Parameters
        ----------
        run_time : pd.Timestamp lub str
            Czas inicjalizacji runu, np. ``pd.Timestamp('2026-05-23 06:00')``.
            Używana jest tylko część godzinowa (``00``/``06``/``12``/``18``).
        config : dict
            Konfiguracja z :func:`load_config`.
        save_path : str lub Path, opcjonalnie
            Katalog docelowy.  Jeśli ``None``, używa ``config["dwd"]["save_dir"]``.

        Returns
        -------
        pd.DataFrame
            Wynik :meth:`dwd_download` dla wszystkich parametrów (z kolumną
            ``grib_path``), połączony w jeden DataFrame.
        """
        run_ts = pd.Timestamp(run_time)
        hour: str = f"{run_ts.hour:02d}"

        dwd_cfg: dict = config.get("dwd", {})
        parent_path: str = dwd_cfg.get("parent_path", "icon-eu-eps/grib/")
        if save_path is None:
            save_path = dwd_cfg.get("save_dir", "data/dwd/icon-eu-eps")

        params_cfg: dict = config.get("parameters", {})
        dwd_params = [key for key, pcfg in params_cfg.items() if not pcfg.get("derived")]

        downloads: list[pd.DataFrame] = []
        for param in dwd_params:
            df_files = self.dwd_list_files(f"{parent_path}{hour}/{param}/")
            df_dl = self.dwd_download(df_files, save_path=save_path, unzip=True)
            downloads.append(df_dl)

        return pd.concat(downloads, ignore_index=True)

    def icon_fetch_latest(
        self,
        config: dict,
        save_path: str | Path | None = None,
        hours: list[str] | None = None,
    ) -> pd.DataFrame:
        """
        Skrót: pobiera najnowszy kompletny run ICON-EU-EPS.

        Wywołuje :meth:`icon_list_runs` i przekazuje najnowszy kompletny run
        do :meth:`icon_fetch`.  Do synchronizacji z bazą danych użyj tych metod
        bezpośrednio, aby mieć kontrolę nad tym które runy są pobierane.

        Raises
        ------
        RuntimeError
            Gdy żaden run nie spełnia kryterium kompletności.
        """
        df_runs = self.icon_list_runs(config, hours=hours)
        complete = df_runs[df_runs["is_complete"]]
        if complete.empty:
            min_files = config.get("dwd", {}).get("min_files_per_hour", 25)
            raise RuntimeError(
                f"Brak kompletnych runów ICON-EU-EPS na serwerze DWD "
                f"(wymagane ≥ {min_files} plików)."
            )
        return self.icon_fetch(complete.iloc[0]["run_time"], config, save_path=save_path)

    def icon_compute_stats(
        self,
        df_forecast: pd.DataFrame,
        config: dict,
    ) -> pd.DataFrame:
        """
        Oblicza statystyki ensembla i prawdopodobieństwa przekroczeń progów.

        Agreguje surowy DataFrame z :meth:`icon_extract_points` po wymiarach
        (parametr, poziom, czas prognozy, punkt) i oblicza:

        - podstawowe statystyki: średnia, odchylenie std, min, max
        - percentyle ensembla (konfigurowalne)
        - prawdopodobieństwo przekroczenia/poniżej każdego progu z konfiguracji

        Parameters
        ----------
        df_forecast : pd.DataFrame
            Wyjście :meth:`icon_extract_points`.
        config : dict
            Konfiguracja wczytana przez :func:`load_config`.  Używane klucze:
            ``parameters``, ``percentiles``.

        Returns
        -------
        pd.DataFrame
            Format długi (tidy): jeden wiersz na (shortName, typeOfLevel,
            level, forecast_time, target_name, **aggregation_type**).
            Kolumna ``aggregation_type`` zawiera nazwę agregacji
            (``mean``, ``std``, ``min``, ``max``, ``p01`` … ``p99``,
            ``prob_gt_*``, ``prob_lt_*``), a kolumna ``value`` jej wartość.

        Notes
        -----
        Kolumna ``shortName`` zawiera GRIB shortName (np. ``2t``, ``tp``).
        Prawdopodobieństwo przekroczenia liczone jest po wszystkich członkach
        ensembla i wszystkich sąsiadach siatki łącznie.
        """
        params_cfg: dict = config.get("parameters", {})
        percentiles: list[int] = config.get("percentiles", [1, 5, 25, 50, 75, 95, 99])

        df = df_forecast.copy()

        # grib_shortname → config_key (dla filtrowania i powiązania z progami)
        sn_to_key: dict[str, str] = {
            pcfg.get("grib_shortname", key): key
            for key, pcfg in params_cfg.items()
        }

        # Oblicz pochodne z akumulowanych/uśrednionych parametrów
        for key, pcfg in params_cfg.items():
            source_sn = pcfg.get("grib_shortname", key)
            if source_sn not in df["shortName"].values:
                continue
            if pcfg.get("derive_hourly"):
                # Suma narastająca → natężenie godzinowe (mm/h)
                df = pd.concat(
                    [df, self._derive_from_accumulated(
                        df, source_sn, "tp_hourly", "Precipitation (hourly)", "mm/h",
                        per_hour=True,
                    )],
                    ignore_index=True,
                )
            elif pcfg.get("derive_flux_avg"):
                # Średnia narastająca → strumień godzinowy (W/m²)
                result_sn = pcfg.get("derive_result", key + "_hourly")
                derived = self._derive_flux_from_running_avg(df, source_sn, result_sn)
                df = pd.concat([df, derived], ignore_index=True)
                # Opcjonalnie: indeks klarowności (% ETR)
                if pcfg.get("derive_clearness"):
                    clearness_sn = pcfg.get("clearness_result", result_sn + "_pct")
                    T_L = float(pcfg.get("clearness_turbidity", _LINKE_TURBIDITY_PL))
                    clearness = self._derive_solar_clearness(df, result_sn, clearness_sn, T_L=T_L)
                    df = pd.concat([df, clearness], ignore_index=True)

        # Oblicz prawdopodobieństwo burzy (wymaga tp_hourly i cape_ml w df)
        for key, pcfg in params_cfg.items():
            src = pcfg.get("storm_source")
            if not src:
                continue
            derived = self._derive_storm_probability(
                df,
                precip_sn  = src.get("precip_sn",  "tp_hourly"),
                cape_sn    = src.get("cape_sn",     "cape_ml"),
                precip_thr = float(src.get("precip_thr", 0.1)),
                cape_thr   = float(src.get("cape_thr",   250.0)),
                result_sn  = key,
            )
            if not derived.empty:
                df = pd.concat([df, derived], ignore_index=True)

        # Ogranicz do skonfigurowanych parametrów (według GRIB shortName)
        df = df[df["shortName"].isin(sn_to_key)].copy()

        # Podstawowe statystyki
        agg_dict: dict = {
            "value_mean": ("value", "mean"),
            "value_std":  ("value", "std"),
            "value_min":  ("value", "min"),
            "value_max":  ("value", "max"),
        }
        for p in percentiles:
            agg_dict[f"value_p{p:02d}"] = ("value", lambda x, _p=p: x.quantile(_p / 100))

        df_stats = (
            df.groupby(_STATS_GROUP_COLS, dropna=False)
            .agg(**agg_dict)
            .reset_index()
        )

        # Prawdopodobieństwa przekroczeń – osobno dla każdego parametru
        for key, pcfg in params_cfg.items():
            thresholds: list[dict] = pcfg.get("thresholds") or []
            if not thresholds:
                continue

            grib_sn = pcfg.get("grib_shortname", key)
            df_param = df[df["shortName"] == grib_sn].copy()
            if df_param.empty:
                continue

            prob_cols: list[str] = []
            for thr in thresholds:
                val: float = thr["value"]
                label: str = thr["label"]
                direction: str = thr.get("direction", "above")
                if direction == "below":
                    col = f"prob_lt_{label}"
                    df_param[col] = (df_param["value"] < val).astype(float)
                else:
                    col = f"prob_gt_{label}"
                    df_param[col] = (df_param["value"] > val).astype(float)
                prob_cols.append(col)

            df_exc = (
                df_param.groupby(_STATS_GROUP_COLS, dropna=False)[prob_cols]
                .mean()
                .reset_index()
            )
            df_stats = df_stats.merge(df_exc, on=_STATS_GROUP_COLS, how="left")

        # Przekształć do formatu długiego: stat kolumny → wiersze
        stat_cols = [c for c in df_stats.columns if c not in _STATS_GROUP_COLS]
        df_long = df_stats.melt(
            id_vars=_STATS_GROUP_COLS,
            value_vars=stat_cols,
            var_name="aggregation_type",
            value_name="value",
        )
        # Uprość nazwy: value_mean → mean, value_p05 → p05
        df_long["aggregation_type"] = df_long["aggregation_type"].str.removeprefix("value_")
        return df_long.reset_index(drop=True)

    # ------------------------------------------------------------------
    # Wersje niskopamięciowe — streaming przez SQLite (dla RPi i innych
    # środowisk z ograniczoną pamięcią RAM).
    # Użycie:
    #   db = nwp.icon_extract_to_db(grib_files, grid_file, points, n_neighbors, "raw.db")
    #   df_stats = nwp.icon_compute_stats_from_db(db, cfg)
    # ------------------------------------------------------------------

    # Kolumny zapisywane / odczytywane z tabeli raw_forecast
    _DB_COLS = [
        "dataDate", "dataTime", "stepRange", "forecast_time", "msg_number",
        "shortName", "name", "units", "typeOfLevel", "level", "centre",
        "paramId", "parameterCategory", "parameterNumber",
        "target_name", "target_lat", "target_lon",
        "neighbor_rank", "lat_found", "lon_found", "dist_deg", "value",
    ]

    @staticmethod
    def _compute_forecast_time_col(df: pd.DataFrame) -> pd.Series:
        return (
            pd.to_datetime(df["dataDate"].astype(str), format="%Y%m%d")
            + pd.to_timedelta(pd.to_numeric(df["dataTime"]) / 100, unit="h")
            + pd.to_timedelta(
                df["stepRange"]
                    .astype(str)
                    .str.replace("m", "", regex=False)
                    .str.extract(r"(\d+)$")[0]
                    .astype(float),
                unit="h",
            )
        )

    def icon_extract_to_db(
        self,
        grib_files: Iterable[str | Path],
        grid_file: str | Path,
        points: list[dict],
        n_neighbors: int = 9,
        db_path: str | Path | None = None,
        overwrite: bool = True,
    ) -> Path:
        """
        Wersja niskopamięciowa ``icon_extract_points``.

        Zamiast akumulować dane w RAM, każdy plik GRIB jest natychmiast
        zapisywany do SQLite.  Peak RAM ≈ dane z jednego pliku (~7 MB
        dla n_neighbors=300 i 40 członków ensembla).

        Parameters
        ----------
        db_path : str | Path | None
            Ścieżka do pliku SQLite. Domyślnie: ``data/forecast_raw.db``
            względem katalogu projektu (PROJECT_ROOT z env lub cwd).
            Katalog nadrzędny jest tworzony automatycznie.
        overwrite : bool
            Jeśli True, usuwa istniejący plik przed startem.

        Returns
        -------
        Path
            Ścieżka do zapisanego pliku SQLite.
        """
        import sqlite3

        if db_path is None:
            project_root = Path(os.environ.get("PROJECT_ROOT", Path.cwd()))
            db_path = project_root / "data" / "forecast_raw.db"

        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if overwrite and db_path.exists():
            db_path.unlink()

        # ── Wczytaj siatkę (identycznie jak w icon_extract_points) ──────
        grid_file = Path(grid_file)
        ds = xr.open_dataset(grid_file)
        lats = ds["clat"].values
        lons = ds["clon"].values
        ds.close()
        if lats.max() > np.pi / 2:
            lats = lats * (180.0 / np.pi)
            lons = lons * (180.0 / np.pi)

        point_neighbors: list[dict] = []
        ranks = np.arange(n_neighbors)
        for point in points:
            dist = np.sqrt((lats - point["lat"]) ** 2 + (lons - point["lon"]) ** 2)
            idx  = np.argpartition(dist, n_neighbors)[:n_neighbors]
            point_neighbors.append({
                "name":       point["name"],
                "target_lat": point["lat"],
                "target_lon": point["lon"],
                "indices":    idx,
                "lats_found": lats[idx],
                "lons_found": lons[idx],
                "dists":      dist[idx],
            })

        n_pts        = len(point_neighbors)
        rows_per_msg = n_pts * n_neighbors
        geo_target_name  = np.repeat([p["name"]       for p in point_neighbors], n_neighbors)
        geo_target_lat   = np.repeat([p["target_lat"] for p in point_neighbors], n_neighbors)
        geo_target_lon   = np.repeat([p["target_lon"] for p in point_neighbors], n_neighbors)
        geo_neighbor_rank = np.tile(ranks, n_pts)
        geo_lat_found    = np.concatenate([p["lats_found"] for p in point_neighbors])
        geo_lon_found    = np.concatenate([p["lons_found"] for p in point_neighbors])
        geo_dist_deg     = np.concatenate([p["dists"]      for p in point_neighbors])

        # ── Połącz z SQLite i utwórz tabelę ─────────────────────────────
        _TEXT_COLS = {
            "stepRange", "shortName", "name", "units",
            "typeOfLevel", "forecast_time", "target_name",
        }
        _REAL_COLS = {
            "level", "target_lat", "target_lon",
            "lat_found", "lon_found", "dist_deg", "value",
        }
        col_defs = ", ".join(
            f"{c} TEXT"    if c in _TEXT_COLS else
            f"{c} REAL"    if c in _REAL_COLS else
            f"{c} INTEGER"
            for c in self._DB_COLS
        )
        create_sql = f"CREATE TABLE IF NOT EXISTS raw_forecast ({col_defs})"

        print(f"[icon_extract_to_db] Plik bazy: {db_path.resolve()}")
        con = sqlite3.connect(str(db_path))
        try:
            con.execute(create_sql)
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_sn "
                "ON raw_forecast (shortName, target_name)"
            )
            con.commit()

            # ── Iteruj po plikach GRIB ────────────────────────────────
            for grib_file in grib_files:
                msg_frames: list[pd.DataFrame] = []
                with open(grib_file, "rb") as f:
                    msg_number = 0
                    while True:
                        handle = eccodes.codes_grib_new_from_file(f)
                        if handle is None:
                            break
                        msg_number += 1
                        try:
                            values = eccodes.codes_get_array(handle, "values")
                            meta: dict = {"msg_number": msg_number}
                            for key in _GRIB_META_KEYS:
                                try:
                                    val = eccodes.codes_get(handle, key)
                                    if key == "shortName" and isinstance(val, str):
                                        val = val.lower()
                                    meta[key] = val
                                except Exception:
                                    meta[key] = None

                            block: dict = {k: [v] * rows_per_msg for k, v in meta.items()}
                            block["target_name"]   = geo_target_name
                            block["target_lat"]    = geo_target_lat
                            block["target_lon"]    = geo_target_lon
                            block["neighbor_rank"] = geo_neighbor_rank
                            block["lat_found"]     = geo_lat_found
                            block["lon_found"]     = geo_lon_found
                            block["dist_deg"]      = geo_dist_deg
                            block["value"] = np.concatenate(
                                [values[p["indices"]] for p in point_neighbors]
                            )
                            msg_frames.append(pd.DataFrame(block))
                        except Exception as e:
                            print(f"Pominięto msg {msg_number} w {grib_file}: {e}")
                        finally:
                            eccodes.codes_release(handle)

                if not msg_frames:
                    continue

                df_file = pd.concat(msg_frames, ignore_index=True)
                del msg_frames

                df_file["forecast_time"] = (
                    self._compute_forecast_time_col(df_file)
                    .dt.strftime("%Y-%m-%d %H:%M:%S")
                )
                cols_present = [c for c in self._DB_COLS if c in df_file.columns]
                df_file[cols_present].to_sql(
                    "raw_forecast", con, if_exists="append", index=False,
                )
                del df_file
                con.commit()

        finally:
            con.close()

        print(f"[icon_extract_to_db] Zapisano: {db_path.resolve()}")
        return db_path

    def icon_compute_stats_from_db(
        self,
        db_path: str | Path,
        config: dict,
    ) -> pd.DataFrame:
        """
        Wersja niskopamięciowa ``icon_compute_stats``.

        Czyta z SQLite po jednym parametrze naraz.
        Peak RAM ≈ dane jednego parametru (≈ 200–400 MB dla n_neighbors=300).
        Dla ``thunder_prob`` ładuje dwa parametry jednocześnie przez JOIN w SQLite
        (≈ 400–500 MB zamiast 800 MB).

        Parameters
        ----------
        db_path : str | Path
            Plik SQLite wyprodukowany przez ``icon_extract_to_db``.
        config : dict
            Ta sama konfiguracja YAML co dla ``icon_compute_stats``.
        """
        import sqlite3

        db_path     = Path(db_path)
        params_cfg  = config.get("parameters", {})
        percentiles = config.get("percentiles", [1, 5, 25, 50, 75, 95, 99])

        sn_to_key: dict[str, str] = {
            pcfg.get("grib_shortname", key): key
            for key, pcfg in params_cfg.items()
        }

        con = sqlite3.connect(db_path)

        def _load_sn(sn: str) -> pd.DataFrame:
            df = pd.read_sql(
                "SELECT * FROM raw_forecast WHERE shortName = ?",
                con, params=(sn,),
            )
            df["forecast_time"] = pd.to_datetime(df["forecast_time"])
            return df

        def _sn_exists(sn: str) -> bool:
            return pd.read_sql(
                "SELECT COUNT(*) AS cnt FROM raw_forecast WHERE shortName = ?",
                con, params=(sn,),
            )["cnt"].iloc[0] > 0

        def _save_derived(df: pd.DataFrame) -> None:
            df_save = df.copy()
            df_save["forecast_time"] = (
                df_save["forecast_time"]
                .dt.strftime("%Y-%m-%d %H:%M:%S")
                if pd.api.types.is_datetime64_any_dtype(df_save["forecast_time"])
                else df_save["forecast_time"]
            )
            cols = [c for c in self._DB_COLS if c in df_save.columns]
            df_save[cols].to_sql(
                "raw_forecast", con, if_exists="append", index=False,
                method="multi", chunksize=5000,
            )
            con.commit()

        # ── 1. Pochodne z akumulowanych / uśrednionych parametrów ────────
        for key, pcfg in params_cfg.items():
            source_sn = pcfg.get("grib_shortname", key)
            if not _sn_exists(source_sn):
                continue

            if pcfg.get("derive_hourly"):
                df_src  = _load_sn(source_sn)
                derived = self._derive_from_accumulated(
                    df_src, source_sn, "tp_hourly",
                    "Precipitation (hourly)", "mm/h", per_hour=True,
                )
                _save_derived(derived)
                del df_src, derived

            elif pcfg.get("derive_flux_avg"):
                result_sn = pcfg.get("derive_result", key + "_hourly")
                df_src    = _load_sn(source_sn)
                derived   = self._derive_flux_from_running_avg(df_src, source_sn, result_sn)
                del df_src
                _save_derived(derived)

                if pcfg.get("derive_clearness"):
                    clearness_sn = pcfg.get("clearness_result", result_sn + "_pct")
                    T_L          = float(pcfg.get("clearness_turbidity", _LINKE_TURBIDITY_PL))
                    clearness    = self._derive_solar_clearness(derived, result_sn, clearness_sn, T_L=T_L)
                    _save_derived(clearness)
                    del clearness
                del derived

        # ── 2. Prawdopodobieństwo burzy – JOIN w SQLite ──────────────────
        for key, pcfg in params_cfg.items():
            src = pcfg.get("storm_source")
            if not src:
                continue
            precip_sn  = src.get("precip_sn",  "tp_hourly")
            cape_sn    = src.get("cape_sn",     "cape_ml")
            precip_thr = float(src.get("precip_thr", 0.1))
            cape_thr   = float(src.get("cape_thr",   250.0))

            # JOIN w SQLite → ładuje tylko wynik (nie dwa osobne pełne tabele)
            query = f"""
                SELECT tp.*, cape.value AS _cape
                FROM raw_forecast tp
                JOIN raw_forecast cape
                  ON  tp.forecast_time  = cape.forecast_time
                  AND tp.target_name    = cape.target_name
                  AND tp.neighbor_rank  = cape.neighbor_rank
                  AND tp.msg_number     = cape.msg_number
                WHERE tp.shortName   = '{precip_sn}'
                  AND cape.shortName = '{cape_sn}'
            """
            merged = pd.read_sql(query, con)
            if merged.empty:
                continue
            merged["forecast_time"] = pd.to_datetime(merged["forecast_time"])
            merged["value"]     = (
                (merged["value"].astype(float) > precip_thr)
                & (merged["_cape"].astype(float) > cape_thr)
            ).astype(float)
            merged["shortName"] = key
            merged["name"]      = "Storm probability indicator"
            merged["units"]     = "1"
            _save_derived(merged.drop(columns=["_cape"], errors="ignore"))
            del merged

        # ── 3. Statystyki – jeden parametr naraz ────────────────────────
        agg_dict: dict = {
            "value_mean": ("value", "mean"),
            "value_std":  ("value", "std"),
            "value_min":  ("value", "min"),
            "value_max":  ("value", "max"),
        }
        for p in percentiles:
            agg_dict[f"value_p{p:02d}"] = ("value", lambda x, _p=p: x.quantile(_p / 100))

        all_stats: list[pd.DataFrame] = []

        for key, pcfg in params_cfg.items():
            grib_sn   = pcfg.get("grib_shortname", key)
            df_param  = _load_sn(grib_sn)
            if df_param.empty:
                continue

            df_stats = (
                df_param.groupby(_STATS_GROUP_COLS, dropna=False)
                .agg(**agg_dict)
                .reset_index()
            )

            thresholds: list[dict] = pcfg.get("thresholds") or []
            if thresholds:
                prob_cols: list[str] = []
                for thr in thresholds:
                    val:       float = thr["value"]
                    label:     str   = thr["label"]
                    direction: str   = thr.get("direction", "above")
                    if direction == "below":
                        col = f"prob_lt_{label}"
                        df_param[col] = (df_param["value"].astype(float) < val).astype(float)
                    else:
                        col = f"prob_gt_{label}"
                        df_param[col] = (df_param["value"].astype(float) > val).astype(float)
                    prob_cols.append(col)

                df_exc = (
                    df_param.groupby(_STATS_GROUP_COLS, dropna=False)[prob_cols]
                    .mean()
                    .reset_index()
                )
                df_stats = df_stats.merge(df_exc, on=_STATS_GROUP_COLS, how="left")

            del df_param
            all_stats.append(df_stats)

        con.close()

        if not all_stats:
            return pd.DataFrame()

        df_all = pd.concat(all_stats, ignore_index=True)
        stat_cols = [c for c in df_all.columns if c not in _STATS_GROUP_COLS]
        df_long = df_all.melt(
            id_vars=_STATS_GROUP_COLS,
            value_vars=stat_cols,
            var_name="aggregation_type",
            value_name="value",
        )
        df_long["aggregation_type"] = df_long["aggregation_type"].str.removeprefix("value_")
        return df_long.reset_index(drop=True)
