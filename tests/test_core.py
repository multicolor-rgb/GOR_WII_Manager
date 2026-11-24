"""Core engine tests: WBFS layout, imports, extraction, scrubbing and repair."""

from __future__ import annotations

import os
import struct
import tempfile
import unittest

from gor_wii_manager.core import diskio as diskio_module
from gor_wii_manager.core import wbfs_layout as layout
from gor_wii_manager.core.disc import IsoSource, build_usage, parse_header
from gor_wii_manager.core.diskio import SplitFileIO
from gor_wii_manager.core.titles import TitleDB
from gor_wii_manager.core.art import cover_urls
from gor_wii_manager.core.util import (
    DuplicateDiscError,
    game_filename,
    human_size,
    parse_size,
    sanitize_filename,
)
from gor_wii_manager.core.wbfs import WbfsVolume, clone_game_to_file

WII_MAGIC = 0x5D1C9EA3
GC_MAGIC = 0xC2339F3D


def be32(value: int) -> bytes:
    return int(value).to_bytes(4, "big")


def make_wii_iso(path: str, size: int = 0x800000, with_update: bool = True) -> bytes:
    data = bytearray(size)
    data[0:6] = b"RTPP01"
    data[6] = 0
    data[7] = 2
    data[0x18:0x1C] = be32(WII_MAGIC)
    data[0x20:0x30] = b"Test Game".ljust(0x10, b"\x00")
    data[0x4E000:0x4E004] = be32(2)
    data[0x4FFFC:0x50000] = be32(0xC3F81A8E)

    parts = []
    if with_update:
        parts.append((0x50000, 1))
    parts.append((0x500000, 0))
    data[0x40000:0x40004] = be32(len(parts))
    data[0x40004:0x40008] = be32(0x40020 // 4)
    for index, (offset, part_type) in enumerate(parts):
        entry = 0x40020 + index * 8
        data[entry:entry + 4] = be32(offset // 4)
        data[entry + 4:entry + 8] = be32(part_type)
        span = offset + 0x2A4
        tmd_size, tmd_off = 0x200, 0x300
        cert_size, cert_off = 0x100, 0x600
        h3_off = 0x800
        data_off, data_size = 0x1000, 0x10000
        values = [
            tmd_size,
            tmd_off // 4,
            cert_size,
            cert_off // 4,
            h3_off // 4,
            data_off // 4,
            data_size // 4,
        ]
        for position, value in enumerate(values):
            data[span + position * 4:span + position * 4 + 4] = be32(value)
        title = b"Test Game".decode().encode("utf-16-be")
        base = offset + tmd_off + 0x1E0
        data[base:base + len(title)] = title
        for chunk in range(0x1000, 0x11000, 0x1000):
            pattern = bytes([(offset + chunk) & 0xFF]) * 64
            data[offset + chunk:offset + chunk + len(pattern)] = pattern
    with open(path, "wb") as handle:
        handle.write(data)
    return bytes(data)


def make_gc_iso(path: str, size: int = 0x200000) -> bytes:
    data = bytearray(size)
    data[0:6] = b"GTEE01"
    data[0x1C:0x20] = be32(GC_MAGIC)
    data[0x20:0x30] = b"GC Test Game".ljust(0x10, b"\x00")
    for chunk in range(0x20000, size, 0x20000):
        data[chunk:chunk + 32] = bytes([chunk & 0xFF]) * 32
    with open(path, "wb") as handle:
        handle.write(data)
    return bytes(data)


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wbfs-test-")
        self.tmp = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def path(self, name: str) -> str:
        return os.path.join(self.tmp, name)


class GeometryTests(unittest.TestCase):
    def test_auto_geometry_is_consistent(self) -> None:
        geometry = layout.choose_geometry(500 * 1024 ** 3)
        self.assertEqual(geometry.wbfs_sec_sz, 8 * 1024 ** 2)
        self.assertGreaterEqual(geometry.max_disc, 500)
        self.assertEqual(
            geometry.freeblks_lba * geometry.hd_sec_sz + geometry.bitmap_bytes_aligned,
            geometry.wbfs_sec_sz,
        )
        self.assertLessEqual(geometry.n_wbfs_sec, 0xFFFF)

    def test_header_roundtrip(self) -> None:
        geometry = layout.choose_geometry(32 * 1024 ** 2)
        header = layout.build_header(geometry)
        parsed = layout.parse_geometry(header)
        self.assertEqual(parsed.wbfs_sec_sz, geometry.wbfs_sec_sz)
        self.assertEqual(parsed.max_disc, geometry.max_disc)

    def test_bad_header_rejected(self) -> None:
        with self.assertRaises(Exception):
            layout.parse_geometry(b"NOPE" + b"\x00" * 508)

    def test_bitmap_bits(self) -> None:
        geometry = layout.choose_geometry(32 * 1024 ** 2)
        bitmap = layout.empty_bitmap(geometry)
        self.assertTrue(layout.bitmap_get(bitmap, 1))
        layout.bitmap_set(bitmap, 1, False)
        self.assertFalse(layout.bitmap_get(bitmap, 1))
        layout.bitmap_set(bitmap, 1, True)
        self.assertTrue(layout.bitmap_get(bitmap, 1))


class UtilTests(unittest.TestCase):
    def test_sizes(self) -> None:
        self.assertEqual(parse_size("32G"), 32 * 1024 ** 3)
        self.assertEqual(parse_size("4.5 GiB"), int(4.5 * 1024 ** 3))
        self.assertEqual(parse_size("1048576"), 1048576)
        self.assertEqual(human_size(1024 ** 3), "1.0 GiB")

    def test_filenames(self) -> None:
        self.assertEqual(sanitize_filename('A/B:C*D?'), "A_B_C_D_")
        self.assertEqual(
            game_filename("Wii Sports", "RSPE01", "iso"), "Wii Sports [RSPE01].iso"
        )

    def test_titles_and_art(self) -> None:
        database = TitleDB(self.tmp if False else tempfile.mkdtemp())
        database.import_text("RSPE01 = Wii Sports\n# comment\nSMNE01 = New Super Mario Bros. Wii\n")
        self.assertEqual(database.get("rspe01"), "Wii Sports")
        self.assertEqual(database.get("SMNE01"), "New Super Mario Bros. Wii")
        urls = cover_urls("SMNE01", "E")
        self.assertTrue(urls[0].endswith("US/SMNE01.png"))
        self.assertTrue(any("/wii/cover3D/" in url for url in urls))


class WbfsVolumeTests(TempDirTestCase):
    def make_volume(self, size: int = 64 * 1024 ** 2) -> WbfsVolume:
        return WbfsVolume.create(self.path("test.wbfs"), size)

    def test_create_and_summary(self) -> None:
        volume = self.make_volume()
        self.assertEqual(volume.game_count, 0)
        self.assertEqual(volume.games(), [])
        self.assertGreater(volume.free_blocks, 0)
        summary = volume.summary()
        self.assertEqual(summary["max_games"], volume.geometry.max_disc)
        volume.close()
        reopened = WbfsVolume.open(self.path("test.wbfs"))
        self.assertEqual(reopened.geometry.wbfs_sec_sz, volume.geometry.wbfs_sec_sz)
        reopened.close()

    def test_wii_import_export_full_partitions(self) -> None:
        iso = self.path("game.iso")
        original = make_wii_iso(iso)
        volume = self.make_volume()
        with IsoSource(iso) as source:
            self.assertEqual(source.info.disc_id, "RTPP01")
            self.assertTrue(source.info.is_wii)
            self.assertEqual(len(source.info.partitions), 2)
            entry = volume.add_disc(source, mode="all")
        self.assertTrue(entry.valid)
        self.assertEqual(entry.disc_id, "RTPP01")
        self.assertGreater(entry.blocks, 0)
        self.assertEqual(len(volume.games()), 1)
        output = self.path("out.iso")
        volume.extract_iso(entry, output, size=len(original))
        with open(output, "rb") as handle:
            self.assertEqual(handle.read(), original)
        volume.close()

    def test_remove_update_partition(self) -> None:
        iso = self.path("game.iso")
        original = make_wii_iso(iso)
        volume = self.make_volume()
        with IsoSource(iso) as source:
            entry = volume.add_disc(source, mode="remove_update")
        output = self.path("out.iso")
        volume.extract_iso(entry, output, size=len(original))
        with open(output, "rb") as handle:
            data = handle.read()
        self.assertEqual(data[0x40000:0x40004], be32(1))
        self.assertEqual(data[0x40024:0x40028], be32(0))
        self.assertFalse(any(data[0x50000:0x61000]))
        self.assertEqual(data[0x500000:0x501000], original[0x500000:0x501000])
        self.assertEqual(data[0x20:0x30].rstrip(b"\x00"), b"Test Game")
        volume.close()

    def test_duplicate_and_replace(self) -> None:
        iso = self.path("game.iso")
        make_wii_iso(iso)
        volume = self.make_volume()
        with IsoSource(iso) as source:
            first = volume.add_disc(source, mode="all")
        with IsoSource(iso) as source:
            with self.assertRaises(DuplicateDiscError):
                volume.add_disc(source, mode="all")
        with IsoSource(iso) as source:
            second = volume.add_disc(source, mode="all", replace=True)
        self.assertEqual(len(volume.games()), 1)
        self.assertGreaterEqual(second.blocks, 1)
        self.assertEqual(first.disc_id, second.disc_id)
        volume.close()

    def test_remove_frees_space(self) -> None:
        iso = self.path("game.iso")
        make_wii_iso(iso)
        volume = self.make_volume()
        before = volume.free_blocks
        with IsoSource(iso) as source:
            entry = volume.add_disc(source, mode="all")
        self.assertEqual(volume.free_blocks, before - entry.blocks)
        volume.remove_game(entry.slot)
        self.assertEqual(volume.free_blocks, before)
        self.assertEqual(volume.game_count, 0)
        volume.close()

    def test_verify_and_repair_bitmap(self) -> None:
        iso = self.path("game.iso")
        make_wii_iso(iso)
        volume = self.make_volume()
        with IsoSource(iso) as source:
            entry = volume.add_disc(source, mode="all")
        report = volume.verify()
        self.assertTrue(report.ok, report.errors)
        used = next(block for block in entry.wlba if block)
        stored = volume.stored_bitmap()
        layout.bitmap_set(stored, used, True)
        volume.io.pwrite(
            volume.geometry.freeblks_lba * volume.geometry.hd_sec_sz, bytes(stored)
        )
        volume.io.flush()
        report = volume.verify()
        self.assertFalse(report.ok)
        self.assertIn(used, report.bitmap_mismatch)
        changes = volume.repair()
        self.assertGreaterEqual(changes, 1)
        self.assertTrue(volume.verify().ok)
        volume.close()

    def test_single_game_wbfs_copy(self) -> None:
        iso = self.path("game.iso")
        make_wii_iso(iso)
        volume = self.make_volume()
        with IsoSource(iso) as source:
            entry = volume.add_disc(source, mode="remove_update")
        target = self.path("single.wbfs")
        volume.extract_wbfs(entry, target)
        copy = WbfsVolume.open(target)
        games = copy.games()
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0].disc_id, "RTPP01")
        output = self.path("copy.iso")
        copy.extract_iso(games[0], output, size=0x800000)
        with open(output, "rb") as handle:
            data = handle.read()
        self.assertEqual(data[0x500000:0x501000], make_wii_iso(iso)[0x500000:0x501000])
        copy.close()
        volume.close()

    def test_clone_game_preserves_blocks_and_size(self) -> None:
        iso = self.path("game.iso")
        make_wii_iso(iso)
        volume = self.make_volume()
        with IsoSource(iso) as source:
            entry = volume.add_disc(source, mode="remove_update")
        output = self.path("cloned.wbfs")
        clone_game_to_file(volume, entry, output, title="Cloned")
        self.assertEqual(
            os.path.getsize(output),
            (entry.blocks + 1) * volume.geometry.wbfs_sec_sz,
        )
        clone = WbfsVolume.open(output)
        try:
            games = clone.games()
            self.assertEqual(games[0].disc_id, entry.disc_id)
            self.assertEqual(games[0].blocks, entry.blocks)
            self.assertEqual(games[0].title, "Cloned")
        finally:
            clone.close()
            volume.close()

    def test_sparse_copy_between_wbfs_volumes(self) -> None:
        iso = self.path("game.iso")
        make_wii_iso(iso)
        source = self.make_volume()
        with IsoSource(iso) as iso_source:
            entry = source.add_disc(iso_source, mode="remove_update")
        target = WbfsVolume.create(self.path("target.wbfs"), 64 * 1024 ** 2)
        try:
            target.add_disc(source.source(entry), mode="remove_update")
            copied = target.games()[0]
            self.assertEqual(copied.blocks, entry.blocks)
        finally:
            target.close()
            source.close()

    def test_gamecube_roundtrip(self) -> None:
        iso = self.path("gc.iso")
        original = make_gc_iso(iso)
        volume = self.make_volume()
        with IsoSource(iso) as source:
            self.assertTrue(source.info.is_gc)
            entry = volume.add_disc(source, mode="all")
        self.assertEqual(entry.disc_id, "GTEE01")
        self.assertEqual(entry.kind, "GameCube")
        output = self.path("gc-out.iso")
        volume.extract_iso(entry, output)
        with open(output, "rb") as handle:
            self.assertEqual(handle.read(), original)
        volume.close()

    def test_scrub_mode_only_game(self) -> None:
        iso = self.path("game.iso")
        make_wii_iso(iso)
        volume = self.make_volume()
        with IsoSource(iso) as source:
            usage = build_usage(source, source.info, "only_game")
        self.assertFalse(usage.used(0x50000 // 0x8000))
        self.assertTrue(usage.used(0x500000 // 0x8000))
        table = usage.patches[0x40000]
        self.assertEqual(struct.unpack(">I", table[:4])[0], 1)
        volume.close()


class SplitFileTests(TempDirTestCase):
    def test_split_write_read(self) -> None:
        original = diskio_module.SPLIT_LIMIT
        diskio_module.SPLIT_LIMIT = 0x1000
        try:
            path = self.path("split.wbfs")
            io = SplitFileIO(path, writable=True, virtual_size=0x3000)
            payload = bytes(range(256)) * 4
            io.pwrite(0xFF0, payload)
            io.flush()
            self.assertEqual(io.size, 0x3000)
            self.assertEqual(io.pread(0xFF0, len(payload)), payload)
            self.assertTrue(os.path.exists(self.path("split.wbf1")))
            self.assertTrue(os.path.exists(self.path("split.wbf2")))
            io.close()
            reopened = SplitFileIO(path)
            self.assertEqual(reopened.pread(0xFF0, len(payload)), payload)
            reopened.close()
        finally:
            diskio_module.SPLIT_LIMIT = original


class SplitVolumeTests(TempDirTestCase):
    def test_split_wbfs_volume_roundtrip(self) -> None:
        original_limit = diskio_module.SPLIT_LIMIT
        diskio_module.SPLIT_LIMIT = 2 * 1024 * 1024
        try:
            path = self.path("split.wbfs")
            iso = self.path("gc.iso")
            original = make_gc_iso(iso, size=0x200000)
            volume = WbfsVolume.create(path, 8 * 1024 * 1024, split=True)
            with IsoSource(iso) as source:
                entry = volume.add_disc(source, mode="all")
            volume.close()
            self.assertTrue(os.path.exists(self.path("split.wbf1")))
            reopened = WbfsVolume.open(path)
            games = reopened.games()
            self.assertEqual([game.disc_id for game in games], ["GTEE01"])
            output = self.path("out.iso")
            reopened.extract_iso(games[0], output)
            reopened.close()
            with open(output, "rb") as handle:
                self.assertEqual(handle.read(), original)
        finally:
            diskio_module.SPLIT_LIMIT = original_limit


class SafetyTests(TempDirTestCase):
    def test_replace_refuses_source_from_same_file(self) -> None:
        iso = self.path("game.iso")
        make_wii_iso(iso)
        path = self.path("volume.wbfs")
        volume = WbfsVolume.create(path, 64 * 1024 ** 2)
        with IsoSource(iso) as source:
            entry = volume.add_disc(source, mode="all")
        other = WbfsVolume.open(path)
        try:
            source = other.source(entry)
            with self.assertRaises(Exception) as context:
                volume.add_disc(source, mode="all", replace=True)
            self.assertIn("samego pliku", str(context.exception))
        finally:
            other.close()
            volume.close()


class HeaderTests(unittest.TestCase):
    def test_parse_header_regions(self) -> None:
        header = bytearray(0x100)
        header[0:6] = b"SMNE01"
        header[3] = ord("E")
        header[0x18:0x1C] = be32(WII_MAGIC)
        header[0x20:0x30] = b"Mario"
        info = parse_header(bytes(header), 1024)
        self.assertEqual(info.disc_id, "SMNE01")
        self.assertEqual(info.title, "Mario")
        self.assertTrue(info.is_wii)
        self.assertFalse(info.is_gc)


if __name__ == "__main__":
    unittest.main()
