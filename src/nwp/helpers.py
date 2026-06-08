"""
Funkcje pomocnicze dla modułu NWP.
"""

from __future__ import annotations

import time
from pathlib import Path


_GRIB_SUFFIXES = {".grib2", ".grib", ".bz2", ".grb", ".grb2"}


def clean_grib_dir(
    path: str | Path,
    pattern: str = "*.grib2",
    recursive: bool = False,
) -> list[Path]:
    """
    Usuwa pliki GRIB z podanego folderu.

    Parameters
    ----------
    path : str lub Path
        Folder do wyczyszczenia.
    pattern : str
        Glob pattern plików do usunięcia. Domyślnie ``'*.grib2'``.
        Przykłady: ``'*.grib2'``, ``'*.bz2'``, ``'*'``.
    recursive : bool
        Jeśli ``True``, usuwa pliki rekurencyjnie w podfolderach.
        Domyślnie ``False``.

    Returns
    -------
    list of Path
        Lista usuniętych plików.

    Examples
    --------
    >>> deleted = clean_grib_dir("data/dwd/icon-eu-eps")
    >>> print(f"Usunięto {len(deleted)} plików")

    >>> # Usuń też skompresowane
    >>> clean_grib_dir("data/dwd/icon-eu-eps", pattern="*.bz2")
    """
    directory = Path(path)
    if not directory.exists():
        return []

    glob_fn = directory.rglob if recursive else directory.glob
    deleted: list[Path] = []

    for file in glob_fn(pattern):
        if not file.is_file():
            continue
        for attempt in range(3):
            try:
                file.unlink()
                deleted.append(file)
                break
            except PermissionError:
                if attempt < 2:
                    time.sleep(0.5)
                else:
                    print(f"  Pominięto (plik zajęty): {file.name}")

    return deleted
