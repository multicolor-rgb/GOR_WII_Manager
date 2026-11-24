"""Export Wii/GameCube games into USB loader folder libraries.

Supported layouts
-----------------
* USB Loader GX / WiiFlow / CFG:
    <base>/wbfs/<Title> [<ID>]/<ID>.wbfs  (+ <ID>.wbf1 ... on FAT32)
* flat variant:
    <base>/wbfs/<ID>.wbfs
* Nintendont (GameCube):
    <base>/games/<Title> [<ID>]/game.iso
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import dataclass, field

from . import device as device_module
from .convert import estimate_wii_wbfs_size, source_to_iso, source_to_wbfs
from .disc import DiscInfo, open_disc_source
from .util import (
    CancelledError,
    WbfsError,
    fs_type,
    is_fat,
    human_size,
)
from . import wbfs_layout as layout
from .wbfs import (
    GameEntry,
    WbfsSource,
    WbfsVolume,
    clone_game_to_file,
    single_game_wbfs_size,
)

FAT_FILESYSTEMS = {"vfat", "msdos", "fat", "fat32"}
LIBRARY_FILESYSTEMS = {"vfat", "msdos", "fat", "fat32", "exfat", "ntfs", "ext2", "ext3", "ext4", "btrfs", "xfs"}
LOADER_PROFILES = {
    "auto": "Automatycznie (Wii → WBFS, GC → Nintendont ISO)",
    "usbloadergx": "USB Loader GX / WiiFlow / CFG (foldery wbfs/)",
    "flat": "Płasko: wbfs/<ID>.wbfs",
    "nintendont": "Nintendont (GameCube ISO w games/)",
}
OVERWRITE_MODES = {
    "replace": "Nadpisuj istniejące",
    "skip": "Pomiń istniejące",
    "ask": "Pytaj przy konflikcie",
}

_BAD_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_ID_SUFFIX = re.compile(r"\[([A-Za-z0-9]{6})\]$")


class LibraryError(WbfsError):
    pass


def sanitize_component(text: str, limit: int = 110) -> str:
    text = _BAD_COMPONENT.sub("_", text or "")
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "Gra"
    return text[:limit].rstrip(" .")


def folder_label(title: str, disc_id: str) -> str:
    return f"{sanitize_component(title)} [{disc_id.upper()}]"


def _id_from_filename(name: str) -> str | None:
    stem = name
    lowered = stem.lower()
    for extension in (".wbfs", ".iso"):
        if lowered.endswith(extension):
            stem = stem[: -len(extension)]
            break
    if len(stem) == 6 and stem.isalnum():
        return stem.upper()
    match = _ID_SUFFIX.search(stem)
    return match.group(1).upper() if match else None


def _id_from_folder(name: str) -> str | None:
    match = _ID_SUFFIX.search(name)
    return match.group(1).upper() if match else None


@dataclass
class LibraryTarget:
    base_path: str
    filesystem: str = ""
    free_bytes: int = 0
    label: str = ""
    device_path: str = ""
    removable: bool = False
    model: str = ""

    @property
    def display_name(self) -> str:
        name = self.label or self.model or os.path.basename(self.base_path.rstrip("/")) or self.base_path
        return f"{name} ({self.base_path}) · {self.filesystem or '?'} · wolne {human_size(self.free_bytes)}"

    @property
    def is_fat(self) -> bool:
        return self.filesystem in FAT_FILESYSTEMS

    @classmethod
    def from_path(cls, path: str) -> "LibraryTarget":
        path = os.path.abspath(os.path.expanduser(path))
        probe = path
        while not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        try:
            stats = os.statvfs(probe)
            free = stats.f_bavail * stats.f_frsize
        except OSError as error:
            raise LibraryError(f"nie można odczytać miejsca docelowego: {path}") from error
        return cls(
            base_path=path,
            filesystem=fs_type(probe),
            free_bytes=free,
        )


def detect_targets() -> list[LibraryTarget]:
    """Mounted filesystems that can hold a loader folder library."""
    system_disks = device_module.system_disk_names()
    result = []
    for node in device_module.flatten(device_module.list_block_devices()):
        if not node.is_partition or not node.mountpoint:
            continue
        if node.fstype not in LIBRARY_FILESYSTEMS:
            continue
        if node.parent in system_disks:
            continue
        mountpoint = node.mountpoint
        if mountpoint == "/" or mountpoint.startswith(("/boot", "/snap", "/var")):
            continue
        if not os.access(mountpoint, os.W_OK):
            continue
        try:
            free = os.statvfs(mountpoint).f_bavail * os.statvfs(mountpoint).f_frsize
        except OSError:
            free = 0
        result.append(
            LibraryTarget(
                base_path=mountpoint,
                filesystem=node.fstype,
                free_bytes=free,
                label=node.label,
                device_path=node.path,
                removable=node.removable or node.transport == "usb",
                model=node.model,
            )
        )
    result.sort(key=lambda item: (not item.removable, item.base_path))
    return result


@dataclass
class LibraryOptions:
    base_path: str
    profile: str = "auto"
    root_wii: str = "wbfs"
    root_gc: str = "games"
    overwrite: str = "replace"
    scrub: str = "remove_update"
    force_split: bool | None = None

    @property
    def target(self) -> LibraryTarget:
        target = LibraryTarget.from_path(self.base_path)
        return target


@dataclass
class LibraryItem:
    title: str
    disc_id: str
    is_gc: bool
    source_kind: str  # "wbfs" or "file"
    volume: WbfsVolume | None = None
    entry: GameEntry | None = None
    path: str | None = None
    info: DiscInfo | None = None
    object_source: object | None = None
    folder_library: object | None = None
    folder_game: object | None = None
    iso_size_bytes: int | None = None

    @classmethod
    def from_wbfs(cls, volume: WbfsVolume, entry: GameEntry, title: str | None = None) -> "LibraryItem":
        return cls(
            title=title or entry.title,
            disc_id=entry.disc_id.upper(),
            is_gc=entry.is_gc,
            source_kind="wbfs",
            volume=volume,
            entry=entry,
        )

    @classmethod
    def from_object_source(cls, source, title: str | None = None) -> "LibraryItem":
        info = source.info
        iso_size = getattr(source, "iso_size", None)
        if iso_size is None and hasattr(source, "entry") and hasattr(source, "volume"):
            try:
                iso_size = source.entry.iso_size(source.volume.geometry)
            except Exception:
                iso_size = None
        return cls(
            title=title or info.title,
            disc_id=info.disc_id.upper(),
            is_gc=info.is_gc,
            source_kind="object",
            info=info,
            object_source=source,
            iso_size_bytes=iso_size,
        )

    @classmethod
    def from_folder_game(cls, library, game, title: str | None = None) -> "LibraryItem":
        return cls(
            title=title or game.title,
            disc_id=game.disc_id.upper(),
            is_gc=game.is_gc,
            source_kind="folder",
            folder_library=library,
            folder_game=game,
            iso_size_bytes=game.iso_size_bytes,
        )

    @classmethod
    def from_file(cls, path: str, title: str | None = None) -> "LibraryItem":
        source = open_disc_source(path)
        try:
            info = source.info
        finally:
            source.close()
        return cls(
            title=title or info.title,
            disc_id=info.disc_id.upper(),
            is_gc=info.is_gc,
            source_kind="file",
            path=path,
            info=info,
        )

    def open_source(self):
        if self.source_kind == "wbfs":
            if self.volume is None or self.entry is None:
                raise LibraryError("brak źródła gry WBFS")
            return self.volume.source(self.entry)
        if self.source_kind == "object":
            if self.object_source is None:
                raise LibraryError("brak źródła gry")
            return self.object_source
        if self.source_kind == "folder":
            if self.folder_library is None or self.folder_game is None:
                raise LibraryError("brak gry w bibliotece")
            return self.folder_library.source(self.folder_game)
        if self.path is None:
            raise LibraryError("brak ścieżki pliku źródłowego")
        return open_disc_source(self.path)

    def wbfs_source_pair(self):
        """Return (volume, entry, volume_to_close) for block-preserving copies."""
        if self.source_kind == "wbfs" and self.volume is not None and self.entry is not None:
            return self.volume, self.entry, None
        if self.source_kind == "object" and self.object_source is not None:
            source = self.object_source
            if isinstance(source, WbfsSource):
                return source.volume, source.entry, None
            volume = getattr(source, "wbfs_volume", None)
            entry = getattr(source, "wbfs_entry", None)
            if volume is not None and entry is not None:
                return volume, entry, None
            return None
        if (
            self.source_kind == "folder"
            and self.folder_game is not None
            and getattr(self.folder_game, "container", "") == "wbfs"
        ):
            try:
                volume = WbfsVolume.open(self.folder_game.path, read_only=True)
                entries = volume.games()
                entry = next(
                    (item for item in entries if item.slot == self.folder_game.slot),
                    None,
                ) or (entries[0] if entries else None)
                if entry is None:
                    volume.close()
                    return None
                return volume, entry, volume
            except Exception:
                return None
        return None

    def iso_size(self) -> int:
        if self.iso_size_bytes is not None:
            return self.iso_size_bytes
        if self.source_kind == "wbfs" and self.volume is not None and self.entry is not None:
            return self.entry.iso_size(self.volume.geometry)
        if self.source_kind == "folder" and self.folder_game is not None:
            return self.folder_game.iso_size_bytes
        return self.info.size if self.info is not None else 0

    def estimated_size(self, profile: str, scrub: str) -> int:
        if self.is_gc:
            return self.iso_size()
        if self.source_kind == "folder" and self.folder_game is not None:
            game = self.folder_game
            if (
                getattr(game, "container", "") == "wbfs"
                and game.geometry is not None
                and game.blocks
            ):
                return layout.size_for_used_blocks(
                    game.blocks,
                    game.geometry.wbfs_sec_sz,
                    hd_sec_sz=game.geometry.hd_sec_sz,
                )
            return game.size_bytes
        if self.source_kind == "wbfs" and self.volume is not None and self.entry is not None:
            return single_game_wbfs_size(self.volume, self.entry)
        if self.source_kind == "object":
            source = self.object_source
            if isinstance(source, WbfsSource):
                return single_game_wbfs_size(source.volume, source.entry)
            volume = getattr(source, "wbfs_volume", None)
            entry = getattr(source, "wbfs_entry", None)
            if volume is not None and entry is not None:
                return single_game_wbfs_size(volume, entry)
            # The source is consumed later by the exporter; do not close it here.
            return estimate_wii_wbfs_size(self.open_source(), scrub)
        source = self.open_source()
        try:
            return estimate_wii_wbfs_size(source, scrub)
        finally:
            source.close()


def resolve_profile(profile: str, item: LibraryItem) -> str:
    if profile == "auto":
        return "nintendont" if item.is_gc else "usbloadergx"
    if profile == "usbloadergx" and item.is_gc:
        raise LibraryError(
            "profil USB Loader GX obsługuje tylko gry Wii; "
            "dla GameCube użyj profilu automatycznego lub Nintendont"
        )
    if profile == "flat" and item.is_gc:
        raise LibraryError(
            "profil płaski obsługuje tylko gry Wii; użyj Nintendont dla GameCube"
        )
    if profile == "nintendont" and not item.is_gc:
        raise LibraryError("profil Nintendont obsługuje tylko gry GameCube")
    return profile


@dataclass
class PlannedExport:
    item: LibraryItem
    profile: str
    destination: str
    split: bool
    estimate: int
    existing: list[str] = field(default_factory=list)
    action: str = "write"  # write / skip
    overwritten: bool = False

    @property
    def is_gc(self) -> bool:
        return self.profile == "nintendont"

    @property
    def destination_dir(self) -> str:
        return os.path.dirname(self.destination)

    @property
    def filename(self) -> str:
        return os.path.basename(self.destination)


class LibraryPlanner:
    def __init__(self, options: LibraryOptions):
        self.options = options
        self.target = options.target
        self.wii_root = os.path.join(self.target.base_path, options.root_wii)
        self.gc_root = os.path.join(self.target.base_path, options.root_gc)
        self.existing_wii = self._scan_wii()
        self.existing_gc = self._scan_gc()

    # -- scanning --------------------------------------------------------
    def _scan_wii(self) -> dict[str, str]:
        result: dict[str, str] = {}
        root = self.wii_root
        if not os.path.isdir(root):
            return result

        def consider(path: str, fallback_id: str | None = None) -> None:
            name = os.path.basename(path)
            disc_id = _id_from_filename(name) or fallback_id
            if not disc_id:
                return
            if disc_id not in result:
                result[disc_id] = path

        for entry in os.scandir(root):
            try:
                if entry.is_file() and entry.name.lower().endswith(".wbfs"):
                    consider(entry.path)
                elif entry.is_dir():
                    folder_id = _id_from_folder(entry.name)
                    for child in os.scandir(entry.path):
                        if child.is_file() and child.name.lower().endswith(".wbfs"):
                            consider(child.path, folder_id)
            except OSError:
                continue
        return result

    def _scan_gc(self) -> dict[str, str]:
        result: dict[str, str] = {}
        root = self.gc_root
        if not os.path.isdir(root):
            return result
        for entry in os.scandir(root):
            try:
                if not entry.is_dir():
                    continue
                iso_path = os.path.join(entry.path, "game.iso")
                if not os.path.isfile(iso_path):
                    continue
                disc_id = _id_from_folder(entry.name)
                if not disc_id:
                    try:
                        with open(iso_path, "rb") as handle:
                            head = handle.read(0x20)
                        if len(head) >= 6:
                            disc_id = head[:6].decode("latin-1").upper()
                    except OSError:
                        disc_id = None
                if disc_id and disc_id not in result:
                    result[disc_id] = iso_path
            except OSError:
                continue
        return result

    # -- planning --------------------------------------------------------
    def plan(self, items: list[LibraryItem]) -> list[PlannedExport]:
        if not items:
            raise LibraryError("nie wybrano żadnych gier")
        os.makedirs(self.target.base_path, exist_ok=True)
        planned: list[PlannedExport] = []
        for item in items:
            profile = resolve_profile(self.options.profile, item)
            if profile == "nintendont":
                destination = self._nintendont_path(item)
                existing = [destination] if os.path.exists(destination) else []
            else:
                destination = self._wbfs_path(item, profile)
                existing = self._existing_wbfs_files(destination)
            split = False
            if profile != "nintendont":
                split = (
                    self.options.force_split
                    if self.options.force_split is not None
                    else self.target.is_fat
                )
            if (
                existing
                and split
                and profile != "nintendont"
                and os.path.basename(destination).upper()
                != f"{item.disc_id.upper()}.WBFS"
            ):
                destination = os.path.join(
                    self.wii_root, f"{item.disc_id.upper()}.wbfs"
                )
                existing = self._existing_wbfs_files(destination)
            estimate = item.estimated_size(profile, self.options.scrub)
            action = "skip" if existing and self.options.overwrite == "skip" else "write"
            planned.append(
                PlannedExport(
                    item=item,
                    profile=profile,
                    destination=destination,
                    split=split,
                    estimate=estimate,
                    existing=existing,
                    action=action,
                )
            )
        required = 0
        for entry in planned:
            if entry.action != "write":
                continue
            required += entry.estimate
            # Old split/base files are kept until the new export is complete.
            for path in entry.existing:
                try:
                    required += os.path.getsize(path)
                except OSError:
                    pass
        if required > self.target.free_bytes - 16 * 1024 * 1024:
            raise LibraryError(
                "za mało miejsca na dysku docelowym: potrzeba około "
                f"{human_size(required)}, wolne {human_size(self.target.free_bytes)}"
            )
        return planned

    def _wbfs_path(self, item: LibraryItem, profile: str) -> str:
        existing = self.existing_wii.get(item.disc_id.upper())
        if existing:
            return existing
        filename = f"{item.disc_id.upper()}.wbfs"
        if profile == "flat":
            return os.path.join(self.wii_root, filename)
        folder = folder_label(item.title, item.disc_id)
        return os.path.join(self.wii_root, folder, filename)

    def _nintendont_path(self, item: LibraryItem) -> str:
        existing = self.existing_gc.get(item.disc_id.upper())
        if existing:
            return existing
        folder = folder_label(item.title, item.disc_id)
        return os.path.join(self.gc_root, folder, "game.iso")

    @staticmethod
    def _existing_wbfs_files(destination: str) -> list[str]:
        result = []
        if os.path.exists(destination):
            result.append(destination)
        directory = os.path.dirname(destination)
        stem = destination[:-1] if destination.lower().endswith(".wbfs") else destination
        for index in range(1, 10):
            candidate = f"{stem}{index}" if destination.lower().endswith(".wbfs") else f"{destination}{index}"
            if os.path.exists(candidate):
                result.append(candidate)
        return result


@dataclass
class LibraryResult:
    exported: int = 0
    overwritten: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_written: int = 0
    errors: list[str] = field(default_factory=list)


class LibraryExporter:
    def __init__(self, options: LibraryOptions):
        self.options = options
        self.planner = LibraryPlanner(options)

    def export(
        self, items: list[LibraryItem], progress=None, cancel=None, on_item=None
    ) -> LibraryResult:
        plan = self.planner.plan(items)
        return self.export_plan(
            plan, progress=progress, cancel=cancel, on_item=on_item
        )

    def export_plan(
        self, plan: list[PlannedExport], progress=None, cancel=None, on_item=None
    ) -> LibraryResult:
        total = sum(item.estimate for item in plan if item.action == "write")
        done = 0
        result = LibraryResult()
        for planned in plan:
            if cancel is not None and cancel.is_set():
                raise CancelledError("operacja anulowana")
            if planned.action == "skip":
                result.skipped += 1
                continue
            if on_item is not None:
                on_item(planned)
            try:
                self._export_one(planned, cancel, progress, done, total)
            except CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                result.failed += 1
                result.errors.append(
                    f"{planned.item.title} [{planned.item.disc_id}]: {error}"
                )
            else:
                result.exported += 1
                if planned.overwritten:
                    result.overwritten += 1
                result.bytes_written += planned.estimate
            done += planned.estimate
            if progress is not None:
                progress(done, total)
        return result

    # ------------------------------------------------------------------
    def _export_one(self, planned: PlannedExport, cancel, progress, done: int, total: int) -> None:
        item = planned.item
        destination = planned.destination
        directory = planned.destination_dir
        os.makedirs(directory, exist_ok=True)
        temp_dir = os.path.join(directory, f".gor-tmp-{uuid.uuid4().hex}")
        os.makedirs(temp_dir, exist_ok=True)
        temp_path = os.path.join(temp_dir, planned.filename)

        def item_progress(value: int, maximum: int) -> None:
            if progress is None or not planned.estimate:
                return
            fraction = min(1.0, value / maximum) if maximum else 0.0
            progress(done + int(planned.estimate * fraction), total)

        try:
            clone = (
                item.wbfs_source_pair()
                if planned.profile != "nintendont"
                else None
            )
            if clone is not None:
                volume, entry, owned_volume = clone
                try:
                    clone_game_to_file(
                        volume,
                        entry,
                        temp_path,
                        split=planned.split,
                        progress=item_progress,
                        cancel=cancel,
                        title=item.title,
                    )
                finally:
                    if owned_volume is not None:
                        owned_volume.close()
                    if item.source_kind == "object" and item.object_source is not None:
                        try:
                            item.object_source.close()
                        except Exception:
                            pass
            else:
                source = item.open_source()
                try:
                    if planned.profile == "nintendont":
                        source_to_iso(
                            source,
                            temp_path,
                            size=item.iso_size(),
                            progress=item_progress,
                            cancel=cancel,
                        )
                    else:
                        source_to_wbfs(
                            source,
                            temp_path,
                            split=planned.split,
                            mode=self.options.scrub,
                            progress=item_progress,
                            cancel=cancel,
                        )
                finally:
                    source.close()

            self._validate_export(temp_path, planned)
            self._set_wbfs_title(temp_path, planned)
            if cancel is not None and cancel.is_set():
                raise CancelledError("operacja anulowana")

            existed = bool(planned.existing) and any(
                os.path.exists(path) for path in planned.existing
            )
            if existed:
                planned.overwritten = True
                for path in planned.existing:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

            temp_files = sorted(os.listdir(temp_dir))
            # Move split parts first and the base file last.
            temp_files.sort(key=lambda name: 0 if not name.endswith(planned.filename) else 1)
            for name in temp_files:
                shutil.move(os.path.join(temp_dir, name), os.path.join(directory, name))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            try:
                if not os.listdir(directory):
                    os.rmdir(directory)
            except OSError:
                pass

    def _validate_export(self, temp_path: str, planned: PlannedExport) -> None:
        if planned.profile == "nintendont":
            try:
                with open(temp_path, "rb") as handle:
                    head = handle.read(0x20)
            except OSError as error:
                raise LibraryError(f"nie można odczytać wyeksportowanego ISO: {error}") from error
            if len(head) < 0x20 or head[0x1C:0x20] != b"\xc2\x33\x9f\x3d":
                raise LibraryError("wyeksportowany plik nie jest obrazem GameCube")
            disc_id = head[:6].decode("latin-1", "replace").upper()
            if disc_id != planned.item.disc_id.upper():
                raise LibraryError(
                    f"ID wyeksportowanego ISO ({disc_id}) nie zgadza się z grą"
                )
            return

        volume = WbfsVolume.open(temp_path)
        try:
            games = volume.games()
            if not games:
                raise LibraryError("wyeksportowany plik WBFS nie zawiera gry")
            disc_id = games[0].disc_id.upper()
            if disc_id != planned.item.disc_id.upper():
                raise LibraryError(
                    f"ID wyeksportowanej gry ({disc_id}) nie zgadza się z grą"
                )
        finally:
            volume.close()

    def _set_wbfs_title(self, temp_path: str, planned: PlannedExport) -> None:
        if planned.profile == "nintendont":
            return
        try:
            volume = WbfsVolume.open(temp_path)
        except WbfsError:
            return
        try:
            games = volume.games()
            if games:
                volume.rename_game(games[0].slot, planned.item.title)
        finally:
            volume.close()
