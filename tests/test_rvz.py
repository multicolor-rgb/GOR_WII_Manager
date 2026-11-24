"""Tests for the native RVZ reader and conversions."""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gor_wii_manager.core import wbfs_layout as layout
from gor_wii_manager.core.convert import source_to_iso, source_to_wbfs
from gor_wii_manager.core.disc import parse_header
from gor_wii_manager.core.rvz import RvzFormatError, RvzSource
from gor_wii_manager.core.rvz import codecs
from gor_wii_manager.core.rvz.codecs import LaggedFibonacciGenerator, unpack_rvz
from gor_wii_manager.core.wbfs import WbfsVolume

from rvz_writer import build_rvz, make_synthetic_wii_iso

WII_MAGIC = 0x5D1C9EA3
GC_MAGIC = 0xC2339F3D


def make_gc_iso(size: int = 4 * 1024 * 1024) -> bytes:
    data = bytearray(size)
    data[0:6] = b"GTEE01"
    data[0x1C:0x20] = GC_MAGIC.to_bytes(4, "big")
    data[0x20:0x30] = b"RVZ GC Test".ljust(0x10, b"\x00")
    for offset in range(0x20000, size, 0x20000):
        data[offset:offset + 64] = bytes([offset & 0xFF]) * 64
    return bytes(data)


class RvzTempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rvz-test-")
        self.tmp = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def path(self, name: str) -> str:
        return os.path.join(self.tmp, name)


class CodecTests(unittest.TestCase):
    def test_rvz_unpack_literals_and_junk(self) -> None:
        seed = bytes(range(68))
        literal = b"LITERAL-DATA" * 37
        junk_count = 5000
        generator = LaggedFibonacciGenerator(seed, len(literal))
        junk = generator.get(junk_count)
        stream = (
            struct.pack(">I", len(literal))
            + literal
            + struct.pack(">I", 0x80000000 | junk_count)
            + seed
        )
        expected = literal + junk
        self.assertEqual(unpack_rvz(stream, len(expected), 0), expected)

    def test_padding_matches_reference(self) -> None:
        seed = bytes(range(68))

        def reference(offset: int, count: int) -> bytes:
            buffer = list(struct.unpack(">17I", seed)) + [0] * (521 - 17)
            for index in range(17, 521):
                buffer[index] = (
                    (buffer[index - 17] << 23)
                    ^ (buffer[index - 16] >> 9)
                    ^ buffer[index - 1]
                ) & 0xFFFFFFFF

            def advance():
                for index in range(521):
                    buffer[index] ^= buffer[(index + 521 - 32) % 521]

            for _ in range(4):
                advance()
            out = bytearray()
            while len(out) < offset + count:
                for value in buffer:
                    out += bytes(
                        (
                            (value >> 24) & 0xFF,
                            (value >> 18) & 0xFF,
                            (value >> 8) & 0xFF,
                            value & 0xFF,
                        )
                    )
                advance()
            return bytes(out[offset:offset + count])

        for offset in (0, 12345, 0x8000 - 3):
            expected = reference(offset, 10000)
            generator = LaggedFibonacciGenerator(seed, offset)
            self.assertEqual(generator.get(10000), expected)

    def test_packed_wii_chunk_after_exception_list(self) -> None:
        from gor_wii_manager.core.rvz.container import GroupEntry, RvzContainer

        payload = bytes(range(256)) * 100
        packed = struct.pack(">I", len(payload)) + payload
        # Empty exception list, 4-byte alignment, then the packed stream.
        data = b"\x00\x00\x00\x00" + packed
        container = object.__new__(RvzContainer)
        container.groups = (
            GroupEntry(0, len(data), False, False, len(packed)),
        )
        container.compression = 0
        container.compressor_data = b""
        container._read = lambda offset, size: data[offset:offset + size]
        body, lists = container.group_data(0, len(payload), 1, 0)
        self.assertEqual(lists, [[]])
        self.assertEqual(body, payload)

    def test_codecs_roundtrip(self) -> None:
        payload = bytes(range(256)) * 200 + b"tail"
        for method in (
            codecs.COMPRESSION_ZSTD,
            codecs.COMPRESSION_BZIP2,
            codecs.COMPRESSION_LZMA,
            codecs.COMPRESSION_LZMA2,
            codecs.COMPRESSION_NONE,
        ):
            if method == codecs.COMPRESSION_ZSTD:
                compressed = codecs.zstd_compress(payload)
                props = b""
            elif method == codecs.COMPRESSION_BZIP2:
                import bz2

                compressed = bz2.compress(payload)
                props = b""
            elif method == codecs.COMPRESSION_LZMA:
                import lzma

                filters = [
                    {
                        "id": lzma.FILTER_LZMA1,
                        "dict_size": 1 << 20,
                        "lc": 3,
                        "lp": 0,
                        "pb": 2,
                    }
                ]
                compressed = lzma.compress(
                    payload, format=lzma.FORMAT_RAW, filters=filters
                )
                props = bytes([(2 * 5) * 9 + 3]) + (1 << 20).to_bytes(4, "little")
            elif method == codecs.COMPRESSION_LZMA2:
                import lzma

                compressed = lzma.compress(
                    payload,
                    format=lzma.FORMAT_RAW,
                    filters=[{"id": lzma.FILTER_LZMA2, "dict_size": 1 << 20}],
                )
                props = bytes([16])
            else:
                compressed = payload
                props = b""
            result = codecs.decompress(
                method, compressed, hint=len(payload), properties=props
            )
            self.assertEqual(result, payload, codecs.COMPRESSION_NAMES[method])


class RvzReaderTests(RvzTempCase):
    def test_gamecube_all_codecs(self) -> None:
        iso = make_gc_iso()
        for method in (
            codecs.COMPRESSION_NONE,
            codecs.COMPRESSION_ZSTD,
            codecs.COMPRESSION_BZIP2,
            codecs.COMPRESSION_LZMA,
            codecs.COMPRESSION_LZMA2,
        ):
            name = f"gc-{method}.rvz"
            path = self.path(name)
            build_rvz(path, iso, compression=method)
            with RvzSource(path) as source:
                self.assertEqual(source.size, len(iso))
                self.assertTrue(source.info.is_gc)
                self.assertEqual(source.info.disc_id, "GTEE01")
                self.assertEqual(source.read_zeros(0, len(iso)), iso, name)
                rest = source.read_zeros(len(iso) - 10, 4096)
                self.assertEqual(rest[:10], iso[-10:])
                self.assertEqual(rest[10:], b"\x00" * (4096 - 10))

            output = self.path(f"gc-{method}.iso")
            with RvzSource(path) as source:
                source_to_iso(source, output)
            with open(output, "rb") as handle:
                self.assertEqual(handle.read(), iso)

    def test_gamecube_small_chunks(self) -> None:
        iso = make_gc_iso(1024 * 1024)
        for chunk in (0x20000, 0x8000):
            path = self.path(f"gc-chunk-{chunk}.rvz")
            build_rvz(path, iso, compression=codecs.COMPRESSION_ZSTD, chunk_size=chunk)
            with RvzSource(path) as source:
                self.assertEqual(source.read_zeros(0, len(iso)), iso, hex(chunk))

    def test_wii_two_partition_entries(self) -> None:
        iso, decrypted = make_synthetic_wii_iso(size=0x1000000, sectors=128)
        path = self.path("wii-split.rvz")
        build_rvz(
            path,
            iso,
            compression=codecs.COMPRESSION_ZSTD,
            chunk_size=0x200000,
            wii_partition=(
                0x420000,
                0x820000,
                bytes(range(16)),
                decrypted,
                0x620000,
            ),
        )
        with RvzSource(path) as source:
            self.assertEqual(len(source.info.partitions), 1)
            self.assertEqual(source.read_zeros(0, len(iso)), iso)

    def test_wii_small_chunks(self) -> None:
        iso, decrypted = make_synthetic_wii_iso()
        for chunk in (0x20000, 0x8000):
            path = self.path(f"wii-chunk-{chunk}.rvz")
            build_rvz(
                path,
                iso,
                compression=codecs.COMPRESSION_ZSTD,
                chunk_size=chunk,
                wii_partition=(0x420000, 0x460000, bytes(range(16)), decrypted),
            )
            with RvzSource(path) as source:
                self.assertEqual(source.read_zeros(0, len(iso)), iso, hex(chunk))

    def test_wii_roundtrip(self) -> None:
        iso, decrypted = make_synthetic_wii_iso()
        for method in (codecs.COMPRESSION_NONE, codecs.COMPRESSION_ZSTD):
            path = self.path(f"wii-{method}.rvz")
            build_rvz(
                path,
                iso,
                compression=method,
                wii_partition=(0x420000, 0x460000, bytes(range(16)), decrypted),
            )
            with RvzSource(path) as source:
                self.assertEqual(source.size, len(iso))
                self.assertTrue(source.info.is_wii)
                self.assertEqual(source.info.disc_id, "RVTP01")
                self.assertEqual(len(source.info.partitions), 1)
                self.assertEqual(source.info.partitions[0].offset, 0x400000)
                self.assertEqual(source.read_zeros(0, len(iso)), iso, method)
                output = self.path(f"wii-{method}.iso")
                source_to_iso(source, output)
            with open(output, "rb") as handle:
                self.assertEqual(handle.read(), iso)

    def test_wii_hash_exceptions(self) -> None:
        digest = b"\xAA" * 20
        for offset in (0, 0x400, 0x280):
            iso, decrypted = make_synthetic_wii_iso(exceptions=[(offset, digest)])
            path = self.path(f"exc-{offset}.rvz")
            build_rvz(
                path,
                iso,
                compression=codecs.COMPRESSION_ZSTD,
                wii_partition=(0x420000, 0x460000, bytes(range(16)), decrypted),
                exceptions=[(offset, digest)],
            )
            with RvzSource(path) as source:
                self.assertEqual(source.read_zeros(0, len(iso)), iso, hex(offset))

    def test_wii_import_to_wbfs(self) -> None:
        iso, decrypted = make_synthetic_wii_iso()
        rvz = self.path("game.rvz")
        build_rvz(
            rvz,
            iso,
            compression=codecs.COMPRESSION_ZSTD,
            wii_partition=(0x420000, 0x460000, bytes(range(16)), decrypted),
        )
        target = self.path("out.wbfs")
        with RvzSource(rvz) as source:
            source_to_wbfs(source, target)
        volume = WbfsVolume.open(target)
        try:
            games = volume.games()
            self.assertEqual(len(games), 1)
            self.assertEqual(games[0].disc_id, "RVTP01")
            output = self.path("from-wbfs.iso")
            volume.extract_iso(games[0], output, size=len(iso))
        finally:
            volume.close()
        with open(output, "rb") as handle:
            self.assertEqual(handle.read(), iso)

    def test_corrupted_header_rejected(self) -> None:
        iso = make_gc_iso(0x40000)
        path = self.path("bad.rvz")
        build_rvz(path, iso)
        with open(path, "r+b") as handle:
            handle.seek(0x20)
            handle.write(b"\xff")
        with self.assertRaises(RvzFormatError):
            RvzSource(path)

    def test_wia_is_reported(self) -> None:
        path = self.path("fake.wia")
        with open(path, "wb") as handle:
            handle.write(b"WIA\x01" + b"\x00" * 0x100)
        with self.assertRaises(RvzFormatError):
            RvzSource(path)


if __name__ == "__main__":
    unittest.main()
