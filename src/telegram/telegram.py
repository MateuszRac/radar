"""
Powiadomienia Telegram o silnym opadzie w monitorowanych punktach
=================================================================
Sprawdza prognozy deterministyczne (STEPS / LINDA — te, które policzono w danym
przebiegu) w punktach z ``config/watch_points.json``. Jeśli w którymkolwiek kroku
prognozy przewidywane natężenie opadu w punkcie przekroczy próg (domyślnie
10 mm/h), wysyła wiadomość Telegram zawierającą m.in. **algorytm**, z którego
pochodzi ostrzeżenie.

Wymaga zmiennych środowiskowych (np. w ``.env``):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import requests
from dotenv import load_dotenv
from pyproj import Proj, Transformer

load_dotenv()
log = logging.getLogger(__name__)

# ── Ścieżki i parametry ───────────────────────────────────────────────────────
PROJECT_ROOT      = Path(__file__).resolve().parents[2]   # src/telegram → src → root
WATCH_POINTS_FILE = PROJECT_ROOT / "config" / "watch_points.json"
ALERT_STATE_FILE  = PROJECT_ROOT / "data" / "telegram_alert_state.json"

ALERT_THRESHOLD_MMH = 10.0       # próg natężenia opadu [mm/h]
ALERT_COOLDOWN_S    = 30 * 60    # min. odstęp między alertami dla (punkt, algorytm)

# Granice administracyjne (geoBoundaries, uproszczone) — pobierane RAZ do data/geo/
# i odtąd czytane z dysku. ADM1 = województwa, ADM2 = powiaty.
GEO_CACHE_DIR = PROJECT_ROOT / "data" / "geo"
_GEOBOUNDARIES = {
    "woj": ("pol_adm1.geojson",
            "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/"
            "gbOpen/POL/ADM1/geoBoundaries-POL-ADM1_simplified.geojson"),
    "pow": ("pol_adm2.geojson",
            "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/"
            "gbOpen/POL/ADM2/geoBoundaries-POL-ADM2_simplified.geojson"),
}
_BOUNDARIES_CACHE: dict = {}     # projdef → (woj_geom, pow_geom) w natywnym CRS radaru


ALERT_MAP_WINDOW_KM = 100.0      # bok kwadratu mapki alertu [km]


def _send_telegram(token: str, chat_id: str, text: str) -> bool:
    """Wysyła wiadomość przez Bot API Telegrama. Zwraca True przy sukcesie."""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        log.warning("Błąd wysyłania Telegram: %s", exc)
        return False


def _send_telegram_photo(token: str, chat_id: str, caption: str, photo: bytes) -> bool:
    """Wysyła zdjęcie (mapkę alertu) z podpisem przez Bot API. True przy sukcesie."""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("alert.png", photo, "image/png")},
            timeout=30,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        log.warning("Błąd wysyłania zdjęcia Telegram: %s", exc)
        return False


_RATE_PALETTE = None      # cache (cmap, norm) palety RATE — wczytywane leniwie


def _get_rate_palette():
    """Zwraca (cmap, norm) palety RATE z radar.palette — ta sama co overlaye."""
    global _RATE_PALETTE
    if _RATE_PALETTE is None:
        from radar.palette import RadarPalette
        cmap, norm, _ = RadarPalette().get("RATE", style="imgw")
        _RATE_PALETTE = (cmap, norm)
    return _RATE_PALETTE


def _ensure_geojson(fname: str, url: str) -> Path:
    """Zwraca ścieżkę do pliku granic; pobiera RAZ jeśli go nie ma w data/geo/."""
    path = GEO_CACHE_DIR / fname
    if path.exists() and path.stat().st_size > 0:
        return path
    GEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Pobieranie granic administracyjnych (jednorazowo): %s", fname)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    path.write_bytes(resp.content)
    return path


def _get_admin_boundaries(projdef: str):
    """
    Zwraca (województwa, powiaty) jako GeoSeries w natywnym CRS radaru, albo
    (None, None) gdy granice niedostępne. Plik pobierany raz (cache na dysku),
    a reprojekcja liczona raz na proces (cache w pamięci, kluczowany projdef).
    """
    if projdef in _BOUNDARIES_CACHE:
        return _BOUNDARIES_CACHE[projdef]
    result = (None, None)
    try:
        import geopandas as gpd
        geoms = []
        for key in ("woj", "pow"):
            fname, url = _GEOBOUNDARIES[key]
            gdf = gpd.read_file(_ensure_geojson(fname, url))
            geoms.append(gdf.to_crs(projdef).geometry)
        result = (geoms[0], geoms[1])
    except Exception as exc:
        log.warning("Granice administracyjne niedostępne (%s) — mapka bez granic.", exc)
    _BOUNDARIES_CACHE[projdef] = result
    return result


def _render_alert_map(
    obs_field: np.ndarray,
    info: dict,
    lat: float,
    lon: float,
    name: str,
    algo: str,
    peak_val: float,
    valid_peak: datetime,
    window_km: float = ALERT_MAP_WINDOW_KM,
) -> bytes | None:
    """
    Renderuje mapkę PNG: wycinek ``window_km × window_km`` z aktualnego skanu
    radaru, wyśrodkowany na punkcie (lat, lon), z zaznaczonym punktem i paletą
    RATE. Zwraca bajty PNG albo ``None`` gdy się nie uda (punkt poza siatką itp.).

    Wycinek bierzemy wprost z natywnej siatki radaru (regularnej) — na 100 km
    zniekształcenie projekcji jest pomijalne, więc nie ma potrzeby reprojekcji.
    """
    try:
        import io
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.cm import ScalarMappable

        cmap, norm = _get_rate_palette()

        to_native = Transformer.from_proj(Proj("EPSG:4326"), Proj(info["projdef"]), always_xy=True)
        xn, yn = to_native.transform(lon, lat)
        x1, x2, y1, y2 = info["x1"], info["x2"], info["y1"], info["y2"]
        xsize, ysize = info["xsize"], info["ysize"]
        if not (x1 <= xn <= x2 and y1 <= yn <= y2):
            return None

        px_m  = (x2 - x1) / xsize           # rozmiar piksela [m]
        px_km = px_m / 1000.0
        col = int(round((xn - x1) / (x2 - x1) * (xsize - 1)))
        row = int(round((yn - y1) / (y2 - y1) * (ysize - 1)))
        half = max(int(round((window_km * 1000 / 2) / px_m)), 1)

        c0, c1 = max(col - half, 0), min(col + half + 1, xsize)
        r0, r1 = max(row - half, 0), min(row + half + 1, ysize)

        sub = np.asarray(obs_field[r0:r1, c0:c1], dtype=np.float64)
        sub = np.ma.masked_where(~np.isfinite(sub) | (sub <= 0), sub)
        rgba = ScalarMappable(norm=norm, cmap=cmap).to_rgba(sub, bytes=True)

        # Extent w km względem punktu (marker w 0,0 — poprawne także przy krawędzi).
        extent = [(c0 - col) * px_km, (c1 - 1 - col) * px_km,
                  (r0 - row) * px_km, (r1 - 1 - row) * px_km]

        fig = Figure(figsize=(5.2, 5.4), dpi=110)
        FigureCanvasAgg(fig)
        ax = fig.add_axes([0.10, 0.10, 0.84, 0.80])
        ax.set_facecolor("#e7edf3")                       # tło = brak opadu
        ax.imshow(rgba, origin="lower", extent=extent, interpolation="nearest")

        # Granice administracyjne — przeniesione do układu „km od punktu":
        # (x - xn)/1000, (y - yn)/1000. Powiaty cienką szarą, województwa grubą czarną.
        woj, pow_ = _get_admin_boundaries(info["projdef"])

        def _draw(geoms, **kw):
            if geoms is None:
                return
            g = geoms.translate(xoff=-xn, yoff=-yn).scale(xfact=1e-3, yfact=1e-3, origin=(0, 0))
            g.boundary.plot(ax=ax, **kw)

        _draw(pow_, color="#777777", linewidth=0.5, zorder=2)   # powiaty
        _draw(woj, color="#000000", linewidth=1.4, zorder=3)    # województwa

        ax.plot(0, 0, marker="o", ms=12, mfc="none", mec="#cc0000", mew=2.2, zorder=5)
        ax.plot(0, 0, marker="+", ms=16, color="#cc0000", mew=2.2, zorder=5)
        ax.grid(True, color="#ffffff", alpha=0.5, lw=0.5)
        ax.set_xlim(extent[0], extent[1])                 # przytnij do okna (granice nie rozpychają)
        ax.set_ylim(extent[2], extent[3])
        ax.set_xlabel("km od punktu")
        ax.set_ylabel("km od punktu")
        ax.set_title(f"{name} — {algo}\nszczyt {peak_val:.1f} mm/h  •  ok. {valid_peak:%H:%M} UTC",
                     fontsize=11)

        sm = ScalarMappable(norm=norm, cmap=cmap); sm.set_array([])
        cb = fig.colorbar(sm, ax=ax, fraction=0.046, pad=0.03)
        cb.set_label("natężenie opadu [mm/h]")

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor="white")
        return buf.getvalue()
    except Exception as exc:
        log.warning("Nie udało się wyrenderować mapki alertu (%s): %s", name, exc)
        return None


def _sample_series(stack: np.ndarray, info: dict, lat: float, lon: float) -> np.ndarray | None:
    """
    Zwraca szereg wartości prognozy (po krokach czasowych) w punkcie (lat, lon),
    albo ``None`` gdy punkt leży poza siatką radaru.

    Konwencja siatki (jak w read_compo_h5 + build_pysteps_metadata):
      wiersz 0 = y1 (południe), kolumna 0 = x1 (zachód), yorigin "lower".
    """
    to_native = Transformer.from_proj(Proj("EPSG:4326"), Proj(info["projdef"]), always_xy=True)
    xn, yn = to_native.transform(lon, lat)

    x1, x2, y1, y2 = info["x1"], info["x2"], info["y1"], info["y2"]
    xsize, ysize = info["xsize"], info["ysize"]
    if not (x1 <= xn <= x2 and y1 <= yn <= y2):
        return None

    col = int(round((xn - x1) / (x2 - x1) * (xsize - 1)))
    row = int(round((yn - y1) / (y2 - y1) * (ysize - 1)))
    col = min(max(col, 0), xsize - 1)
    row = min(max(row, 0), ysize - 1)

    arr = np.asarray(stack, dtype=np.float64)
    return np.nan_to_num(arr[:, row, col], nan=0.0)


def _load_state() -> dict:
    if ALERT_STATE_FILE.exists():
        try:
            return json.loads(ALERT_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def notify_precip_alerts(
    forecasts: dict[str, np.ndarray | None],
    info: dict,
    last_ts: datetime,
    timestep: int = 5,
    threshold: float = ALERT_THRESHOLD_MMH,
    cooldown_s: int = ALERT_COOLDOWN_S,
    obs_field: np.ndarray | None = None,
) -> None:
    """
    Sprawdza punkty monitorowane i wysyła alerty Telegram o silnym opadzie.

    Parametry
    ---------
    forecasts:
        Mapowanie ``nazwa_algorytmu → stos (N_LEADTIMES, ysize, xsize) [mm/h]``,
        np. ``{"STEPS": R_det, "LINDA": R_linda}``. Wartości ``None`` (np. gdy
        LINDA pominięta) są ignorowane.
    info:
        Geometria siatki radaru (projdef, x1/x2/y1/y2, xsize/ysize).
    last_ts:
        Czas ostatniego skanu radaru (UTC) — baza dla czasów ważności prognozy.
    obs_field:
        Aktualny skan radaru (2D, mm/h, natywna siatka). Gdy podany, do alertu
        dołączana jest mapka 100×100 km wyśrodkowana na punkcie. Bez niego
        wysyłany jest sam tekst.
    """
    token   = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.info("Telegram: brak TELEGRAM_BOT_TOKEN/CHAT_ID — pomijam alerty.")
        return

    forecasts = {k: v for k, v in forecasts.items() if v is not None}
    if not forecasts:
        return
    if not WATCH_POINTS_FILE.exists():
        log.warning("Brak %s — pomijam alerty Telegram.", WATCH_POINTS_FILE)
        return
    try:
        watch_points = json.loads(WATCH_POINTS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Nie można wczytać watch_points.json: %s", exc)
        return

    state    = _load_state()
    now_ts   = time.time()
    modified = False

    for algo, stack in forecasts.items():
        for point in watch_points:
            name = point.get("name", "?")
            try:
                lat, lon = float(point["lat"]), float(point["lon"])
            except (KeyError, ValueError, TypeError):
                continue

            series = _sample_series(stack, info, lat, lon)
            if series is None:
                continue

            above = series > threshold
            if not above.any():
                continue

            # Cooldown osobny dla każdej pary (punkt, algorytm)
            key = f"{name}|{algo}"
            if now_ts - state.get(key, 0) < cooldown_s:
                log.debug("Alert %s w cooldownie — pomijam.", key)
                continue

            first_idx = int(np.argmax(above))          # pierwszy krok > progu
            peak_idx  = int(np.argmax(series))          # krok z maksimum
            peak_val  = float(series[peak_idx])
            lead_first = (first_idx + 1) * timestep
            lead_peak  = (peak_idx + 1) * timestep
            valid_first = last_ts + timedelta(minutes=lead_first)
            valid_peak  = last_ts + timedelta(minutes=lead_peak)

            msg = (
                f"⛈️ <b>Ostrzeżenie o silnym opadzie</b>\n"
                f"Punkt: <b>{name}</b>\n"
                f"Algorytm prognozy: <b>{algo}</b>\n"
                f"\n"
                f"Prognozowane natężenie do <b>{peak_val:.1f} mm/h</b> "
                f"(próg {threshold:.0f} mm/h)\n"
                f"Przekroczenie progu od +{lead_first} min "
                f"(ok. {valid_first:%H:%M} UTC)\n"
                f"Szczyt: +{lead_peak} min (ok. {valid_peak:%H:%M} UTC)\n"
                f"Ostatni skan radaru: {last_ts:%Y-%m-%d %H:%M} UTC"
            )

            photo = None
            if obs_field is not None:
                photo = _render_alert_map(obs_field, info, lat, lon, name, algo,
                                          peak_val, valid_peak)
            sent = (_send_telegram_photo(token, chat_id, msg, photo) if photo
                    else _send_telegram(token, chat_id, msg))

            if sent:
                state[key] = now_ts
                modified = True
                log.info("Alert Telegram → %s [%s] %.1f mm/h%s",
                         name, algo, peak_val, " (z mapką)" if photo else "")

    if modified:
        ALERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ALERT_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
