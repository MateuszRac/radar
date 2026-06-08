#!/usr/bin/env python
"""
Pipeline prognozy STEPS – czyszczenie, generowanie, transfer FTP
=================================================================
Łączy trzy kroki w jeden uruchamialny pipeline:

1. **Czyszczenie** – usuwa istniejące obrazy radarowe (PNG + bounds.json +
   meta.json) z katalogu ``www/data``.
2. **Generowanie** – uruchamia odpowiedni skrypt nowcast z ``src/pysteps/``:
     • domyślnie  → ``steps_nowcast_imgw.py``       (tylko produkt ``det`` – bez ICON)
     • z ``--icon`` → ``steps_nowcast_imgw_icon.py`` (DWA typy obok siebie:
       ``det`` = S-PROG bez ICON oraz ``icon`` = S-PROG + ICON-EU; zapisuje też
       meta.json informujący front, że można przełączać typy).
3. **Transfer** – wysyła zawartość ``www/data`` na serwer przez FTP/FTPS
   (``src/transfer/ftp.py``), w trybie mirror (usuwa zdalnie nieaktualne pliki).

Uruchomienie:
    uv run python src/pipeline/pipeline.py                # S-PROG + LINDA (bez ICON)
    uv run python src/pipeline/pipeline.py --icon         # + warianty z ICON-EU
    uv run python src/pipeline/pipeline.py --no-linda     # tylko S-PROG (bez LINDA)
    uv run python src/pipeline/pipeline.py --no-upload    # bez transferu FTP

Konfiguracja FTP (zmienne środowiskowe / .env):
    FTP_HOST, FTP_PORT, FTP_USER, FTP_PASSWORD, FTP_TLS
    FTP_REMOTE_IMG_DIR  – katalog docelowy na serwerze
                          (domyślnie "/public_html/steps/data")
"""

import argparse
import importlib.util
import logging
import os
import sys
from pathlib import Path

# ── Ścieżki projektu ─────────────────────────────────────────────────────────
SRC_DIR        = Path(__file__).resolve().parents[1]          # → src/
PROJECT_ROOT   = Path(__file__).resolve().parents[2]          # → katalog główny
WWW_DATA_DIR   = PROJECT_ROOT / "www" / "data"
NOWCAST_BASE   = SRC_DIR / "pysteps" / "steps_nowcast_imgw.py"
NOWCAST_ICON   = SRC_DIR / "pysteps" / "steps_nowcast_imgw_icon.py"

# src/ na końcu sys.path → import `transfer.ftp` bez instalacji pakietu,
# a zainstalowany `pysteps` nie jest zasłonięty przez src/pysteps/.
if str(SRC_DIR) not in sys.path:
    sys.path.append(str(SRC_DIR))

from transfer.ftp import FtpUploader  # noqa: E402

# ── Logowanie ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pipeline")

# Pliki uznawane za artefakty prognozy (do wyczyszczenia przed nowym przebiegiem).
_DATA_GLOBS = ("*.png", "bounds.json", "manifest.json", "meta.json")


def _load_module(path: Path, name: str):
    """
    Ładuje moduł nowcast bezpośrednio z pliku (importlib).

    Nie używamy ``import pysteps.…`` — nazwa ``pysteps`` odnosi się do
    zainstalowanej biblioteki, nie do katalogu ``src/pysteps/``.
    """
    if not path.exists():
        log.error("Nie znaleziono skryptu prognozy: %s", path)
        sys.exit(1)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clean_radar_images(data_dir: Path = WWW_DATA_DIR) -> int:
    """Usuwa istniejące artefakty prognozy z katalogu danych. Zwraca liczbę plików."""
    data_dir.mkdir(parents=True, exist_ok=True)
    removed = 0
    for pattern in _DATA_GLOBS:
        for f in data_dir.glob(pattern):
            if f.is_file():
                f.unlink()
                removed += 1
    log.info("Czyszczenie %s: usunięto %d plik(ów).", data_dir, removed)
    return removed


def generate_images(icon: bool, linda: bool = True) -> None:
    """
    Uruchamia generator overlayów do www/data.

    icon=False → steps_nowcast_imgw.py        (S-PROG [+ LINDA])
    icon=True  → steps_nowcast_imgw_icon.py   (S-PROG [+ LINDA] + warianty ICON)
    linda      → czy dołączyć prognozy LINDA / LINDA+ICON.
    """
    metody = "S-PROG" + (" + LINDA" if linda else "")
    if icon:
        log.info("Generowanie typów: %s oraz warianty z ICON-EU...", metody)
        module = _load_module(NOWCAST_ICON, "steps_nowcast_imgw_icon")
    else:
        log.info("Generowanie typów: %s (bez ICON)...", metody)
        module = _load_module(NOWCAST_BASE, "steps_nowcast_imgw")
    module.main(ensemble=False, linda=linda)
    log.info("Generowanie zakończone.")


def upload_images(
    data_dir: Path = WWW_DATA_DIR,
    remote_dir: str | None = None,
) -> bool:
    """
    Wysyła zawartość katalogu danych na serwer przez FTP/FTPS (mirror).

    Zwraca ``True`` gdy transfer wykonano, ``False`` gdy FTP nie jest
    skonfigurowany (brak FTP_HOST / FTP_USER).
    """
    remote_dir = remote_dir or os.getenv("FTP_REMOTE_IMG_DIR", "/public_html/steps/data")
    uploader = FtpUploader()

    if not uploader.is_configured():
        log.warning("FTP nie jest skonfigurowany (ustaw FTP_HOST i FTP_USER) — pomijam transfer.")
        return False

    log.info("Transfer plików → %s%s", uploader.host, f"/{remote_dir}")
    # Synchronizacja atomowa: najpierw nowe pliki, potem manifest (przełączenie),
    # na końcu usunięcie starych — bez „okna bez danych” dla użytkownika.
    uploader.sync_atomic(local_dir=data_dir, remote_dir=remote_dir)
    return True


def run_pipeline(
    icon: bool = False,
    linda: bool = True,
    do_upload: bool = True,
    remote_dir: str | None = None,
) -> None:
    """Wykonuje pełny pipeline: czyszczenie → generowanie → transfer."""
    log.info("=== START pipeline STEPS%s%s ===",
             "  +LINDA" if linda else "", "  +ICON" if icon else "")

    clean_radar_images()
    generate_images(icon=icon, linda=linda)

    if do_upload:
        upload_images(remote_dir=remote_dir)
    else:
        log.info("Transfer FTP pominięty (--no-upload).")

    log.info("=== KONIEC pipeline STEPS ===")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pipeline STEPS: czyszczenie www/data, generowanie obrazów "
                    "prognostycznych i transfer FTP.",
    )
    parser.add_argument(
        "--icon", dest="icon", action="store_true",
        help="Generuj dwa typy prognozy: bez ICON (det) i z ICON-EU (icon). "
             "Domyślnie tylko bez ICON.",
    )
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--linda", dest="linda", action="store_true",
                     help="Generuj też prognozy LINDA (domyślnie włączone).")
    grp.add_argument("--no-linda", dest="linda", action="store_false",
                     help="Pomiń LINDA (szybciej, tylko S-PROG).")
    parser.set_defaults(linda=True)
    parser.add_argument(
        "--no-upload", dest="upload", action="store_false",
        help="Nie wysyłaj plików przez FTP (tylko czyszczenie + generowanie).",
    )
    parser.set_defaults(upload=True)
    parser.add_argument(
        "--remote-dir", dest="remote_dir", default=None,
        help="Katalog docelowy na serwerze FTP "
             "(domyślnie zmienna FTP_REMOTE_IMG_DIR lub '/public_html/steps/data').",
    )

    args = parser.parse_args()
    run_pipeline(icon=args.icon, linda=args.linda,
                 do_upload=args.upload, remote_dir=args.remote_dir)


if __name__ == "__main__":
    main()
