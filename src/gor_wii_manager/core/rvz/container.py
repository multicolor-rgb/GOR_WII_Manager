"""WIA/RVZ container parsing (RVZ only; WIA is reported as unsupported)."""

from __future__ import annotations

import bisect
import hashlib
import os
import struct
from dataclasses import dataclass, field

from ..util import WbfsError
from . import codecs
from .codecs import (
    COMPRESSION_NONE,
    COMPRESSION_PURGE,
    COMPRESSION_NAMES,
)

RVZ_MAGIC = b"RVZ\x01"
WIA_MAGIC = b"WIA\x01"
RVZ_VERSION = 0x01000000
RVZ_READ_COMPATIBLE = 0x00030000

BLOCK_TOTAL_SIZE = 0x8000
BLOCK_DATA_SIZE = 0x7C00
GROUP_DATA_SIZE = BLOCK_DATA_SIZE * 64
GROUP_TOTAL_SIZE = BLOCK_TOTAL_SIZE * 64


class RvzFormatError(WbfsError):
    pass


@dataclass(frozen=True)
class GroupEntry:
    offset: int
    stored_size: int
    compressed: bool
    zero: bool
    packed_size: int


@dataclass(frozen=True)
class PartitionDataEntry:
    first_sector: int
    sectors: int
    group_index: int
    group_count: int


@dataclass(frozen=True)
class Partition:
    key: bytes
    data: tuple[PartitionDataEntry, PartitionDataEntry]
    decrypted_size: int
    first_data_offset: int


@dataclass(frozen=True)
class Segment:
    start: int
    end: int
    kind: str  # "raw" or "wii"
    group_index: int
    group_count: int
    chunk_size: int
    dec_start: int = 0
    dec_size: int = 0
    key: bytes = b""
    partition_index: int = -1


@dataclass
class RvzContainer:
    path: str
    fd: int
    file_size: int
    iso_size: int
    disc_type: int
    compression: int
    compression_level: int
    chunk_size: int
    chunk_data_size: int
    compressor_data: bytes
    disc_header: bytes
    partitions: tuple[Partition, ...]
    raw_entries: tuple[tuple[int, int, int, int], ...]
    groups: tuple[GroupEntry, ...]
    segments: tuple[Segment, ...]
    version: int
    _starts: list[int] = field(default_factory=list)

    # ------------------------------------------------------------------
    @classmethod
    def open(cls, path: str) -> "RvzContainer":
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError as error:
            raise WbfsError(f"nie można otworzyć pliku RVZ: {error}") from error
        try:
            return cls._parse(path, fd)
        except BaseException:
            os.close(fd)
            raise

    @classmethod
    def _parse(cls, path: str, fd: int) -> "RvzContainer":
        file_size = os.fstat(fd).st_size
        header = _read_exact(fd, 0, 0x48)
        magic = header[:4]
        if magic == WIA_MAGIC:
            raise RvzFormatError(
                "format WIA nie jest jeszcze obsługiwany — użyj pliku .rvz"
            )
        if magic != RVZ_MAGIC:
            raise RvzFormatError("to nie jest plik RVZ (zły nagłówek)")
        version, compatible, header_2_size = struct.unpack_from(">III", header, 4)
        header_2_hash = header[0x10:0x24]
        iso_size, stored_file_size = struct.unpack_from(">QQ", header, 0x24)
        header_1_hash = header[0x34:0x48]
        if hashlib.sha1(header[:0x34]).digest() != header_1_hash:
            raise RvzFormatError("uszkodzony nagłówek RVZ (SHA-1)")
        if compatible > RVZ_VERSION or version < RVZ_READ_COMPATIBLE:
            raise RvzFormatError(
                f"nieobsługiwana wersja RVZ: {version:08x} "
                f"(zgodna od {compatible:08x})"
            )
        if stored_file_size != file_size:
            raise RvzFormatError(
                "rozmiar pliku RVZ nie zgadza się z nagłówkiem "
                f"({stored_file_size} != {file_size})"
            )
        if header_2_size < 0xD5 or header_2_size > 0x10000:
            raise RvzFormatError("nieprawidłowy rozmiar nagłówka RVZ")
        header_2 = _read_exact(fd, 0x48, header_2_size)
        if hashlib.sha1(header_2).digest() != header_2_hash:
            raise RvzFormatError("uszkodzony drugi nagłówek RVZ (SHA-1)")
        if len(header_2) < 0xD5:
            raise RvzFormatError("za krótki drugi nagłówek RVZ")

        disc_type, compression, compression_level, chunk_size = struct.unpack_from(
            ">IIiI", header_2, 0
        )
        disc_header = bytes(header_2[0x10:0x90])
        (
            n_partitions,
            partition_entry_size,
            partition_entries_offset,
        ) = struct.unpack_from(">IIQ", header_2, 0x90)
        partition_entries_hash = header_2[0xA0:0xB4]
        (
            n_raw,
            raw_entries_offset,
            raw_entries_stored_size,
        ) = struct.unpack_from(">IQI", header_2, 0xB4)
        (
            n_groups,
            group_entries_offset,
            group_entries_stored_size,
        ) = struct.unpack_from(">IQI", header_2, 0xC4)
        compressor_size = header_2[0xD4]
        if compressor_size > 7:
            raise RvzFormatError("nieprawidłowe dane kompresora RVZ")
        compressor_data = bytes(header_2[0xD5:0xD5 + compressor_size])

        if disc_type not in (1, 2):
            raise RvzFormatError(f"nieznany typ dysku RVZ: {disc_type}")
        if compression == COMPRESSION_PURGE:
            raise RvzFormatError(
                "kompresja PURGE nie występuje w poprawnych plikach RVZ"
            )
        if compression > codecs.COMPRESSION_ZSTD:
            raise RvzFormatError(f"nieznana kompresja RVZ: {compression}")
        if (
            chunk_size < 0x8000
            or chunk_size & (chunk_size - 1)
            or chunk_size % 0x8000
        ):
            raise RvzFormatError(f"nieprawidłowy rozmiar chunka RVZ: {chunk_size}")
        chunk_data_size = chunk_size * BLOCK_DATA_SIZE // BLOCK_TOTAL_SIZE
        if chunk_data_size % BLOCK_DATA_SIZE:
            raise RvzFormatError("nieprawidłowy rozmiar chunka dla danych Wii")
        if not 0 < iso_size <= 64 * 1024 ** 3:
            raise RvzFormatError("nieprawidłowy rozmiar obrazu w nagłówku RVZ")
        if partition_entry_size != 0x30:
            raise RvzFormatError(
                f"nieobsługiwany rozmiar wpisu partycji: {partition_entry_size}"
            )

        container = cls(
            path=path,
            fd=fd,
            file_size=file_size,
            iso_size=iso_size,
            disc_type=disc_type,
            compression=compression,
            compression_level=compression_level,
            chunk_size=chunk_size,
            chunk_data_size=chunk_data_size,
            compressor_data=compressor_data,
            disc_header=disc_header,
            partitions=(),
            raw_entries=(),
            groups=(),
            segments=(),
            version=version,
        )

        # Partition entries (stored uncompressed) ---------------------------
        partitions: list[Partition] = []
        if n_partitions:
            table = _read_exact(
                fd,
                partition_entries_offset,
                n_partitions * partition_entry_size,
            )
            if hashlib.sha1(table).digest() != partition_entries_hash:
                raise RvzFormatError("uszkodzona tablica partycji RVZ (SHA-1)")
            for index in range(n_partitions):
                base = index * partition_entry_size
                key = table[base:base + 16]
                entries = []
                for sub in range(2):
                    offset = base + 16 + sub * 16
                    first, sectors, group, count = struct.unpack_from(
                        ">IIII", table, offset
                    )
                    entries.append(
                        PartitionDataEntry(first, sectors, group, count)
                    )
                first_sector = entries[0].first_sector
                if entries[0].sectors:
                    total_sectors = (
                        entries[1].first_sector - first_sector + entries[1].sectors
                        if entries[1].sectors
                        else entries[0].sectors
                    )
                else:
                    total_sectors = entries[1].sectors
                if total_sectors <= 0:
                    continue
                partitions.append(
                    Partition(
                        key=key,
                        data=(entries[0], entries[1]),
                        decrypted_size=total_sectors * BLOCK_DATA_SIZE,
                        first_data_offset=first_sector * BLOCK_TOTAL_SIZE,
                    )
                )

        # Raw entries --------------------------------------------------------
        raw_entries: list[tuple[int, int, int, int]] = []
        if n_raw:
            raw_size = n_raw * 0x18
            raw_table = container._read_table_sized(
                raw_entries_offset, raw_entries_stored_size, raw_size
            )
            for index in range(n_raw):
                offset, size, group, count = struct.unpack_from(
                    ">QQII", raw_table, index * 0x18
                )
                raw_entries.append((offset, size, group, count))

        # Group entries ------------------------------------------------------
        groups: list[GroupEntry] = []
        if n_groups:
            group_size = n_groups * 12
            group_table = container._read_table_sized(
                group_entries_offset, group_entries_stored_size, group_size
            )
            for index in range(n_groups):
                data_offset, data_size, packed_size = struct.unpack_from(
                    ">III", group_table, index * 12
                )
                stored = data_size & 0x7FFFFFFF
                entry = GroupEntry(
                    offset=data_offset << 2,
                    stored_size=stored,
                    compressed=bool(data_size & 0x80000000),
                    zero=stored == 0,
                    packed_size=packed_size,
                )
                if not entry.zero and entry.offset + stored > file_size:
                    raise RvzFormatError(
                        f"grupa RVZ {index} wykracza poza plik"
                    )
                groups.append(entry)

        container.partitions = tuple(partitions)
        container.raw_entries = tuple(raw_entries)
        container.groups = tuple(groups)

        # Build the disc offset map -----------------------------------------
        segments: list[Segment] = []
        for raw_index, (offset, size, group, count) in enumerate(raw_entries):
            if size == 0:
                continue
            skip = offset % BLOCK_TOTAL_SIZE
            start = offset - skip
            size += skip
            end = min(start + size, iso_size)
            if start >= iso_size:
                continue
            if group + count > len(groups):
                raise RvzFormatError(
                    f"nieprawidłowy zakres grup dla danych surowych {raw_index}"
                )
            segments.append(
                Segment(
                    start=start,
                    end=end,
                    kind="raw",
                    group_index=group,
                    group_count=count,
                    chunk_size=chunk_size,
                )
            )

        for partition_index, partition in enumerate(partitions):
            for entry_index, entry in enumerate(partition.data):
                if not entry.sectors:
                    continue
                start = entry.first_sector * BLOCK_TOTAL_SIZE
                end = min(start + entry.sectors * BLOCK_TOTAL_SIZE, iso_size)
                if start >= iso_size:
                    continue
                dec_start = (
                    entry.first_sector - partition.data[0].first_sector
                ) * BLOCK_DATA_SIZE
                dec_size = entry.sectors * BLOCK_DATA_SIZE
                if entry.group_index + entry.group_count > len(groups):
                    raise RvzFormatError(
                        f"nieprawidłowy zakres grup partycji {partition_index}"
                    )
                segments.append(
                    Segment(
                        start=start,
                        end=end,
                        kind="wii",
                        group_index=entry.group_index,
                        group_count=entry.group_count,
                        chunk_size=chunk_data_size,
                        dec_start=dec_start,
                        dec_size=dec_size,
                        key=partition.key,
                        partition_index=partition_index,
                    )
                )

        segments.sort(key=lambda item: item.start)
        previous_end = -1
        for segment in segments:
            if segment.start < previous_end:
                raise RvzFormatError(
                    "nachodzące na siebie obszary danych w pliku RVZ"
                )
            previous_end = max(previous_end, segment.end)
        container.segments = tuple(segments)
        container._starts = [segment.start for segment in segments]
        return container

    # ------------------------------------------------------------------
    @property
    def compression_name(self) -> str:
        return COMPRESSION_NAMES.get(self.compression, str(self.compression))

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass

    def _read(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > self.file_size:
            raise RvzFormatError("odczyt poza plikiem RVZ")
        data = os.pread(self.fd, size, offset)
        if len(data) != size:
            raise RvzFormatError("niepełny odczyt pliku RVZ")
        return data

    def _read_table_sized(
        self, offset: int, stored_size: int, uncompressed_size: int
    ) -> bytes:
        if uncompressed_size <= 0:
            return b""
        stored = self._read(offset, stored_size)
        actual_compression = self.compression
        if actual_compression == COMPRESSION_NONE:
            data = stored
        else:
            data = codecs.decompress(
                actual_compression,
                stored,
                hint=uncompressed_size,
                properties=self.compressor_data,
            )
        if len(data) < uncompressed_size:
            raise RvzFormatError("za mała tabela RVZ po dekompresji")
        return data[:uncompressed_size]

    # ------------------------------------------------------------------
    def segment_at(self, offset: int) -> Segment | None:
        if not self.segments:
            return None
        index = bisect.bisect_right(self._starts, offset) - 1
        if index < 0:
            return None
        segment = self.segments[index]
        if offset < segment.end:
            return segment
        return None

    def next_segment_start(self, offset: int) -> int | None:
        index = bisect.bisect_right(self._starts, offset)
        if index >= len(self.segments):
            return None
        return self.segments[index].start

    # ------------------------------------------------------------------
    def raw_group(self, segment: Segment, index: int) -> bytes:
        expected = min(
            segment.chunk_size,
            segment.end - segment.start - index * segment.chunk_size,
        )
        if expected <= 0:
            return b""
        body, _lists = self.group_data(
            segment.group_index + index,
            expected,
            0,
            index * segment.chunk_size,
        )
        return body

    def wii_chunk(self, segment: Segment, index: int) -> tuple[bytes, list]:
        expected = min(
            segment.chunk_size,
            segment.dec_size - index * segment.chunk_size,
        )
        if expected <= 0:
            return b"", []
        exception_lists = max(1, segment.chunk_size // GROUP_DATA_SIZE)
        return self.group_data(
            segment.group_index + index,
            expected,
            exception_lists,
            index * segment.chunk_size,
        )

    def group_data(
        self,
        index: int,
        expected: int,
        exception_lists: int,
        data_offset: int,
    ) -> tuple[bytes, list]:
        if index < 0 or index >= len(self.groups):
            raise RvzFormatError("numer grupy RVZ poza zakresem")
        entry = self.groups[index]
        if entry.zero:
            return b"\x00" * expected, []
        stored = self._read(entry.offset, entry.stored_size)
        compression = self.compression if entry.compressed else COMPRESSION_NONE
        hint = expected + exception_lists * 0x20000 + 0x1000
        if compression == COMPRESSION_NONE:
            data = stored
        else:
            data = codecs.decompress(
                compression, stored, hint=hint, properties=self.compressor_data
            )
        if exception_lists:
            lists, position = _parse_exception_lists(
                data, exception_lists, compression == COMPRESSION_NONE
            )
            payload = data[position:]
            if entry.packed_size:
                packed = payload[:entry.packed_size]
                body = codecs.unpack_rvz(packed, expected, data_offset)
                return body, lists
            body = payload[:expected]
            if len(body) < expected:
                body += b"\x00" * (expected - len(body))
            return body, lists
        if entry.packed_size:
            packed_size = min(entry.packed_size, len(data))
            body = codecs.unpack_rvz(
                data[:packed_size], expected, data_offset
            )
            return body, []
        body = data[:expected]
        if len(body) < expected:
            body += b"\x00" * (expected - len(body))
        return body, []


def _parse_exception_lists(
    data: bytes, count: int, uncompressed: bool
) -> tuple[list[list[tuple[int, bytes]]], int]:
    position = 0
    lists: list[list[tuple[int, bytes]]] = []
    for index in range(count):
        if position + 2 > len(data):
            raise RvzFormatError("uszkodzone listy wyjątków hash RVZ")
        number = int.from_bytes(data[position:position + 2], "big")
        position += 2
        entries: list[tuple[int, bytes]] = []
        if position + number * 22 > len(data):
            raise RvzFormatError("uszkodzone listy wyjątków hash RVZ")
        for _ in range(number):
            offset = int.from_bytes(data[position:position + 2], "big")
            digest = data[position + 2:position + 22]
            position += 22
            entries.append((offset, digest))
        lists.append(entries)
        if uncompressed and index == count - 1:
            position = (position + 3) & ~3
    return lists, position


def _read_exact(fd: int, offset: int, size: int) -> bytes:
    data = os.pread(fd, size, offset)
    if len(data) != size:
        raise RvzFormatError("nieoczekiwany koniec pliku RVZ")
    return data
