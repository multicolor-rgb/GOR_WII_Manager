"""A USB loader folder library exposed as an application "drive".

It presents ``wbfs/<Title> [<ID>]/<ID>.wbfs`` files and Nintendont
``games/<Title> [<ID>]/game.iso`` files through the same interface the UI
uses for WBFS volumes.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

from .convert import source_to_iso, source_to_wbfs
from .device import flatten, list_block_devices, system_disk_names
from .disc import IsoSource, parse_header
from .diskio import split_paths
from .library import (
    LibraryExporter,
    LibraryItem,
    LibraryOptions,
    LibraryTarget,
    detect_targets,
    folder_label,
    sanitize_component,
)
from .util import (
    DuplicateDiscError,
    WbfsError,
    fs_type,
    human_size,
    region_name,
)
from .wbfs import WbfsVolume

LIBRARY_FILESYSTEMS = {
    "vfat",
    "msdos",
    "fat",
    "fat32",
    "exfat",
    "ntfs",
    "ext2",
    "ext3",
    "ext4",
    "btrfs",
    "xfs",
}


@dataclass
class LibraryGame:
    path: str
    container: str  # "wbfs" or "gc_iso"
    disc_id: str
    title: str
    region: str = "?"
    disc_number: int = 0
    version: int = 0
    is_wii: bool = False
    is_gc: bool = False
    header: bytes = b""
    geometry: object | None = None
    size_bytes: int = 0
    iso_size_bytes: int = 0
    parts: list[str] = field(default_factory=list)
    slot: int = 0
    blocks: int = 0
    last_index: int = 0
    valid: bool = True
    error: str = ""

    @property
    def kind(self) -> str:
        return "Wii" if self.is_wii else "GameCube" if self.is_gc else "?"

    @property
    def region_name(self) -> str:
        return region_name(self.region)

    def stored_size(self, geometry=None) -> int:
        return self.size_bytes

    def iso_size(self, geometry=None) -> int:
        return self.iso_size_bytes


class _OwnedSource:
    def __init__(self, source, owner):
        self._source = source
        self._owner = owner
        self.info = source.info
        self.size = source.size
        self.iso_size = getattr(source, "iso_size", source.size)
        self.wbfs_volume = getattr(source, "volume", None)
        self.wbfs_entry = getattr(source, "entry", None)
        self.stored_size = (
            self.wbfs_entry.stored_size(self.wbfs_volume.geometry)
            if self.wbfs_volume is not None and self.wbfs_entry is not None
            else 0
        )

    def read_zeros(self, offset: int, length: int) -> bytes:
        return self._source.read_zeros(offset, length)

    read = read_zeros

    def close(self) -> None:
        try:
            self._source.close()
        finally:
            self._owner.close()


def normalize_library_base(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(path))
    name = os.path.basename(path.rstrip("/"))
    if name in {"wbfs", "games"}:
        return os.path.dirname(path)
    return path


def create_library(base_path: str) -> "FolderLibrary":
    base = normalize_library_base(base_path)
    os.makedirs(os.path.join(base, "wbfs"), exist_ok=True)
    os.makedirs(os.path.join(base, "games"), exist_ok=True)
    return FolderLibrary(base)


class FolderLibrary:
    is_folder_library = True

    def __init__(
        self,
        base_path: str,
        root_wii: str = "wbfs",
        root_gc: str = "games",
        label: str | None = None,
    ):
        self.path = normalize_library_base(base_path)
        self.root_wii = root_wii
        self.root_gc = root_gc
        self.label = label or os.path.basename(self.path.rstrip("/")) or self.path
        self.filesystem = fs_type(self.path) if os.path.isdir(self.path) else ""
        self.writable = os.path.isdir(self.path) and os.access(self.path, os.W_OK)
        self.read_only = not self.writable
        self.geometry = None
        self._cache: list[LibraryGame] | None = None

    # ------------------------------------------------------------------
    @property
    def wii_root(self) -> str:
        return os.path.join(self.path, self.root_wii)

    @property
    def gc_root(self) -> str:
        return os.path.join(self.path, self.root_gc)

    def refresh(self) -> None:
        self._cache = None

    def close(self) -> None:
        pass

    def geometry_for(self, game: LibraryGame):
        return game.geometry

    # ------------------------------------------------------------------
    def games(self) -> list[LibraryGame]:
        if self._cache is None:
            games: list[LibraryGame] = []
            games.extend(self._scan_wii())
            games.extend(self._scan_gc())
            games.sort(key=lambda game: game.title.casefold())
            self._cache = games
        return list(self._cache)

    def find_game(self, disc_id: str) -> LibraryGame | None:
        wanted = disc_id.upper()
        for game in self.games():
            if game.disc_id.upper() == wanted:
                return game
        return None

    def _scan_wii(self) -> list[LibraryGame]:
        root = self.wii_root
        if not os.path.isdir(root):
            return []
        bases: list[str] = []
        try:
            for entry in os.scandir(root):
                try:
                    if entry.is_file() and entry.name.lower().endswith(".wbfs"):
                        bases.append(entry.path)
                    elif entry.is_dir():
                        for child in os.scandir(entry.path):
                            if child.is_file() and child.name.lower().endswith(".wbfs"):
                                bases.append(child.path)
                except OSError:
                    continue
        except OSError:
            return []

        games = []
        for path in bases:
            try:
                volume = WbfsVolume.open(path, read_only=True)
            except Exception:
                continue
            try:
                parts = split_paths(path)
                size = sum(os.path.getsize(part) for part in parts if os.path.exists(part))
                for entry in volume.games():
                    if not entry.valid:
                        continue
                    games.append(
                        LibraryGame(
                            path=path,
                            container="wbfs",
                            disc_id=entry.disc_id,
                            title=entry.title or entry.disc_id,
                            region=entry.region,
                            disc_number=entry.disc_number,
                            version=entry.version,
                            is_wii=entry.is_wii,
                            is_gc=entry.is_gc,
                            header=entry.header,
                            geometry=volume.geometry,
                            size_bytes=size,
                            iso_size_bytes=entry.iso_size(volume.geometry),
                            parts=parts,
                            slot=entry.slot,
                            blocks=entry.blocks,
                            last_index=entry.last_index,
                        )
                    )
            finally:
                volume.close()
        return games

    def _scan_gc(self) -> list[LibraryGame]:
        root = self.gc_root
        if not os.path.isdir(root):
            return []
        games = []
        try:
            entries = list(os.scandir(root))
        except OSError:
            return []
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
                path = os.path.join(entry.path, "game.iso")
                if not os.path.isfile(path):
                    continue
                size = os.path.getsize(path)
                with open(path, "rb") as handle:
                    header = handle.read(0x100)
                info = parse_header(header, size)
                if not info.is_gc:
                    continue
                games.append(
                    LibraryGame(
                        path=path,
                        container="gc_iso",
                        disc_id=info.disc_id,
                        title=info.title or info.disc_id,
                        region=info.region,
                        disc_number=info.disc_number,
                        version=info.version,
                        is_wii=False,
                        is_gc=True,
                        header=header,
                        geometry=None,
                        size_bytes=size,
                        iso_size_bytes=size,
                        parts=[path],
                    )
                )
            except Exception:
                continue
        return games

    # ------------------------------------------------------------------
    def source(self, game: LibraryGame):
        if game.container == "wbfs":
            volume = WbfsVolume.open(game.path, read_only=True)
            entries = volume.games()
            entry = next(
                (item for item in entries if item.slot == game.slot), None
            ) or (entries[0] if entries else None)
            if entry is None:
                volume.close()
                raise WbfsError("nie znaleziono gry w pliku WBFS")
            return _OwnedSource(volume.source(entry), volume)
        return IsoSource(game.path)

    # ------------------------------------------------------------------
    def add_disc(
        self,
        source,
        mode: str = "remove_update",
        progress=None,
        cancel=None,
        replace: bool = False,
    ):
        info = source.info
        existing = self.find_game(info.disc_id)
        if existing is not None and not replace:
            raise DuplicateDiscError(info.disc_id, 0, existing.title)
        title = info.title or info.disc_id
        item = LibraryItem.from_object_source(source, title)
        options = LibraryOptions(
            base_path=self.path,
            profile="auto",
            overwrite="replace",
            scrub=mode,
        )
        result = LibraryExporter(options).export(
            [item], progress=progress, cancel=cancel
        )
        self.refresh()
        if result.failed:
            raise WbfsError(result.errors[0] if result.errors else "eksport nie powiódł się")
        return self.find_game(info.disc_id)

    def remove_game(self, game: LibraryGame) -> None:
        for path in game.parts:
            try:
                os.unlink(path)
            except OSError:
                pass
        directory = os.path.dirname(game.path)
        if directory not in {self.wii_root, self.gc_root}:
            try:
                if not os.listdir(directory):
                    os.rmdir(directory)
            except OSError:
                pass
        self.refresh()

    # ------------------------------------------------------------------
    def extract_iso(
        self,
        game: LibraryGame,
        destination: str,
        size: int | None = None,
        progress=None,
        cancel=None,
    ) -> str:
        source = self.source(game)
        try:
            return source_to_iso(
                source,
                destination,
                size=size or game.iso_size_bytes,
                progress=progress,
                cancel=cancel,
            )
        finally:
            source.close()

    def extract_wbfs(
        self,
        game: LibraryGame,
        destination: str,
        split: bool = False,
        progress=None,
        cancel=None,
    ) -> str:
        source = self.source(game)
        try:
            return source_to_wbfs(
                source,
                destination,
                split=split,
                mode="remove_update",
                progress=progress,
                cancel=cancel,
            )
        finally:
            source.close()

    def rename_game(self, game: LibraryGame, title: str) -> None:
        if game.container == "wbfs":
            volume = WbfsVolume.open(game.path)
            try:
                entries = volume.games()
                entry = next(
                    (item for item in entries if item.slot == game.slot), None
                ) or (entries[0] if entries else None)
                if entry is not None:
                    volume.rename_game(entry.slot, title)
                game.title = title
            finally:
                volume.close()
            return
        new_folder = os.path.join(self.gc_root, folder_label(title, game.disc_id))
        old_folder = os.path.dirname(game.path)
        if new_folder != old_folder and not os.path.exists(new_folder):
            try:
                os.rename(old_folder, new_folder)
                game.path = os.path.join(new_folder, "game.iso")
                game.parts = [game.path]
            except OSError:
                pass
        game.title = title
        self.refresh()

    # ------------------------------------------------------------------
    @property
    def free_bytes(self) -> int:
        try:
            stats = os.statvfs(self.path)
            return stats.f_bavail * stats.f_frsize
        except OSError:
            return 0

    @property
    def capacity_bytes(self) -> int:
        try:
            stats = os.statvfs(self.path)
            return stats.f_blocks * stats.f_frsize
        except OSError:
            return 0

    @property
    def game_count(self) -> int:
        return len(self.games())

    def summary(self) -> dict:
        return {
            "path": self.path,
            "library": True,
            "games": self.game_count,
            "free_bytes": self.free_bytes,
            "capacity_bytes": self.capacity_bytes,
        }


def existing_library_targets() -> list[LibraryTarget]:
    result = []
    for target in detect_targets():
        if os.path.isdir(os.path.join(target.base_path, "wbfs")) or os.path.isdir(
            os.path.join(target.base_path, "games")
        ):
            result.append(target)
    return result
