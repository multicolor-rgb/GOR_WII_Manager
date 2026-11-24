"""GTK list items and asynchronous cover loading."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, GObject, Gtk  # noqa: E402

from ..core import art  # noqa: E402
from ..core.i18n import _  # noqa: E402
from ..core.util import human_size, region_name  # noqa: E402


class GameItem(GObject.Object):
    __gtype_name__ = "WbfsGameItem"

    title = GObject.Property(type=str, default="")
    disc_id = GObject.Property(type=str, default="")
    region = GObject.Property(type=str, default="")
    kind = GObject.Property(type=str, default="")
    size_text = GObject.Property(type=str, default="")
    disc_text = GObject.Property(type=str, default="")
    subtitle = GObject.Property(type=str, default="")
    cover = GObject.Property(type=object)

    def __init__(self, entry, geometry, title: str):
        super().__init__()
        self.entry = entry
        self.geometry = geometry
        self.cover = None
        self.title = title or entry.title
        self.disc_id = entry.disc_id
        self.region = region_name(entry.region)
        self.kind = entry.kind
        self.size_text = human_size(entry.stored_size(geometry))
        self.disc_text = (
            _("Płyta {number}").format(number=entry.disc_number + 1)
            if entry.disc_number
            else ""
        )
        self.subtitle = f"{entry.kind} · {self.region}" + (
            f" · {self.disc_text}" if self.disc_text else ""
        )

    @property
    def system(self) -> str:
        return "wii" if self.entry.is_wii else "gamecube"


class ArtLoader:
    """Downloads GameTDB art in the background and caches Gdk textures."""

    def __init__(self, cache_directory: str, kind: str = "cover3D"):
        self.cache_directory = cache_directory
        self.kind = kind
        self._textures: dict[str, Gdk.Texture | None] = {}
        self._pending: dict[str, list[Callable]] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wbfs-art")

    def cached(self, disc_id: str, system: str) -> Gdk.Texture | None:
        key = f"{system}:{disc_id.upper()}"
        return self._textures.get(key)

    def request(self, item: GameItem, callback: Callable[[Gdk.Texture | None], None]) -> None:
        key = f"{item.system}:{item.disc_id.upper()}"
        with self._lock:
            if key in self._textures:
                texture = self._textures[key]
                GLib.idle_add(callback, texture)
                return
            callbacks = self._pending.get(key)
            if callbacks is not None:
                callbacks.append(callback)
                return
            self._pending[key] = [callback]
        path = art.cache_path(self.cache_directory, item.disc_id, item.system)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            self._finish(key, path)
            return
        disc_id, system, region = item.disc_id, item.system, item.entry.region
        self._pool.submit(self._download, key, disc_id, system, region)

    def _download(self, key: str, disc_id: str, system: str, region: str) -> None:
        try:
            path = art.download_cover(
                disc_id, region, self.cache_directory, system, self.kind
            )
        except Exception:
            path = None
        GLib.idle_add(self._finish, key, path)

    def _finish(self, key: str, path: str | None) -> None:
        texture = None
        if path:
            try:
                texture = Gdk.Texture.new_from_filename(path)
            except Exception:
                texture = None
        with self._lock:
            self._textures[key] = texture
            callbacks = self._pending.pop(key, [])
        for callback in callbacks:
            try:
                callback(texture)
            except Exception:
                pass

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def _ellipsize(label: Gtk.Label) -> None:
    label.set_ellipsize(3)  # Pango.EllipsizeMode.END
    label.set_xalign(0.0)


def setup_title_cell(factory: Gtk.SignalListItemFactory) -> None:
    def setup(_factory, list_item):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        image = Gtk.Image()
        image.set_size_request(48, 48)
        image.set_pixel_size(44)
        image.set_from_icon_name("applications-games-symbolic")
        image.add_css_class("game-cover")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title = Gtk.Label(xalign=0.0)
        _ellipsize(title)
        subtitle = Gtk.Label(xalign=0.0)
        subtitle.add_css_class("caption")
        subtitle.add_css_class("dim-label")
        _ellipsize(subtitle)
        texts.append(title)
        texts.append(subtitle)
        box.append(image)
        box.append(texts)
        list_item.set_child(box)
        list_item.image = image
        list_item.title_label = title
        list_item.subtitle_label = subtitle

    factory.connect("setup", setup)


def bind_title_cell(factory: Gtk.SignalListItemFactory, art_loader: ArtLoader, show_covers: Callable[[], bool]) -> None:
    def bind(_factory, list_item):
        item: GameItem = list_item.get_item()
        list_item.title_label.set_text(item.title)
        list_item.subtitle_label.set_text(item.subtitle)
        image: Gtk.Image = list_item.image
        texture = art_loader.cached(item.disc_id, item.system) if show_covers() else None
        if texture is not None:
            image.set_from_paintable(texture)
        else:
            image.set_from_icon_name("applications-games-symbolic")
        if show_covers() and art_loader.cached(item.disc_id, item.system) is None:
            def delivered(loaded, target=item, widget=list_item, picture=image):
                if widget.get_item() is target and loaded is not None:
                    picture.set_from_paintable(loaded)

            art_loader.request(item, delivered)

    factory.connect("bind", bind)


def setup_text_cell(factory: Gtk.SignalListItemFactory, attribute: str, dim: bool = False) -> None:
    def setup(_factory, list_item):
        label = Gtk.Label(xalign=0.0)
        label.set_margin_start(6)
        label.set_margin_end(6)
        if dim:
            label.add_css_class("dim-label")
        list_item.set_child(label)
        list_item.label = label

    factory.connect("setup", setup)


def bind_text_cell(factory: Gtk.SignalListItemFactory, attribute: str) -> None:
    def bind(_factory, list_item):
        item: GameItem = list_item.get_item()
        list_item.label.set_text(str(getattr(item, attribute) or ""))

    factory.connect("bind", bind)
