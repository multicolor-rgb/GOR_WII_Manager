"""Reconstruction of the encrypted Wii disc layout from decrypted RVZ data."""

from __future__ import annotations

import hashlib
import threading

from ..util import WbfsError

BLOCK_HEADER_SIZE = 0x0400
BLOCK_DATA_SIZE = 0x7C00
BLOCK_TOTAL_SIZE = 0x8000
BLOCKS_PER_GROUP = 64
GROUP_DATA_SIZE = BLOCK_DATA_SIZE * BLOCKS_PER_GROUP
GROUP_TOTAL_SIZE = BLOCK_TOTAL_SIZE * BLOCKS_PER_GROUP
HASH_AREA_SIZE = BLOCK_HEADER_SIZE * BLOCKS_PER_GROUP

_crypto_lock = threading.Lock()
_crypto_ready: bool | None = None


def _ensure_crypto() -> None:
    global _crypto_ready
    if _crypto_ready is not None:
        if not _crypto_ready:
            raise WbfsError(
                "brak biblioteki python3-cryptography (wymaganej do odszyfrowania "
                "RVZ); zainstaluj pakiet: sudo apt install python3-cryptography"
            )
        return
    with _crypto_lock:
        if _crypto_ready is None:
            try:
                from cryptography.hazmat.primitives.ciphers import (  # noqa: F401
                    Cipher,
                    algorithms,
                    modes,
                )

                _crypto_ready = True
            except ImportError:
                _crypto_ready = False
    _ensure_crypto()


def _aes_cbc(key: bytes, iv: bytes, data: bytes) -> bytes:
    _ensure_crypto()
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def build_hash_area(decrypted: bytes) -> bytearray:
    """Build the 64 raw 0x400-byte hash blocks for one 2 MiB Wii group."""
    if len(decrypted) < GROUP_DATA_SIZE:
        decrypted = decrypted + b"\x00" * (GROUP_DATA_SIZE - len(decrypted))
    view = memoryview(decrypted)
    area = bytearray(HASH_AREA_SIZE)

    # H0: 31 SHA-1 hashes of 0x400-byte blocks per cluster.
    for cluster in range(BLOCKS_PER_GROUP):
        base = cluster * BLOCK_HEADER_SIZE
        cluster_data = view[cluster * BLOCK_DATA_SIZE:(cluster + 1) * BLOCK_DATA_SIZE]
        for index in range(31):
            digest = hashlib.sha1(
                cluster_data[index * 0x400:(index + 1) * 0x400]
            ).digest()
            start = base + index * 20
            area[start:start + 20] = digest

    # H1: for each subgroup of 8 clusters, hash all H0 tables.
    for subgroup in range(8):
        first = subgroup * 8
        h1 = bytearray()
        for index in range(8):
            offset = (first + index) * BLOCK_HEADER_SIZE
            h1 += hashlib.sha1(area[offset:offset + 0x26C]).digest()
        for index in range(8):
            offset = (first + index) * BLOCK_HEADER_SIZE + 0x280
            area[offset:offset + 0xA0] = h1

    # H2: 8 hashes of the H1 tables, one per subgroup, copied to every cluster.
    h2 = bytearray()
    for subgroup in range(8):
        offset = (subgroup * 8) * BLOCK_HEADER_SIZE + 0x280
        h2 += hashlib.sha1(area[offset:offset + 0xA0]).digest()
    for cluster in range(BLOCKS_PER_GROUP):
        offset = cluster * BLOCK_HEADER_SIZE + 0x340
        area[offset:offset + 0xA0] = h2

    return area


def apply_hash_exceptions(area: bytearray, exceptions) -> None:
    for offset, digest in exceptions:
        if offset < 0 or offset + 20 > len(area):
            raise WbfsError("nieprawidłowy wyjątek hash w pliku RVZ")
        area[offset:offset + 20] = digest


def encrypt_group(decrypted: bytes, key: bytes, exceptions=()) -> bytes:
    """Rebuild one 2 MiB encrypted Wii group (hash blocks + encrypted data)."""
    if len(key) != 16:
        raise WbfsError("nieprawidłowy klucz partycji w pliku RVZ")
    if len(decrypted) < GROUP_DATA_SIZE:
        decrypted = decrypted + b"\x00" * (GROUP_DATA_SIZE - len(decrypted))
    area = build_hash_area(decrypted[:GROUP_DATA_SIZE])
    if exceptions:
        apply_hash_exceptions(area, exceptions)

    output = bytearray(GROUP_TOTAL_SIZE)
    zero_iv = b"\x00" * 16
    for cluster in range(BLOCKS_PER_GROUP):
        hash_offset = cluster * BLOCK_HEADER_SIZE
        encrypted_hash = _aes_cbc(
            key, zero_iv, bytes(area[hash_offset:hash_offset + BLOCK_HEADER_SIZE])
        )
        data_offset = cluster * BLOCK_DATA_SIZE
        encrypted_data = _aes_cbc(
            key,
            encrypted_hash[0x3D0:0x3E0],
            decrypted[data_offset:data_offset + BLOCK_DATA_SIZE],
        )
        target = cluster * BLOCK_TOTAL_SIZE
        output[target:target + BLOCK_HEADER_SIZE] = encrypted_hash
        output[target + BLOCK_HEADER_SIZE:target + BLOCK_TOTAL_SIZE] = encrypted_data
    return bytes(output)
