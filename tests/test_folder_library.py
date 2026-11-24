"""Tests for the folder library exposed as a drive."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gor_wii_manager import cli
from gor_wii_manager.core import diskio
from gor_wii_manager.core.disc import IsoSource
from gor_wii_manager.core.folder_library import (
    FolderLibrary,
    create_library,
)
from gor_wii_manager.core.library import (
    LibraryExporter,
    LibraryItem,
    LibraryOptions,
)
from gor_wii_manager.core.wbfs import WbfsVolume, single_game_wbfs_size

from test_core import make_gc_iso, make_wii_iso


class FolderLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="folder-library-")
        self.root = self._tmp.name
        self.source = os.path.join(self.root, "source.wbfs")
        self.target = os.path.join(self.root, "usb")
        self.wii_iso = os.path.join(self.root, "wii.iso")
        self.gc_iso = os.path.join(self.root, "gc.iso")
        make_wii_iso(self.wii_iso)
        make_gc_iso(self.gc_iso)
        volume = WbfsVolume.create(self.source, 256 * 1024 ** 2)
        with IsoSource(self.wii_iso) as source:
            self.wii_entry = volume.add_disc(source, mode="remove_update")
        with IsoSource(self.gc_iso) as source:
            self.gc_entry = volume.add_disc(source, mode="all")
        items = [
            LibraryItem.from_wbfs(volume, self.wii_entry, "Test Game"),
            LibraryItem.from_wbfs(volume, self.gc_entry, "GC Test Game"),
        ]
        LibraryExporter(
            LibraryOptions(base_path=self.target, profile="auto", overwrite="replace")
        ).export(items)
        volume.close()
        self.library = FolderLibrary(self.target)

    def tearDown(self) -> None:
        self.library.close()
        self._tmp.cleanup()

    def test_scan(self) -> None:
        games = self.library.games()
        self.assertEqual(len(games), 2)
        by_id = {game.disc_id: game for game in games}
        self.assertTrue(by_id["RTPP01"].is_wii)
        self.assertTrue(by_id["GTEE01"].is_gc)
        self.assertGreater(self.library.free_bytes, 0)
        self.assertGreater(self.library.capacity_bytes, 0)
        self.assertEqual(self.library.game_count, 2)

    def test_add_iso_to_library(self) -> None:
        iso = os.path.join(self.root, "new.iso")
        data = bytearray(make_wii_iso(iso, size=0x1000000))
        data[0:6] = b"RTPP02"
        with open(iso, "wb") as handle:
            handle.write(data)
        source = IsoSource(iso)
        game = self.library.add_disc(source, mode="remove_update")
        self.assertIsNotNone(game)
        self.assertEqual(game.disc_id, "RTPP02")
        self.assertTrue(os.path.isfile(game.path))
        self.assertIn("RTPP02", [g.disc_id for g in self.library.games()])

    def test_copy_library_to_wbfs(self) -> None:
        game = self.library.find_game("RTPP01")
        target_path = os.path.join(self.root, "copy.wbfs")
        target = WbfsVolume.create(target_path, 64 * 1024 ** 2)
        try:
            source = self.library.source(game)
            try:
                target.add_disc(source, mode="remove_update")
            finally:
                source.close()
            self.assertEqual([g.disc_id for g in target.games()], ["RTPP01"])
        finally:
            target.close()

    def test_copy_library_to_library(self) -> None:
        other_path = os.path.join(self.root, "usb2")
        other = create_library(other_path)
        game = self.library.find_game("RTPP01")
        source = self.library.source(game)
        try:
            created = other.add_disc(source, mode="remove_update")
        finally:
            source.close()
        self.assertIsNotNone(created)
        self.assertEqual(created.disc_id, "RTPP01")
        self.assertTrue(os.path.isfile(created.path))

        gc_game = self.library.find_game("GTEE01")
        source = self.library.source(gc_game)
        try:
            created_gc = other.add_disc(source, mode="all")
        finally:
            source.close()
        self.assertIsNotNone(created_gc)
        self.assertEqual(created_gc.disc_id, "GTEE01")
        self.assertLess(os.path.getsize(created_gc.path), 4 * 1024 * 1024)

    def test_remove_split_game(self) -> None:
        limit = diskio.SPLIT_LIMIT
        diskio.SPLIT_LIMIT = 2 * 1024 * 1024
        try:
            target = os.path.join(self.root, "split-usb")
            volume = WbfsVolume.create(os.path.join(self.root, "one.wbfs"), 64 * 1024 ** 2)
            with IsoSource(self.wii_iso) as source:
                entry = volume.add_disc(source, mode="remove_update")
            volume.close()
            volume = WbfsVolume.open(os.path.join(self.root, "one.wbfs"), read_only=True)
            try:
                item = LibraryItem.from_wbfs(volume, volume.games()[0], "Split")
                LibraryExporter(
                    LibraryOptions(
                        base_path=target,
                        profile="auto",
                        overwrite="replace",
                        force_split=True,
                    )
                ).export([item])
            finally:
                volume.close()
            library = FolderLibrary(target)
            game = library.find_game("RTPP01")
            self.assertIsNotNone(game)
            self.assertTrue(any(part.endswith(".wbf1") for part in game.parts))
            base = game.path
            parts = list(game.parts)
            library.remove_game(game)
            self.assertFalse(os.path.exists(base))
            for part in parts:
                self.assertFalse(os.path.exists(part))
        finally:
            diskio.SPLIT_LIMIT = limit

    def test_rename_wbfs_game(self) -> None:
        game = self.library.find_game("RTPP01")
        self.library.rename_game(game, "Nowa Nazwa")
        self.assertEqual(game.title, "Nowa Nazwa")
        volume = WbfsVolume.open(game.path)
        try:
            self.assertEqual(volume.games()[0].title, "Nowa Nazwa")
        finally:
            volume.close()

    def test_wbfs_source_estimate_uses_stored_blocks(self) -> None:
        volume = WbfsVolume.open(self.source, read_only=True)
        try:
            entry = volume.games()[0]
            item = LibraryItem.from_object_source(volume.source(entry))
            estimate = item.estimated_size("auto", "remove_update")
            expected = single_game_wbfs_size(volume, entry)
            self.assertEqual(estimate, expected)
            self.assertGreaterEqual(
                expected, entry.stored_size(volume.geometry)
            )
            self.assertLess(estimate, 1024 * 1024 ** 2)
        finally:
            volume.close()

    def test_duplicate_and_replace(self) -> None:
        source = IsoSource(self.wii_iso)
        with self.assertRaises(Exception):
            self.library.add_disc(source)
        source.close()

    def test_cli_list_and_export_from_library(self) -> None:
        self.assertEqual(cli.main(["list", self.target]), 0)
        other = os.path.join(self.root, "usb-cli")
        self.assertEqual(
            cli.main(["export", other, self.target, "--all"]), 0
        )
        other_library = FolderLibrary(other)
        try:
            self.assertEqual(other_library.game_count, 2)
        finally:
            other_library.close()

    def test_cli_import_to_library(self) -> None:
        iso = os.path.join(self.root, "cli-new.iso")
        data = bytearray(make_wii_iso(iso, size=0x1000000))
        data[0:6] = b"RTPP03"
        with open(iso, "wb") as handle:
            handle.write(data)
        self.assertEqual(cli.main(["import", self.target, iso]), 0)
        self.assertIsNotNone(self.library.find_game("RTPP03"))

    def test_extract_from_library(self) -> None:
        game = self.library.find_game("GTEE01")
        output = os.path.join(self.root, "out.iso")
        self.library.extract_iso(game, output)
        with open(output, "rb") as handle:
            data = handle.read()
        with open(self.gc_iso, "rb") as handle:
            self.assertEqual(data, handle.read())


if __name__ == "__main__":
    unittest.main()
