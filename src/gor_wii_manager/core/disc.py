"""Wii and GameCube disc image parsing and scrubbing (usage) maps."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .diskio import DiskIO, FileIO, open_volume_io
from .util import (
    NotADiscError,
    WbfsError,
    ceil_div,
    human_size,
    read_be32,
)
from .wbfs_layout import WII_SECTOR, WII_SECTORS_PER_DISC

WII_MAGIC = 0x5D1C9EA3
GC_MAGIC = 0xC2339F3D
GC_MAX_SIZE = 1459978240  # full size 1.4 GiB GameCube disc

SCRUB_MODES = {
    "all": "Wszystkie partycje",
    "remove_update": "Pomiń partycję aktualizacji",
    "only_game": "Tylko partycja gry",
}


@dataclass(frozen=True)
class Partition:
    index: int
    offset: int
    type: int
    end: int

    @property
    def size(self) -> int:
        return max(0, self.end - self.offset)

    @property
    def kind(self) -> str:
        return {0: "gra", 1: "aktualizacja", 2: "kanał"}.get(self.type, f"typ {self.type}")


@dataclass
class DiscInfo:
    disc_id: str
    title: str
    region: str
    disc_number: int
    version: int
    is_wii: bool
    is_gc: bool
    size: int
    header: bytes = b""
    partitions: tuple[Partition, ...] = ()
    partitions_ok: bool = True
    parse_error: str = ""

    @property
    def kind(self) -> str:
        return "Wii" if self.is_wii else "GameCube" if self.is_gc else "?"

    @property
    def is_multidisc(self) -> bool:
        return self.disc_number > 0

    @property
    def display_size(self) -> str:
        return human_size(self.size)


def decode_title(raw: bytes) -> str:
    raw = raw.split(b"\x00", 1)[0]
    raw = raw.split(b"\n", 1)[0]
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
        if text:
            return text
    return ""


def _decode_tmd_title(raw: bytes) -> str:
    raw = raw.rstrip(b"\x00")
    if len(raw) >= 2 and len(raw) % 2 == 0:
        odd_zero = sum(1 for byte in raw[1::2] if byte == 0)
        if odd_zero > len(raw) // 4:
            try:
                text = raw.decode("utf-16-be").strip("\x00 ")
                if text:
                    return text
            except UnicodeDecodeError:
                pass
    return decode_title(raw)


def parse_header(header: bytes, size: int) -> DiscInfo:
    if len(header) < 0x100:
        header = header + b"\x00" * (0x100 - len(header))
    magic = read_be32(header, 0x18)
    gc_magic = read_be32(header, 0x1C)
    if magic == WII_MAGIC:
        is_wii, is_gc = True, False
    elif gc_magic == GC_MAGIC or gc_magic == 0xC2339F3D:
        is_wii, is_gc = False, True
    else:
        raise NotADiscError(
            "to nie jest obraz Wii ani GameCube (brak magicznego nagłówka)"
        )
    disc_id = decode_title(header[0:6]) or header[0:6].hex().upper()
    title = decode_title(header[0x20:0x60]) or disc_id
    region = chr(header[3]) if 32 <= header[3] < 127 else "?"
    return DiscInfo(
        disc_id=disc_id,
        title=title,
        region=region,
        disc_number=header[6],
        version=header[7],
        is_wii=is_wii,
        is_gc=is_gc,
        size=size,
        header=bytes(header[:0x100]),
    )


def read_partitions(reader, info: DiscInfo) -> tuple[Partition, ...]:
    if not info.is_wii:
        return ()
    table = reader.read_zeros(0x40000, 0x100)
    count = read_be32(table, 0)
    table_offset = read_be32(table, 4) * 4
    if count < 1 or count > 64:
        raise WbfsError(f"nieprawidłowa liczba partycji: {count}")
    if table_offset < 0x40000 or table_offset > 0x1000000:
        raise WbfsError("nieprawidłowy offset tablicy partycji")
    entries = reader.read_zeros(table_offset, count * 8)
    partitions = []
    for index in range(count):
        offset = read_be32(entries, index * 8) * 4
        part_type = read_be32(entries, index * 8 + 4)
        if offset < 0x10000 or offset > reader.size:
            raise WbfsError(f"partycja {index} ma offset poza obrazem")
        span = reader.read_zeros(offset + 0x2A4, 0x1C)
        data_offset = read_be32(span, 0x14) * 4
        data_size = read_be32(span, 0x18) * 4
        end = offset + data_offset + data_size
        if end <= offset or end > reader.size + WII_SECTOR:
            raise WbfsError(f"partycja {index} ma nieprawidłowy rozmiar")
        end = min(end, reader.size)
        partitions.append(Partition(index, offset, part_type, end))
    return tuple(partitions)


def read_info(reader) -> DiscInfo:
    header = reader.read_zeros(0, 0x100)
    info = parse_header(header, reader.size)
    if info.is_wii:
        try:
            info.partitions = read_partitions(reader, info)
        except WbfsError as error:
            info.partitions_ok = False
            info.parse_error = str(error)
            info.partitions = ()
        else:
            if not info.partitions:
                info.partitions_ok = False
                info.parse_error = "brak partycji"
        if info.partitions:
            info.title = _tmd_or_header_title(reader, info) or info.title
    return info


def _tmd_or_header_title(reader, info: DiscInfo) -> str:
    for part in info.partitions:
        if part.type != 0:
            continue
        try:
            span = reader.read_zeros(part.offset + 0x2A4, 0x1C)
            tmd_size = read_be32(span, 0)
            tmd_offset = read_be32(span, 4) * 4
            if not (0 < tmd_size <= 0x100000):
                return ""
            tmd = reader.read_zeros(part.offset + tmd_offset, min(tmd_size, 0x400))
            if len(tmd) >= 0x220:
                return _decode_tmd_title(tmd[0x1E0:0x220])
        except (WbfsError, OSError):
            return ""
    return ""


@dataclass
class Usage:
    """Which 0x8000 Wii sectors must be copied for a disc."""

    table: bytearray
    total_used_bytes: int
    patches: dict[int, bytes] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def used(self, sector: int) -> bool:
        return 0 <= sector < len(self.table) and bool(self.table[sector])

    def block_used(self, block: int, sectors_per_block: int) -> bool:
        start = block * sectors_per_block
        end = min(start + sectors_per_block, len(self.table))
        return any(self.table[start:end])


def _keep_partition(part_type: int, mode: str) -> bool:
    if mode == "all":
        return True
    if mode == "only_game":
        return part_type == 0
    return part_type != 1  # remove_update


def _patched_partition_table(reader, info: DiscInfo, kept: list[Partition], mode: str) -> bytes:
    block = bytearray(reader.read_zeros(0x40000, WII_SECTOR))
    count = read_be32(block, 0)
    table_words = read_be32(block, 4)
    within = table_words * 4 - 0x40000
    if not (0 <= within <= len(block) - 8 * count) or count != len(info.partitions):
        raise WbfsError("nie można poprawić tablicy partycji")
    kept_entries = []
    for part in kept:
        source = within + part.index * 8
        kept_entries.append(bytes(block[source:source + 8]))
    block[0:4] = len(kept_entries).to_bytes(4, "big")
    for index in range(count):
        offset = within + index * 8
        if index < len(kept_entries):
            block[offset:offset + 8] = kept_entries[index]
        else:
            block[offset:offset + 8] = b"\x00" * 8
    return bytes(block)


def build_usage(reader, info: DiscInfo, mode: str = "remove_update") -> Usage:
    """Build the map of sectors to copy, mirroring libwbfs/wiidisc behaviour."""
    table = bytearray(WII_SECTORS_PER_DISC)
    limit = min(WII_SECTORS_PER_DISC, ceil_div(max(reader.size, 1), WII_SECTOR))
    warnings: list[str] = []

    def mark(start: int, end: int) -> None:
        first = max(0, start // WII_SECTOR)
        last = min(limit, ceil_div(max(end, start), WII_SECTOR))
        if last > first:
            table[first:last] = b"\x01" * (last - first)

    mark(0, min(0x8000, reader.size))
    if reader.size > 0x40000:
        mark(0x40000, min(0x50000, reader.size))
    patches: dict[int, bytes] = {}

    if info.is_gc:
        mark(0, min(reader.size, GC_MAX_SIZE + WII_SECTOR * 16))
        if reader.size > GC_MAX_SIZE + WII_SECTOR * 16:
            warnings.append("obraz GameCube większy niż standardowa płyta – kopiowany w całości")
    elif info.is_wii:
        if not info.partitions_ok or not info.partitions:
            mark(0, reader.size)
            warnings.append(
                "nie udało się odczytać tablicy partycji – kopia bez czyszczenia"
            )
        else:
            kept = [part for part in info.partitions if _keep_partition(part.type, mode)]
            if not kept:
                kept = list(info.partitions)
            for part in kept:
                mark(part.offset, part.end)
            if mode != "all":
                try:
                    patches[0x40000] = _patched_partition_table(reader, info, kept, mode)
                except WbfsError as error:
                    warnings.append(
                        "nie poprawiono tablicy partycji — zachowano wszystkie "
                        f"partycje: {error}"
                    )
                    for part in info.partitions:
                        mark(part.offset, part.end)
    else:
        mark(0, reader.size)

    used = sum(table)
    return Usage(
        table=table,
        total_used_bytes=used * WII_SECTOR,
        patches=patches,
        warnings=warnings,
    )


class Reader:
    """Minimal random access interface used by the disc parser."""

    def __init__(self, io: DiskIO):
        self.io = io

    @property
    def size(self) -> int:
        return self.io.size

    def read_zeros(self, offset: int, length: int) -> bytes:
        return self.io.read_zeros(offset, length)


class IsoSource:
    """A Wii/GameCube ISO (or GCM) file."""

    def __init__(self, path: str, io: DiskIO | None = None):
        self.path = path
        self.io = io or open_volume_io(path, writable=False)
        self.reader = Reader(self.io)
        self.info = read_info(self.reader)
        self.size = self.io.size

    def read_zeros(self, offset: int, length: int) -> bytes:
        return self.io.read_zeros(offset, length)

    read = read_zeros

    def close(self) -> None:
        self.io.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


IMAGE_EXTENSIONS = (".iso", ".gcm", ".wbfs", ".rvz")

RVZ_MAGIC = b"RVZ\x01"
WIA_MAGIC = b"WIA\x01"
WBFS_MAGIC = b"WBFS"


def open_disc_source(path: str, io: DiskIO | None = None):
    """Open an ISO/GCM, or a native RVZ reader, based on the file magic."""
    probe = io
    close_probe = False
    if probe is None:
        probe = FileIO(path, writable=False)
        close_probe = True
    try:
        magic = probe.pread(0, 4)
    finally:
        if close_probe:
            probe.close()
    if magic in (RVZ_MAGIC, WIA_MAGIC):
        from .rvz import RvzSource

        return RvzSource(path)
    return IsoSource(path, io=io)


def find_disc_images(folder: str, recursive: bool = True) -> list[str]:
    result = []
    if not os.path.isdir(folder):
        return result
    if recursive:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                if name.lower().endswith(IMAGE_EXTENSIONS) and not name.lower().endswith(
                    (".wbf1", ".wbf2", ".wbf3", ".wbf4", ".wbf5", ".wbf6", ".wbf7", ".wbf8", ".wbf9")
                ):
                    result.append(os.path.join(root, name))
    else:
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if os.path.isfile(path) and name.lower().endswith(IMAGE_EXTENSIONS):
                result.append(path)
    return sorted(result)
