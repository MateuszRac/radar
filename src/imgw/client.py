"""Klient HTTP do publicznego API IMGW (danepubliczne.imgw.pl)."""

import logging
import re
import time
from datetime import datetime

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.exceptions import ConnectTimeout, ReadTimeout

log = logging.getLogger(__name__)

_API_URL  = "https://danepubliczne.imgw.pl/pl/datastore/getFilesList"
_BASE_URL = "https://danepubliczne.imgw.pl/pl/"

_UNIT_NORM = {
    "dBZ": "DBZH", "V": "VRADH", "dBR": "RATE",
    "RhoHV": "RHOHV", "PhiDP": "PHIDP",
}


class ImgwClient:
    """Pobiera listę plików i ściąga pliki z publicznego API IMGW."""

    def __init__(self, product_type: str = "oper", timeout: tuple = (5, 60)):
        self._product_type = product_type
        self._timeout = timeout

    def get_file_list(self, path: str) -> pd.DataFrame | None:
        """
        Pobiera listę plików dla ścieżki produktu.
        Zwraca DataFrame z kolumnami: url, filename, timestamp, unit, level.
        """
        log.debug("Pobieranie listy plików: %s", path)
        try:
            resp = requests.post(
                _API_URL,
                data={"productType": self._product_type, "path": path},
                timeout=15,
            )
        except requests.RequestException as e:
            log.error("Błąd API IMGW (%s): %s", path, e)
            return None

        if resp.status_code != 200:
            log.warning("IMGW zwrócił HTTP %s dla: %s", resp.status_code, path)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        files = [
            (_BASE_URL + a["href"].strip(), a.get_text(strip=True))
            for a in soup.find_all("a", href=True)
        ]
        if not files:
            log.debug("Brak plików na liście dla: %s", path)
            return None

        df = pd.DataFrame(files, columns=["url", "filename"])
        df["timestamp"] = df["filename"].apply(self._timestamp_from_filename)
        df["unit"]      = df["filename"].apply(self._unit_from_filename)
        df["level"]     = df["filename"].apply(self._level_from_filename)
        log.debug("Znaleziono %d plików dla: %s", len(df), path)
        return df

    def download_file(self, url: str, output_path: str, max_retries: int = 5,
                      chunk_size: int = 65536) -> bool:
        """Pobiera plik z URL strumieniowo z retry i backoff wykładniczym."""
        for attempt in range(1, max_retries + 1):
            try:
                with requests.get(url, timeout=self._timeout, stream=True) as resp:
                    resp.raise_for_status()
                    with open(output_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=chunk_size):
                            f.write(chunk)
                return True
            except (ConnectTimeout, ReadTimeout):
                if attempt == max_retries:
                    log.error("Timeout po %d próbach: %s", max_retries, url)
                    return False
                wait = 2 ** attempt
                log.warning("Timeout (próba %d/%d), czekam %ds: %s",
                            attempt, max_retries, wait, url)
                time.sleep(wait)
            except requests.RequestException as e:
                log.error("Błąd pobierania %s: %s", url, e)
                return False
        return False

    @staticmethod
    def _timestamp_from_filename(filename: str) -> datetime | None:
        match = re.match(r"(\d{14})", filename)
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S") if match else None

    @staticmethod
    def _unit_from_filename(filename: str) -> str | None:
        match = re.match(r"\d{16}([A-Za-z]+)\.", filename)
        if not match:
            return None
        raw = match.group(1)
        return _UNIT_NORM.get(raw, raw)

    @staticmethod
    def _level_from_filename(filename: str) -> str | None:
        parts = filename.split(".")
        return parts[-2] if len(parts) >= 2 else None
