"""End-to-end UI smoke test (run manually: ``PYTHONPATH=src python3 tests/ui_smoke.py``).

It creates a temporary WBFS volume with two games and exercises the real
window, queue, dialogs and file operations. Requires a graphical session.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

from test_core import make_gc_iso, make_wii_iso  # noqa: E402
from rvz_writer import build_rvz  # noqa: E402
from gor_wii_manager.core.rvz import codecs as rvz_codecs  # noqa: E402
from gor_wii_manager.core.disc import IsoSource  # noqa: E402
from gor_wii_manager.core.library import (  # noqa: E402
    LibraryItem,
    LibraryOptions,
    LibraryPlanner,
)
from gor_wii_manager.core.wbfs import WbfsVolume  # noqa: E402
from gor_wii_manager.ui import dialogs  # noqa: E402
from gor_wii_manager.ui.app import GorWiiApplication, build_parser  # noqa: E402

STATE = {}


def build_demo(directory: str) -> str:
    volume_path = os.path.join(directory, "games.wbfs")
    volume = WbfsVolume.create(volume_path, 256 * 1024 ** 2)
    try:
        for disc_id, title in (("SMNE01", "New Super Mario Bros. Wii"), ("RSPE01", "Wii Sports")):
            iso_path = os.path.join(directory, f"{disc_id}.iso")
            data = bytearray(make_wii_iso(iso_path, size=0x1000000))
            data[0:6] = disc_id.encode()
            encoded = title.encode()[:0x40]
            data[0x20:0x60] = encoded + b"\x00" * (0x40 - len(encoded))
            with open(iso_path, "wb") as handle:
                handle.write(data)
            with IsoSource(iso_path) as source:
                volume.add_disc(source, mode="remove_update")
        gc_path = os.path.join(directory, "gc.iso")
        make_gc_iso(gc_path, size=0x200000)
        with IsoSource(gc_path) as source:
            volume.add_disc(source, mode="all")
    finally:
        volume.close()
    return volume_path


class SmokeApp(GorWiiApplication):
    def do_activate(self) -> None:
        super().do_activate()
        if getattr(self, "_scheduled", False):
            return
        self._scheduled = True
        GLib.timeout_add(1200, self.step_wait_loaded)

    # -- helpers ---------------------------------------------------------
    def step_wait_loaded(self):
        window = self.window
        if window.current is None or window.current.loading:
            return True
        items = window._selected_items()
        if not items:
            window.selection_model.select_all()
            items = window._selected_items()
        print("selected:", [item.disc_id for item in items])
        assert len(items) == 3, "expected three games"
        STATE["items"] = items
        STATE["source"] = window.current.path
        target = os.path.join(STATE["tmp"], "backup.wbfs")
        STATE["target"] = target
        window._queue_format_file(target, 128 * 1024 ** 2, None)
        GLib.timeout_add(400, self.step_after_format)
        return False

    def step_after_format(self):
        window = self.window
        if window.queue.active_jobs():
            return True
        job = window.queue.jobs[-1]
        assert job.status == "done", job.error
        assert os.path.exists(STATE["target"])
        print("format: ok")
        window.open_path(STATE["target"])
        window._select_volume(STATE["source"])
        GLib.timeout_add(500, self.step_copy)
        return False

    def step_copy(self):
        window = self.window
        target = window.volumes.get(STATE["target"])
        assert target is not None
        item = next(item for item in STATE["items"] if item.disc_id == "RSPE01")
        source = window.volumes[STATE["source"]]
        window._queue_copy([item], source, target)
        GLib.timeout_add(400, self.step_copy_done)
        return False

    def step_copy_done(self):
        window = self.window
        if window.queue.active_jobs():
            return True
        job = window.queue.jobs[-1]
        assert job.status == "done", job.error
        target = window.volumes[STATE["target"]]
        games = target.volume.games()
        print("copy: target has", [game.disc_id for game in games])
        assert any(game.disc_id == "RSPE01" for game in games)
        window._select_volume(STATE["source"])
        GLib.timeout_add(400, self.step_extract)
        return False

    def step_extract(self):
        window = self.window
        assert window.current.loaded
        item = next(item for item in window.current.items if item.disc_id == "SMNE01")
        STATE["iso"] = os.path.join(STATE["tmp"], "out.iso")
        window._queue_extract_iso(window.current, [item], [STATE["iso"]])
        GLib.timeout_add(400, self.step_extract_done)
        return False

    def step_extract_done(self):
        window = self.window
        if window.queue.active_jobs():
            return True
        job = window.queue.jobs[-1]
        assert job.status == "done", job.error
        size = os.path.getsize(STATE["iso"])
        print("extract:", size, "bytes")
        assert size >= 143432 * 0x8000
        with open(STATE["iso"], "rb") as handle:
            header = handle.read(0x20)
        assert header[0:6] == b"SMNE01"
        self.step_dialogs()

    def step_dialogs(self):
        window = self.window
        format_dialog = dialogs.FormatDialog(window, lambda *args: None)
        preferences = dialogs.PreferencesWindow(
            window, window.settings, window.titles, lambda: None
        )
        properties = dialogs.PropertiesWindow(
            window, window.current, window.current.items[0], window.art
        )
        shortcuts = dialogs.ShortcutsWindow(window)
        library_dialog = dialogs.LibraryExportDialog(window, 2, lambda *args: None)
        for dialog in (
            format_dialog,
            preferences,
            properties,
            shortcuts,
            library_dialog,
        ):
            dialog.present()
        GLib.timeout_add(
            400,
            lambda: (
                format_dialog.close(),
                preferences.close(),
                properties.close(),
                shortcuts.close(),
                library_dialog.close(),
                False,
            )[-1],
        )
        print("dialogs: ok")
        GLib.timeout_add(500, self.step_delete)

    def step_delete(self):
        window = self.window
        items = window.current.items
        victim = next(item for item in items if item.disc_id == "RSPE01")
        window._queue_delete(window.current, [victim])
        GLib.timeout_add(400, self.step_delete_done)
        return False

    def step_delete_done(self):
        window = self.window
        if window.queue.active_jobs():
            return True
        job = window.queue.jobs[-1]
        assert job.status == "done", job.error
        source = window.volumes[STATE["source"]]
        remaining = [game.disc_id for game in source.volume.games()]
        print("delete: remaining", remaining)
        assert "RSPE01" not in remaining
        report = source.volume.verify()
        assert report.ok, report.errors
        source.volume.repair()
        print("verify/repair: ok")

        new_iso = os.path.join(STATE["tmp"], "new-game.iso")
        make_gc_iso(new_iso, size=0x200000)
        with open(new_iso, "r+b") as handle:
            handle.seek(0)
            handle.write(b"GTEE02")
        window._import_paths(window.current, [new_iso])
        GLib.timeout_add(400, self.step_import_done)
        return False

    def step_import_done(self):
        window = self.window
        if window.queue.active_jobs():
            return True
        job = window.queue.jobs[-1]
        assert job.status == "done", job.error
        source = window.volumes[STATE["source"]]
        ids = [game.disc_id for game in source.volume.games()]
        print("import: games", ids)
        assert "GTEE02" in ids

        rvz_iso = os.path.join(STATE["tmp"], "rvz-source.iso")
        data = bytearray(make_gc_iso(rvz_iso, size=0x200000))
        data[0:6] = b"GTEE03"
        with open(rvz_iso, "wb") as handle:
            handle.write(data)
        rvz_path = os.path.join(STATE["tmp"], "rvz-source.rvz")
        build_rvz(rvz_path, bytes(data), compression=rvz_codecs.COMPRESSION_ZSTD)
        STATE["rvz"] = rvz_path
        window._import_paths(window.current, [rvz_path])
        GLib.timeout_add(400, self.step_rvz_done)
        return False

    def step_rvz_done(self):
        window = self.window
        if window.queue.active_jobs():
            return True
        job = window.queue.jobs[-1]
        assert job.status == "done", job.error
        source = window.volumes[STATE["source"]]
        ids = [game.disc_id for game in source.volume.games()]
        print("rvz import: games", ids)
        assert "GTEE03" in ids

        output = os.path.join(STATE["tmp"], "converted.wbfs")
        STATE["converted"] = output
        window._queue_convert([STATE["rvz"]], [output], "wbfs")
        GLib.timeout_add(400, self.step_convert_done)
        return False

    def step_convert_done(self):
        window = self.window
        if window.queue.active_jobs():
            return True
        job = window.queue.jobs[-1]
        assert job.status == "done", job.error
        assert os.path.exists(STATE["converted"])
        volume = WbfsVolume.open(STATE["converted"])
        try:
            ids = [game.disc_id for game in volume.games()]
        finally:
            volume.close()
        print("rvz convert: games", ids)
        assert "GTEE03" in ids

        target = os.path.join(STATE["tmp"], "usb-library")
        STATE["library"] = target
        library_items = [
            LibraryItem.from_wbfs(window.current.volume, item.entry, item.title)
            for item in window.current.items
            if getattr(item.entry, "valid", False)
        ]
        options = LibraryOptions(
            base_path=target, profile="auto", overwrite="replace"
        )
        plan = LibraryPlanner(options).plan(library_items)
        window._queue_library_plan(plan, options, window.current)
        GLib.timeout_add(500, self.step_library_done)
        return False

    def step_library_done(self):
        window = self.window
        if window.queue.active_jobs():
            return True
        job = window.queue.jobs[-1]
        assert job.status == "done", job.error
        target = STATE["library"]
        found_wbfs = []
        found_gc = []
        for root, _dirs, files in os.walk(target):
            for name in files:
                if name.endswith(".wbfs"):
                    found_wbfs.append(os.path.join(root, name))
                elif name == "game.iso":
                    found_gc.append(os.path.join(root, name))
        print("library export: wbfs", len(found_wbfs), "gc", len(found_gc))
        assert found_wbfs and found_gc
        window.open_library_path(target)
        GLib.timeout_add(500, self.step_library_open)
        return False

    def step_library_open(self):
        window = self.window
        if window.current is None or window.current.loading:
            return True
        assert getattr(window.current.volume, "is_folder_library", False)
        count = len(window.current.items)
        systems = sorted({item.entry.kind for item in window.current.items})
        print("library open: games", count, "systems", systems)
        assert count >= 4
        assert "Wii" in systems and "GameCube" in systems

        new_iso = os.path.join(STATE["tmp"], "library-add.iso")
        data = bytearray(make_gc_iso(new_iso, size=0x200000))
        data[0:6] = b"GTEE04"
        with open(new_iso, "wb") as handle:
            handle.write(data)
        window._import_paths(window.current, [new_iso])
        GLib.timeout_add(500, self.step_library_add)
        return False

    def step_library_add(self):
        window = self.window
        if window.queue.active_jobs():
            return True
        job = window.queue.jobs[-1]
        assert job.status == "done", job.error
        ids = [item.disc_id for item in window.current.items]
        print("library add: games", ids)
        assert "GTEE04" in ids

        target_state = window.volumes.get(STATE["target"])
        assert target_state is not None
        item = next(
            entry for entry in window.current.items if entry.disc_id == "SMNE01"
        )
        window._queue_copy([item], window.current, target_state)
        GLib.timeout_add(500, self.step_library_copy)
        return False

    def step_library_copy(self):
        window = self.window
        if window.queue.active_jobs():
            return True
        job = window.queue.jobs[-1]
        assert job.status == "done", job.error
        target = window.volumes[STATE["target"]]
        ids = [game.disc_id for game in target.volume.games()]
        print("library copy to WBFS:", ids)
        assert "SMNE01" in ids
        print("ALL UI CHECKS PASSED")
        self.quit()
        return False


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wbfs-ui-")
    STATE["tmp"] = tmp
    try:
        volume_path = build_demo(tmp)
        options = build_parser().parse_args(["--open", volume_path])
        app = SmokeApp(options)
        code = app.run([sys.argv[0]])
        return code
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
