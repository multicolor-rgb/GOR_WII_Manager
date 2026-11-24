"""Block device discovery (lsblk) for WBFS drives and folders."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field

WBFS_FSTYPE = "wbfs"


@dataclass
class BlockDevice:
    name: str
    path: str
    size: int
    type: str
    fstype: str = ""
    label: str = ""
    mountpoint: str = ""
    transport: str = ""
    removable: bool = False
    model: str = ""
    parent: str = ""
    children: list["BlockDevice"] = field(default_factory=list)

    @property
    def is_wbfs(self) -> bool:
        return self.fstype == WBFS_FSTYPE or "wbfs" in self.label.lower()

    @property
    def is_mounted(self) -> bool:
        return bool(self.mountpoint)

    @property
    def is_partition(self) -> bool:
        return self.type == "part"

    @property
    def display_name(self) -> str:
        label = self.label or self.model or self.name
        return f"{label} ({self.path})"


def _lsblk() -> dict:
    command = [
        "lsblk",
        "--json",
        "--bytes",
        "--output",
        "NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT,TRAN,RM,MODEL,PKNAME",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout or "{}")
    except ValueError:
        return {}


def _node(raw: dict, parent: str = "") -> BlockDevice:
    size = raw.get("size") or 0
    node = BlockDevice(
        name=raw.get("name") or "",
        path=raw.get("path") or "",
        size=int(size),
        type=raw.get("type") or "",
        fstype=(raw.get("fstype") or "").lower(),
        label=raw.get("label") or "",
        mountpoint=raw.get("mountpoint") or "",
        transport=raw.get("tran") or "",
        removable=bool(raw.get("rm")),
        model=raw.get("model") or "",
        parent=raw.get("pkname") or parent,
    )
    for child in raw.get("children") or []:
        node.children.append(_node(child, node.name))
    return node


_UDEV_CACHE: dict[str, tuple[float, dict]] = {}
_UDEV_TTL = 5.0


def udev_properties(path: str) -> dict:
    """Properties from the udev database (includes ID_FS_TYPE for WBFS)."""
    now = time.monotonic()
    cached = _UDEV_CACHE.get(path)
    if cached is not None and now - cached[0] < _UDEV_TTL:
        return cached[1]
    properties: dict[str, str] = {}
    try:
        result = subprocess.run(
            ["udevadm", "info", "--query=property", "--name", path],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                properties[key] = value
    except (OSError, subprocess.SubprocessError):
        properties = {}
    _UDEV_CACHE[path] = (now, properties)
    return properties


def _enrich(node: BlockDevice) -> None:
    if node.is_partition and node.path:
        properties = udev_properties(node.path)
        if properties:
            node.fstype = node.fstype or (properties.get("ID_FS_TYPE") or "").lower()
            node.label = node.label or properties.get("ID_FS_LABEL") or ""
            node.transport = node.transport or properties.get("ID_BUS") or ""
    for child in node.children:
        _enrich(child)


def list_block_devices() -> list[BlockDevice]:
    data = _lsblk()
    nodes = [_node(raw) for raw in data.get("blockdevices") or []]
    for node in nodes:
        _enrich(node)
    return nodes


def flatten(devices: list[BlockDevice]) -> list[BlockDevice]:
    result = []
    for device in devices:
        result.append(device)
        result.extend(flatten(device.children))
    return result


def system_disk_names() -> set[str]:
    """Disks that hold the root filesystem (never format those)."""
    names: set[str] = set()
    try:
        result = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE", "/"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        source = result.stdout.strip()
        if source.startswith("/dev/"):
            parent = subprocess.run(
                ["lsblk", "-n", "-o", "PKNAME", source],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout.strip()
            names.add(parent or os.path.basename(source))
    except (OSError, subprocess.SubprocessError):
        pass
    return names


def wbfs_devices() -> list[BlockDevice]:
    devices = flatten(list_block_devices())
    result = []
    for device in devices:
        if device.is_wbfs and device.size:
            result.append(device)
        elif not device.is_partition and any(child.is_wbfs for child in device.children):
            for child in device.children:
                if child.is_wbfs:
                    result.append(child)
    return result


def format_candidates() -> list[BlockDevice]:
    devices = flatten(list_block_devices())
    system_disks = system_disk_names()
    candidates = []
    for device in devices:
        if not device.is_partition or device.is_mounted:
            continue
        if device.size < 2 * 1024 ** 3:
            continue
        if device.parent in system_disks:
            continue
        candidates.append(device)
    return candidates


def unknown_candidates() -> list[BlockDevice]:
    """Unmounted partitions without a known filesystem (possible WBFS)."""
    devices = flatten(list_block_devices())
    system_disks = system_disk_names()
    result = []
    for device in devices:
        if not device.is_partition or device.is_mounted:
            continue
        if device.size < 2 * 1024 ** 3:
            continue
        if device.parent in system_disks:
            continue
        if device.is_wbfs or device.fstype:
            continue
        result.append(device)
    result.sort(key=lambda item: (not item.removable, item.path))
    return result


def mounted_wbfs_folders() -> list[tuple[str, list[str]]]:
    """Mounted filesystems that contain .wbfs files (usable as folders)."""
    devices = flatten(list_block_devices())
    result = []
    for device in devices:
        if not device.is_partition or not device.mountpoint:
            continue
        if device.fstype not in {"vfat", "exfat", "ntfs", "ext4", "ext3", "btrfs", "xfs"}:
            continue
        # Only scan removable/external mounts; walking "/" is slow and may find
        # unrelated files in system directories.
        if device.mountpoint == "/" or device.mountpoint.startswith(("/boot", "/snap", "/var")):
            continue
        if not (
            device.removable
            or device.transport == "usb"
            or device.mountpoint.startswith(("/media/", "/mnt/", "/run/media/"))
        ):
            continue
        found = _find_wbfs_files(device.mountpoint)
        if found:
            result.append((device.mountpoint, found))
    return result


def _find_wbfs_files(root: str, max_depth: int = 2) -> list[str]:
    found: list[str] = []

    def walk(directory: str, depth: int) -> None:
        if depth > max_depth or len(found) >= 200:
            return
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_file() and entry.name.lower().endswith(".wbfs"):
                    found.append(entry.path)
                elif entry.is_dir() and depth < max_depth:
                    walk(entry.path, depth + 1)
            except OSError:
                continue

    walk(root, 0)
    return found
