"""GameTDB cover art downloader with an on-disk cache."""

from __future__ import annotations

import os
import re
import time
import urllib.error
import urllib.request

USER_AGENT = "gor-wii-manager/0.1 (+linux)"
MISSING_MARKER_AGE = 7 * 24 * 3600

REGION_FOLDERS = {
    "E": "US",
    "J": "JA",
    "W": "JA",
    "P": "EN",
    "D": "DE",
    "F": "FR",
    "I": "IT",
    "S": "ES",
    "K": "KO",
    "Q": "KO",
    "R": "RU",
    "T": "ZH",
    "C": "ZH",
    "A": "AU",
    "L": "EN",
    "X": "EN",
    "Y": "EN",
    "Z": "EN",
}

FALLBACK_FOLDERS = ("US", "EN")
KINDS = ("cover3D", "cover")


def _safe_id(disc_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]", "", disc_id or "")[:6].upper()
    return value


def cover_urls(disc_id: str, region: str, system: str = "wii", kind: str = "cover3D"):
    disc_id = _safe_id(disc_id)
    if not disc_id:
        return []
    folders = []
    primary = REGION_FOLDERS.get((region or "").upper()[:1], "US")
    folders.append(primary)
    for folder in FALLBACK_FOLDERS:
        if folder not in folders:
            folders.append(folder)
    kinds = [kind] + [item for item in KINDS if item != kind]
    urls = []
    for art_kind in kinds:
        for folder in folders:
            urls.append(
                f"https://art.gametdb.com/{system}/{art_kind}/{folder}/{disc_id}.png"
            )
    return urls


def cache_path(cache_directory: str, disc_id: str, system: str = "wii") -> str:
    return os.path.join(cache_directory, f"{system}-{_safe_id(disc_id)}.png")


def _missing_marker(target: str) -> str:
    return target + ".missing"


def _recently_missing(target: str) -> bool:
    marker = _missing_marker(target)
    try:
        return time.time() - os.stat(marker).st_mtime < MISSING_MARKER_AGE
    except OSError:
        return False


def download_cover(
    disc_id: str,
    region: str,
    cache_directory: str,
    system: str = "wii",
    kind: str = "cover3D",
) -> str | None:
    """Download a cover into the cache. Returns the file path or None."""
    disc_id = _safe_id(disc_id)
    if not disc_id:
        return None
    target = cache_path(cache_directory, disc_id, system)
    if os.path.exists(target) and os.path.getsize(target) > 0:
        return target
    if _recently_missing(target):
        return None
    os.makedirs(cache_directory, exist_ok=True)
    missing = True
    for url in cover_urls(disc_id, region, system, kind):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = response.read()
        except urllib.error.HTTPError as error:
            if error.code != 404:
                missing = False
            continue
        except (urllib.error.URLError, TimeoutError, OSError):
            missing = False
            continue
        if not data:
            continue
        tmp = target + ".part"
        try:
            with open(tmp, "wb") as handle:
                handle.write(data)
            os.replace(tmp, target)
            try:
                os.unlink(_missing_marker(target))
            except OSError:
                pass
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            continue
        return target
    if missing:
        try:
            with open(_missing_marker(target), "w", encoding="utf-8") as marker:
                marker.write("missing\n")
        except OSError:
            pass
    return None
