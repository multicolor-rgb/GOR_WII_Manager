"""Small helpers shared by the WBFS core."""

from __future__ import annotations

import os
import re
import stat
import subprocess

from .i18n import _, _EN  # noqa: E402


class WbfsError(Exception):
    """Base error for the WBFS core."""


class NotADiscError(WbfsError):
    """The file/device is not a Wii or GameCube disc image."""


class CorruptVolumeError(WbfsError):
    """The WBFS container is damaged or has an unsupported layout."""


class CancelledError(WbfsError):
    """An operation was cancelled by the user."""


class DuplicateDiscError(WbfsError):
    """A disc with the same ID already exists in the target volume."""

    def __init__(self, disc_id: str, slot: int, title: str = ""):
        self.disc_id = disc_id
        self.slot = slot
        self.title = title
        super().__init__(
            _("Gra {disc_id} ({title}) już istnieje w tym napędzie").format(
                disc_id=disc_id, title=title
            )
        )


def align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def align_down(value: int, alignment: int) -> int:
    return value & ~(alignment - 1)


def ceil_div(value: int, divisor: int) -> int:
    return -(-value // divisor)


def read_be16(data: bytes, offset: int = 0) -> int:
    return int.from_bytes(data[offset:offset + 2], "big")


def read_be32(data: bytes, offset: int = 0) -> int:
    return int.from_bytes(data[offset:offset + 4], "big")


def write_be16(value: int) -> bytes:
    return int(value & 0xFFFF).to_bytes(2, "big")


def write_be32(value: int) -> bytes:
    return int(value & 0xFFFFFFFF).to_bytes(4, "big")


def size_to_shift(size: int) -> int:
    if size <= 0 or size & (size - 1):
        raise ValueError(f"rozmiar nie jest potęgą dwójki: {size}")
    return size.bit_length() - 1


_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]


def human_size(value: float, decimals: int = 1) -> str:
    value = float(value)
    negative = value < 0
    value = abs(value)
    unit = 0
    while value >= 1024.0 and unit < len(_UNITS) - 1:
        value /= 1024.0
        unit += 1
    if unit == 0:
        text = f"{int(value)} {_UNITS[unit]}"
    else:
        text = f"{value:.{decimals}f} {_UNITS[unit]}"
    return ("-" if negative else "") + text


def human_speed(bps: float) -> str:
    return human_size(bps) + "/s"


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


_SIZE_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([kmgt]?)i?b?\s*$", re.IGNORECASE)


def parse_size(text: str) -> int:
    """Parse strings like '32G', '4.7 GiB', '1024M' or '734003200'."""
    match = _SIZE_RE.match(text or "")
    if not match:
        raise ValueError(f"nieprawidłowy rozmiar: {text!r}")
    number = float(match.group(1).replace(",", "."))
    factor = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}[
        match.group(2).lower()
    ]
    return int(number * factor)


def fs_type(path: str) -> str:
    """Return the filesystem type for a path (``vfat``, ``ntfs``, ...)."""
    try:
        result = subprocess.run(
            ["stat", "-f", "-c", "%T", path],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip().lower()


def is_fat(path: str) -> bool:
    """True when the filesystem cannot hold files larger than 4 GiB."""
    return fs_type(path) in {"vfat", "msdos", "fat", "fat32"}


def is_block_device(path: str) -> bool:
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False


_BAD_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def sanitize_filename(name: str, max_length: int = 120) -> str:
    name = _BAD_CHARS.sub("_", name).strip(" .")
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = "gra"
    return name[:max_length].rstrip(" .")


def game_filename(title: str, disc_id: str, extension: str) -> str:
    title = sanitize_filename(title or disc_id)
    disc_id = sanitize_filename(disc_id or "UNKNOWN", 16)
    return f"{title} [{disc_id}].{extension.lstrip('.')}"


REGION_NAMES = {
    "E": "USA",
    "J": "Japonia",
    "P": "Europa",
    "D": "Niemcy",
    "F": "Francja",
    "I": "Włochy",
    "S": "Hiszpania",
    "K": "Korea",
    "R": "Rosja",
    "T": "Tajwan",
    "Q": "Korea",
    "L": "Europa",
    "A": "Australia",
    "C": "Chiny",
    "W": "Tajwan",
    "X": "Europa",
    "Y": "Europa",
    "Z": "Europa",
}


def region_name(code: str) -> str:
    return _(REGION_NAMES.get((code or "?").upper()[:1], code or "?"))
