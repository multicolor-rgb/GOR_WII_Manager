"""Low level random access I/O for WBFS files, split files and block devices."""

from __future__ import annotations

import array
import fcntl
import os
import shutil
import stat
from typing import List

from .util import WbfsError, is_block_device

BLKGETSIZE64 = 0x80081272
BLKSSZGET = 0x1268

# FAT32 cannot store files of 4 GiB, use a safe limit for split parts.
SPLIT_LIMIT = 0xFFFF0000
MAX_SPLIT_PARTS = 10


class DiskIO:
    """Random access byte oriented view of a volume."""

    size: int = 0
    writable: bool = False
    path: str = ""

    def pread(self, offset: int, length: int) -> bytes:  # pragma: no cover
        raise NotImplementedError

    def pwrite(self, offset: int, data: bytes) -> None:  # pragma: no cover
        raise NotImplementedError

    def flush(self) -> None:
        pass

    def truncate(self, size: int) -> None:
        raise WbfsError("nie można zmienić rozmiaru tego urządzenia")

    def close(self) -> None:
        pass

    def read_zeros(self, offset: int, length: int) -> bytes:
        data = self.pread(offset, length)
        if len(data) < length:
            data += b"\x00" * (length - len(data))
        return data

    def __enter__(self) -> "DiskIO":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _pwrite_all(fd: int, data: bytes, offset: int) -> None:
    view = memoryview(data)
    written = 0
    while written < len(view):
        written += os.pwrite(fd, view[written:], offset + written)


class FileIO(DiskIO):
    def __init__(self, path: str, writable: bool = False, create: bool = False):
        self.path = path
        mode = "r+b" if writable else "rb"
        if create and writable:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        else:
            fd = os.open(path, os.O_RDWR if writable else os.O_RDONLY)
        self.fd = fd
        self.writable = writable
        self.size = os.fstat(fd).st_size

    def pread(self, offset: int, length: int) -> bytes:
        if offset >= self.size:
            return b""
        return os.pread(self.fd, min(length, self.size - offset), offset)

    def pwrite(self, offset: int, data: bytes) -> None:
        if not self.writable:
            raise WbfsError("plik otwarty tylko do odczytu")
        need = offset + len(data)
        if need > self.size:
            os.ftruncate(self.fd, need)
            self.size = need
        _pwrite_all(self.fd, data, offset)

    def truncate(self, size: int) -> None:
        if not self.writable:
            raise WbfsError("plik otwarty tylko do odczytu")
        os.ftruncate(self.fd, size)
        self.size = size

    def flush(self) -> None:
        if self.writable:
            os.fsync(self.fd)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class BlockDeviceIO(DiskIO):
    def __init__(self, path: str, writable: bool = False):
        flags = os.O_RDWR if writable else os.O_RDONLY
        self.fd = os.open(path, flags)
        self.path = path
        self.writable = writable
        self.size = _block_size(self.fd)
        self.sector_size = _block_sector_size(self.fd)

    def pread(self, offset: int, length: int) -> bytes:
        if offset >= self.size:
            return b""
        return os.pread(self.fd, min(length, self.size - offset), offset)

    def pwrite(self, offset: int, data: bytes) -> None:
        if not self.writable:
            raise WbfsError("urządzenie otwarte tylko do odczytu")
        if offset + len(data) > self.size:
            raise WbfsError("zapis poza końcem urządzenia")
        _pwrite_all(self.fd, data, offset)

    def flush(self) -> None:
        if self.writable:
            os.fsync(self.fd)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class FdIO(DiskIO):
    """A volume reached through a file descriptor (e.g. from UDisks2)."""

    def __init__(self, fd: int, size: int, writable: bool = False, path: str = ""):
        self.fd = fd
        self.size = size
        self.writable = writable
        self.path = path

    def pread(self, offset: int, length: int) -> bytes:
        if offset >= self.size:
            return b""
        return os.pread(self.fd, min(length, self.size - offset), offset)

    def pwrite(self, offset: int, data: bytes) -> None:
        if not self.writable:
            raise WbfsError("urządzenie otwarte tylko do odczytu")
        if offset + len(data) > self.size:
            raise WbfsError("zapis poza końcem urządzenia")
        _pwrite_all(self.fd, data, offset)

    def flush(self) -> None:
        if self.writable:
            os.fsync(self.fd)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class SplitFileIO(DiskIO):
    """Virtual file made of ``.wbfs`` + ``.wbf1`` ... ``.wbf9`` parts."""

    def __init__(self, base_path: str, writable: bool = False, virtual_size: int | None = None):
        self.path = base_path
        self.writable = writable
        self._parts: List[int | None] = []
        self._paths: List[str] = []
        existing = [path for path in split_paths(base_path) if os.path.exists(path)]
        if not existing and not writable:
            raise WbfsError(f"nie znaleziono: {base_path}")
        for path in existing:
            self._paths.append(path)
            self._parts.append(os.open(path, os.O_RDWR if writable else os.O_RDONLY))
        real_size = sum(os.fstat(fd).st_size for fd in self._parts if fd is not None)
        self.size = virtual_size if virtual_size is not None else real_size
        if virtual_size is not None and writable:
            self._ensure_files(virtual_size)

    # -- helpers ---------------------------------------------------------
    def _part_path(self, index: int) -> str:
        return self._path_for_index(index)

    def _path_for_index(self, index: int) -> str:
        if index == 0:
            return self.path
        return self.path[:-1] + str(index)

    def _ensure_files(self, size: int) -> None:
        needed = max(1, -(-size // SPLIT_LIMIT))
        if needed > MAX_SPLIT_PARTS:
            raise WbfsError(
                "obraz jest za duży na pliki dzielone (maksymalnie 10 części)"
            )
        for index in range(needed):
            while len(self._paths) <= index:
                path = self._part_path(len(self._paths))
                self._paths.append(path)
                self._parts.append(None)
            if self._parts[index] is None:
                self._parts[index] = os.open(
                    self._paths[index], os.O_RDWR | os.O_CREAT, 0o644
                )
            part_size = min(SPLIT_LIMIT, size - index * SPLIT_LIMIT)
            fd = self._parts[index]
            if os.fstat(fd).st_size < part_size:
                os.ftruncate(fd, part_size)
        for index, fd in enumerate(self._parts[needed:], start=needed):
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(self._path_for_index(index))
            except OSError:
                pass
        self._parts = self._parts[:needed]
        self._paths = self._paths[:needed]

    def _locate(self, offset: int):
        index, within = divmod(offset, SPLIT_LIMIT)
        if index >= len(self._parts):
            raise WbfsError("odczyt poza końcem obrazu dzielonego")
        return index, within

    # -- API -------------------------------------------------------------
    def pread(self, offset: int, length: int) -> bytes:
        if offset >= self.size:
            return b""
        length = min(length, self.size - offset)
        result = bytearray()
        while length > 0:
            index, within = self._locate(offset)
            fd = self._parts[index]
            if fd is None:
                chunk = b""
            else:
                chunk = os.pread(fd, min(length, SPLIT_LIMIT - within), within)
            if len(chunk) < min(length, SPLIT_LIMIT - within):
                chunk += b"\x00" * (min(length, SPLIT_LIMIT - within) - len(chunk))
            result += chunk
            offset += len(chunk)
            length -= len(chunk)
        return bytes(result)

    def pwrite(self, offset: int, data: bytes) -> None:
        if not self.writable:
            raise WbfsError("obraz dzielony otwarty tylko do odczytu")
        view = memoryview(data)
        while view:
            index, within = self._locate(offset)
            fd = self._parts[index]
            if fd is None:
                raise WbfsError("brak części obrazu dzielonego")
            count = min(len(view), SPLIT_LIMIT - within)
            _pwrite_all(fd, bytes(view[:count]), within)
            view = view[count:]
            offset += count

    def truncate(self, size: int) -> None:
        if not self.writable:
            raise WbfsError("obraz dzielony otwarty tylko do odczytu")
        self.size = size
        self._ensure_files(size)

    def flush(self) -> None:
        if not self.writable:
            return
        for fd in self._parts:
            if fd is not None:
                os.fsync(fd)

    def close(self) -> None:
        for fd in self._parts:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._parts = []


def _block_size(fd: int) -> int:
    buf = array.array("Q", [0])
    try:
        fcntl.ioctl(fd, BLKGETSIZE64, buf, True)
        return int(buf[0])
    except OSError:
        return os.lseek(fd, 0, os.SEEK_END)


def _block_sector_size(fd: int) -> int:
    buf = array.array("i", [512])
    try:
        fcntl.ioctl(fd, BLKSSZGET, buf, True)
        return int(buf[0])
    except OSError:
        return 512


def fd_size(fd: int) -> int:
    """Size in bytes of an open block device or file descriptor."""
    try:
        return os.fstat(fd).st_size or _block_size(fd)
    except OSError:
        return 0


def split_paths(base_path: str) -> List[str]:
    """Return existing split parts in order (base file first)."""
    paths = [base_path]
    for index in range(1, MAX_SPLIT_PARTS):
        candidate = base_path[:-1] + str(index)
        if os.path.exists(candidate):
            paths.append(candidate)
        else:
            break
    return paths


def has_split_parts(base_path: str) -> bool:
    return os.path.exists(base_path[:-1] + "1")


def open_volume_io(
    path: str,
    writable: bool = False,
    create: bool = False,
    virtual_size: int | None = None,
    split: bool | None = None,
) -> DiskIO:
    """Open a WBFS file, split set or a block device for random access."""
    if not path:
        raise WbfsError("nie podano ścieżki")
    if create:
        if virtual_size is None:
            raise WbfsError("brak rozmiaru nowego obrazu")
        use_split = split if split is not None else (has_split_parts(path) or virtual_size > SPLIT_LIMIT)
        if use_split:
            return SplitFileIO(path, writable=True, virtual_size=virtual_size)
        io = FileIO(path, writable=True, create=True)
        io.truncate(virtual_size)
        return io
    if not os.path.exists(path):
        raise WbfsError(f"nie znaleziono: {path}")
    if is_block_device(path):
        return BlockDeviceIO(path, writable=writable)
    if path.endswith(".wbfs") and has_split_parts(path):
        return SplitFileIO(path, writable=writable)
    return FileIO(path, writable=writable)


def copy_file(source: str, destination: str) -> None:
    """Plain file copy helper used for backups."""
    shutil.copy2(source, destination)


def fs_free_space(path: str) -> int:
    try:
        stats = os.statvfs(path)
    except OSError:
        return 0
    return stats.f_bavail * stats.f_frsize


def ensure_directory(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def file_size(path: str) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def same_device(path_a: str, path_b: str) -> bool:
    try:
        return os.stat(path_a).st_dev == os.stat(path_b).st_dev
    except OSError:
        return False


def is_regular_file(path: str) -> bool:
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False
