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

            if _send_telegram(token, chat_id, msg):
                state[key] = now_ts
                modified = True
                log.info("Alert Telegram → %s [%s] %.1f mm/h", name, algo, peak_val)

    if modified:
        ALERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ALERT_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
