"""Conversions between source images (ISO/GCM/RVZ) and WBFS/ISO outputs."""

from __future__ import annotations

import os

from .disc import build_usage
from .util import CancelledError, WbfsError
from .wbfs import WbfsVolume
from .wbfs_layout import MIN_BLOCK_SIZE, WII_SECTOR

MAX_ISO_CHUNK = 4 * 1024 * 1024


def _check(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise CancelledError("operacja anulowana")


def source_to_iso(
    source, destination: str, size: int | None = None, progress=None, cancel=None
) -> str:
    """Write the full ISO view of any source (ISO or RVZ)."""
    total = size or source.size
    with open(destination, "wb") as handle:
        handle.truncate(total)
        try:
            offset = 0
            while offset < total:
                _check(cancel)
                length = min(MAX_ISO_CHUNK, total - offset)
                data = source.read_zeros(offset, length)
                if len(data) < length:
                    data += b"\x00" * (length - len(data))
                if data.strip(b"\x00"):
                    handle.seek(offset)
                    handle.write(data)
                offset += length
                if progress:
                    progress(offset, total)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            try:
                handle.close()
                os.unlink(destination)
            except OSError:
                pass
            raise
    if progress:
        progress(total, total)
    return destination


def estimate_wii_wbfs_size(source, mode: str = "remove_update") -> int:
    """Upper bound for a single-game .wbfs file created from a Wii source."""
    needed, _total = _needed_blocks(source, mode)
    return (needed + 1) * MIN_BLOCK_SIZE


def _needed_blocks(source, mode: str) -> tuple[int, int]:
    usage = build_usage(source, source.info, mode)
    sectors_per_block = MIN_BLOCK_SIZE // WII_SECTOR
    needed = 0
    for index in range(1 + len(usage.table) // sectors_per_block):
        start = index * sectors_per_block
        if start >= len(usage.table):
            break
        if any(usage.table[start:start + sectors_per_block]):
            needed += 1
    return needed, usage.total_used_bytes


def source_to_wbfs(
    source,
    destination: str,
    split: bool = False,
    mode: str = "remove_update",
    progress=None,
    cancel=None,
) -> str:
    """Create a single-game .wbfs file from any source image."""
    needed, _total = _needed_blocks(source, mode)
    if needed <= 0:
        raise WbfsError("obraz nie zawiera danych do zapisania")
    size = (needed + 1) * MIN_BLOCK_SIZE
    volume = WbfsVolume.create(
        destination, size, block_size=MIN_BLOCK_SIZE, split=split
    )
    try:
        volume.add_disc(
            source, mode=mode, progress=progress, cancel=cancel
        )
    except BaseException:
        try:
            volume.close()
        finally:
            try:
                os.unlink(destination)
            except OSError:
                pass
        raise
    finally:
        volume.close()
    return destination


def convert_file(
    input_path: str,
    output_path: str,
    mode: str = "remove_update",
    split: bool = False,
    progress=None,
    cancel=None,
) -> str:
    from .disc import open_disc_source

    source = open_disc_source(input_path)
    try:
        if output_path.lower().endswith(".wbfs"):
            return source_to_wbfs(
                source,
                output_path,
                split=split,
                mode=mode,
                progress=progress,
                cancel=cancel,
            )
        return source_to_iso(source, output_path, progress=progress, cancel=cancel)
    finally:
        source.close()
