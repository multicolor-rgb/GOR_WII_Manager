"""Privileged helper used via ``pkexec`` to format WBFS partitions.

Run as::

    pkexec env PYTHONPATH=/path/to/src python3 -m gor_wii_manager.helper format /dev/sdX1

Only whole-partition formatting is implemented; no partition table changes.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from .core.util import WbfsError, human_size, is_block_device, parse_size
from .core.wbfs import WbfsVolume


def _unmount(path: str) -> None:
    try:
        subprocess.run(["umount", path], capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def _real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _mounted_mountpoints(device: str) -> list[str]:
    device = _real(device)
    result = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2 and _real(parts[0]) == device:
                    result.append(parts[1])
    except OSError:
        pass
    return result


def _parent_disk(device: str) -> str:
    try:
        output = subprocess.run(
            ["lsblk", "-n", "-o", "PKNAME", device],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
        return output.splitlines()[0] if output else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _root_disk() -> str:
    try:
        source = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE", "/"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
        if not source:
            return ""
        output = subprocess.run(
            ["lsblk", "-n", "-o", "PKNAME", source],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
        return output.splitlines()[0] if output else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _guard_device(path: str) -> None:
    from .core.diskio import open_volume_io

    if not is_block_device(path):
        raise WbfsError(f"{path} nie jest urządzeniem blokowym")
    root_disk = _root_disk()
    parent = _parent_disk(path)
    if root_disk and parent and parent == root_disk:
        raise WbfsError(
            "odmowa: to partycja dysku systemowego — nie można jej sformatować"
        )
    _unmount(path)
    mountpoints = _mounted_mountpoints(path)
    if mountpoints:
        raise WbfsError(
            "urządzenie jest nadal zamontowane ("
            + ", ".join(mountpoints)
            + ") — odmontuj je najpierw"
        )
    io = open_volume_io(path, writable=True)
    try:
        if io.size < 2 * 1024 ** 3:
            raise WbfsError("urządzenie jest za małe na partycję WBFS")
    finally:
        io.close()


def command_format(args: argparse.Namespace) -> int:
    path = args.device
    try:
        _guard_device(path)
        volume = WbfsVolume.format_device(
            path,
            block_size=parse_size(args.block_size) if args.block_size else None,
        )
    except (WbfsError, ValueError) as error:
        print(f"Błąd: {error}", file=sys.stderr)
        return 1
    print(
        "Sformatowano WBFS:\n"
        f"  urządzenie: {path}\n"
        f"  rozmiar:    {human_size(volume.geometry.total_bytes)}\n"
        f"  blok:       {human_size(volume.geometry.wbfs_sec_sz)}\n"
        f"  maks. gier: {volume.geometry.max_disc}"
    )
    volume.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gor-wii-manager-helper")
    sub = parser.add_subparsers(dest="command", required=True)
    fmt = sub.add_parser("format", help="sformatuj partycję jako WBFS")
    fmt.add_argument("device")
    fmt.add_argument("--block-size", default=None)
    fmt.set_defaults(func=command_format)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print(
            "Ten program trzeba uruchomić przez pkexec (wymagane uprawnienia roota).",
            file=sys.stderr,
        )
        return 3
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
