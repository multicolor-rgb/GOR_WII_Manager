"""Decompression backends for the RVZ (WIA/RVZ) container.

Compression methods used by Dolphin:

* 0 NONE
* 1 PURGE (WIA only)
* 2 BZIP2
* 3 LZMA (raw LZMA1 stream with 7-Zip properties)
* 4 LZMA2 (raw stream)
* 5 Zstandard

The RVZ "packing" layer additionally replaces runs of pseudo-random padding
with a seed for a lagged Fibonacci generator.  Only raw (non-hashed) groups
can be packed.
"""

from __future__ import annotations

import bz2
import ctypes
import ctypes.util
import lzma
import struct
import threading

from ..util import WbfsError

COMPRESSION_NONE = 0
COMPRESSION_PURGE = 1
COMPRESSION_BZIP2 = 2
COMPRESSION_LZMA = 3
COMPRESSION_LZMA2 = 4
COMPRESSION_ZSTD = 5

COMPRESSION_NAMES = {
    COMPRESSION_NONE: "brak",
    COMPRESSION_PURGE: "Purge",
    COMPRESSION_BZIP2: "BZip2",
    COMPRESSION_LZMA: "LZMA",
    COMPRESSION_LZMA2: "LZMA2",
    COMPRESSION_ZSTD: "Zstandard",
}


class RvzCodecError(WbfsError):
    pass


# ---------------------------------------------------------------------------
# Zstandard via the system library (no Python package required)
# ---------------------------------------------------------------------------
_zstd_lock = threading.Lock()
_zstd = None


def _zstd_library():
    global _zstd
    if _zstd is not None:
        return _zstd
    with _zstd_lock:
        if _zstd is not None:
            return _zstd
        candidates = [ctypes.util.find_library("zstd"), "libzstd.so.1", "libzstd.so"]
        last_error = None
        for candidate in candidates:
            if not candidate:
                continue
            try:
                library = ctypes.CDLL(candidate)
            except OSError as error:
                last_error = error
                continue
            library.ZSTD_decompress.argtypes = [
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_size_t,
            ]
            library.ZSTD_decompress.restype = ctypes.c_size_t
            library.ZSTD_isError.argtypes = [ctypes.c_size_t]
            library.ZSTD_isError.restype = ctypes.c_uint
            library.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            library.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
            library.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
            library.ZSTD_compressBound.restype = ctypes.c_size_t
            library.ZSTD_compress.argtypes = [
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_int,
            ]
            library.ZSTD_compress.restype = ctypes.c_size_t
            _zstd = library
            return library
        raise RvzCodecError(
            "brak biblioteki libzstd (zwykle wystarczy pakiet libzstd1): "
            f"{last_error}"
        )


ZSTD_CONTENTSIZE_UNKNOWN = 0xFFFFFFFFFFFFFFFF
ZSTD_CONTENTSIZE_ERROR = 0xFFFFFFFFFFFFFFFE


def zstd_decompress(data: bytes, hint: int = 0) -> bytes:
    library = _zstd_library()
    source = ctypes.create_string_buffer(data, len(data))
    size = int(library.ZSTD_getFrameContentSize(source, len(data)))
    if size in (ZSTD_CONTENTSIZE_UNKNOWN, ZSTD_CONTENTSIZE_ERROR):
        size = max(hint, len(data) * 8, 1024 * 1024)
    for attempt in range(6):
        capacity = max(size, hint, 1024)
        destination = ctypes.create_string_buffer(capacity)
        written = int(library.ZSTD_decompress(destination, capacity, source, len(data)))
        if not library.ZSTD_isError(written):
            return destination.raw[:written]
        if not hint and attempt == 0:
            hint = max(1024 * 1024, len(data) * 16)
            size = hint
            continue
        size = capacity * 2
    raise RvzCodecError("nie udało się zdekompresować danych Zstandard")


def zstd_compress(data: bytes, level: int = 5) -> bytes:
    """Used by the test fixture writer."""
    library = _zstd_library()
    source = ctypes.create_string_buffer(data, len(data))
    capacity = int(library.ZSTD_compressBound(len(data)))
    destination = ctypes.create_string_buffer(capacity)
    written = int(
        library.ZSTD_compress(destination, capacity, source, len(data), level)
    )
    if library.ZSTD_isError(written):
        raise RvzCodecError("nie udało się skompresować danych Zstandard")
    return destination.raw[:written]


# ---------------------------------------------------------------------------
# LZMA / LZMA2 properties (7-Zip encoding used by WIA/RVZ)
# ---------------------------------------------------------------------------
def _lzma_filters(compression: int, properties: bytes):
    if compression == COMPRESSION_LZMA:
        if len(properties) < 5:
            raise RvzCodecError("brak właściwości kompresora LZMA")
        value = properties[0]
        if value >= 9 * 5 * 5:
            raise RvzCodecError("nieobsługiwane właściwości LZMA")
        lc = value % 9
        value //= 9
        pb = value // 5
        lp = value % 5
        dictionary = int.from_bytes(properties[1:5], "little")
        return [
            {
                "id": lzma.FILTER_LZMA1,
                "dict_size": max(dictionary, 4096),
                "lc": lc,
                "lp": lp,
                "pb": pb,
            }
        ]
    if len(properties) < 1:
        raise RvzCodecError("brak właściwości kompresora LZMA2")
    prop = properties[0]
    if prop > 40:
        raise RvzCodecError("nieobsługiwane właściwości LZMA2")
    dictionary = 0xFFFFFFFF if prop == 40 else ((2 | (prop & 1)) << (prop // 2 + 11))
    return [{"id": lzma.FILTER_LZMA2, "dict_size": dictionary}]


def _lzma_decompress(data: bytes, compression: int, properties: bytes) -> bytes:
    filters = _lzma_filters(compression, properties)
    decompressor = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
    try:
        return decompressor.decompress(data)
    except lzma.LZMAError as error:
        raise RvzCodecError(f"błąd dekompresji LZMA: {error}") from error


# ---------------------------------------------------------------------------
# Generic entry point
# ---------------------------------------------------------------------------
def decompress(
    compression: int,
    data: bytes,
    hint: int = 0,
    properties: bytes = b"",
) -> bytes:
    """Decompress one RVZ/WIA chunk (without removing the RVZ packing)."""
    if compression == COMPRESSION_NONE:
        return data
    if compression == COMPRESSION_BZIP2:
        try:
            return bz2.decompress(data)
        except OSError as error:
            raise RvzCodecError(f"błąd dekompresji BZip2: {error}") from error
    if compression in (COMPRESSION_LZMA, COMPRESSION_LZMA2):
        return _lzma_decompress(data, compression, properties)
    if compression == COMPRESSION_ZSTD:
        return zstd_decompress(data, hint)
    if compression == COMPRESSION_PURGE:
        raise RvzCodecError(
            "grupy PURGE występują tylko w formacie WIA, który nie jest jeszcze "
            "obsługiwany"
        )
    raise RvzCodecError(f"nieznana metoda kompresji RVZ: {compression}")


# ---------------------------------------------------------------------------
# RVZ packing (lagged Fibonacci padding)
# ---------------------------------------------------------------------------
def _swap32(value: int) -> int:
    return int.from_bytes(int(value & 0xFFFFFFFF).to_bytes(4, "little"), "big")


class LaggedFibonacciGenerator:
    """Dolphin's generator used to reproduce pseudo-random disc padding."""

    K = 521
    J = 32
    SEED_WORDS = 17
    SEED_BYTES = SEED_WORDS * 4
    BLOCK_BYTES = K * 4

    def __init__(self, seed: bytes, offset: int):
        if len(seed) < self.SEED_BYTES:
            raise RvzCodecError("za krótki ziarno generatora padding-u RVZ")
        words = list(struct.unpack(">17I", seed[: self.SEED_BYTES]))
        buffer = [0] * self.K
        buffer[: self.SEED_WORDS] = words
        for index in range(self.SEED_WORDS, self.K):
            buffer[index] = (
                (buffer[index - 17] << 23)
                ^ (buffer[index - 16] >> 9)
                ^ buffer[index - 1]
            ) & 0xFFFFFFFF

        # Dolphin folds the "shift by 18 instead of 16" quirk and the byte swap
        # into the buffer so that GetBytes can be a plain memcpy.  Mirror it.
        for index, value in enumerate(buffer):
            buffer[index] = _swap32(
                (value & 0xFF00FFFF) | ((value >> 2) & 0x00FF0000)
            )
        self.buffer = buffer
        self.position = 0
        self._bytes = b""
        for _ in range(4):
            self._step()
        self.forward(offset % 0x8000)

    def _refresh(self) -> None:
        self._bytes = struct.pack("<521I", *self.buffer)

    def _step(self) -> None:
        for index in range(self.J):
            self.buffer[index] ^= self.buffer[index + self.K - self.J]
        for index in range(self.J, self.K):
            self.buffer[index] ^= self.buffer[index - self.J]
        self._refresh()

    def forward(self, count: int) -> None:
        self.position += count
        while self.position >= self.BLOCK_BYTES:
            self._step()
            self.position -= self.BLOCK_BYTES

    def get(self, count: int) -> bytes:
        result = bytearray()
        while count > 0:
            if self.position >= self.BLOCK_BYTES:
                self._step()
                self.position = 0
            available = self.BLOCK_BYTES - self.position
            take = min(count, available)
            result += self._bytes[self.position:self.position + take]
            self.position += take
            count -= take
        return bytes(result)


def unpack_rvz(data: bytes, expected: int, data_offset: int) -> bytes:
    """Expand a packed RVZ stream to exactly ``expected`` bytes."""
    output = bytearray()
    position = 0
    size = len(data)
    while len(output) < expected:
        if position + 4 > size:
            raise RvzCodecError("uszkodzony strumień pakowania RVZ (brak nagłówka)")
        record = int.from_bytes(data[position:position + 4], "big")
        position += 4
        junk = bool(record & 0x80000000)
        count = record & 0x7FFFFFFF
        if count == 0:
            continue
        if junk:
            if position + LaggedFibonacciGenerator.SEED_BYTES > size:
                raise RvzCodecError("uszkodzony strumień pakowania RVZ (brak ziarna)")
            generator = LaggedFibonacciGenerator(
                data[position:position + LaggedFibonacciGenerator.SEED_BYTES],
                data_offset + len(output),
            )
            position += LaggedFibonacciGenerator.SEED_BYTES
            output += generator.get(count)
        else:
            if position + count > size:
                raise RvzCodecError("uszkodzony strumień pakowania RVZ (brak danych)")
            output += data[position:position + count]
            position += count
    if len(output) < expected:
        output += b"\x00" * (expected - len(output))
    return bytes(output[:expected])
