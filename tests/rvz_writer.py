"""Test-only RVZ writer used to validate the native RVZ reader.

It mirrors the structures documented in Dolphin's docs/WiaAndRvz.md:
header 1/2, partition/raw/group tables, chunk compression and exception lists.
It is intentionally small and only supports what the tests need.
"""

from __future__ import annotations

import bz2
import hashlib
import lzma
import struct

from gor_wii_manager.core.rvz import codecs
from gor_wii_manager.core.rvz.wii import (
    apply_hash_exceptions,
    build_hash_area,
    encrypt_group,
)

BLOCK_TOTAL = 0x8000
BLOCK_DATA = 0x7C00
GROUP_DATA = BLOCK_DATA * 64

RVZ_MAGIC = b"RVZ\x01"
RVZ_VERSION = 0x01000000
RVZ_COMPAT = 0x00030000


def _be32(value: int) -> bytes:
    return int(value).to_bytes(4, "big")


def _compress(data: bytes, method: int):
    if method == codecs.COMPRESSION_NONE:
        return data, b""
    if method == codecs.COMPRESSION_ZSTD:
        return codecs.zstd_compress(data), b""
    if method == codecs.COMPRESSION_BZIP2:
        return bz2.compress(data), b""
    if method == codecs.COMPRESSION_LZMA:
        filters = [
            {
                "id": lzma.FILTER_LZMA1,
                "dict_size": 1 << 20,
                "lc": 3,
                "lp": 0,
                "pb": 2,
            }
        ]
        compressed = lzma.compress(data, format=lzma.FORMAT_RAW, filters=filters)
        props = bytes([(2 * 5 + 0) * 9 + 3]) + (1 << 20).to_bytes(4, "little")
        return compressed, props
    if method == codecs.COMPRESSION_LZMA2:
        filters = [{"id": lzma.FILTER_LZMA2, "dict_size": 1 << 20}]
        compressed = lzma.compress(data, format=lzma.FORMAT_RAW, filters=filters)
        return compressed, bytes([16])  # (2|0) << (16/2+11) = 1 MiB
    raise ValueError("unsupported compression")


def _group_stream(data: bytes, method: int):
    compressed, _props = _compress(data, method)
    return compressed


def build_rvz(
    path: str,
    iso: bytes,
    *,
    compression: int = codecs.COMPRESSION_NONE,
    chunk_size: int = 0x200000,
    wii_partition=None,
    exceptions=(),
) -> str:
    """Write an RVZ container.

    ``wii_partition`` is ``(data_start, data_end, key, decrypted_bytes)`` for
    a Wii image, or None for GameCube.  The ISO is expected to already contain
    the encrypted partition data produced from ``decrypted_bytes``.
    """
    iso = bytes(iso)
    chunk_data = chunk_size * BLOCK_DATA // BLOCK_TOTAL
    units = []
    if wii_partition is None:
        units.append(("raw", 0, len(iso)))
        disc_type = 1
    else:
        data_start, data_end, key, decrypted = wii_partition[:4]
        split = wii_partition[4] if len(wii_partition) > 4 else None
        units.append(("raw", 0, data_start))
        units.append(("part", data_start, data_end, key, decrypted, split))
        units.append(("raw", data_end, len(iso)))
        disc_type = 2

    raw_entries = []
    partitions = []
    groups: list[tuple[bytes, bool]] = []  # (decompressed group bytes, compressed flag)
    group_index = 0

    for unit in units:
        if unit[0] == "raw":
            _kind, offset, size = unit
            if size <= 0:
                continue
            skip = min(0x80 - offset, size) if offset < 0x80 else 0
            metadata_offset = offset + skip
            metadata_size = size - skip
            aligned_start = offset - offset % BLOCK_TOTAL
            raw = iso[aligned_start:offset + size]
            count = max(1, -(-len(raw) // chunk_size))
            raw_entries.append(
                (metadata_offset, metadata_size, group_index, count)
            )
            for index in range(count):
                chunk = raw[index * chunk_size:(index + 1) * chunk_size]
                if len(chunk) < chunk_size:
                    chunk += b"\x00" * (chunk_size - len(chunk))
                groups.append(chunk)
            group_index += count
        else:
            _kind, data_start, data_end, key, decrypted, split = unit
            ranges = []
            if split is None:
                ranges.append((data_start, data_end, 0))
            else:
                ranges.append((data_start, split, 0))
                ranges.append((split, data_end, (split - data_start) // BLOCK_TOTAL * BLOCK_DATA))
            pd_entries = []
            for range_index, (pd_start, pd_end, dec_offset) in enumerate(ranges):
                sectors = (pd_end - pd_start) // BLOCK_TOTAL
                dec_data = decrypted[
                    dec_offset:dec_offset + sectors * BLOCK_DATA
                ]
                count = max(1, -(-len(dec_data) // chunk_data))
                pd_entries.append(
                    (pd_start // BLOCK_TOTAL, sectors, group_index, count)
                )
                for index in range(count):
                    chunk = dec_data[index * chunk_data:(index + 1) * chunk_data]
                    if len(chunk) < chunk_data:
                        chunk += b"\x00" * (chunk_data - len(chunk))
                    lists = max(1, chunk_data // GROUP_DATA)
                    prefix_parts = []
                    for list_index in range(lists):
                        entries = []
                        if range_index == 0 and index == 0 and list_index == 0:
                            entries = list(exceptions)
                        prefix_parts.append(len(entries).to_bytes(2, "big"))
                        for offset, digest in entries:
                            prefix_parts.append(offset.to_bytes(2, "big") + digest)
                    prefix = b"".join(prefix_parts)
                    if compression == codecs.COMPRESSION_NONE:
                        prefix += b"\x00" * ((4 - len(prefix) % 4) % 4)
                    groups.append(prefix + chunk)
                group_index += count
            partitions.append((key, pd_entries))

    # Fixed layout keeps the writer simple and avoids circular table sizes.
    partition_offset = 0x1000 if partitions else 0
    raw_offset = 0x2000
    group_offset = 0x3000
    data_offset = 0x10000

    partition_table = b"".join(
        key
        + b"".join(
            b"".join(_be32(value) for value in pd)
            for pd in list(entries) + [None] * (2 - len(entries))
            if pd is not None
        )
        + b"\x00" * 16 * max(0, 2 - len(entries))
        for key, entries in partitions
    )
    raw_table = b"".join(
        struct.pack(">QQII", off, size, group, count)
        for off, size, group, count in raw_entries
    )
    group_entries = []
    blob_position = data_offset
    blobs = []
    compressed_flag = compression != codecs.COMPRESSION_NONE
    for stream in groups:
        blob, _ = _compress(stream, compression)
        value = len(blob) | (0x80000000 if compressed_flag else 0)
        group_entries.append(struct.pack(">III", blob_position >> 2, value, 0))
        blobs.append(blob)
        padding = (4 - (blob_position + len(blob)) % 4) % 4
        if padding:
            blobs.append(b"\x00" * padding)
        blob_position += len(blob) + padding
    group_table = b"".join(group_entries)

    packed_partition = partition_table  # partition entries are always raw
    if compressed_flag:
        packed_raw, props = _compress(raw_table, compression)
        packed_groups, _ = _compress(group_table, compression)
    else:
        packed_raw = raw_table
        packed_groups = group_table
        props = b""

    header2_size = 0xDC
    header2 = bytearray(header2_size)
    struct.pack_into(">IIiI", header2, 0, disc_type, compression, 0, chunk_size)
    header2[0x10:0x90] = iso[:0x80]
    struct.pack_into(
        ">IIQ", header2, 0x90, len(partitions), 0x30, partition_offset
    )
    header2[0xA0:0xB4] = hashlib.sha1(partition_table).digest()
    struct.pack_into(
        ">IQI",
        header2,
        0xB4,
        len(raw_entries),
        raw_offset,
        len(packed_raw),
    )
    struct.pack_into(
        ">IQI",
        header2,
        0xC4,
        len(groups),
        group_offset,
        len(packed_groups),
    )
    header2[0xD4] = len(props)
    header2[0xD5:0xD5 + len(props)] = props

    total_size = blob_position
    header1 = bytearray(0x48)
    header1[:4] = RVZ_MAGIC
    struct.pack_into(
        ">III",
        header1,
        4,
        RVZ_VERSION,
        RVZ_COMPAT,
        header2_size,
    )
    header1[0x10:0x24] = hashlib.sha1(bytes(header2)).digest()
    struct.pack_into(">QQ", header1, 0x24, len(iso), total_size)

    output = bytearray(total_size)
    output[:0x48] = header1
    output[0x48:0x48 + header2_size] = header2
    if partitions:
        output[partition_offset:partition_offset + len(partition_table)] = partition_table
    output[raw_offset:raw_offset + len(packed_raw)] = packed_raw
    output[group_offset:group_offset + len(packed_groups)] = packed_groups
    position = data_offset
    for blob in blobs:
        output[position:position + len(blob)] = blob
        position += len(blob)
    header1[0x34:0x48] = hashlib.sha1(bytes(header1[:0x34])).digest()
    output[:0x48] = header1
    with open(path, "wb") as handle:
        handle.write(output)
    return path


def make_synthetic_wii_iso(
    size: int = 0x800000,
    data_start: int = 0x420000,
    sectors: int = 8,
    key: bytes = bytes(range(16)),
    exceptions=(),
):
    """Build a minimal Wii ISO with one encrypted, hashed game partition."""
    iso = bytearray(size)
    iso[0:6] = b"RVTE01"
    iso[3] = ord("P")
    iso[0x18:0x1C] = _be32(0x5D1C9EA3)
    iso[0x20:0x30] = b"RVZ Test".ljust(0x10, b"\x00")

    partition = 0x400000
    data_size = sectors * BLOCK_TOTAL
    data_end = data_start + data_size
    iso[0x40000:0x40004] = _be32(1)
    iso[0x40004:0x40008] = _be32(0x40020 // 4)
    iso[0x40020:0x40024] = _be32(partition // 4)
    iso[0x40024:0x40028] = _be32(0)
    data_offset = data_start - partition
    fields = [
        0x100,
        0x300 // 4,
        0x100,
        0x400 // 4,
        0x800 // 4,
        data_offset // 4,
        data_size // 4,
    ]
    for index, value in enumerate(fields):
        position = partition + 0x2A4 + index * 4
        iso[position:position + 4] = _be32(value)

    decrypted = bytearray(sectors * BLOCK_DATA)
    for cluster in range(sectors):
        pattern = bytes([(cluster * 7) & 0xFF]) * 64
        start = cluster * BLOCK_DATA
        decrypted[start:start + 64] = pattern

    group_count = max(1, -(-sectors // 64))
    h3_table = bytearray()
    position = data_start
    for group in range(group_count):
        clusters = min(64, sectors - group * 64)
        disc_bytes = clusters * BLOCK_TOTAL
        dec_group = bytes(
            decrypted[group * GROUP_DATA:(group + 1) * GROUP_DATA]
        ).ljust(GROUP_DATA, b"\x00")
        exceptions_group = exceptions if group == 0 else ()
        encrypted = encrypt_group(dec_group, key, exceptions_group)
        iso[position:position + disc_bytes] = encrypted[:disc_bytes]
        position += disc_bytes
        hash_area = build_hash_area(dec_group)
        if exceptions_group:
            apply_hash_exceptions(hash_area, exceptions_group)
        h3_table += hashlib.sha1(hash_area[0x340:0x3E0]).digest()
    iso[partition + 0x800:partition + 0x800 + len(h3_table)] = h3_table
    return bytes(iso), bytes(decrypted)
