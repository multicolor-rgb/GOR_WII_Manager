"""WBFS container implementation (compatible with libwbfs / USB loaders)."""

from __future__ import annotations

import os
import struct
import time
from array import array
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import wbfs_layout as layout
from .diskio import DiskIO, open_volume_io, split_paths
from .disc import (
    DiscInfo,
    Reader,
    Usage,
    build_usage,
    parse_header,
    read_info,
)
from .util import (
    CancelledError,
    CorruptVolumeError,
    DuplicateDiscError,
    NotADiscError,
    WbfsError,
    align_up,
    human_size,
    read_be32,
    region_name,
    write_be16,
)
from .wbfs_layout import WII_SECTOR, WII_SECTORS_PER_DISC

ProgressFn = Callable[[int, int], None]


def _noop_progress(done: int, total: int) -> None:
    pass


def _check_cancel(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise CancelledError("operacja anulowana")


@dataclass
class GameEntry:
    slot: int
    disc_id: str
    title: str
    region: str
    disc_number: int
    version: int
    is_wii: bool
    is_gc: bool
    header: bytes
    wlba: array
    valid: bool = True
    error: str = ""

    blocks: int = 0
    last_index: int = 0

    @property
    def kind(self) -> str:
        return "Wii" if self.is_wii else "GameCube" if self.is_gc else "?"

    @property
    def region_name(self) -> str:
        return region_name(self.region)

    def stored_size(self, geometry: layout.Geometry) -> int:
        return self.blocks * geometry.wbfs_sec_sz

    def iso_size(self, geometry: layout.Geometry) -> int:
        if self.is_wii:
            dual = self.last_index >= geometry.n_wbfs_sec_per_disc // 2
            return (WII_SECTORS_PER_DISC if dual else 143432) * WII_SECTOR
        return min(
            (self.last_index + 1) * geometry.wbfs_sec_sz,
            (WII_SECTORS_PER_DISC + 16) * WII_SECTOR,
        )


@dataclass
class VerifyReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duplicate_blocks: list[tuple[int, int, int]] = field(default_factory=list)
    bitmap_mismatch: list[int] = field(default_factory=list)
    invalid_slots: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.errors or self.duplicate_blocks or self.bitmap_mismatch)


class WbfsDiscReader:
    """Random access view of one game inside a WBFS volume."""

    def __init__(self, volume: "WbfsVolume", entry: GameEntry):
        self.volume = volume
        self.entry = entry
        self.geometry = volume.geometry
        self.size = self.geometry.n_wbfs_sec_per_disc * self.geometry.wbfs_sec_sz
        self._wlba = entry.wlba

    def read_zeros(self, offset: int, length: int) -> bytes:
        if offset >= self.size or length <= 0:
            return b""
        wanted = min(length, self.size - offset)
        length = wanted
        block_size = self.geometry.wbfs_sec_sz
        result = bytearray()
        io = self.volume.io
        while length > 0:
            index, within = divmod(offset, block_size)
            if index >= len(self._wlba):
                break
            block = self._wlba[index]
            count = min(length, block_size - within)
            if block == 0:
                result += b"\x00" * count
            else:
                chunk = io.pread(self.geometry.block_byte_offset(block) + within, count)
                if len(chunk) < count:
                    chunk += b"\x00" * (count - len(chunk))
                result += chunk
            offset += count
            length -= count
        if len(result) < wanted:
            result += b"\x00" * (wanted - len(result))
        return bytes(result)

    read = read_zeros


class WbfsSource:
    """A game stored inside another WBFS volume."""

    def __init__(self, volume: "WbfsVolume", entry: GameEntry):
        self.volume = volume
        self.entry = entry
        self.reader = WbfsDiscReader(volume, entry)
        self.info = read_info(self.reader)
        self.size = self.reader.size
        self.path = f"{volume.path}:{entry.disc_id}"

    def read_zeros(self, offset: int, length: int) -> bytes:
        return self.reader.read_zeros(offset, length)

    read = read_zeros

    @property
    def iso_size(self) -> int:
        return self.entry.iso_size(self.volume.geometry)

    def close(self) -> None:
        pass


class WbfsVolume:
    """An opened WBFS partition or ``.wbfs`` file."""

    def __init__(
        self,
        io: DiskIO,
        header: bytes,
        geometry: layout.Geometry,
        path: str = "",
        read_only: bool = False,
    ):
        self.io = io
        self.path = path or io.path
        self.geometry = geometry
        self.read_only = read_only
        self.header = bytearray(header[: geometry.hd_sec_sz])
        if len(self.header) < geometry.hd_sec_sz:
            self.header += b"\x00" * (geometry.hd_sec_sz - len(self.header))
        self._bitmap: bytearray | None = None
        self._used: set[int] | None = None
        self._free_count: int | None = None
        self._next_block = 1
        self.truncated = geometry.total_bytes > io.size

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    def open(cls, path: str, read_only: bool = False, io: DiskIO | None = None) -> "WbfsVolume":
        io = io or open_volume_io(path, writable=not read_only)
        head = io.pread(0, 512)
        geometry = layout.parse_geometry(head)
        header = io.pread(0, geometry.hd_sec_sz)
        return cls(io, header, geometry, path=path, read_only=read_only)

    @classmethod
    def create(
        cls,
        path: str,
        size: int,
        block_size: int | None = None,
        hd_sec_sz: int = 512,
        split: bool = False,
        io: DiskIO | None = None,
    ) -> "WbfsVolume":
        geometry = layout.choose_geometry(size, hd_sec_sz=hd_sec_sz, block_size=block_size)
        if io is None:
            io = open_volume_io(
                path,
                writable=True,
                create=True,
                virtual_size=geometry.total_bytes,
                split=split,
            )
        elif io.size > geometry.total_bytes:
            geometry = layout.choose_geometry(io.size, hd_sec_sz=hd_sec_sz, block_size=block_size)
        header = layout.build_header(geometry)
        bitmap = layout.empty_bitmap(geometry)
        io.pwrite(0, header)
        io.pwrite(geometry.freeblks_lba * geometry.hd_sec_sz, bytes(bitmap))
        io.flush()
        return cls(io, header, geometry, path=path, read_only=False)

    @classmethod
    def format_device(
        cls, path: str, block_size: int | None = None, hd_sec_sz: int = 512
    ) -> "WbfsVolume":
        io = open_volume_io(path, writable=True)
        if io.size < layout.MIN_BLOCK_SIZE * 2:
            io.close()
            raise WbfsError("urządzenie jest za małe na partycję WBFS")
        zero = bytes(4096)
        io.pwrite(0, zero)
        volume = cls.create(
            path, io.size, block_size=block_size, hd_sec_sz=hd_sec_sz, io=io
        )
        return volume

    # ------------------------------------------------------------------
    # header / bitmap helpers
    # ------------------------------------------------------------------
    @property
    def disc_table(self) -> bytearray:
        return self.header[12:12 + self.geometry.max_disc]

    def _table_get(self, slot: int) -> bool:
        return bool(self.header[12 + slot])

    def _table_set(self, slot: int, value: bool) -> None:
        self.header[12 + slot] = 1 if value else 0

    def _ensure_state(self) -> None:
        """Rebuild the free-block map from the disc tables (like wwt does).

        Stored bitmaps are frequently wrong, so they are only used for
        diagnostics in :meth:`verify` and never trusted for allocation.
        """
        if self._bitmap is not None:
            return
        used: set[int] = set()
        per_disc = self.geometry.n_wbfs_sec_per_disc
        for slot in range(self.geometry.max_disc):
            if not self._table_get(slot):
                continue
            raw = self._read_slot(slot)
            table = raw[0x100:0x100 + 2 * per_disc]
            if not table:
                continue
            wlba = array("H")
            wlba.frombytes(table)
            if struct.pack("=H", 1) != struct.pack(">H", 1):
                wlba.byteswap()
            for value in wlba:
                if value:
                    used.add(value)
        self._used = used
        self._bitmap = layout.build_bitmap(used, self.geometry)
        self._free_count = layout.count_free_blocks(self._bitmap, self.usable_blocks)

    @property
    def bitmap(self) -> bytearray:
        self._ensure_state()
        return self._bitmap  # type: ignore[return-value]

    @property
    def used_blocks(self) -> set[int]:
        self._ensure_state()
        return self._used  # type: ignore[return-value]

    def stored_bitmap(self) -> bytearray:
        """The bitmap as currently stored on disk (may be inconsistent)."""
        raw = self.io.pread(
            self.geometry.freeblks_lba * self.geometry.hd_sec_sz,
            self.geometry.bitmap_bytes_aligned,
        )
        bitmap = bytearray(self.geometry.bitmap_bytes_aligned)
        bitmap[: len(raw)] = raw
        return bitmap

    def save(self) -> None:
        if self.read_only:
            raise WbfsError("napęd jest otwarty tylko do odczytu")
        self._ensure_state()
        self.io.pwrite(0, bytes(self.header))
        self.io.pwrite(
            self.geometry.freeblks_lba * self.geometry.hd_sec_sz, bytes(self.bitmap)
        )
        self.io.flush()

    def close(self) -> None:
        self.io.close()

    # ------------------------------------------------------------------
    # space accounting
    # ------------------------------------------------------------------
    @property
    def usable_blocks(self) -> int:
        limit = max(0, self.geometry.n_wbfs_sec - 1)
        if self.io.size:
            limit = min(limit, max(0, self.io.size // self.geometry.wbfs_sec_sz - 1))
        return limit

    @property
    def free_blocks(self) -> int:
        self._ensure_state()
        return self._free_count or 0

    @property
    def free_bytes(self) -> int:
        return self.free_blocks * self.geometry.wbfs_sec_sz

    @property
    def capacity_bytes(self) -> int:
        return self.usable_blocks * self.geometry.wbfs_sec_sz

    def _scan_free(self, start: int) -> int:
        limit = self.usable_blocks
        bitmap = self.bitmap
        block = max(1, start)
        while block <= limit:
            word_index = (block - 1) >> 5
            bit = (block - 1) & 31
            value = read_be32(bitmap, word_index * 4) & ~((1 << bit) - 1)
            if value:
                candidate = word_index * 32 + ((value & -value).bit_length() - 1) + 1
                if candidate <= limit:
                    return candidate
            block = (word_index + 1) * 32 + 1
        return 0

    def _allocate_block(self) -> int:
        block = self._scan_free(self._next_block)
        if not block and self._next_block > 1:
            block = self._scan_free(1)
        if not block:
            raise WbfsError("brak wolnego miejsca na tym napędzie WBFS")
        layout.bitmap_set(self.bitmap, block, False)
        self.used_blocks.add(block)
        self._next_block = block + 1
        self._free_count = max(0, (self._free_count or 1) - 1)
        return block

    def _release_block(self, block: int) -> None:
        if block <= 0:
            return
        layout.bitmap_set(self.bitmap, block, True)
        self.used_blocks.discard(block)
        self._next_block = min(self._next_block, block)
        self._free_count = (self._free_count or 0) + 1

    # ------------------------------------------------------------------
    # reading games
    # ------------------------------------------------------------------
    def _read_slot(self, slot: int) -> bytes:
        offset = self.geometry.slot_byte_offset(slot)
        return self.io.pread(offset, self.geometry.disc_info_sz)

    def _parse_slot(self, slot: int, raw: bytes) -> GameEntry:
        header = raw[:0x100]
        table_bytes = raw[0x100:0x100 + 2 * self.geometry.n_wbfs_sec_per_disc]
        wlba = array("H")
        if table_bytes:
            wlba.frombytes(table_bytes)
            if struct.pack("=H", 1) != struct.pack(">H", 1):
                wlba.byteswap()
        if len(wlba) < self.geometry.n_wbfs_sec_per_disc:
            wlba.extend([0] * (self.geometry.n_wbfs_sec_per_disc - len(wlba)))
        blocks = sum(1 for value in wlba if value)
        last_index = 0
        for index, value in enumerate(wlba):
            if value:
                last_index = index
        disc_id = header[:6].decode("latin-1", "replace")
        try:
            info = parse_header(header, 0)
        except NotADiscError as error:
            return GameEntry(
                slot=slot,
                disc_id=disc_id or f"SLOT{slot}",
                title="Nieznana gra",
                region="?",
                disc_number=0,
                version=0,
                is_wii=False,
                is_gc=False,
                header=header,
                wlba=wlba,
                valid=False,
                error=str(error),
                blocks=blocks,
                last_index=last_index,
            )
        return GameEntry(
            slot=slot,
            disc_id=info.disc_id,
            title=info.title,
            region=info.region,
            disc_number=info.disc_number,
            version=info.version,
            is_wii=info.is_wii,
            is_gc=info.is_gc,
            header=header,
            wlba=wlba,
            blocks=blocks,
            last_index=last_index,
        )

    def games(self) -> list[GameEntry]:
        result: list[GameEntry] = []
        for slot in range(self.geometry.max_disc):
            if not self._table_get(slot):
                continue
            raw = self._read_slot(slot)
            if len(raw) < 0x100:
                result.append(
                    GameEntry(
                        slot=slot,
                        disc_id=f"SLOT{slot}",
                        title="Uszkodzony wpis",
                        region="?",
                        disc_number=0,
                        version=0,
                        is_wii=False,
                        is_gc=False,
                        header=b"",
                        wlba=array("H"),
                        valid=False,
                        error="nie można odczytać wpisu gry",
                    )
                )
                continue
            result.append(self._parse_slot(slot, raw))
        return result

    def find_game(self, disc_id: str) -> GameEntry | None:
        for entry in self.games():
            if entry.disc_id.upper() == disc_id.upper():
                return entry
        return None

    def reader(self, entry: GameEntry) -> WbfsDiscReader:
        return WbfsDiscReader(self, entry)

    def source(self, entry: GameEntry) -> WbfsSource:
        return WbfsSource(self, entry)

    # ------------------------------------------------------------------
    # writing games
    # ------------------------------------------------------------------
    def _find_free_slot(self) -> int:
        for slot in range(self.geometry.max_disc):
            if not self._table_get(slot):
                return slot
        raise WbfsError(
            f"brak wolnych wpisów w tablicy gier (limit {self.geometry.max_disc})"
        )

    def add_disc(
        self,
        source,
        mode: str = "remove_update",
        progress: ProgressFn | None = None,
        cancel=None,
        replace: bool = False,
        usage: Usage | None = None,
    ) -> GameEntry:
        if self.read_only:
            raise WbfsError("napęd jest otwarty tylko do odczytu")
        progress = progress or _noop_progress
        info: DiscInfo = source.info
        if not info.disc_id:
            raise WbfsError("obraz nie ma identyfikatora gry")

        existing = self.find_game(info.disc_id)
        if existing is not None:
            if not replace:
                raise DuplicateDiscError(info.disc_id, existing.slot, existing.title)
            if isinstance(source, WbfsSource) and source.volume is self:
                raise WbfsError("nie można zastąpić gry jej własnym źródłem")
            if isinstance(source, WbfsSource):
                try:
                    same_file = os.path.realpath(source.volume.path) == os.path.realpath(
                        self.path
                    )
                except OSError:
                    same_file = False
                if same_file:
                    raise WbfsError(
                        "nie można zastąpić gry, czytając ją z tego samego pliku"
                    )
            self.remove_game(existing.slot)

        if usage is None:
            if isinstance(source, WbfsSource):
                usage = _wbfs_source_usage(source)
            else:
                usage = build_usage(source, info, mode)
        sectors_per_block = self.geometry.wii_sec_per_wbfs_sec
        max_sector = min(
            self.geometry.n_wbfs_sec_per_disc * sectors_per_block, len(usage.table)
        )
        needed: list[int] = []
        for index in range(self.geometry.n_wbfs_sec_per_disc):
            start = index * sectors_per_block
            if start >= max_sector:
                break
            end = min(start + sectors_per_block, max_sector)
            if any(usage.table[start:end]):
                needed.append(index)
        if not needed:
            raise WbfsError("obraz nie zawiera danych do zapisania")
        if len(needed) > self.free_blocks:
            raise WbfsError(
                "za mało miejsca: potrzeba "
                f"{human_size(len(needed) * self.geometry.wbfs_sec_sz)}, "
                f"wolne {human_size(self.free_bytes)}"
            )
        if len(needed) > self.geometry.n_wbfs_sec_per_disc:
            raise WbfsError("gra jest za duża dla tego WBFS")

        slot = self._find_free_slot()
        wlba = array("H", [0]) * self.geometry.n_wbfs_sec_per_disc
        allocated: list[int] = []
        total = usage.total_used_bytes or len(needed) * self.geometry.wbfs_sec_sz
        done = 0
        block_size = self.geometry.wbfs_sec_sz
        try:
            for index in needed:
                _check_cancel(cancel)
                block = self._allocate_block()
                allocated.append(block)
                wlba[index] = block
                base = index * sectors_per_block
                start_sector = base
                end_sector = min(base + sectors_per_block, max_sector)
                used_flags = usage.table[start_sector:end_sector]
                target = self.geometry.block_byte_offset(block)
                if used_flags and all(used_flags) and block_size == len(used_flags) * WII_SECTOR:
                    data = source.read_zeros(start_sector * WII_SECTOR, block_size)
                    patch = usage.patches.get(start_sector * WII_SECTOR)
                    if patch is not None:
                        data = patch + data[len(patch):]
                    self.io.pwrite(target, data)
                    done += len(used_flags) * WII_SECTOR
                else:
                    buffer = bytearray(block_size)
                    for offset, used in enumerate(used_flags):
                        sector = start_sector + offset
                        if not used:
                            continue
                        absolute = sector * WII_SECTOR
                        chunk = source.read_zeros(absolute, WII_SECTOR)
                        patch = usage.patches.get(absolute)
                        if patch is not None:
                            chunk = patch
                        start = offset * WII_SECTOR
                        buffer[start:start + len(chunk)] = chunk[:WII_SECTOR]
                        done += WII_SECTOR
                        _check_cancel(cancel)
                    self.io.pwrite(target, bytes(buffer))
                progress(done, total)
            _check_cancel(cancel)

            slot_data = bytearray(self.geometry.disc_info_sz)
            slot_data[:0x100] = info.header[:0x100]
            for index, value in enumerate(wlba):
                if value:
                    slot_data[0x100 + index * 2:0x102 + index * 2] = write_be16(value)
            self.io.pwrite(self.geometry.slot_byte_offset(slot), bytes(slot_data))
            self._table_set(slot, True)
            self.save()
        except BaseException:
            for block in allocated:
                self._release_block(block)
            try:
                self._table_set(slot, False)
                self.save()
            except Exception:
                pass
            raise
        entry = self._parse_slot(slot, self._read_slot(slot))
        progress(total, total)
        return entry

    def remove_game(self, slot: int) -> None:
        if self.read_only:
            raise WbfsError("napęd jest otwarty tylko do odczytu")
        raw = self._read_slot(slot)
        entry = self._parse_slot(slot, raw)
        for block in entry.wlba:
            if block:
                self._release_block(block)
        self.io.pwrite(
            self.geometry.slot_byte_offset(slot), b"\x00" * self.geometry.disc_info_sz
        )
        self._table_set(slot, False)
        self.save()

    def rename_game(self, slot: int, title: str) -> None:
        if self.read_only:
            raise WbfsError("napęd jest otwarty tylko do odczytu")
        raw = bytearray(self._read_slot(slot))
        name = title.encode("latin-1", "replace")[:0x40]
        raw[0x20:0x60] = name + b"\x00" * (0x40 - len(name))
        self.io.pwrite(self.geometry.slot_byte_offset(slot), bytes(raw))

    # ------------------------------------------------------------------
    # extraction
    # ------------------------------------------------------------------
    def extract_iso(
        self,
        entry: GameEntry,
        destination: str,
        size: int | None = None,
        progress: ProgressFn | None = None,
        cancel=None,
    ) -> str:
        progress = progress or _noop_progress
        size = size or entry.iso_size(self.geometry)
        block_size = self.geometry.wbfs_sec_sz
        with open(destination, "wb") as handle:
            handle.truncate(size)
            try:
                for index, block in enumerate(entry.wlba):
                    if not block:
                        continue
                    _check_cancel(cancel)
                    offset = index * block_size
                    if offset >= size:
                        break
                    count = min(block_size, size - offset)
                    data = self.io.pread(self.geometry.block_byte_offset(block), count)
                    if len(data) < count:
                        data += b"\x00" * (count - len(data))
                    if data.strip(b"\x00"):
                        handle.seek(offset)
                        handle.write(data)
                    progress(min(offset + count, size), size)
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                try:
                    handle.close()
                    os.unlink(destination)
                except OSError:
                    pass
                raise
        progress(size, size)
        return destination

    def extract_wbfs(
        self,
        entry: GameEntry,
        destination: str,
        split: bool = False,
        progress: ProgressFn | None = None,
        cancel=None,
    ) -> str:
        progress = progress or _noop_progress
        sectors_per_block = self.geometry.wii_sec_per_wbfs_sec
        source_sectors = max((entry.last_index + 1) * sectors_per_block, sectors_per_block)
        block_size = layout.MIN_BLOCK_SIZE
        payload_blocks = -(-source_sectors * WII_SECTOR // block_size)
        size = (payload_blocks + 2) * block_size
        target = WbfsVolume.create(
            destination, size, block_size=block_size, split=split
        )
        try:
            source = self.source(entry)
            target.add_disc(
                source,
                mode="remove_update",
                progress=progress,
                cancel=cancel,
            )
        finally:
            target.close()
        return destination

    # ------------------------------------------------------------------
    # verification / repair
    # ------------------------------------------------------------------
    def verify(self, deep: bool = False) -> VerifyReport:
        report = VerifyReport()
        owners: dict[int, list[int]] = {}
        entries = self.games()
        for entry in entries:
            if not entry.valid:
                report.invalid_slots.append(entry.slot)
                report.errors.append(
                    f"slot {entry.slot}: nieprawidłowy nagłówek gry ({entry.error})"
                )
            for block in entry.wlba:
                if block == 0:
                    continue
                if block > self.usable_blocks:
                    report.errors.append(
                        f"{entry.disc_id}: blok {block} poza zakresem napędu"
                    )
                    continue
                owners.setdefault(block, []).append(entry.slot)
        for block, slots in owners.items():
            if len(slots) > 1:
                report.duplicate_blocks.append((block, slots[0], slots[1]))
        stored = self.stored_bitmap()
        computed = self.bitmap
        for block in range(1, self.usable_blocks + 1):
            if layout.bitmap_get(stored, block) != layout.bitmap_get(computed, block):
                report.bitmap_mismatch.append(block)
        if report.bitmap_mismatch:
            report.errors.append(
                f"tablica wolnych bloków nie zgadza się z zawartością "
                f"({len(report.bitmap_mismatch)} bloków)"
            )
        if report.duplicate_blocks:
            report.errors.append(
                f"wykryto {len(report.duplicate_blocks)} bloków współdzielonych przez gry"
            )
        if deep:
            for entry in entries:
                if not entry.valid:
                    continue
                try:
                    source = self.source(entry)
                    usage = build_usage(source, source.info, "all")
                    del usage
                except Exception as error:  # pragma: no cover - defensive
                    report.warnings.append(
                        f"{entry.disc_id}: nie można odczytać danych ({error})"
                    )
        return report

    def repair(self, clear_invalid: bool = False) -> int:
        if self.read_only:
            raise WbfsError("napęd jest otwarty tylko do odczytu")
        stored = self.stored_bitmap()
        changes = 0
        if clear_invalid:
            for slot in range(self.geometry.max_disc):
                if not self._table_get(slot):
                    continue
                raw = self._read_slot(slot)
                try:
                    parse_header(raw[:0x100], 0)
                except NotADiscError:
                    self._table_set(slot, False)
                    self.io.pwrite(
                        self.geometry.slot_byte_offset(slot),
                        b"\x00" * self.geometry.disc_info_sz,
                    )
                    changes += 1
        self._bitmap = None
        self._used = None
        self._free_count = None
        self._ensure_state()
        computed = self.bitmap
        for block in range(1, self.usable_blocks + 1):
            if layout.bitmap_get(stored, block) != layout.bitmap_get(computed, block):
                changes += 1
        self.save()
        return changes

    # ------------------------------------------------------------------
    # info
    # ------------------------------------------------------------------
    @property
    def game_count(self) -> int:
        return sum(1 for slot in range(self.geometry.max_disc) if self._table_get(slot))

    def summary(self) -> dict:
        return {
            "path": self.path,
            "read_only": self.read_only,
            "games": self.game_count,
            "free_bytes": self.free_bytes,
            "capacity_bytes": self.capacity_bytes,
            "block_size": self.geometry.wbfs_sec_sz,
            "max_games": self.geometry.max_disc,
            "hd_sector_size": self.geometry.hd_sec_sz,
        }



def _wbfs_source_usage(source: "WbfsSource") -> Usage:
    """Usage map that preserves the blocks already allocated in a WBFS game."""
    block_sectors = source.volume.geometry.wbfs_sec_sz // WII_SECTOR
    table = bytearray(WII_SECTORS_PER_DISC)
    for index, block in enumerate(source.entry.wlba):
        if not block:
            continue
        start = index * block_sectors
        end = min(start + block_sectors, len(table))
        if start >= len(table):
            break
        table[start:end] = b"\x01" * (end - start)
    return Usage(table=table, total_used_bytes=sum(table) * WII_SECTOR)


def single_game_wbfs_size(
    source: "WbfsVolume",
    entry: GameEntry,
    block_size: int | None = None,
) -> int:
    """Minimal valid single-game WBFS size for an existing game entry."""
    used = sum(1 for block in entry.wlba if block)
    if not used:
        raise WbfsError("gra nie zawiera żadnych bloków danych")
    block_size = block_size or source.geometry.wbfs_sec_sz
    return layout.size_for_used_blocks(
        used, block_size, hd_sec_sz=source.geometry.hd_sec_sz
    )


def clone_game_to_file(
    source: "WbfsVolume",
    entry: GameEntry,
    destination: str,
    split: bool = False,
    progress: ProgressFn | None = None,
    cancel=None,
    title: str | None = None,
) -> str:
    """Create a single-game WBFS file preserving the source's used blocks.

    Unlike a fresh import/scrub this keeps an already scrubbed 368 MB game at
    368 MB instead of expanding it to the full game partition.
    """
    progress = progress or _noop_progress
    used = [(index, block) for index, block in enumerate(entry.wlba) if block]
    if not used:
        raise WbfsError("gra nie zawiera żadnych bloków danych")
    block_size = source.geometry.wbfs_sec_sz
    target = WbfsVolume.create(
        destination,
        single_game_wbfs_size(source, entry, block_size),
        block_size=block_size,
        split=split,
    )
    try:
        wlba = array("H", [0]) * source.geometry.n_wbfs_sec_per_disc
        total = len(used) * block_size
        done = 0
        for index, source_block in used:
            _check_cancel(cancel)
            target_block = target._allocate_block()
            data = source.io.pread(
                source.geometry.block_byte_offset(source_block), block_size
            )
            if len(data) < block_size:
                data += b"\x00" * (block_size - len(data))
            target.io.pwrite(target.geometry.block_byte_offset(target_block), data)
            wlba[index] = target_block
            done += block_size
            progress(done, total)
        _check_cancel(cancel)

        slot_data = bytearray(target.geometry.disc_info_sz)
        slot_data[:0x100] = entry.header[:0x100]
        if title:
            encoded = title.encode("latin-1", "replace")[:0x40]
            slot_data[0x20:0x60] = encoded + b"\x00" * (0x40 - len(encoded))
        for index, value in enumerate(wlba):
            if value:
                slot_data[0x100 + index * 2:0x102 + index * 2] = write_be16(value)
        target.io.pwrite(target.geometry.slot_byte_offset(0), bytes(slot_data))
        target._table_set(0, True)
        target.save()
    except BaseException:
        try:
            target.close()
        finally:
            for part in split_paths(destination):
                try:
                    os.unlink(part)
                except OSError:
                    pass
        raise
    target.close()
    return destination
