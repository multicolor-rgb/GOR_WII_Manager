"""Tests for exporting games into USB loader folder libraries."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gor_wii_manager import cli
from gor_wii_manager.core import diskio
from gor_wii_manager.core.disc import IsoSource
from gor_wii_manager.core.library import (
    LibraryError,
    LibraryExporter,
    LibraryItem,
    LibraryOptions,
    LibraryPlanner,
)
from gor_wii_manager.core.rvz import codecs as rvz_codecs
from gor_wii_manager.core.wbfs import WbfsVolume

from rvz_writer import build_rvz
from test_core import make_gc_iso, make_wii_iso


class LibraryTempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="library-test-")
        self.root = self._tmp.name
        self.source = os.path.join(self.root, "source.wbfs")
        self.target = os.path.join(self.root, "usb")
        os.makedirs(self.target, exist_ok=True)
        self.wii_iso = os.path.join(self.root, "wii.iso")
        self.gc_iso = os.path.join(self.root, "gc.iso")
        make_wii_iso(self.wii_iso)
        make_gc_iso(self.gc_iso)
        self.volume = WbfsVolume.create(self.source, 256 * 1024 ** 2)
        with IsoSource(self.wii_iso) as source:
            self.wii_entry = self.volume.add_disc(source, mode="remove_update")
        with IsoSource(self.gc_iso) as source:
            self.gc_entry = self.volume.add_disc(source, mode="all")

    def tearDown(self) -> None:
        try:
            self.volume.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def items(self):
        return [
            LibraryItem.from_wbfs(self.volume, self.wii_entry, "Test Game"),
            LibraryItem.from_wbfs(self.volume, self.gc_entry, "GC Test Game"),
        ]

    def options(self, **kwargs):
        values = {
            "base_path": self.target,
            "profile": "auto",
            "overwrite": "replace",
        }
        values.update(kwargs)
        return LibraryOptions(**values)


class LibraryExportTests(LibraryTempCase):
    def test_auto_export_layout(self) -> None:
        exporter = LibraryExporter(self.options())
        result = exporter.export(self.items())
        self.assertEqual(result.exported, 2)
        wii_path = os.path.join(
            self.target, "wbfs", "Test Game [RTPP01]", "RTPP01.wbfs"
        )
        gc_path = os.path.join(
            self.target, "games", "GC Test Game [GTEE01]", "game.iso"
        )
        self.assertTrue(os.path.isfile(wii_path))
        self.assertTrue(os.path.isfile(gc_path))
        volume = WbfsVolume.open(wii_path)
        try:
            games = volume.games()
            self.assertEqual(games[0].disc_id, "RTPP01")
            self.assertEqual(games[0].title, "Test Game")
        finally:
            volume.close()
        with open(gc_path, "rb") as handle:
            data = handle.read()
        with open(self.gc_iso, "rb") as handle:
            original = handle.read()
        self.assertEqual(data, original)

    def test_flat_profile(self) -> None:
        exporter = LibraryExporter(self.options(profile="flat"))
        exporter.export([self.items()[0]])
        self.assertTrue(
            os.path.isfile(os.path.join(self.target, "wbfs", "RTPP01.wbfs"))
        )

    def test_split_and_stale_parts_removed(self) -> None:
        original_limit = diskio.SPLIT_LIMIT
        diskio.SPLIT_LIMIT = 2 * 1024 * 1024
        try:
            options = self.options(force_split=True)
            exporter = LibraryExporter(options)
            exporter.export([self.items()[0]])
            base = os.path.join(
                self.target, "wbfs", "Test Game [RTPP01]", "RTPP01.wbfs"
            )
            self.assertTrue(os.path.isfile(base))
            self.assertTrue(os.path.isfile(base[:-1] + "1"))
            volume = WbfsVolume.open(base)
            try:
                self.assertEqual(volume.games()[0].disc_id, "RTPP01")
            finally:
                volume.close()

            exporter = LibraryExporter(self.options(force_split=False))
            exporter.export([self.items()[0]])
            self.assertTrue(os.path.isfile(base))
            self.assertFalse(os.path.exists(base[:-1] + "1"))
        finally:
            diskio.SPLIT_LIMIT = original_limit

    def test_skip_existing(self) -> None:
        LibraryExporter(self.options()).export([self.items()[0]])
        base = os.path.join(
            self.target, "wbfs", "Test Game [RTPP01]", "RTPP01.wbfs"
        )
        before = os.stat(base).st_mtime_ns
        result = LibraryExporter(self.options(overwrite="skip")).export(
            [self.items()[0]]
        )
        self.assertEqual(result.skipped, 1)
        self.assertEqual(os.stat(base).st_mtime_ns, before)

    def test_existing_folder_is_reused(self) -> None:
        LibraryExporter(self.options()).export([self.items()[0]])
        old_dir = os.path.join(self.target, "wbfs", "Test Game [RTPP01]")
        new_dir = os.path.join(self.target, "wbfs", "Stara nazwa [RTPP01]")
        os.rename(old_dir, new_dir)
        planner = LibraryPlanner(self.options(overwrite="replace"))
        plan = planner.plan([self.items()[0]])
        self.assertEqual(
            plan[0].destination,
            os.path.join(new_dir, "RTPP01.wbfs"),
        )

    def test_nintendont_export(self) -> None:
        exporter = LibraryExporter(self.options(profile="nintendont"))
        result = exporter.export([self.items()[1]])
        self.assertEqual(result.exported, 1)
        path = os.path.join(
            self.target, "games", "GC Test Game [GTEE01]", "game.iso"
        )
        self.assertTrue(os.path.isfile(path))
        original = os.path.getsize(self.gc_iso)
        self.assertEqual(os.path.getsize(path), original)
        with open(path, "rb") as handle:
            head = handle.read(0x20)
        self.assertEqual(head[0x1C:0x20], b"\xc2\x33\x9f\x3d")

    def test_profile_mismatch(self) -> None:
        exporter = LibraryExporter(self.options(profile="nintendont"))
        with self.assertRaises(LibraryError):
            exporter.export([self.items()[0]])

    def test_free_space_check(self) -> None:
        planner = LibraryPlanner(self.options())
        planner.target.free_bytes = 1
        with self.assertRaises(LibraryError):
            planner.plan(self.items())

    def test_rvz_file_export(self) -> None:
        with open(self.gc_iso, "rb") as handle:
            original = handle.read()
        rvz_path = os.path.join(self.root, "game.rvz")
        build_rvz(rvz_path, original, compression=rvz_codecs.COMPRESSION_ZSTD)
        item = LibraryItem.from_file(rvz_path)
        item.title = "RVZ GC"
        exporter = LibraryExporter(self.options())
        result = exporter.export([item])
        self.assertEqual(result.exported, 1)
        destination = os.path.join(
            self.target, "games", "RVZ GC [GTEE01]", "game.iso"
        )
        self.assertTrue(os.path.isfile(destination))
        with open(destination, "rb") as handle:
            self.assertEqual(handle.read(), original)

    def test_cli_export_to_new_target(self) -> None:
        target = os.path.join(self.root, "new-usb")
        code = cli.main(
            [
                "export",
                target,
                self.source,
                "--all",
                "--profile",
                "auto",
                "--lang",
                "pl",
            ]
        )
        self.assertEqual(code, 0)
        self.assertTrue(
            os.path.isfile(
                os.path.join(
                    target, "wbfs", "Test Game [RTPP01]", "RTPP01.wbfs"
                )
            )
        )
        gc_files = []
        for root, _dirs, files in os.walk(os.path.join(target, "games")):
            for name in files:
                if name == "game.iso":
                    gc_files.append(os.path.join(root, name))
        self.assertTrue(gc_files)

    def test_overwrite_replaces_content(self) -> None:
        LibraryExporter(self.options()).export([self.items()[0]])
        base = os.path.join(
            self.target, "wbfs", "Test Game [RTPP01]", "RTPP01.wbfs"
        )
        with open(base, "r+b") as handle:
            handle.seek(0x10000)
            handle.write(b"BROKEN")
        result = LibraryExporter(self.options(overwrite="replace")).export(
            [self.items()[0]]
        )
        self.assertEqual(result.overwritten, 1)
        volume = WbfsVolume.open(base)
        try:
            self.assertEqual(volume.games()[0].disc_id, "RTPP01")
        finally:
            volume.close()


if __name__ == "__main__":
    unittest.main()
