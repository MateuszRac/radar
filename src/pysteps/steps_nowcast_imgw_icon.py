#!/usr/bin/env python
"""
Prognoza S-PROG + ICON-EU  (drugi „typ” prognozy)
=================================================
Działa na tej samej zasadzie co ``steps_nowcast_imgw.py``:

  • domyślnie liczy deterministyczną ekstrapolację radarową **S-PROG**,
  • opcjonalnie (``--ensemble``) dokłada stochastyczny ensemble **STEPS**
    (mean + prawdopodobieństwa),

ale każdą prognozę dodatkowo **miesza liniowo z polem opadu modelu ICON-EU**
(DWD opendata) — radar dominuje na krótkich horyzontach, NWP na dłuższych:

  +0   min → 100% radar,   0% NWP
  +60  min →  50% radar,  50% NWP
  +120 min →   0% radar, 100% NWP

Format wyjścia jest **identyczny** jak w skrypcie bazowym (te same overlaye
``obs_*`` / ``fcst_*`` w ``www/data/`` i ta sama paleta RATE). Różni się tylko
plik ``meta.json``, który oznacza ten przebieg jako prognozę „+ICON”.

Implementacja overlayów, dekodowania HDF5 i palet jest reużywana z
``steps_nowcast_imgw.py`` — gwarancja identycznego formatu danych.

Uruchomienie:
    uv run python src/pysteps/steps_nowcast_imgw_icon.py              # tylko S-PROG+ICON
    uv run python src/pysteps/steps_nowcast_imgw_icon.py --ensemble   # + mean + prob. (blend)
"""

import importlib.util as _ilu
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import eccodes
import numpy as np
from pyproj import Proj, Transformer
from scipy.interpolate import RegularGridInterpolator

try:
    from imgw.client import ImgwClient
    from nwp.core import NWP
except ImportError:
    sys.path.append(str(Path(__file__).resolve().parents[1]))  # → src/
    from imgw.client import ImgwClient
    from nwp.core import NWP

from pysteps import nowcasts
from pysteps.motion.lucaskanade import dense_lucaskanade
from pysteps.postprocessing.ensemblestats import excprob
from pysteps.utils import transformation

# ── Reużycie implementacji bazowego skryptu (identyczny format wyjścia) ────────
# Ładujemy po ścieżce pliku — `steps_nowcast_imgw` jako nazwa pakietu kolidowałaby
# z zainstalowaną biblioteką `pysteps`, a tu chcemy konkretny moduł z src/pysteps/.
_BASE_PATH = Path(__file__).resolve().parent / "steps_nowcast_imgw.py"
_spec = _ilu.spec_from_file_location("steps_nowcast_imgw_base", _BASE_PATH)
base = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(base)

# ── Konfiguracja logowania ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Parametry (spójne z bazowym skryptem → identyczna siatka i horyzont) ───────
TIMESTEP      = base.TIMESTEP
N_FRAMES      = base.N_FRAMES
N_LEADTIMES   = base.N_LEADTIMES
N_ENS_MEMBERS = base.N_ENS_MEMBERS
SEED          = base.SEED

HORIZON_MIN = N_LEADTIMES * TIMESTEP        # całkowity horyzont prognozy [min]
HORIZON_H   = HORIZON_MIN / 60.0

COMPO_PATH = "/Oper/Polrad/Produkty/HVD/HVD_COMPO_DPSRI.comp.sri"
ICON_PARAM = "tot_prec"   # akumulowany opad ICON-EU [kg m-2 = mm]

FORECAST_TYPE       = "S-PROG + ICON-EU"
FORECAST_TYPE_SHORT = "S-PROG+ICON"


# ── Funkcje ICON-EU ───────────────────────────────────────────────────────────

def find_icon_eu_run(nwp: NWP, t_now: datetime) -> tuple[datetime, str, list[int]]:
    """
    Wyszukuje najnowszy dostępny run ICON-EU na DWD i wyznacza listę kroków
    godzinowych tot_prec potrzebnych do pokrycia całego horyzontu prognozy
    [t_now, t_now + HORIZON_MIN].

    ICON-EU startuje co 3h (00, 03, … 21 UTC); pliki gotowe ≥2h po init.

    Zwraca: (run_dt, hour_str, steps) — gdzie ``steps`` to kolejne kroki
    całkogodzinne potrzebne do de-akumulacji (różnice sąsiednich kroków
    dają natężenia godzinowe).
    """
    now_utc = datetime.utcnow()

    for delta_h in range(0, 28, 3):
        base_dt = now_utc - timedelta(hours=delta_h)
        run_h   = (base_dt.hour // 3) * 3
        run_dt  = base_dt.replace(hour=run_h, minute=0, second=0, microsecond=0)

        if (now_utc - run_dt).total_seconds() < 7200:   # run jeszcze niegotowy
            continue

        elapsed_h = (t_now - run_dt).total_seconds() / 3600.0
        if elapsed_h < 0:
            continue

        step_first = int(elapsed_h)                       # krok ≤ t_now
        last_tau   = elapsed_h + HORIZON_H                # model. czas na końcu horyzontu
        last_hour  = int(np.floor(last_tau - 1e-9))       # ostatni potrzebny kubełek godzinowy
        steps      = list(range(step_first, last_hour + 2))  # +1 na akumulację domykającą

        if steps[0] < 0 or steps[-1] > 120:
            continue

        hour_str = f"{run_dt.hour:02d}"
        run_tag  = run_dt.strftime("%Y%m%d%H")
        try:
            df = nwp.dwd_list_files(f"icon-eu/grib/{hour_str}/{ICON_PARAM}/")
            if not df.empty and df["name"].str.contains(run_tag).any():
                log.info("ICON-EU run: %s UTC  |  kroki %s",
                         run_dt.strftime("%Y-%m-%d %H:%M"), steps)
                return run_dt, hour_str, steps
        except Exception:
            continue

    raise RuntimeError("Nie znaleziono aktualnego runu ICON-EU na serwerze DWD.")


def download_icon_eu_steps(
    nwp: NWP,
    run_dt: datetime,
    hour_str: str,
    steps: list[int],
    tmpdir: Path,
) -> dict[int, Path]:
    """Pobiera wybrane kroki tot_prec ICON-EU. Zwraca {step: ścieżka_grib2}."""
    run_tag  = run_dt.strftime("%Y%m%d%H")
    df_files = nwp.dwd_list_files(f"icon-eu/grib/{hour_str}/{ICON_PARAM}/")

    def _step(name: str) -> int | None:
        m = re.search(r"_(\d{10})_(\d{3,4})_", name)
        return int(m.group(2)) if (m and m.group(1) == run_tag) else None

    df_files["step"] = df_files["name"].apply(_step)
    df_sel = df_files[df_files["step"].isin(steps)].sort_values("step")

    missing = set(steps) - set(df_sel["step"].dropna().astype(int))
    if missing:
        raise RuntimeError(f"Brak kroków ICON-EU na serwerze DWD: {sorted(missing)}")

    df_dl = nwp.dwd_download(df_sel, save_path=tmpdir, unzip=True)
    return {int(row["step"]): Path(row["grib_path"]) for _, row in df_dl.iterrows()}


def read_icon_eu_grib(path: str | Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Odczytuje pierwsze pole z pliku GRIB2 ICON-EU.
    Zwraca (values_2d [Nj×Ni], lats_1d [Nj], lons_1d [Ni]).
    """
    with open(path, "rb") as fh:
        handle = eccodes.codes_grib_new_from_file(fh)
        try:
            values  = eccodes.codes_get_array(handle, "values").astype(np.float64)
            Ni      = eccodes.codes_get(handle, "Ni")
            Nj      = eccodes.codes_get(handle, "Nj")
            lat0    = eccodes.codes_get(handle, "latitudeOfFirstGridPointInDegrees")
            lat1    = eccodes.codes_get(handle, "latitudeOfLastGridPointInDegrees")
            lon0    = eccodes.codes_get(handle, "longitudeOfFirstGridPointInDegrees")
            lon1    = eccodes.codes_get(handle, "longitudeOfLastGridPointInDegrees")
            missing = eccodes.codes_get(handle, "missingValue")
            # Zeruj TYLKO punkty realnie brakujące. UWAGA: eccodes domyślnie
            # zwraca missingValue = 9999, więc próg „abs(v - missing) < 1e10”
            # zerował CAŁE pole (abs(2.5 - 9999) ≈ 9999 < 1e10 == True) — co
            # kasowało opad ICON i sprawiało, że ostatni krok blendu znikał.
            values[~np.isfinite(values)] = 0.0
            values[values == missing]    = 0.0      # maska/bitmap (np. 9999)
            values[np.abs(values) > 1e6] = 0.0      # gigantyczny GRIB2 missing (~1e20)
            values[values < 0.0]         = 0.0      # akumulacja nie może być ujemna
        finally:
            eccodes.codes_release(handle)

    lats = np.linspace(lat0, lat1, Nj)
    lons = np.linspace(lon0, lon1, Ni)
    return values.reshape(Nj, Ni), lats, lons


def reproject_icon_to_radar(
    icon_rate: np.ndarray,    # (Nj, Ni) mm/h na siatce ICON-EU lat-lon
    icon_lats: np.ndarray,    # (Nj,) — mogą być malejące (N→S)
    icon_lons: np.ndarray,    # (Ni,)
    radar_info: dict,
) -> np.ndarray:
    """Interpoluje dwuliniowo pole ICON-EU (regularne lat-lon) na siatkę radaru."""
    to_wgs84 = Transformer.from_proj(
        Proj(radar_info["projdef"]), Proj("EPSG:4326"), always_xy=True
    )
    xs = np.linspace(radar_info["x1"], radar_info["x2"], radar_info["xsize"])
    ys = np.linspace(radar_info["y1"], radar_info["y2"], radar_info["ysize"])
    Xr, Yr = np.meshgrid(xs, ys)
    r_lons, r_lats = to_wgs84.transform(Xr.ravel(), Yr.ravel())

    icon_lats = np.asarray(icon_lats, dtype=np.float64)
    icon_lons = np.asarray(icon_lons, dtype=np.float64)

    # RGI wymaga ściśle rosnących osi — TYLKO pełny obrót tablicy (flip),
    # nigdy argsort/przeplatanie kolumn (to dawało pasy w polu wyjściowym).
    if icon_lats[0] > icon_lats[-1]:
        icon_lats = icon_lats[::-1]
        icon_rate = icon_rate[::-1, :]
    if icon_lons[0] > icon_lons[-1]:
        icon_lons = icon_lons[::-1]
        icon_rate = icon_rate[:, ::-1]

    # Nie ruszamy siatki ICON. Zamiast tego przesuwamy długości punktów radaru
    # do układu osi ICON (która bywa w 0–360 lub rozszerzona ~336.5°…405°):
    #   r_lon_q = lon0 + ((r_lon - lon0) mod 360)  ∈ [lon0, lon0+360)
    # Dzięki temu punkty Polski trafiają w oś ICON niezależnie od konwencji,
    # a dane pozostają nienaruszone (brak pasów).
    lon0 = float(icon_lons[0])
    r_lons_q = lon0 + np.mod(r_lons - lon0, 360.0)

    interp = RegularGridInterpolator(
        (icon_lats, icon_lons), icon_rate,
        method="linear", bounds_error=False, fill_value=0.0,
    )
    result = interp(
        np.column_stack([r_lats.ravel(), r_lons_q.ravel()])
    ).reshape(radar_info["ysize"], radar_info["xsize"])
    return np.maximum(result, 0.0)


def build_icon_rate_stack(
    rates: list[np.ndarray],   # natężenia [mm/h] kolejnych godzin modelu (na siatce radaru)
    first_step: int,           # numer kroku ICON odpowiadający rates[0] (godzina [first_step, +1])
    elapsed_h: float,          # godzin od startu runu do t_now
) -> np.ndarray:
    """
    Buduje stos (N_LEADTIMES, ysize, xsize) natężeń ICON-EU [mm/h].
    Każdemu krokowi 5-min przypisuje stawkę godzinową właściwą dla jego
    bezwzględnego czasu modelu (elapsed_h + lead).
    """
    ny, nx = rates[0].shape
    stack = np.empty((N_LEADTIMES, ny, nx), dtype=np.float64)
    for i in range(N_LEADTIMES):
        tau = elapsed_h + (i + 1) * TIMESTEP / 60.0     # bezwzględny czas modelu [h]
        j = int(np.floor(tau)) - first_step             # indeks godziny w `rates`
        j = max(0, min(j, len(rates) - 1))
        stack[i] = rates[j]
    return np.maximum(stack, 0.0)


def fetch_icon_stack(nwp: NWP, t_now: datetime, info: dict) -> tuple[np.ndarray, datetime]:
    """
    Pełny pipeline ICON-EU: wyszukanie runu → pobranie → de-akumulacja →
    reprojekcja → stos (N_LEADTIMES, ysize, xsize) mm/h na siatce radaru.
    Zwraca (icon_stack, run_dt).
    """
    run_dt, hour_str, steps = find_icon_eu_run(nwp, t_now)
    elapsed_h = (t_now - run_dt).total_seconds() / 3600.0

    icon_tmpdir = Path(tempfile.mkdtemp(prefix="icon_eu_"))
    log.info("Pobieranie tot_prec ICON-EU (kroki %s)...", steps)
    step_files = download_icon_eu_steps(nwp, run_dt, hour_str, steps, icon_tmpdir)

    log.info("Odczyt i de-akumulacja tot_prec...")
    accum = {}
    icon_lats = icon_lons = None
    for s in steps:
        vals, lats, lons = read_icon_eu_grib(step_files[s])
        accum[s] = vals
        if icon_lats is None:
            icon_lats, icon_lons = lats, lons

    # Natężenia godzinowe = różnice akumulacji sąsiednich kroków
    icon_res_km = float(np.mean(np.diff(icon_lons)) * 111)
    rates = []
    for k in range(len(steps) - 1):
        s0, s1 = steps[k], steps[k + 1]
        rate_icon = np.maximum(accum[s1] - accum[s0], 0.0)
        rate_radar = reproject_icon_to_radar(rate_icon, icon_lats, icon_lons, info)
        # Diagnostyka: max na całej domenie ICON vs max po reprojekcji na siatce
        # radaru. Jeśli „ICON max” > 0, a „radar max” ≈ 0 → problem z reprojekcją
        # (np. zakres długości). Jeśli oba ≈ 0 → ICON faktycznie bez opadu.
        log.info("ICON-EU godz. [%d→%d]: max(ICON)=%.2f mm/h | max(na radarze)=%.2f mm/h, śr=%.3f",
                 s0, s1, float(rate_icon.max()),
                 float(rate_radar.max()), float(rate_radar.mean()))
        rates.append(rate_radar)

    log.info("Reprojekcja ICON-EU (~%.0f km/px) → siatka radaru (%d godz. natężeń).",
             icon_res_km, len(rates))

    icon_stack = build_icon_rate_stack(rates, steps[0], elapsed_h)
    return icon_stack, run_dt


def blend_radar_nwp(radar: np.ndarray, icon_stack: np.ndarray) -> np.ndarray:
    """
    Liniowy blend radar↔NWP zależny od horyzontu. Działa zarówno dla pola
    (N_LEADTIMES, y, x) jak i ensemblu (N_ENS, N_LEADTIMES, y, x) — wagi
    rozgłaszają się po osi leadtime.
    """
    lead_min = np.array([(i + 1) * TIMESTEP for i in range(N_LEADTIMES)], dtype=np.float64)
    w_nwp    = np.clip(lead_min / HORIZON_MIN, 0.0, 1.0)   # 0 → 1
    w_radar  = 1.0 - w_nwp

    if radar.ndim == 4:        # (ens, lead, y, x)
        wr = w_radar[None, :, None, None]
        wn = w_nwp[None, :, None, None]
    else:                      # (lead, y, x)
        wr = w_radar[:, None, None]
        wn = w_nwp[:, None, None]

    return np.maximum(wr * radar + wn * icon_stack, 0.0)


def build_forecast_meta(run_dt: datetime, last_ts: datetime, with_linda: bool = True) -> dict:
    """
    Buduje słownik meta dla manifestu — lista dostępnych typów prognozy
    (S-PROG / LINDA, każdy także w wariancie z ICON-EU) + czas runu ICON-EU.
    """
    types = [{"key": "det", "label": "S-PROG", "short": "S-PROG"}]
    if with_linda:
        types.append({"key": "linda", "label": "LINDA", "short": "LINDA"})
    types.append({"key": "icon", "label": "S-PROG + ICON-EU", "short": "S-PROG+ICON"})
    if with_linda:
        types.append({"key": "lindaicon", "label": "LINDA + ICON-EU", "short": "LINDA+ICON"})
    return {
        "types":      types,
        "default":    "det",
        "radar_scan": last_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "icon_run":   run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ── Główna logika ─────────────────────────────────────────────────────────────

def main(ensemble: bool = False, linda: bool = True) -> None:
    """
    Pipeline S-PROG (+ opcjonalnie LINDA / ensemble STEPS) zmiksowany z ICON-EU.
    Gdy ``linda=True`` (domyślnie) generuje też typy ``linda`` i ``lindaicon``.
    Wyjście identyczne jak w skrypcie bazowym + plik meta.json (typ „+ICON”).
    """
    client = ImgwClient()
    nwp    = NWP()

    # 1. Lista + pobranie skanów DPSRI (z cache, reużyte z bazowego skryptu) ──
    records = base.fetch_radar_scans(client)

    # 2. Dekodowanie HDF5 (reużyte z bazowego skryptu) ────────────────────────
    log.info("Dekodowanie plików HDF5...")
    frames     = [base.read_compo_h5(p) for _, p in records]
    info       = frames[0]
    timestamps = [f["timestamp"] for f in frames]

    R_obs = np.stack([f["data"] for f in frames]).astype(np.float64)
    R_obs[R_obs < 0] = np.nan
    metadata = base.build_pysteps_metadata(info, timestamps)

    # 3. dBR transform + pole ruchu ───────────────────────────────────────────
    R_dBR, _ = transformation.dB_transform(
        R_obs.copy(), metadata.copy(), threshold=0.1, zerovalue=-15.0
    )
    R_dBR[~np.isfinite(R_dBR)] = -15.0

    log.info("Estymacja pola ruchu (Lucas-Kanade)...")
    V = dense_lucaskanade(R_dBR)

    kmperpixel = (info["x2"] - info["x1"]) / info["xsize"] / 1000.0
    log.info("Rozdzielczość siatki: %.2f km/px  |  %d × %d px",
             kmperpixel, info["xsize"], info["ysize"])

    # 4. S-PROG deterministyczna (zawsze) ─────────────────────────────────────
    log.info("Obliczanie S-PROG (deterministyczna)...")
    sprog = nowcasts.get_method("sprog")
    R_det = sprog(R_dBR[-3:, :, :], V, N_LEADTIMES,
                  n_cascade_levels=6, precip_thr=-10.0)
    R_det = np.maximum(transformation.dB_transform(R_det, threshold=-10.0, inverse=True)[0], 0.0)

    # 4b. LINDA deterministyczna (opcjonalnie) ────────────────────────────────
    R_linda = base.compute_linda(R_obs, V, kmperpixel) if linda else None

    # 5. ICON-EU → stos natężeń na siatce radaru ──────────────────────────────
    t_now = timestamps[-1]
    icon_stack, run_dt = fetch_icon_stack(nwp, t_now, info)

    log.info("Blending radar ↔ ICON-EU (radar 100%%→0%%, NWP 0%%→100%% w %d min)...",
             HORIZON_MIN)

    # 6. Blendy deterministyczne — warianty z ICON ───────────────────────────
    # det → produkt `det` (S-PROG), linda → `linda` (LINDA), a ich blendy z ICON
    # to `icon` (S-PROG+ICON) oraz `lindaicon` (LINDA+ICON).
    R_det_blend   = blend_radar_nwp(R_det, icon_stack)
    R_linda_blend = blend_radar_nwp(R_linda, icon_stack) if R_linda is not None else None

    # Diagnostyka ostatniego kroku (lead = HORIZON_MIN, waga NWP = 100%):
    # icon_stack[-1] to czysto ICON. Jeśli ≈ 0, opad w ostatniej klatce zniknie.
    log.info("Ostatni krok (+%d min): max S-PROG(det)=%.2f | max ICON=%.2f | "
             "max blend(icon)=%.2f mm/h",
             HORIZON_MIN, float(R_det[-1].max()),
             float(icon_stack[-1].max()), float(R_det_blend[-1].max()))

    # 7. Ensemble STEPS (opcjonalnie) → blend → mean + prawdopodobieństwa ──────
    R_mean: np.ndarray | None = None
    P_all:  np.ndarray | None = None

    if ensemble:
        n_workers = os.cpu_count() or 1
        log.info("Obliczanie STEPS: %d kroków × %d min, %d członków (%d wątków)...",
                 N_LEADTIMES, TIMESTEP, N_ENS_MEMBERS, n_workers)
        steps_method = nowcasts.get_method("steps")
        R_f = steps_method(
            R_dBR[-3:, :, :], V, N_LEADTIMES, N_ENS_MEMBERS,
            n_cascade_levels=4, precip_thr=-10.0, kmperpixel=kmperpixel,
            timestep=TIMESTEP, noise_method="nonparametric",
            vel_pert_method="bps", mask_method="incremental",
            num_workers=n_workers, seed=SEED,
        )
        R_f = np.maximum(transformation.dB_transform(R_f, threshold=-10.0, inverse=True)[0], 0.0)

        R_blended = blend_radar_nwp(R_f, icon_stack)   # (ens, lead, y, x)
        R_mean = np.nanmean(R_blended, axis=0)

        log.info("Obliczanie prawdopodobieństw przekroczenia progów...")
        P_all = np.stack([
            np.stack([excprob(R_blended[:, i, :, :], thr) for i in range(N_LEADTIMES)])
            for thr, _, _ in base.PROB_THRESHOLDS
        ])
    else:
        log.info("Pomijanie ensemblu STEPS — tylko prognoza deterministyczna (S-PROG+ICON).")

    log.info("Prognoza gotowa. Przygotowywanie overlayów...")

    # 8. Overlaye Leaflet (reużyte) — typy: det, linda, icon, lindaicon ───────
    last_ts = timestamps[-1]
    extra = []
    if R_linda is not None:
        extra.append(("linda", R_linda))
    extra.append(("icon", R_det_blend))
    if R_linda_blend is not None:
        extra.append(("lindaicon", R_linda_blend))

    base.generate_leaflet_overlays(
        frames, R_mean, R_det, P_all, info, last_ts,
        extra_dets=extra,
        meta=build_forecast_meta(run_dt, last_ts, with_linda=R_linda is not None),
    )

    # Alerty Telegram o silnym opadzie — sprawdzamy czyste prognozy radarowe
    # (STEPS / LINDA), bez wariantów ICON (te tłumią opad na krótkich horyzontach).
    try:
        forecasts = {"STEPS": R_det}
        if R_linda is not None:
            forecasts["LINDA"] = R_linda
        base.notify_precip_alerts(forecasts, info, last_ts, timestep=TIMESTEP)
    except Exception as exc:
        log.warning("Powiadomienia Telegram pominięte: %s", exc)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Prognoza S-PROG + ICON-EU (overlaye Leaflet). "
                    "Domyślnie tylko deterministyczna."
    )
    parser.add_argument(
        "--ensemble", dest="ensemble", action="store_true",
        help="Dodatkowo policz ensemble STEPS (blend z ICON): mean + prawdopodobieństwa.",
    )
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--linda", dest="linda", action="store_true",
                     help="Generuj też LINDA i LINDA+ICON (domyślnie włączone).")
    grp.add_argument("--no-linda", dest="linda", action="store_false",
                     help="Pomiń LINDA (tylko S-PROG i S-PROG+ICON).")
    parser.set_defaults(linda=True)
    args = parser.parse_args()

    main(ensemble=args.ensemble, linda=args.linda)
