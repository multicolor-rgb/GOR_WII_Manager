"""UDisks2 access: open block devices with polkit authorization."""

from __future__ import annotations

import os

from .util import WbfsError

UDISKS_NAME = "org.freedesktop.UDisks2"
UDISKS_PATH = "/org/freedesktop/UDisks2"
BLOCK_IFACE = "org.freedesktop.UDisks2.Block"
FILESYSTEM_IFACE = "org.freedesktop.UDisks2.Filesystem"


def _connection():
    try:
        from gi.repository import Gio
    except ImportError as error:  # pragma: no cover - gi is a hard dependency
        raise WbfsError("brak biblioteki Gio (python3-gi)") from error
    return Gio, Gio.bus_get_sync(Gio.BusType.SYSTEM, None)


def object_path(device_path: str) -> str:
    name = os.path.basename(device_path.rstrip("/"))
    return f"{UDISKS_PATH}/block_devices/{name}"


def _call_simple(device_path: str, interface: str, method: str, parameters=None):
    Gio, connection = _connection()
    parameters = parameters or Gio.Variant("(a{sv})", ({},))
    try:
        return connection.call_sync(
            UDISKS_NAME,
            object_path(device_path),
            interface,
            method,
            parameters,
            None,
            Gio.DBusCallFlags.NONE,
            120000,
            None,
        )
    except Exception as error:  # GLib.Error and friends
        raise WbfsError(f"UDisks2: {error}") from error


def unmount(device_path: str) -> None:
    """Unmount a filesystem; errors are ignored when it was not mounted."""
    try:
        _call_simple(device_path, FILESYSTEM_IFACE, "Unmount")
    except WbfsError:
        pass


def _open_device_call(connection, Gio, path: str, mode: str, legacy: bool):
    if legacy:
        parameters = Gio.Variant("(a{sv})", ({},))
    else:
        parameters = Gio.Variant("(sa{sv})", (mode, {}))
    reply, fd_list = connection.call_with_unix_fd_list_sync(
        UDISKS_NAME,
        object_path(path),
        BLOCK_IFACE,
        "OpenDevice",
        parameters,
        Gio.VariantType.new("(h)"),
        Gio.DBusCallFlags.NONE,
        120000,
        None,
        None,
    )
    if fd_list is None:
        raise WbfsError("UDisks2 nie zwrócił deskryptora urządzenia")
    handle = reply.get_child_value(0).get_handle()
    return fd_list.get(handle)


def open_device(device_path: str, writable: bool = True) -> int:
    """Ask UDisks2 for an fd of a block device (triggers a polkit prompt).

    Supports both the current ``OpenDevice(mode, options)`` API (udisks 2.10+)
    and the older ``OpenDevice(options)`` signature.
    """
    Gio, connection = _connection()
    mode = "rw" if writable else "r"
    first_error = None
    for legacy in (False, True):
        try:
            return _open_device_call(connection, Gio, device_path, mode, legacy)
        except Exception as error:  # GLib.Error, marshalling or polkit errors
            message = str(error).lower()
            retry = isinstance(error, TypeError) or any(
                token in message
                for token in ("invalid", "signature", "unknownmethod", "unknown method")
            )
            if first_error is None:
                first_error = error
            if legacy or not retry:
                raise WbfsError(
                    "nie udało się otworzyć urządzenia przez UDisks2 "
                    f"({error}). Spróbuj odmontować napęd lub uruchomić "
                    "aplikację z uprawnieniami administratora."
                ) from error
    raise WbfsError(
        "nie udało się otworzyć urządzenia przez UDisks2 "
        f"({first_error}). Spróbuj odmontować napęd lub uruchomić aplikację "
        "z uprawnieniami administratora."
    ) from first_error


def device_size(device_path: str) -> int:
    try:
        with open(device_path, "rb", buffering=0) as handle:
            return os.lseek(handle.fileno(), 0, os.SEEK_END)
    except OSError:
        return 0
