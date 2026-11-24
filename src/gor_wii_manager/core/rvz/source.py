"""Random-access view of an RVZ file as an encrypted Wii/GameCube ISO."""

from __future__ import annotations

import threading
from collections import OrderedDict

from ..disc import read_info
from ..util import WbfsError
from .container import (
    BLOCK_DATA_SIZE,
    BLOCK_TOTAL_SIZE,
    GROUP_DATA_SIZE,
    GROUP_TOTAL_SIZE,
    RvzContainer,
    Segment,
)
from .wii import encrypt_group


class RvzSource:
    """Expose an RVZ container through the same interface as ``IsoSource``."""

    def __init__(self, path: str):
        self.path = path
        self.container = RvzContainer.open(path)
        self.size = self.container.iso_size
        self._raw_cache: OrderedDict[int, bytes] = OrderedDict()
        self._chunk_cache: OrderedDict[int, tuple[bytes, list]] = OrderedDict()
        self._group_cache: OrderedDict[tuple[int, int], bytes] = OrderedDict()
        self._lock = threading.RLock()
        self.info = read_info(self)

    # ------------------------------------------------------------------
    def close(self) -> None:
        self.container.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ------------------------------------------------------------------
    def _remember(self, cache: OrderedDict, key, value, limit: int):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)
        return value

    def _raw_group(self, segment: Segment, index: int) -> bytes:
        key = segment.group_index + index
        with self._lock:
            cached = self._raw_cache.get(key)
            if cached is not None:
                self._raw_cache.move_to_end(key)
                return cached
        data = self.container.raw_group(segment, index)
        with self._lock:
            return self._remember(self._raw_cache, key, data, 6)

    def _wii_chunk(self, segment: Segment, index: int):
        key = segment.group_index + index
        with self._lock:
            cached = self._chunk_cache.get(key)
            if cached is not None:
                self._chunk_cache.move_to_end(key)
                return cached
        data = self.container.wii_chunk(segment, index)
        with self._lock:
            return self._remember(self._chunk_cache, key, data, 16)

    def _encrypted_group(self, partition_index: int, group_index: int) -> bytes:
        key = (partition_index, group_index)
        with self._lock:
            cached = self._group_cache.get(key)
            if cached is not None:
                self._group_cache.move_to_end(key)
                return cached

        container = self.container
        partition = container.partitions[partition_index]
        decrypted_start = group_index * GROUP_DATA_SIZE
        decrypted_end = decrypted_start + GROUP_DATA_SIZE
        buffer = bytearray(GROUP_DATA_SIZE)
        exceptions: list[tuple[int, bytes]] = []

        for segment in container.segments:
            if segment.kind != "wii" or segment.partition_index != partition_index:
                continue
            segment_dec_start = segment.dec_start
            segment_dec_end = segment.dec_start + segment.dec_size
            if segment_dec_end <= decrypted_start or segment_dec_start >= decrypted_end:
                continue
            chunk_size = segment.chunk_size
            first_chunk = max(0, (decrypted_start - segment_dec_start) // chunk_size)
            index = first_chunk
            while True:
                chunk_dec_start = segment_dec_start + index * chunk_size
                if chunk_dec_start >= segment_dec_end or chunk_dec_start >= decrypted_end:
                    break
                chunk_dec_end = min(chunk_dec_start + chunk_size, segment_dec_end)
                data, lists = self._wii_chunk(segment, index)
                copy_start = max(chunk_dec_start, decrypted_start)
                copy_end = min(chunk_dec_end, decrypted_end)
                if copy_end > copy_start:
                    source_offset = copy_start - chunk_dec_start
                    target_offset = copy_start - decrypted_start
                    length = copy_end - copy_start
                    piece = data[source_offset:source_offset + length]
                    if len(piece) < length:
                        piece += b"\x00" * (length - len(piece))
                    buffer[target_offset:target_offset + length] = piece
                additional = (
                    (chunk_dec_start % GROUP_DATA_SIZE) // BLOCK_DATA_SIZE * 0x400
                )
                for list_index, entries in enumerate(lists):
                    list_group = chunk_dec_start // GROUP_DATA_SIZE + list_index
                    if list_group != group_index:
                        continue
                    for offset, digest in entries:
                        absolute = additional + list_index * 0x10000 + offset
                        if 0 <= absolute <= 0x10000 - 20:
                            exceptions.append((absolute, digest))
                index += 1

        encrypted = encrypt_group(bytes(buffer), partition.key, exceptions)
        with self._lock:
            return self._remember(
                self._group_cache, key, encrypted, 8
            )

    # ------------------------------------------------------------------
    def read_zeros(self, offset: int, length: int) -> bytes:
        if offset < 0 or length <= 0:
            return b""
        if offset >= self.size:
            return b"\x00" * length
        wanted = min(length, self.size - offset)
        out = bytearray()
        position = offset
        remaining = wanted

        if position < 0x80:
            count = min(0x80 - position, remaining)
            out += self.container.disc_header[position:position + count]
            position += count
            remaining -= count

        container = self.container
        while remaining > 0:
            segment = container.segment_at(position)
            if segment is None:
                next_start = container.next_segment_start(position)
                boundary = next_start if next_start is not None else self.size
                count = min(remaining, max(boundary - position, 1))
                out += b"\x00" * count
                position += count
                remaining -= count
                continue
            if segment.kind == "raw":
                relative = position - segment.start
                index = relative // segment.chunk_size
                group = self._raw_group(segment, index)
                within = relative % segment.chunk_size
                count = min(remaining, segment.end - position, len(group) - within)
                if count <= 0:
                    out += b"\x00"
                    position += 1
                    remaining -= 1
                    continue
                out += group[within:within + count]
            else:
                partition = container.partitions[segment.partition_index]
                partition_relative = position - partition.first_data_offset
                group_index = partition_relative // GROUP_TOTAL_SIZE
                within = partition_relative % GROUP_TOTAL_SIZE
                group = self._encrypted_group(segment.partition_index, group_index)
                count = min(remaining, segment.end - position, len(group) - within)
                out += group[within:within + count]
            position += count
            remaining -= count

        if len(out) < wanted:
            out += b"\x00" * (wanted - len(out))
        if len(out) < length:
            out += b"\x00" * (length - len(out))
        return bytes(out)

    read = read_zeros
