"""On-disk WBFS layout: header, geometry, disc slots and the free-block bitmap."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .util import (
    CorruptVolumeError,
    align_up,
    read_be32,
    size_to_shift,
    write_be32,
)

WII_SECTOR = 0x8000
WII_SECTORS_PER_DISC = 143432 * 2  # dual layer
WBFS_MAGIC = b"WBFS"
WBFS_HEADER_FIXED = 12
MIN_BLOCK_SIZE = 2 * 1024 * 1024
MAX_BLOCK_SIZE = 64 * 1024 * 1024


@dataclass(frozen=True)
class Geometry:
    hd_sec_sz: int
    hd_sec_sz_s: int
    wbfs_sec_sz: int
    wbfs_sec_sz_s: int
    n_hd_sec: int
    n_wii_sec: int
    n_wbfs_sec: int
    n_wbfs_sec_per_disc: int
    disc_info_sz: int
    disc_info_sz_lba: int
    freeblks_lba: int
    bitmap_bytes: int
    bitmap_bytes_aligned: int
    max_disc: int
    total_bytes: int

    @property
    def wii_sec_per_wbfs_sec(self) -> int:
        return self.wbfs_sec_sz // WII_SECTOR

    @property
    def slot_offset(self) -> int:
        """Byte offset of the first disc slot."""
        return self.hd_sec_sz

    def slot_byte_offset(self, slot: int) -> int:
        return self.hd_sec_sz + slot * self.disc_info_sz

    def block_byte_offset(self, block: int) -> int:
        return block * self.wbfs_sec_sz


def _geometry(
    *,
    hd_sec_sz: int,
    wbfs_sec_sz: int,
    n_hd_sec: int,
    n_wii_sec: int,
) -> Geometry:
    hd_sec_sz_s = size_to_shift(hd_sec_sz)
    wbfs_sec_sz_s = size_to_shift(wbfs_sec_sz)
    shift = wbfs_sec_sz_s - size_to_shift(WII_SECTOR)
    n_wbfs_sec = n_wii_sec >> shift
    if n_wbfs_sec <= 1:
        raise CorruptVolumeError("obraz WBFS jest za mały")
    if n_wbfs_sec > 0xFFFF:
        raise CorruptVolumeError("zbyt wiele bloków WBFS dla 16-bitowej tablicy")
    n_wbfs_sec_per_disc = WII_SECTORS_PER_DISC >> shift
    disc_info_sz = align_up(0x100 + 2 * n_wbfs_sec_per_disc, hd_sec_sz)
    bitmap_bytes = max(4, n_wbfs_sec // 8)
    bitmap_bytes_aligned = align_up(bitmap_bytes, hd_sec_sz)
    freeblks_lba = (wbfs_sec_sz - bitmap_bytes_aligned) >> hd_sec_sz_s
    disc_info_sz_lba = disc_info_sz >> hd_sec_sz_s
    max_disc = (freeblks_lba - 1) // disc_info_sz_lba
    max_disc = min(max_disc, hd_sec_sz - WBFS_HEADER_FIXED)
    if max_disc < 1:
        raise CorruptVolumeError("obraz WBFS jest za mały na tablicę gier")
    return Geometry(
        hd_sec_sz=hd_sec_sz,
        hd_sec_sz_s=hd_sec_sz_s,
        wbfs_sec_sz=wbfs_sec_sz,
        wbfs_sec_sz_s=wbfs_sec_sz_s,
        n_hd_sec=n_hd_sec,
        n_wii_sec=n_wii_sec,
        n_wbfs_sec=n_wbfs_sec,
        n_wbfs_sec_per_disc=n_wbfs_sec_per_disc,
        disc_info_sz=disc_info_sz,
        disc_info_sz_lba=disc_info_sz_lba,
        freeblks_lba=freeblks_lba,
        bitmap_bytes=bitmap_bytes,
        bitmap_bytes_aligned=bitmap_bytes_aligned,
        max_disc=max_disc,
        total_bytes=n_hd_sec * hd_sec_sz,
    )


def choose_geometry(
    total_bytes: int,
    hd_sec_sz: int = 512,
    block_size: int | None = None,
) -> Geometry:
    """Pick the WBFS geometry the way libwbfs does (smallest block that fits)."""
    n_hd_sec = total_bytes // hd_sec_sz
    if n_hd_sec <= 0:
        raise ValueError("rozmiar obrazu musi być większy od zera")
    n_wii_sec = (n_hd_sec // WII_SECTOR) * hd_sec_sz
    if n_wii_sec < 2:
        n_wii_sec = max(2, (n_hd_sec * hd_sec_sz) // WII_SECTOR)
    if block_size:
        if block_size & (block_size - 1):
            raise ValueError("rozmiar bloku musi być potęgą dwójki")
        if not (MIN_BLOCK_SIZE <= block_size <= MAX_BLOCK_SIZE):
            raise ValueError("rozmiar bloku musi mieścić się w 2–64 MiB")
        if n_wii_sec >= (1 << 16) * (block_size // WII_SECTOR):
            raise ValueError(
                "ten rozmiar bloku jest za mały dla tak dużego napędu "
                "(przekroczono limit 65535 bloków)"
            )
        wbfs_sec_sz = block_size
    else:
        shift = 6
        for shift in range(6, 11):
            if n_wii_sec < ((1 << 16) << shift):
                break
        wbfs_sec_sz = WII_SECTOR << shift
    return _geometry(
        hd_sec_sz=hd_sec_sz,
        wbfs_sec_sz=wbfs_sec_sz,
        n_hd_sec=n_hd_sec,
        n_wii_sec=n_wii_sec,
    )


def size_for_used_blocks(
    used: int,
    block_size: int,
    hd_sec_sz: int = 512,
) -> int:
    """Smallest valid WBFS file size that can hold ``used`` data blocks.

    WBFS geometry rounds the block count down to 16 MiB boundaries, so a file
    sized as ``(used + 1) * block_size`` can report fewer usable blocks than
    requested.  This helper rounds up until the geometry really has room for
    all data blocks (block 0 is reserved for the header).
    """
    if used <= 0:
        raise ValueError("liczba bloków musi być większa od zera")
    if used >= 0xFFFF:
        raise ValueError("zbyt wiele bloków WBFS dla 16-bitowej tablicy")
    if block_size & (block_size - 1):
        raise ValueError("rozmiar bloku musi być potęgą dwójki")
    step = WII_SECTOR * hd_sec_sz
    size = align_up((used + 1) * block_size, step)
    while True:
        geometry = choose_geometry(
            size, hd_sec_sz=hd_sec_sz, block_size=block_size
        )
        if geometry.n_wbfs_sec - 1 >= used:
            return geometry.total_bytes
        size += step


def parse_geometry(header: bytes) -> Geometry:
    if len(header) < WBFS_HEADER_FIXED or header[:4] != WBFS_MAGIC:
        raise CorruptVolumeError("to nie jest obraz WBFS (zły nagłówek)")
    hd_sec_sz_s = header[8]
    wbfs_sec_sz_s = header[9]
    if not 9 <= hd_sec_sz_s <= 12:
        raise CorruptVolumeError(f"nieobsługiwany rozmiar sektora: 2^{hd_sec_sz_s}")
    if not 15 <= wbfs_sec_sz_s <= 27:
        raise CorruptVolumeError(f"nieobsługiwany rozmiar bloku: 2^{wbfs_sec_sz_s}")
    hd_sec_sz = 1 << hd_sec_sz_s
    wbfs_sec_sz = 1 << wbfs_sec_sz_s
    n_hd_sec = read_be32(header, 4)
    if n_hd_sec <= 0:
        raise CorruptVolumeError("nagłówek WBFS zawiera zerowy rozmiar")
    n_wii_sec = (n_hd_sec // WII_SECTOR) * hd_sec_sz
    if n_wii_sec < 2:
        n_wii_sec = max(2, (n_hd_sec * hd_sec_sz) // WII_SECTOR)
    return _geometry(
        hd_sec_sz=hd_sec_sz,
        wbfs_sec_sz=wbfs_sec_sz,
        n_hd_sec=n_hd_sec,
        n_wii_sec=n_wii_sec,
    )


def build_header(geometry: Geometry) -> bytes:
    header = bytearray(geometry.hd_sec_sz)
    header[0:4] = WBFS_MAGIC
    header[4:8] = write_be32(geometry.n_hd_sec)
    header[8] = geometry.hd_sec_sz_s
    header[9] = geometry.wbfs_sec_sz_s
    header[10:12] = b"\x00\x00"
    return bytes(header)


def bitmap_get(bitmap: bytes, block: int) -> bool:
    """True when ``block`` (1-based) is free.

    Bit order follows libwbfs: a big-endian u32 word stores 32 blocks, bit
    ``j`` of word ``i`` describes block ``i*32 + j + 1``.
    """
    if block <= 0:
        return False
    index = (block - 1) >> 5
    bit = (block - 1) & 31
    offset = index * 4
    if offset + 4 > len(bitmap):
        return False
    value = read_be32(bitmap, offset)
    return bool(value & (1 << bit))


def bitmap_set(bitmap: bytearray, block: int, free: bool) -> None:
    if block <= 0:
        return
    index = (block - 1) >> 5
    bit = (block - 1) & 31
    offset = index * 4
    if offset + 4 > len(bitmap):
        raise CorruptVolumeError("blok poza tablicą wolnych miejsc")
    value = read_be32(bitmap, offset)
    if free:
        value |= 1 << bit
    else:
        value &= ~(1 << bit)
    bitmap[offset:offset + 4] = write_be32(value)


def build_bitmap(used_blocks: Iterable[int], geometry: Geometry) -> bytearray:
    """Create a free-block bitmap: every block not in ``used_blocks`` is free."""
    used = set(used_blocks)
    bitmap = bytearray(geometry.bitmap_bytes_aligned)
    limit = max(0, geometry.n_wbfs_sec - 1)
    for block in range(1, limit + 1):
        if block not in used:
            bitmap_set(bitmap, block, True)
    return bitmap


def empty_bitmap(geometry: Geometry) -> bytearray:
    return build_bitmap((), geometry)


def count_free_blocks(bitmap: bytes, limit: int) -> int:
    return sum(1 for block in range(1, limit + 1) if bitmap_get(bitmap, block))


def first_free_block(bitmap: bytes, limit: int, start: int = 1) -> int:
    for block in range(start, limit + 1):
        if bitmap_get(bitmap, block):
            return block
    return 0
