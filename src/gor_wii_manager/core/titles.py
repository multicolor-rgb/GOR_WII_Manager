"""WiiTDB / GameTDB title database with a local cache.

Supports both the plain ``wiitdb.txt`` (English titles) and the
``wiitdb.zip`` archive which contains ``wiitdb.xml`` with localized titles.
"""

from __future__ import annotations

import io
import os
import re
import urllib.request
import zipfile
import xml.etree.ElementTree as ElementTree

DEFAULT_URL = "https://www.gametdb.com/wiitdb.zip"
TEXT_URL = "https://www.gametdb.com/wiitdb.txt"
USER_AGENT = "gor-wii-manager/0.1 (+linux)"

_LINE = re.compile(r"^\s*([A-Z0-9]{6})\s*=\s*(.+?)\s*$")
_HEADER_KEYS = {"TITLES", "VERSION"}


class TitleDB:
    """Game titles by 6 character disc ID."""

    def __init__(self, cache_directory: str, language: str = "pl"):
        self.cache_directory = cache_directory
        self.language = language
        self.titles: dict[str, str] = {}
        self.loaded_from: list[str] = []

    # -- files -----------------------------------------------------------
    def _path(self, name: str) -> str:
        return os.path.join(self.cache_directory, name)

    def _read_file(self, path: str) -> int:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return self.import_text(handle.read())
        except OSError:
            return 0

    def import_text(self, text: str) -> int:
        count = 0
        for line in text.splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            match = _LINE.match(line)
            if not match:
                continue
            disc_id, title = match.group(1), match.group(2)
            if disc_id in _HEADER_KEYS:
                continue
            title = title.split("|")[0].strip()
            if title:
                self.titles[disc_id] = title
                count += 1
        return count

    def load(self) -> int:
        self.titles.clear()
        self.loaded_from = []
        for name in ("titles.txt", f"titles_{self.language}.txt"):
            path = self._path(name)
            if os.path.exists(path):
                if self._read_file(path):
                    self.loaded_from.append(path)
        return len(self.titles)

    # -- lookup ----------------------------------------------------------
    def get(self, disc_id: str) -> str | None:
        if not disc_id:
            return None
        return self.titles.get(disc_id.upper()[:6])

    def __len__(self) -> int:
        return len(self.titles)

    # -- download --------------------------------------------------------
    def refresh(self, url: str = DEFAULT_URL, progress=None) -> tuple[int, str]:
        """Download and import the database. Returns (title count, info)."""
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        chunks: list[bytes] = []
        with urllib.request.urlopen(request, timeout=120) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total or done)
        payload = b"".join(chunks)

        english: dict[str, str] = {}
        localized: dict[str, str] = {}
        if payload[:2] == b"PK":
            english, localized = self._read_archive(payload, progress)
        else:
            text = payload.decode("utf-8", "replace")
            english = self._parse_text(text)
        if english:
            self._write_cache("titles.txt", english)
        if localized:
            self._write_cache(f"titles_{self.language}.txt", localized)
        if not english and not localized:
            raise ValueError("nie znaleziono tytułów w pobranym pliku")
        count = self.load()
        info = f"Zaktualizowano bazę: {count} tytułów"
        if localized:
            info += f" (w tym {len(localized)} po polsku)"
        return count, info

    def _write_cache(self, name: str, titles: dict[str, str]) -> None:
        tmp = self._path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            for disc_id in sorted(titles):
                handle.write(f"{disc_id} = {titles[disc_id]}\n")
        os.replace(tmp, self._path(name))

    def _parse_text(self, text: str) -> dict[str, str]:
        titles: dict[str, str] = {}
        for line in text.splitlines():
            if "=" not in line:
                continue
            match = _LINE.match(line)
            if not match:
                continue
            disc_id, title = match.group(1), match.group(2)
            if disc_id in _HEADER_KEYS:
                continue
            title = title.split("|")[0].strip()
            if title:
                titles[disc_id] = title
        return titles

    def _read_archive(self, payload: bytes, progress) -> tuple[dict, dict]:
        english: dict[str, str] = {}
        localized: dict[str, str] = {}
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            text_names = [
                name
                for name in names
                if name.lower().endswith(".txt") and "titles" in name.lower()
            ]
            for name in text_names:
                base = os.path.basename(name).lower()
                if base not in {"titles.txt", f"titles_{self.language}.txt"}:
                    continue
                data = archive.read(name)
                target = english if base == "titles.txt" else localized
                target.update(self._parse_text(data.decode("utf-8", "replace")))
            if not english and not localized:
                xml_name = next(
                    (name for name in names if name.lower().endswith(".xml")), None
                )
                if xml_name is None:
                    raise ValueError("archiwum nie zawiera bazy tytułów")
                with archive.open(xml_name) as handle:
                    english, localized = self._parse_xml(handle, progress)
        return english, localized

    def _parse_xml(self, handle, progress=None) -> tuple[dict, dict]:
        english: dict[str, str] = {}
        localized: dict[str, str] = {}
        language = self.language.upper()
        processed = 0
        context = ElementTree.iterparse(handle, events=("end",))
        for _event, element in context:
            if element.tag != "game":
                continue
            disc_id = (element.findtext("id") or "").strip().upper()
            if disc_id:
                base_title = (element.get("name") or "").strip()
                english_title = ""
                localized_title = ""
                for locale in element.findall("locale"):
                    lang = (locale.get("lang") or "").upper()
                    title = (locale.findtext("title") or "").strip()
                    if not title:
                        continue
                    if lang == "EN":
                        english_title = title
                    elif lang == language:
                        localized_title = title
                final_english = english_title or base_title
                if final_english:
                    english[disc_id] = final_english
                if localized_title:
                    localized[disc_id] = localized_title
            element.clear()
            processed += 1
        return english, localized
