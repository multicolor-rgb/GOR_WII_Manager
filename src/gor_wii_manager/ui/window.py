"""Main application window."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ..core import device as device_module  # noqa: E402
from ..core import diskio, udisks  # noqa: E402
from ..core.convert import convert_file  # noqa: E402
from ..core.disc import IsoSource, find_disc_images, open_disc_source  # noqa: E402
from ..core.folder_library import (  # noqa: E402
    FolderLibrary,
    LibraryGame,
    create_library,
    existing_library_targets,
    normalize_library_base,
)
from ..core.library import (  # noqa: E402
    LibraryExporter,
    LibraryItem,
    LibraryOptions,
    LibraryPlanner,
    LibraryResult,
)
from ..core.i18n import _, set_language  # noqa: E402
from ..core.jobs import JobQueue  # noqa: E402
from ..core.settings import APP_NAME, APP_VERSION, cache_dir  # noqa: E402
from ..core.util import (  # noqa: E402
    CancelledError,
    DuplicateDiscError,
    WbfsError,
    game_filename,
    human_size,
    is_fat,
)
from ..core.wbfs import WbfsVolume  # noqa: E402
from . import dialogs  # noqa: E402
from .games import (  # noqa: E402
    ArtLoader,
    GameItem,
    bind_text_cell,
    bind_title_cell,
    setup_text_cell,
    setup_title_cell,
)
from .queue import QueueToggleButton, QueueView  # noqa: E402


class VolumeState:
    def __init__(self, path: str, label: str, volume: WbfsVolume, writable: bool):
        self.path = path
        self.label = label
        self.volume = volume
        self.writable = writable
        self.items: list[GameItem] = []
        self.game_count = 0
        self.loaded = False
        self.loading = False


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.app = app
        self.settings = app.settings
        self.titles = app.titles
        self.set_title(APP_NAME)

        width = int(self.settings.get("window_width", 1180))
        height = int(self.settings.get("window_height", 760))
        self.set_default_size(width, height)

        self.volumes: dict[str, VolumeState] = {}
        self.current: VolumeState | None = None
        self.sort_ascending = True
        self._queue_update_pending = False
        self._handled_jobs: set[int] = set()
        self._folder_files: list[tuple[str, list[str]]] = []
        self._scanned_folders = False
        self._preferences: dialogs.PreferencesWindow | None = None
        self._properties: dialogs.PropertiesWindow | None = None
        self._load_pool = threading.Thread
        self.art = ArtLoader(
            os.path.join(cache_dir(), "covers"),
            kind=self.settings.get("cover_kind", "cover3D"),
        )
        self.queue = JobQueue(self._queue_changed)
        self._build_actions()
        self._build_ui()
        self._refresh_sidebar()
        self._update_actions()
        self._scan_folders_async()
        if getattr(self.settings, "migration_notes", None):
            GLib.idle_add(self._notify_migration)

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _build_actions(self) -> None:
        specifications = {
            "open": self._action_open,
            "open-library": self._action_open_library,
            "new-library": self._action_new_library,
            "close-volume": self._action_close_volume,
            "add-files": self._action_add_files,
            "add-folder": self._action_add_folder,
            "extract": self._action_extract,
            "extract-wbfs": self._action_extract_wbfs,
            "copy": self._action_copy,
            "delete": self._action_delete,
            "properties": self._action_properties,
            "format": self._action_format,
            "verify": self._action_verify,
            "repair": self._action_repair,
            "refresh": self._action_refresh,
            "refresh-titles": self._action_refresh_titles,
            "preferences": self._action_preferences,
            "shortcuts": self._action_shortcuts,
            "convert-iso": self._action_convert_iso,
            "convert-wbfs": self._action_convert_wbfs,
            "export-selected": self._action_export_selected,
            "export-files": self._action_export_files,
            "sync-library": self._action_sync_library,
            "select-all": self._action_select_all,
            "select-none": self._action_select_none,
        }
        for name, callback in specifications.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        app = self.get_application()
        app.set_accels_for_action("win.open", ["<Primary>o"])
        app.set_accels_for_action("win.add-files", ["<Primary>n"])
        app.set_accels_for_action("win.extract", ["<Primary>e"])
        app.set_accels_for_action("win.delete", ["Delete"])
        app.set_accels_for_action("win.refresh", ["F5"])
        app.set_accels_for_action("win.preferences", ["<Primary>comma"])
        app.set_accels_for_action("win.close-volume", ["<Primary><Shift>w"])
        app.set_accels_for_action("win.select-all", ["<Primary>a"])
        app.set_accels_for_action("win.select-none", ["<Primary><Shift>a"])

    def _enable(self, name: str, enabled: bool) -> None:
        action = self.lookup_action(name)
        if action is not None:
            action.set_enabled(enabled)

    def _update_actions(self) -> None:
        has_volume = self.current is not None
        selected = self._selected_items()
        for name in ("close-volume", "verify", "repair", "add-files", "add-folder"):
            self._enable(name, has_volume or name in {"add-files", "add-folder"})
        for name in ("extract", "extract-wbfs", "copy", "delete", "properties"):
            self._enable(name, has_volume and bool(selected))
        self._enable("export-selected", has_volume and bool(selected))
        self._enable("sync-library", has_volume)
        self._enable("export-files", True)
        self._enable("open-library", True)
        self._enable("new-library", True)
        is_library = has_volume and getattr(self.current.volume, "is_folder_library", False)
        self._enable("verify", has_volume and not is_library)
        self._enable("repair", has_volume and not is_library)
        self._enable("delete", has_volume and bool(selected) and self.current.writable)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()

        self.title_widget = Adw.WindowTitle(
            title=APP_NAME, subtitle=_("Wybierz napęd")
        )
        header.set_title_widget(self.title_widget)

        add_button = Gtk.Button(icon_name="list-add-symbolic")
        add_button.add_css_class("suggested-action")
        add_button.set_tooltip_text(_("Dodaj gry z plików (Ctrl+N)"))
        add_button.connect("clicked", lambda *_: self.activate_action("win.add-files", None))
        header.pack_start(add_button)

        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_button.add_css_class("flat")
        refresh_button.set_tooltip_text(_("Odśwież (F5)"))
        refresh_button.connect("clicked", lambda *_: self.activate_action("win.refresh", None))
        header.pack_end(refresh_button)

        self.queue_view = QueueView(self._cancel_job, self._clear_jobs)
        self.queue_toggle = QueueToggleButton(self.queue_view)
        header.pack_end(self.queue_toggle)

        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_tooltip_text(_("Menu"))
        menu_button.set_menu_model(self._build_menu())
        header.pack_end(menu_button)

        self.toolbar_view.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        content.set_vexpand(True)
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_position(292)
        self.paned.set_wide_handle(True)
        self.paned.set_shrink_start_child(False)
        self.paned.set_resize_start_child(False)
        self.paned.set_start_child(self._build_sidebar())
        self.paned.set_end_child(self._build_main())
        content.append(self.paned)
        self.toolbar_view.set_content(content)

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self.toolbar_view)
        self.set_content(self.toast_overlay)

        self.connect("close-request", self._on_close_request)

    def _build_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        menu.append(_("Dodaj gry z folderu…"), "win.add-folder")
        menu.append(_("Wypakuj jako WBFS…"), "win.extract-wbfs")
        menu.append(_("Zamknij napęd"), "win.close-volume")

        conversion = Gio.Menu()
        conversion.append(_("Konwertuj RVZ/ISO do ISO…"), "win.convert-iso")
        conversion.append(
            _("Konwertuj RVZ/ISO do pliku WBFS…"), "win.convert-wbfs"
        )
        menu.append_section(None, conversion)

        library = Gio.Menu()
        library.append(_("Otwórz bibliotekę USB…"), "win.open-library")
        library.append(_("Nowa biblioteka USB…"), "win.new-library")
        library.append(
            _("Eksportuj zaznaczone do biblioteki USB…"), "win.export-selected"
        )
        library.append(_("Eksportuj pliki ISO/GCM/RVZ…"), "win.export-files")
        library.append(_("Synchronizuj bibliotekę USB…"), "win.sync-library")
        menu.append_section(None, library)

        tools = Gio.Menu()
        tools.append(_("Sprawdź spójność napędu"), "win.verify")
        tools.append(_("Napraw tablicę bloków"), "win.repair")
        tools.append(_("Formatuj nowy napęd…"), "win.format")
        menu.append_section(None, tools)

        application = Gio.Menu()
        application.append(_("Odśwież bazę tytułów"), "win.refresh-titles")
        application.append(_("Skróty klawiszowe"), "win.shortcuts")
        application.append(_("Ustawienia"), "win.preferences")
        application.append(_("O programie"), "app.about")
        menu.append_section(None, application)
        return menu

    def _build_sidebar(self) -> Gtk.Widget:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar.set_size_request(280, -1)
        sidebar.add_css_class("sidebar-panel")

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        self.sidebar_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        scroller.set_child(self.sidebar_content)
        sidebar.append(scroller)

        sidebar.append(Gtk.Separator())
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        footer.set_margin_top(10)
        footer.set_margin_bottom(12)
        footer.set_margin_start(10)
        footer.set_margin_end(10)

        open_button = Gtk.Button()
        open_button.set_child(
            Adw.ButtonContent(
                icon_name="document-open-symbolic",
                label=_("Otwórz plik .wbfs…"),
            )
        )
        open_button.add_css_class("flat")
        open_button.connect("clicked", lambda *_: self.activate_action("win.open", None))
        footer.append(open_button)

        format_button = Gtk.Button()
        format_button.set_child(
            Adw.ButtonContent(
                icon_name="drive-harddisk-symbolic",
                label=_("Formatuj napęd…"),
            )
        )
        format_button.add_css_class("flat")
        format_button.connect("clicked", lambda *_: self.activate_action("win.format", None))
        footer.append(format_button)

        library_button = Gtk.Button()
        library_button.set_child(
            Adw.ButtonContent(
                icon_name="folder-symbolic", label=_("Otwórz bibliotekę USB…")
            )
        )
        library_button.add_css_class("flat")
        library_button.connect(
            "clicked", lambda *_: self.activate_action("win.open-library", None)
        )
        footer.append(library_button)

        new_library_button = Gtk.Button()
        new_library_button.set_child(
            Adw.ButtonContent(
                icon_name="folder-new-symbolic", label=_("Nowa biblioteka USB…")
            )
        )
        new_library_button.add_css_class("flat")
        new_library_button.connect(
            "clicked", lambda *_: self.activate_action("win.new-library", None)
        )
        footer.append(new_library_button)

        self.sidebar_status = Gtk.Label(xalign=0.0)
        self.sidebar_status.add_css_class("caption")
        self.sidebar_status.add_css_class("dim-label")
        self.sidebar_status.set_wrap(True)
        footer.append(self.sidebar_status)
        sidebar.append(footer)
        return sidebar

    def _build_main(self) -> Gtk.Widget:
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.set_margin_top(8)
        toolbar.set_margin_bottom(8)
        toolbar.set_margin_start(12)
        toolbar.set_margin_end(12)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(_("Szukaj gry…"))
        self.search_entry.set_size_request(240, -1)
        self.search_entry.connect("search-changed", self._on_search_changed)
        toolbar.append(self.search_entry)

        self.summary_label = Gtk.Label(xalign=0.0, hexpand=True)
        self.summary_label.add_css_class("caption")
        self.summary_label.add_css_class("dim-label")
        self.summary_label.set_ellipsize(3)
        toolbar.append(self.summary_label)

        self.extract_button = self._toolbar_button(
            "document-save-symbolic",
            _("Wypakuj zaznaczone (Ctrl+E)"),
            "extract",
        )
        self.copy_button = self._toolbar_button(
            "edit-copy-symbolic",
            _("Kopiuj zaznaczone na inny napęd"),
            "copy",
        )
        self.delete_button = self._toolbar_button(
            "edit-delete-symbolic",
            _("Usuń zaznaczone (Delete)"),
            "delete",
        )
        toolbar.append(self.extract_button)
        toolbar.append(self.copy_button)
        toolbar.append(self.delete_button)
        main.append(toolbar)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.set_hexpand(True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        self.stack.add_named(self._build_welcome_page(), "welcome")
        self.stack.add_named(self._build_loading_page(), "loading")
        self.stack.add_named(self._build_empty_page(), "empty")
        self.stack.add_named(self._build_list_page(), "list")
        self.stack.set_visible_child_name("welcome")
        main.append(self.stack)

        self.queue_view_revealer = self.queue_view
        main.append(self.queue_view_revealer)
        return main

    def _toolbar_button(self, icon: str, tooltip: str, action: str) -> Gtk.Button:
        button = Gtk.Button(icon_name=icon)
        button.add_css_class("flat")
        button.set_tooltip_text(tooltip)
        button.set_sensitive(False)
        button.connect("clicked", lambda *_: self.activate_action(f"win.{action}", None))
        return button

    def _build_welcome_page(self) -> Gtk.Widget:
        page = Adw.StatusPage()
        page.set_title(APP_NAME)
        page.set_description(
            _("Zarządzaj kopiami gier Wii i GameCube na partycjach i plikach WBFS.")
        )
        page.set_icon_name("media-optical-symbolic")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_halign(Gtk.Align.CENTER)
        open_button = Gtk.Button(label=_("Otwórz napęd WBFS…"))
        open_button.add_css_class("suggested-action")
        open_button.add_css_class("pill")
        open_button.connect("clicked", lambda *_: self.activate_action("win.open", None))
        format_button = Gtk.Button(label=_("Utwórz nowy plik WBFS…"))
        format_button.add_css_class("pill")
        format_button.connect("clicked", lambda *_: self.activate_action("win.format", None))
        detect_button = Gtk.Button(label=_("Wykryj podłączone napędy"))
        detect_button.add_css_class("flat")
        detect_button.connect("clicked", lambda *_: self._action_refresh(None, None))
        box.append(open_button)
        box.append(format_button)
        box.append(detect_button)
        page.set_child(box)
        return page

    def _build_loading_page(self) -> Gtk.Widget:
        page = Adw.StatusPage()
        page.set_title(_("Wczytywanie gier…"))
        page.set_icon_name("media-optical-symbolic")
        spinner = Gtk.Spinner()
        spinner.set_size_request(32, 32)
        spinner.start()
        page.set_child(spinner)
        return page

    def _build_empty_page(self) -> Gtk.Widget:
        page = Adw.StatusPage()
        page.set_title(_("Brak gier"))
        page.set_description(_("Ten napęd nie zawiera jeszcze żadnych gier."))
        page.set_icon_name("folder-open-symbolic")
        button = Gtk.Button(label=_("Dodaj gry…"))
        button.add_css_class("suggested-action")
        button.add_css_class("pill")
        button.connect("clicked", lambda *_: self.activate_action("win.add-files", None))
        page.set_child(button)
        return page

    def _build_list_page(self) -> Gtk.Widget:
        self.list_store = Gio.ListStore.new(GameItem)
        self.filter = Gtk.CustomFilter.new(self._filter_func)
        self.filter_model = Gtk.FilterListModel(model=self.list_store, filter=self.filter)
        self.sort_model = Gtk.SortListModel(model=self.filter_model)
        self.selection_model = Gtk.MultiSelection(model=self.sort_model)
        self.selection_model.connect("selection-changed", lambda *_: self._update_actions())

        self.column_view = Gtk.ColumnView(model=self.selection_model)
        self.column_view.set_show_row_separators(True)
        self.column_view.set_show_column_separators(False)
        self.column_view.add_css_class("data-table")
        self.column_view.connect("activate", self._on_row_activated)

        title_factory = Gtk.SignalListItemFactory()
        setup_title_cell(title_factory)
        bind_title_cell(
            title_factory,
            self.art,
            lambda: bool(self.settings.get("show_covers", True)),
        )
        self.title_column = Gtk.ColumnViewColumn.new(
            _("Tytuł"), title_factory
        )
        self.title_column.set_expand(True)
        self.title_column.set_resizable(True)
        self.title_column.set_sorter(self._sorter("title", casefold=True))
        self.column_view.append_column(self.title_column)

        self.column_view.append_column(
            self._text_column("ID", "disc_id", 90, False, casefold=False)
        )
        self.column_view.append_column(
            self._text_column("Region", "region", 100, True)
        )
        self.column_view.append_column(
            self._text_column(_("Rozmiar"), "size_text", 110, True, numeric=True)
        )
        self.column_view.append_column(
            self._text_column(_("Płyta"), "disc_text", 80, True)
        )
        self.column_view.sort_by_column(self.title_column, Gtk.SortType.ASCENDING)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_hexpand(True)
        scroller.set_child(self.column_view)
        return scroller

    def _text_column(
        self,
        title: str,
        attribute: str,
        width: int,
        dim: bool,
        numeric: bool = False,
        casefold: bool = False,
    ) -> Gtk.ColumnViewColumn:
        factory = Gtk.SignalListItemFactory()
        setup_text_cell(factory, attribute, dim=dim)
        bind_text_cell(factory, attribute)
        column = Gtk.ColumnViewColumn.new(title, factory)
        column.set_fixed_width(width)
        column.set_resizable(True)
        column.set_sorter(self._sorter(attribute, casefold=casefold, numeric=numeric))
        return column

    def _sorter(self, attribute: str, casefold: bool = False, numeric: bool = False):
        def compare(item_a, item_b, _data):
            value_a = getattr(item_a, attribute) or ""
            value_b = getattr(item_b, attribute) or ""
            if numeric:
                value_a = item_a.entry.stored_size(item_a.geometry)
                value_b = item_b.entry.stored_size(item_b.geometry)
            elif casefold:
                value_a = str(value_a).casefold()
                value_b = str(value_b).casefold()
            return (value_a > value_b) - (value_a < value_b)

        return Gtk.CustomSorter.new(compare, None)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=message))

    def _notify_migration(self) -> bool:
        notes = getattr(self.settings, "migration_notes", None)
        if notes:
            self.toast(
                _("Przeniesiono dane z wbfs-manager: ")
                + ", ".join(_(note) for note in notes)
            )
            self.settings.migration_notes = []
        return False

    def _selected_items(self) -> list[GameItem]:
        if self.current is None:
            return []
        bitset = self.selection_model.get_selection()
        items = []
        for position in range(bitset.get_size()):
            index = bitset.get_nth(position)
            item = self.sort_model.get_item(index)
            if item is not None:
                items.append(item)
        return items

    def _filter_func(self, item, _data=None) -> bool:
        text = self.search_entry.get_text().strip().casefold()
        if not text:
            return True
        return text in (item.title or "").casefold() or text in (item.disc_id or "").casefold()

    def _on_search_changed(self, _entry) -> None:
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def _on_row_activated(self, _view, position: int) -> None:
        item = self.sort_model.get_item(position)
        if item is not None:
            self._show_properties(item)

    def _on_close_request(self, *_args) -> bool:
        self.settings.set("window_width", self.get_width())
        self.settings.set("window_height", self.get_height())
        self.queue.shutdown(wait=False)
        self.art.shutdown()
        return False

    # ------------------------------------------------------------------
    # sidebar
    # ------------------------------------------------------------------
    def _sidebar_row(self, label: str, subtitle: str, icon: str, path: str, kind: str):
        row = Gtk.ListBoxRow()
        row.path = path
        row.kind = kind
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_top(7)
        box.set_margin_bottom(7)
        box.set_margin_start(10)
        box.set_margin_end(10)
        image = Gtk.Image.new_from_icon_name(icon)
        image.set_pixel_size(20)
        image.add_css_class("dim-label")
        box.append(image)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        name = Gtk.Label(label=label, xalign=0.0)
        name.set_ellipsize(3)
        sub = Gtk.Label(label=subtitle, xalign=0.0)
        sub.add_css_class("caption")
        sub.add_css_class("dim-label")
        sub.set_ellipsize(3)
        texts.append(name)
        texts.append(sub)
        box.append(texts)
        row.set_child(box)
        return row

    def _clear_sidebar(self) -> None:
        child = self.sidebar_content.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.sidebar_content.remove(child)
            child = next_child

    def _add_sidebar_section(self, title: str, rows: list[Gtk.ListBoxRow]) -> None:
        if not rows:
            return
        heading = Gtk.Label(label=title.upper(), xalign=0.0)
        heading.add_css_class("sidebar-heading")
        self.sidebar_content.append(heading)
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        listbox.add_css_class("navigation-sidebar")
        listbox.connect("row-activated", self._on_sidebar_activated)
        for row in rows:
            listbox.append(row)
            if row.path == (self.current.path if self.current else None):
                listbox.select_row(row)
        self.sidebar_content.append(listbox)

    def _refresh_sidebar(self) -> None:
        self._clear_sidebar()
        open_rows = []
        for state in self.volumes.values():
            subtitle = (
                _("{count} gier · wolne {free}").format(
                    count=state.game_count,
                    free=human_size(state.volume.free_bytes),
                )
                if state.loaded
                else _("wolne {free}").format(
                    free=human_size(state.volume.free_bytes)
                )
            )
            if not state.writable:
                subtitle += _(" · tylko odczyt")
            icon = (
                "folder-symbolic"
                if getattr(state.volume, "is_folder_library", False)
                else "drive-harddisk-symbolic"
            )
            open_rows.append(
                self._sidebar_row(state.label, subtitle, icon, state.path, "volume")
            )
        self._add_sidebar_section(_("Otwarte napędy"), open_rows)

        try:
            detected = device_module.wbfs_devices()
        except Exception:
            detected = []
        device_rows = [
            self._sidebar_row(
                candidate.label or candidate.model or candidate.path,
                f"{candidate.path} · {human_size(candidate.size)}",
                "drive-removable-media-symbolic",
                candidate.path,
                "device",
            )
            for candidate in detected
            if candidate.path not in self.volumes
        ]
        self._add_sidebar_section(_("Wykryte napędy WBFS"), device_rows)

        try:
            possible = device_module.unknown_candidates()
        except Exception:
            possible = []
        possible_rows = [
            self._sidebar_row(
                candidate.label or candidate.model or candidate.path,
                _("{path} · {size} · brak systemu plików").format(
                    path=candidate.path,
                    size=human_size(candidate.size),
                ),
                "drive-harddisk-symbolic",
                candidate.path,
                "device",
            )
            for candidate in possible[:10]
            if candidate.path not in self.volumes
        ]
        self._add_sidebar_section(
            _("Partycje do sprawdzenia (WBFS?)"), possible_rows
        )

        library_rows = []
        try:
            for target in existing_library_targets():
                if target.base_path in self.volumes:
                    continue
                library_rows.append(
                    self._sidebar_row(
                        target.label or os.path.basename(target.base_path),
                        _("{path} · {filesystem} · wolne {free}").format(
                            path=target.base_path,
                            filesystem=target.filesystem or "?",
                            free=human_size(target.free_bytes),
                        ),
                        "folder-symbolic",
                        target.base_path,
                        "library",
                    )
                )
        except Exception:
            library_rows = []
        self._add_sidebar_section(_("Biblioteki USB"), library_rows)

        recent_rows = []
        for path in self.settings.get("recent", []):
            if path in self.volumes or not os.path.exists(path):
                continue
            recent_rows.append(
                self._sidebar_row(
                    os.path.basename(path),
                    os.path.dirname(path),
                    "document-open-recent-symbolic",
                    path,
                    "file",
                )
            )
        self._add_sidebar_section(
            _("Ostatnio otwierane"), recent_rows[:6]
        )

        folder_rows = []
        for _folder, images in self._folder_files:
            for path in images[:12]:
                if path in self.volumes:
                    continue
                folder_rows.append(
                    self._sidebar_row(
                        os.path.basename(path),
                        os.path.dirname(path),
                        "folder-download-symbolic",
                        path,
                        "file",
                    )
                )
        self._add_sidebar_section(
            _("Pliki WBFS na dyskach"), folder_rows[:20]
        )

        if (
            not open_rows
            and not device_rows
            and not possible_rows
            and not library_rows
            and not recent_rows
            and not folder_rows
        ):
            label = Gtk.Label(
                label=_("Brak napędów. Podłącz dysk lub otwórz plik .wbfs.")
            )
            label.set_wrap(True)
            label.add_css_class("dim-label")
            label.add_css_class("caption")
            label.set_margin_start(14)
            label.set_margin_end(14)
            label.set_margin_top(16)
            self.sidebar_content.append(label)

        self.sidebar_status.set_text(
            _("WBFS na dysku USB: podłącz napęd i naciśnij F5.")
            if not open_rows
            else ""
        )

    def _on_sidebar_activated(self, _listbox, row) -> None:
        path = getattr(row, "path", None)
        kind = getattr(row, "kind", None)
        if not path:
            return
        if kind == "volume":
            self._select_volume(path)
        elif kind == "library":
            self.open_library_path(path)
        else:
            self.open_path(path)

    # ------------------------------------------------------------------
    # opening volumes
    # ------------------------------------------------------------------
    def _open_volume(self, path: str, read_only: bool) -> WbfsVolume:
        if path.startswith("/dev/"):
            try:
                return WbfsVolume.open(path, read_only=read_only)
            except OSError:
                fd = udisks.open_device(path, writable=not read_only)
                size = diskio.fd_size(fd)
                if not size:
                    os.close(fd)
                    raise WbfsError("nie można odczytać rozmiaru urządzenia")
                io = diskio.FdIO(fd, size, writable=not read_only, path=path)
                return WbfsVolume.open(path, read_only=read_only, io=io)
        return WbfsVolume.open(path, read_only=read_only)

    def _label_for(self, path: str) -> str:
        if path.startswith("/dev/"):
            try:
                for candidate in device_module.wbfs_devices():
                    if candidate.path == path:
                        return candidate.label or candidate.model or path
            except Exception:
                pass
            return path
        return os.path.basename(path)

    def _action_open_library(self, _action, _param) -> None:
        dialog = Gtk.FileDialog(title=_("Otwórz bibliotekę USB"))

        def chosen(dialog, result):
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error:
                return
            path = folder.get_path()
            if not path:
                return
            base = normalize_library_base(path)
            if not (
                os.path.isdir(os.path.join(base, "wbfs"))
                or os.path.isdir(os.path.join(base, "games"))
            ):
                dialogs.show_error(
                    self,
                    "To nie jest biblioteka USB.",
                    "W wybranym folderze nie ma katalogu wbfs/ ani games/.",
                )
                return
            self.open_library_path(base)

        dialog.select_folder(self, None, chosen)

    def _action_new_library(self, _action, _param) -> None:
        dialog = Gtk.FileDialog(title=_("Nowa biblioteka USB"))

        def chosen(dialog, result):
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error:
                return
            path = folder.get_path()
            if not path:
                return
            try:
                library = create_library(path)
            except OSError as error:
                dialogs.show_error(
                self,
                _("Nie można utworzyć biblioteki: {error}").format(error=error),
            )
                return
            self.open_library_path(library.path)

        dialog.select_folder(self, None, chosen)

    def open_library_path(self, path: str) -> None:
        base = normalize_library_base(path)
        if base in self.volumes:
            self._select_volume(base)
            return
        if not os.path.isdir(base):
            dialogs.show_error(
                self,
                _("Folder biblioteki nie istnieje: {base}").format(base=base),
            )
            return
        if not (
            os.path.isdir(os.path.join(base, "wbfs"))
            or os.path.isdir(os.path.join(base, "games"))
        ):
            dialogs.show_error(
                self,
                "To nie jest biblioteka USB.",
                "Brak katalogów wbfs/ i games/.",
            )
            return
        library = FolderLibrary(base)
        label = os.path.basename(base.rstrip("/")) or base
        state = VolumeState(base, label, library, library.writable)
        self.volumes[base] = state
        self._select_volume(base, reload=True)

    def open_path(self, path: str, read_only: bool = False) -> None:
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(path):
            self.open_library_path(path)
            return
        if path in self.volumes:
            self._select_volume(path)
            return
        volume = None
        effective_read_only = read_only
        try:
            volume = self._open_volume(path, read_only)
        except (WbfsError, OSError) as error:
            if not read_only:
                try:
                    volume = self._open_volume(path, True)
                    effective_read_only = True
                except (WbfsError, OSError):
                    dialogs.show_error(self, str(error))
                    return
            else:
                dialogs.show_error(self, str(error))
                return
        state = VolumeState(path, self._label_for(path), volume, not effective_read_only)
        self.volumes[path] = state
        self.settings.add_recent(path)
        if effective_read_only:
            self.toast(
                _("Napęd otwarto tylko do odczytu (brak uprawnień do zapisu).")
            )
        self._select_volume(path, reload=True)

    def _select_volume(self, path: str, reload: bool = True) -> None:
        state = self.volumes.get(path)
        if state is None:
            return
        self.current = state
        self.title_widget.set_subtitle(state.label)
        self.search_entry.set_text("")
        self._refresh_sidebar()
        if reload and not state.loaded:
            self._reload_games(state)
        elif state.loaded:
            self._show_state_games(state)
        self._update_actions()

    def _reload_games(self, state: VolumeState) -> None:
        if self.current is not state:
            return
        state.loading = True
        self.stack.set_visible_child_name("loading")
        self.summary_label.set_text(_("Wczytywanie…"))
        volume = state.volume
        if getattr(volume, "is_folder_library", False):
            volume.refresh()
        titles = self.titles

        def work():
            try:
                entries = volume.games()
                data = [
                    (entry, titles.get(entry.disc_id) or entry.title) for entry in entries
                ]
                error = None
            except Exception as exc:  # noqa: BLE001
                data, error = [], str(exc)
            GLib.idle_add(self._games_loaded, state, data, error)

        threading.Thread(target=work, daemon=True).start()

    def _games_loaded(self, state: VolumeState, data, error) -> None:
        state.loading = False
        state.loaded = True
        if error:
            state.game_count = 0
            if self.current is state:
                self.stack.set_visible_child_name("welcome")
                self.summary_label.set_text("")
            dialogs.show_error(self, "Nie można wczytać listy gier.", error)
            return
        geometry_for = getattr(state.volume, "geometry_for", None)
        state.items = [
            GameItem(
                entry,
                geometry_for(entry) if geometry_for is not None else state.volume.geometry,
                title,
            )
            for entry, title in data
        ]
        state.game_count = len(state.items)
        if self.current is state:
            self._show_state_games(state)
        self._refresh_sidebar()

    def _populate_list(self, state: VolumeState) -> None:
        if self.current is not state:
            return
        self.list_store.remove_all()
        for item in state.items:
            self.list_store.append(item)

    def _show_state_games(self, state: VolumeState) -> None:
        if self.current is not state:
            return
        self._populate_list(state)
        summary = _("{count} gier · wolne {free} z {total}").format(
            count=state.game_count,
            free=human_size(state.volume.free_bytes),
            total=human_size(state.volume.capacity_bytes),
        )
        self.summary_label.set_text(summary)
        self.title_widget.set_subtitle(state.label)
        if state.game_count:
            self.stack.set_visible_child_name("list")
        else:
            self.stack.set_visible_child_name("empty")
        self._update_actions()

    def _action_open(self, _action, _param) -> None:
        dialog = Gtk.FileDialog(title=_("Otwórz napęd WBFS"))
        filters = dialogs.Gio_list_store_file_filters()
        dialog.set_filters(filters)
        dialog.set_default_filter(filters.get_item(0))

        def finished(dialog, result):
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return
            path = file.get_path()
            if path:
                self.open_path(path)

        dialog.open(self, None, finished)

    def _action_close_volume(self, _action, _param) -> None:
        if self.current is None:
            return
        state = self.current

        def close_it():
            self.volumes.pop(state.path, None)
            try:
                state.volume.close()
            except Exception:
                pass
            self.current = None
            self.title_widget.set_subtitle(_("Wybierz napęd"))
            self.summary_label.set_text("")
            self.stack.set_visible_child_name("welcome")
            self._refresh_sidebar()
            self._update_actions()

        busy = any(
            state.path in job.volumes for job in self.queue.active_jobs()
        )
        if busy:
            dialogs.show_error(
                self, "Trwa operacja na tym napędzie — poczekaj na jej zakończenie."
            )
            return
        close_it()

    # ------------------------------------------------------------------
    # adding games
    # ------------------------------------------------------------------
    def _action_add_files(self, _action, _param) -> None:
        state = self.current
        if state is None:
            dialogs.show_error(self, "Najpierw otwórz napęd WBFS.")
            return
        if not state.writable:
            dialogs.show_error(self, "Ten napęd jest otwarty tylko do odczytu.")
            return
        dialog = Gtk.FileDialog(title=_("Dodaj obrazy gier"))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        images = Gtk.FileFilter()
        images.set_name(_("Obrazy Wii/GameCube"))
        for pattern in (
            "*.iso",
            "*.gcm",
            "*.wbfs",
            "*.rvz",
            "*.ISO",
            "*.GCM",
            "*.WBFS",
            "*.RVZ",
        ):
            images.add_pattern(pattern)
        filters.append(images)
        everything = Gtk.FileFilter()
        everything.set_name(_("Wszystkie pliki"))
        everything.add_pattern("*")
        filters.append(everything)
        dialog.set_filters(filters)
        dialog.set_default_filter(images)

        def finished(dialog, result):
            try:
                files = dialog.open_multiple_finish(result)
            except GLib.Error:
                return
            paths = []
            for index in range(files.get_n_items()):
                item = files.get_item(index)
                path = item.get_path()
                if path:
                    paths.append(path)
            self._import_paths(state, paths)

        dialog.open_multiple(self, None, finished)

    def _action_add_folder(self, _action, _param) -> None:
        state = self.current
        if state is None:
            dialogs.show_error(self, "Najpierw otwórz napęd WBFS.")
            return
        if not state.writable:
            dialogs.show_error(self, "Ten napęd jest otwarty tylko do odczytu.")
            return
        dialog = Gtk.FileDialog(title=_("Dodaj gry z folderu"))

        def finished(dialog, result):
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error:
                return
            path = folder.get_path()
            if not path:
                return
            images = find_disc_images(path, recursive=True)
            if not images:
                self.toast(_("W tym folderze nie znaleziono obrazów gier."))
                return
            self._import_paths(state, images)

        dialog.select_folder(self, None, finished)

    def _import_paths(self, state: VolumeState, paths: list[str]) -> None:
        if state is None or not paths:
            return
        scrub = self.settings.get("scrub", "remove_update")
        replace = bool(self.settings.get("replace_duplicates", False))
        verify_after = bool(self.settings.get("verify_after_import", False))
        volume = state.volume
        target_label = state.label
        is_library = getattr(volume, "is_folder_library", False)

        def add_source(source, title: str, job) -> None:
            kwargs = {
                "mode": scrub,
                "progress": lambda done, total: job.progress(done, total),
                "cancel": job.cancel_event,
                "replace": replace,
            }
            if is_library:
                kwargs["title"] = title
            volume.add_disc(source, **kwargs)

        def runner(job):
            skipped: list[str] = []
            failures: list[str] = []
            total_files = len(paths)
            for index, path in enumerate(paths):
                if job.cancel_event.is_set():
                    raise CancelledError("operacja anulowana")
                job.detail = os.path.basename(path)
                try:
                    if path.lower().endswith(".wbfs"):
                        source_volume = WbfsVolume.open(path, read_only=True)
                        try:
                            for entry in source_volume.games():
                                if job.cancel_event.is_set():
                                    raise CancelledError("operacja anulowana")
                                job.detail = f"{os.path.basename(path)} — {entry.disc_id}"
                                title = (
                                    self.titles.get(entry.disc_id)
                                    or entry.title
                                )
                                try:
                                    add_source(
                                        source_volume.source(entry), title, job
                                    )
                                except DuplicateDiscError as error:
                                    skipped.append(str(error))
                        finally:
                            source_volume.close()
                    else:
                        with open_disc_source(path) as source:
                            title = (
                                self.titles.get(source.info.disc_id)
                                or source.info.title
                            )
                            try:
                                add_source(source, title, job)
                            except DuplicateDiscError as error:
                                skipped.append(str(error))
                except CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001
                    failures.append(f"{os.path.basename(path)}: {error}")
                job.progress(index + 1, total_files)
            if verify_after and not getattr(volume, "is_folder_library", False):
                report = volume.verify()
                if not report.ok:
                    failures.append("Weryfikacja: " + "; ".join(report.errors[:3]))
            return {"skipped": skipped, "failures": failures}

        title = (
            _("Import: {name}").format(name=os.path.basename(paths[0]))
            if len(paths) == 1
            else _("Import {count} plików").format(count=len(paths))
        )
        job = self.queue.add(title, runner, detail=f"→ {target_label}")
        job.kind = "import"
        job.volumes = {state.path}
        self.queue_view.set_reveal_child(True)
        self.queue_toggle.set_active(True)

    # ------------------------------------------------------------------
    # extraction
    # ------------------------------------------------------------------
    def _suggested_name(self, item: GameItem, extension: str) -> str:
        return game_filename(item.title, item.disc_id, extension)

    def _action_extract(self, _action, _param) -> None:
        items = self._selected_items()
        state = self.current
        if not items or state is None:
            return
        if len(items) == 1:
            item = items[0]
            size = item.entry.iso_size(item.geometry)
            dialog = Gtk.FileDialog(title=_("Wypakuj grę do ISO"))
            dialog.set_initial_name(self._suggested_name(item, "iso"))
            dialog.save(
                self,
                None,
                lambda dialog, result: self._extract_one_saved(
                    dialog, result, state, item, size
                ),
            )
        else:
            dialog = Gtk.FileDialog(title=_("Wypakuj gry do folderu"))
            dialog.select_folder(
                self,
                None,
                lambda dialog, result: self._extract_many_folder(
                    dialog, result, state, items
                ),
            )

    def _extract_one_saved(
        self, dialog, result, state: VolumeState, item: GameItem, size: int
    ) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return
        path = file.get_path()
        if not path:
            return
        if not path.lower().endswith(".iso"):
            path += ".iso"
        directory = os.path.dirname(path) or "."
        if is_fat(directory) and size > 4 * 1024 ** 3 - 1:
            dialogs.show_error(
                self,
                "Nie można zapisać ISO większego niż 4 GiB na partycji FAT32.",
                "Wybierz inny folder albo użyj opcji „Wypakuj jako WBFS”, "
                "która tworzy dzielony plik .wbfs/.wbf1 zgodny z FAT32.",
            )
            return
        self._queue_extract_iso(state, [item], [path])

    def _extract_many_folder(
        self, dialog, result, state: VolumeState, items: list[GameItem]
    ) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        base = folder.get_path()
        if not base:
            return
        paths = [
            self._unique_path(os.path.join(base, self._suggested_name(item, "iso")))
            for item in items
        ]
        self._queue_extract_iso(state, items, paths)

    def _queue_extract_iso(
        self, state: VolumeState, items: list[GameItem], paths: list[str]
    ) -> None:
        if state is None:
            return
        volume = state.volume
        for item, path in zip(items, paths):
            size = item.entry.iso_size(item.geometry)
            if is_fat(os.path.dirname(path) or ".") and size > 4 * 1024 ** 3 - 1:
                dialogs.show_error(
                    self,
                    "ISO nie zmieści się na partycji FAT32.",
                    _(
                        "{filename} ma {size}. Użyj opcji „Wypakuj jako WBFS”, "
                        "która tworzy dzielony plik .wbfs/.wbf1."
                    ).format(
                        filename=os.path.basename(path),
                        size=human_size(size),
                    ),
                )
                return
        pairs = list(zip(items, paths))

        def runner(job):
            for index, (item, path) in enumerate(pairs):
                if job.cancel_event.is_set():
                    raise CancelledError("operacja anulowana")
                job.detail = os.path.basename(path)
                volume.extract_iso(
                    item.entry,
                    path,
                    progress=lambda done, total: job.progress(done, total),
                    cancel=job.cancel_event,
                )
                job.progress(index + 1, len(pairs))
            return len(pairs)

        title = (
            _("Wypakowywanie 1 gry")
            if len(items) == 1
            else _("Wypakowywanie {count} gier").format(count=len(items))
        )
        job = self.queue.add(title, runner)
        job.kind = "extract"
        job.volumes = {state.path}
        self.queue_view.set_reveal_child(True)
        self.queue_toggle.set_active(True)

    def _action_extract_wbfs(self, _action, _param) -> None:
        items = self._selected_items()
        state = self.current
        if not items or state is None:
            return
        if len(items) == 1:
            item = items[0]
            dialog = Gtk.FileDialog(title=_("Zapisz jako plik WBFS"))
            dialog.set_initial_name(self._suggested_name(item, "wbfs"))
            filters = Gio.ListStore.new(Gtk.FileFilter)
            file_filter = Gtk.FileFilter()
            file_filter.set_name(_("Obraz WBFS"))
            file_filter.add_pattern("*.wbfs")
            filters.append(file_filter)
            dialog.set_filters(filters)
            dialog.set_default_filter(file_filter)
            dialog.save(
                self,
                None,
                lambda dialog, result: self._extract_wbfs_saved(
                    dialog, result, state, [item]
                ),
            )
        else:
            dialog = Gtk.FileDialog(title=_("Zapisz gry jako pliki WBFS"))
            dialog.select_folder(
                self,
                None,
                lambda dialog, result: self._extract_wbfs_folder(
                    dialog, result, state, items
                ),
            )

    def _extract_wbfs_saved(
        self, dialog, result, state: VolumeState, items: list[GameItem]
    ) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return
        path = file.get_path()
        if not path:
            return
        if not path.lower().endswith(".wbfs"):
            path += ".wbfs"
        self._queue_extract_wbfs(state, items, [path])

    def _extract_wbfs_folder(
        self, dialog, result, state: VolumeState, items: list[GameItem]
    ) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        base = folder.get_path()
        if not base:
            return
        paths = [
            self._unique_path(os.path.join(base, self._suggested_name(item, "wbfs")))
            for item in items
        ]
        self._queue_extract_wbfs(state, items, paths)

    def _queue_extract_wbfs(
        self, state: VolumeState, items: list[GameItem], paths: list[str]
    ) -> None:
        if state is None:
            return
        volume = state.volume
        pairs = list(zip(items, paths))

        def runner(job):
            for index, (item, path) in enumerate(pairs):
                if job.cancel_event.is_set():
                    raise CancelledError("operacja anulowana")
                job.detail = os.path.basename(path)
                split = is_fat(os.path.dirname(path) or ".")
                volume.extract_wbfs(
                    item.entry,
                    path,
                    split=split,
                    progress=lambda done, total: job.progress(done, total),
                    cancel=job.cancel_event,
                )
                job.progress(index + 1, len(pairs))
            return len(pairs)

        title = (
            _("Zapisywanie WBFS")
            if len(items) == 1
            else _("Zapisywanie {count} plików WBFS").format(count=len(items))
        )
        job = self.queue.add(title, runner)
        job.kind = "extract"
        job.volumes = {state.path}
        self.queue_view.set_reveal_child(True)
        self.queue_toggle.set_active(True)

    # ------------------------------------------------------------------
    # copy / delete / properties
    # ------------------------------------------------------------------
    def _action_copy(self, _action, _param) -> None:
        items = self._selected_items()
        if not items or self.current is None:
            return
        source = self.current
        targets = [
            state
            for state in self.volumes.values()
            if state.writable and state.path != source.path
        ]
        dialogs.choose_copy_target(
            self, targets, lambda target: self._queue_copy(items, source, target)
        )

    def _queue_copy(
        self, items: list[GameItem], source: VolumeState, target: VolumeState
    ) -> None:
        if source is None or not target.writable:
            return
        scrub = self.settings.get("scrub", "remove_update")
        replace = bool(self.settings.get("replace_duplicates", False))

        def runner(job):
            skipped = []
            for index, item in enumerate(items):
                if job.cancel_event.is_set():
                    raise CancelledError("operacja anulowana")
                job.detail = f"{item.title} → {target.label}"
                source_object = source.volume.source(item.entry)
                try:
                    target.volume.add_disc(
                        source_object,
                        mode=scrub,
                        progress=lambda done, total: job.progress(done, total),
                        cancel=job.cancel_event,
                        replace=replace,
                    )
                except DuplicateDiscError as error:
                    skipped.append(str(error))
                finally:
                    try:
                        source_object.close()
                    except Exception:
                        pass
                job.progress(index + 1, len(items))
            return {"target": target.path, "skipped": skipped}

        job = self.queue.add(
            _("Kopiowanie {count} gier").format(count=len(items)), runner
        )
        job.kind = "copy"
        job.volumes = {source.path, target.path}
        self.queue_view.set_reveal_child(True)
        self.queue_toggle.set_active(True)

    def _action_delete(self, _action, _param) -> None:
        items = self._selected_items()
        if not items or self.current is None:
            return
        if not self.current.writable:
            dialogs.show_error(self, "Ten napęd jest otwarty tylko do odczytu.")
            return
        names = ", ".join(f"{item.title} [{item.disc_id}]" for item in items[:5])
        if len(items) > 5:
            names += _(" i {count} więcej").format(count=len(items) - 5)
        state = self.current
        dialogs.confirm(
            self,
            "Usunąć gry?",
            _("Zostanie usuniętych {count} gier: {names}.").format(
                count=len(items), names=names
            ),
            action_label="Usuń",
            on_confirm=lambda: self._queue_delete(state, items),
        )

    def _queue_delete(self, state: VolumeState, items: list[GameItem]) -> None:
        if state is None:
            return
        volume = state.volume

        def runner(job):
            for index, item in enumerate(items):
                if job.cancel_event.is_set():
                    raise CancelledError("operacja anulowana")
                job.detail = item.title
                if getattr(volume, "is_folder_library", False):
                    volume.remove_game(item.entry)
                else:
                    volume.remove_game(item.entry.slot)
                job.progress(index + 1, len(items))
            return len(items)

        job = self.queue.add(
            _("Usuwanie {count} gier").format(count=len(items)), runner
        )
        job.kind = "delete"
        job.volumes = {state.path}
        self.queue_view.set_reveal_child(True)
        self.queue_toggle.set_active(True)

    def _action_properties(self, _action, _param) -> None:
        items = self._selected_items()
        if items:
            self._show_properties(items[0])

    def _show_properties(self, item: GameItem) -> None:
        if self.current is None:
            return
        window = dialogs.PropertiesWindow(
            self,
            self.current,
            item,
            self.art,
            on_renamed=self._refresh_after_change,
        )
        window.present()

    # ------------------------------------------------------------------
    # formatting / verification
    # ------------------------------------------------------------------
    def _action_format(self, _action, _param) -> None:
        dialog = dialogs.FormatDialog(self, self._format_target)
        dialog.present()

    def _format_target(self, mode: str, path: str, size: int, block_size: int | None) -> None:
        if path in self.volumes:
            dialogs.show_error(
                self,
                "Ten napęd jest teraz otwarty.",
                "Zamknij go (Ctrl+Shift+W) przed formatowaniem.",
            )
            return
        if mode == "file":
            if os.path.exists(path):
                dialogs.confirm(
                    self,
                    "Plik już istnieje",
                    _(
                        "Nadpisać {path}? Wszystkie dane w tym pliku "
                        "zostaną utracone."
                    ).format(path=path),
                    action_label="Nadpisz",
                    on_confirm=lambda: self._queue_format_file(path, size, block_size),
                )
                return
            self._queue_format_file(path, size, block_size)
        else:
            self._queue_format_device(path, block_size)

    def _queue_format_file(self, path: str, size: int, block_size: int | None) -> None:
        split = is_fat(os.path.dirname(path) or ".")

        def runner(job):
            job.progress(0, 1)
            volume = WbfsVolume.create(
                path, size, block_size=block_size, split=split
            )
            volume.close()
            job.progress(1, 1)
            return path

        job = self.queue.add(
            _("Formatowanie {name}").format(name=os.path.basename(path)),
            runner,
        )
        job.kind = "format-file"

    def _queue_format_device(self, path: str, block_size: int | None) -> None:
        def runner(job):
            package_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            helper_binary = shutil.which("gor-wii-manager-helper")
            if helper_binary:
                command = ["pkexec", helper_binary, "format", path]
            else:
                command = [
                    "pkexec",
                    "env",
                    f"PYTHONPATH={package_root}",
                    sys.executable,
                    "-m",
                    "gor_wii_manager.helper",
                    "format",
                    path,
                ]
            if block_size:
                command += ["--block-size", str(block_size)]
            process = subprocess.run(
                command, capture_output=True, text=True, timeout=600, check=False
            )
            if process.returncode != 0:
                message = (process.stderr or process.stdout or "").strip()
                if process.returncode in (126, 127):
                    message = _(
                        "Anulowano autoryzację administratora lub brak pkexec."
                    )
                raise WbfsError(
                    message
                    or _("pkexec zwrócił kod {code}").format(
                        code=process.returncode
                    )
                )
            job.progress(1, 1)
            return path

        job = self.queue.add(
            _("Formatowanie {name}").format(name=os.path.basename(path)),
            runner,
        )
        job.kind = "format-device"

    def _action_verify(self, _action, _param) -> None:
        state = self.current
        if state is None:
            return
        if getattr(state.volume, "is_folder_library", False):
            self.toast(
                _("Weryfikacja jest dostępna dla napędów WBFS, nie dla bibliotek.")
            )
            return
        volume = state.volume

        def runner(job):
            report = volume.verify(deep=False)
            return report

        job = self.queue.add(
            _("Sprawdzanie {label}").format(label=state.label), runner
        )
        job.kind = "verify"
        job.volumes = {state.path}

    def _action_repair(self, _action, _param) -> None:
        state = self.current
        if state is None:
            return
        if getattr(state.volume, "is_folder_library", False):
            self.toast(
                _("Naprawa tablicy bloków dotyczy tylko napędów WBFS.")
            )
            return
        if not state.writable:
            dialogs.show_error(self, "Ten napęd jest otwarty tylko do odczytu.")
            return
        dialogs.confirm(
            self,
            "Naprawić tablicę bloków?",
            "Tablica wolnych bloków zostanie odtworzona na podstawie wpisów gier.",
            action_label="Napraw",
            destructive=False,
            on_confirm=lambda: self._queue_repair(state),
        )

    def _queue_repair(self, state: VolumeState) -> None:
        volume = state.volume

        def runner(job):
            changes = volume.repair(clear_invalid=False)
            return changes

        job = self.queue.add(
            _("Naprawa {label}").format(label=state.label), runner
        )
        job.kind = "repair"
        job.volumes = {state.path}

    # ------------------------------------------------------------------
    # titles / settings / help
    # ------------------------------------------------------------------
    def _action_refresh_titles(self, _action, _param) -> None:
        self._start_titles_refresh(silent=False)

    def _start_titles_refresh(self, silent: bool = False) -> None:
        url = self.settings.get("titles_url")
        database = self.titles

        def runner(job):
            count, info = database.refresh(
                url, progress=lambda done, total: job.progress(done, total)
            )
            return info

        job = self.queue.add(_("Pobieranie bazy tytułów"), runner)
        job.kind = "titles"
        job.silent = silent

    def _apply_titles(self) -> None:
        for state in self.volumes.values():
            for item in state.items:
                title = self.titles.get(item.disc_id)
                if title and title != item.title:
                    item.title = title
        if hasattr(self, "sort_model"):
            sorter = self.sort_model.get_sorter()
            self.sort_model.set_sorter(None)
            self.sort_model.set_sorter(sorter)
        if self._preferences is not None:
            self._preferences.update_title_count()

    def _action_preferences(self, _action, _param) -> None:
        if self._preferences is None:
            self._preferences = dialogs.PreferencesWindow(
                self,
                self.settings,
                self.titles,
                on_refresh_titles=self._start_titles_refresh,
                on_settings_changed=self._setting_changed,
            )
            self._preferences.connect("destroy", self._preferences_destroyed)
        self._preferences.present()

    def _preferences_destroyed(self, *_args) -> None:
        self._preferences = None

    def _setting_changed(self, key: str) -> None:
        if key == "ui_language":
            set_language(self.settings.get("ui_language", "en"))
            GLib.idle_add(self.app.reload_window)
            return
        if key == "show_covers" and self.current is not None:
            self._reload_games(self.current)

    def _action_shortcuts(self, _action, _param) -> None:
        dialogs.ShortcutsWindow(self).present()

    def _action_convert_iso(self, _action, _param) -> None:
        self._convert_dialog("iso")

    def _action_convert_wbfs(self, _action, _param) -> None:
        self._convert_dialog("wbfs")

    def _convert_dialog(self, target: str) -> None:
        dialog = Gtk.FileDialog(title=_("Wybierz obrazy do konwersji"))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        images = Gtk.FileFilter()
        images.set_name(_("Obrazy RCZ/RVZ/ISO"))
        for pattern in (
            "*.rvz",
            "*.iso",
            "*.gcm",
            "*.RVZ",
            "*.ISO",
            "*.GCM",
        ):
            images.add_pattern(pattern)
        filters.append(images)
        dialog.set_filters(filters)
        dialog.set_default_filter(images)

        def finished(dialog, result):
            try:
                files = dialog.open_multiple_finish(result)
            except GLib.Error:
                return
            paths = []
            for index in range(files.get_n_items()):
                path = files.get_item(index).get_path()
                if path:
                    paths.append(path)
            if paths:
                self._choose_conversion_output(paths, target)

        dialog.open_multiple(self, None, finished)

    def _choose_conversion_output(self, paths: list[str], target: str) -> None:
        extension = "iso" if target == "iso" else "wbfs"
        if len(paths) == 1:
            stem = os.path.splitext(os.path.basename(paths[0]))[0]
            dialog = Gtk.FileDialog(title=_("Zapisz obraz"))
            dialog.set_initial_name(f"{stem}.{extension}")
            filters = Gio.ListStore.new(Gtk.FileFilter)
            file_filter = Gtk.FileFilter()
            file_filter.set_name(
                _("Obraz ISO") if target == "iso" else _("Obraz WBFS")
            )
            file_filter.add_pattern(f"*.{extension}")
            filters.append(file_filter)
            dialog.set_filters(filters)
            dialog.set_default_filter(file_filter)

            def saved(dialog, result):
                try:
                    file = dialog.save_finish(result)
                except GLib.Error:
                    return
                path = file.get_path()
                if not path:
                    return
                if not path.lower().endswith(f".{extension}"):
                    path += f".{extension}"
                self._queue_convert(paths, [path], target)

            dialog.save(self, None, saved)
            return

        dialog = Gtk.FileDialog(title=_("Wybierz folder docelowy"))

        def folder_chosen(dialog, result):
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error:
                return
            base = folder.get_path()
            if not base:
                return
            outputs = []
            for source in paths:
                stem = os.path.splitext(os.path.basename(source))[0]
                outputs.append(
                    self._unique_path(os.path.join(base, f"{stem}.{extension}"))
                )
            self._queue_convert(paths, outputs, target)

        dialog.select_folder(self, None, folder_chosen)

    def _queue_convert(
        self, inputs: list[str], outputs: list[str], target: str
    ) -> None:
        mode = self.settings.get("scrub", "remove_update")
        pairs = list(zip(inputs, outputs))

        def runner(job):
            for index, (source, destination) in enumerate(pairs):
                if job.cancel_event.is_set():
                    raise CancelledError("operacja anulowana")
                job.detail = os.path.basename(source)
                split = target == "wbfs" and is_fat(
                    os.path.dirname(destination) or "."
                )
                convert_file(
                    source,
                    destination,
                    mode=mode,
                    split=split,
                    progress=lambda done, total: job.progress(done, total),
                    cancel=job.cancel_event,
                )
                job.progress(index + 1, len(pairs))
            return len(pairs)

        what = "ISO" if target == "iso" else "WBFS"
        job = self.queue.add(
            _("Konwersja do {format}: {count} plik(ów)").format(
                format=what, count=len(pairs)
            ),
            runner,
        )
        job.kind = "convert"
        self.queue_view.set_reveal_child(True)
        self.queue_toggle.set_active(True)

    def _library_items_for(self, state: VolumeState, items: list[GameItem]):
        is_library = getattr(state.volume, "is_folder_library", False)
        result = []
        for item in items:
            if not getattr(item.entry, "valid", False):
                continue
            if is_library:
                result.append(
                    LibraryItem.from_folder_game(
                        state.volume, item.entry, item.title
                    )
                )
            else:
                result.append(
                    LibraryItem.from_wbfs(
                        state.volume, item.entry, item.title
                    )
                )
        return result

    def _action_export_selected(self, _action, _param) -> None:
        if self.current is None:
            return
        items = self._selected_items()
        library_items = self._library_items_for(self.current, items)
        if not library_items:
            self.toast(_("Brak poprawnych gier do eksportu."))
            return
        self._open_library_dialog(library_items, self.current)

    def _action_sync_library(self, _action, _param) -> None:
        if self.current is None:
            return
        library_items = self._library_items_for(
            self.current, self.current.items
        )
        if not library_items:
            dialogs.show_error(self, "Ten napęd nie zawiera gier do eksportu.")
            return
        self._open_library_dialog(library_items, self.current)

    def _action_export_files(self, _action, _param) -> None:
        dialog = Gtk.FileDialog(
            title=_("Eksportuj pliki do biblioteki USB")
        )
        filters = Gio.ListStore.new(Gtk.FileFilter)
        images = Gtk.FileFilter()
        images.set_name(_("Obrazy ISO/GCM/RVZ"))
        for pattern in ("*.iso", "*.gcm", "*.rvz", "*.ISO", "*.GCM", "*.RVZ"):
            images.add_pattern(pattern)
        filters.append(images)
        dialog.set_filters(filters)
        dialog.set_default_filter(images)

        def finished(dialog, result):
            try:
                files = dialog.open_multiple_finish(result)
            except GLib.Error:
                return
            library_items = []
            failures = []
            for index in range(files.get_n_items()):
                path = files.get_item(index).get_path()
                if not path:
                    continue
                try:
                    item = LibraryItem.from_file(path)
                except Exception as error:  # noqa: BLE001
                    failures.append(f"{os.path.basename(path)}: {error}")
                    continue
                item.title = self.titles.get(item.disc_id) or item.title
                library_items.append(item)
            if failures:
                dialogs.show_error(
                    self,
                    "Nie udało się odczytać części plików.",
                    _("\n".join(failures)),
                )
            if library_items:
                self._open_library_dialog(library_items, None)

        dialog.open_multiple(self, None, finished)

    def _open_library_dialog(self, items, source_state) -> None:
        def submitted(base, profile, overwrite, root_wii, root_gc):
            options = LibraryOptions(
                base_path=base,
                profile=profile,
                root_wii=root_wii,
                root_gc=root_gc,
                overwrite=overwrite,
                scrub=self.settings.get("scrub", "remove_update"),
            )
            self._prepare_library_export(items, options, source_state)

        dialogs.LibraryExportDialog(
            self,
            len(items),
            submitted,
            source_label=source_state.label if source_state is not None else "",
        ).present()

    def _prepare_library_export(self, items, options, source_state) -> None:
        try:
            planner = LibraryPlanner(options)
            plan = planner.plan(items)
        except Exception as error:  # noqa: BLE001
            dialogs.show_error(self, "Nie można przygotować eksportu.", str(error))
            return
        conflicts = [entry for entry in plan if entry.existing]
        if options.overwrite == "ask" and conflicts:
            dialogs.confirm(
                self,
                "Nadpisać istniejące gry?",
                _(
                    "Na dysku docelowym istnieje już {conflicts} z {total} "
                    "gier. Zostaną one zastąpione nowymi plikami."
                ).format(conflicts=len(conflicts), total=len(plan)),
                action_label="Nadpisz",
                on_confirm=lambda: self._queue_library_plan(
                    plan, options, source_state
                ),
            )
            return
        self._queue_library_plan(plan, options, source_state)

    def _queue_library_plan(self, plan, options, source_state) -> None:
        exporter = LibraryExporter(options)

        def runner(job):
            def on_item(planned):
                job.detail = f"{planned.item.title} [{planned.item.disc_id}]"

            result = exporter.export_plan(
                plan,
                progress=lambda done, total: job.progress(done, total),
                cancel=job.cancel_event,
                on_item=on_item,
            )
            return result

        total = sum(entry.estimate for entry in plan if entry.action == "write")
        job = self.queue.add(
            _("Eksport do biblioteki: {count} gier").format(count=len(plan)),
            runner,
            detail=f"≈ {human_size(total)}",
        )
        job.kind = "library-export"
        if source_state is not None:
            job.volumes = {source_state.path}
        self.queue_view.set_reveal_child(True)
        self.queue_toggle.set_active(True)

    def _action_select_all(self, _action, _param) -> None:
        if self.current is not None:
            self.selection_model.select_all()
            self._update_actions()

    def _action_select_none(self, _action, _param) -> None:
        self.selection_model.unselect_all()
        self._update_actions()

    def _scan_folders_async(self) -> None:
        if self._scanned_folders:
            return
        self._scanned_folders = True

        def work():
            try:
                found = device_module.mounted_wbfs_folders()
            except Exception:
                found = []
            GLib.idle_add(self._folders_scanned, found)

        threading.Thread(target=work, daemon=True).start()

    def _folders_scanned(self, found) -> None:
        self._folder_files = found
        self._refresh_sidebar()

    def _action_refresh(self, _action, _param) -> None:
        self._scanned_folders = False
        self._scan_folders_async()
        self._refresh_sidebar()
        if self.current is not None:
            if getattr(self.current.volume, "is_folder_library", False):
                self.current.volume.refresh()
            self._reload_games(self.current)
        try:
            count = len(device_module.wbfs_devices())
        except Exception:
            count = 0
        self.toast(
            _("Znaleziono {count} napędów WBFS.").format(count=count)
            if count
            else _("Nie znaleziono napędów WBFS.")
        )

    # ------------------------------------------------------------------
    # queue plumbing
    # ------------------------------------------------------------------
    def _queue_changed(self, _job=None) -> None:
        if self._queue_update_pending:
            return
        self._queue_update_pending = True
        GLib.idle_add(self._queue_tick)

    def _queue_tick(self) -> bool:
        self._queue_update_pending = False
        self.queue_view.update(self.queue.jobs)
        for job in self.queue.jobs:
            if job.active or job.id in self._handled_jobs:
                continue
            self._handled_jobs.add(job.id)
            self._after_job(job)
        return False

    def _after_job(self, job) -> None:
        kind = getattr(job, "kind", "")
        if job.status == "error":
            if not getattr(job, "silent", False):
                dialogs.show_error(
                    self,
                    _("Nie udało się wykonać: {title}").format(
                        title=job.title
                    ),
                    job.error,
                )
        elif job.status == "done":
            if kind == "verify":
                self._show_verify_result(job)
            elif kind == "repair":
                self.toast(
                    _("Naprawiono {count} wpisów tablicy.").format(
                        count=job.result
                    )
                )
            elif kind == "format-file":
                stale = self.volumes.pop(job.result, None)
                if stale is not None:
                    try:
                        stale.volume.close()
                    except Exception:
                        pass
                self.toast(_("Utworzono nowy napęd WBFS."))
                self.open_path(job.result)
            elif kind == "format-device":
                self.toast(_("Sformatowano partycję WBFS."))
                self.open_path(job.result)
            elif kind == "convert":
                self.toast(
                    _("Zapisano {count} plik(ów).").format(
                        count=job.result
                    )
                )
            elif kind == "library-export":
                result: LibraryResult = job.result
                if result is not None:
                    if result.errors:
                        dialogs.show_error(
                            self,
                            _(
                                "Nie udało się wyeksportować {count} gier."
                            ).format(count=result.failed),
                            "\n".join(result.errors[:8]),
                        )
                    self.toast(
                        _(
                            "Eksport: {exported} gier (nadpisano "
                            "{overwritten}, pominięto {skipped})."
                        ).format(
                            exported=result.exported,
                            overwritten=result.overwritten,
                            skipped=result.skipped,
                        )
                    )
                for volume_state in self.volumes.values():
                    if getattr(volume_state.volume, "is_folder_library", False):
                        volume_state.volume.refresh()
                        if volume_state is not self.current:
                            volume_state.loaded = False
                if self.current is not None and getattr(
                    self.current.volume, "is_folder_library", False
                ):
                    self._reload_games(self.current)
                self._scanned_folders = False
                self._scan_folders_async()
                self._refresh_sidebar()
            elif kind == "titles":
                self._apply_titles()
                self.toast(
                    job.result or _("Baza tytułów została odświeżona.")
                )
            elif kind in {"import", "delete", "copy"}:
                result = job.result
                if isinstance(result, dict):
                    skipped = result.get("skipped") or []
                    failures = result.get("failures") or []
                    if failures:
                        dialogs.show_error(
                            self,
                            "Część operacji zakończyła się błędem.",
                            "\n".join(failures[:8]),
                        )
                    if skipped:
                        self.toast(
                            _("Pominięto {count} istniejących gier.").format(
                                count=len(skipped)
                            )
                        )
                    target_path = result.get("target")
                    if target_path:
                        target_state = self.volumes.get(target_path)
                        if target_state is not None and target_state is not self.current:
                            target_state.loaded = False
                        self.toast(
                            _("Skopiowano gry do drugiego napędu.")
                        )
                elif kind == "delete":
                    self.toast(
                        _("Usunięto {count} gier.").format(
                            count=job.result
                        )
                    )
                elif kind == "extract":
                    self.toast(_("Zapisano obraz gry."))
                self._refresh_after_change()
        elif job.status == "cancelled":
            self.toast(_("Operacja anulowana."))
            self._refresh_after_change()
        if kind in {"import", "delete", "format-file", "repair"}:
            self._refresh_after_change()

    def _refresh_after_change(self) -> None:
        if self.current is not None and not self.queue.active_jobs():
            self._reload_games(self.current)
        self._refresh_sidebar()

    def _show_verify_result(self, job) -> None:
        report = job.result
        if report is None:
            return
        if report.ok:
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Napęd jest poprawny"),
                body=_(
                    "Struktura WBFS i tablica wolnych bloków są spójne."
                ),
            )
            dialog.add_response("close", _("Zamknij"))
            dialog.set_default_response("close")
            dialog.present()
            return
        body = "\n".join(report.errors[:6])
        if report.warnings:
            body += "\n\n" + "\n".join(report.warnings[:4])
        dialog = Adw.MessageDialog(
            transient_for=self,
            modal=True,
            heading=_("Wykryto problemy"),
            body=body,
        )
        dialog.add_response("close", _("Zamknij"))
        dialog.add_response("repair", _("Napraw"))
        dialog.set_response_appearance("repair", Adw.ResponseAppearance.SUGGESTED)

        def response(_dialog, response_id):
            if response_id == "repair" and self.current is not None:
                self._action_repair(None, None)

        dialog.connect("response", response)
        dialog.present()

    def _cancel_job(self, job) -> None:
        job.cancel()

    def _clear_jobs(self) -> None:
        self.queue.clear_finished()
        self.queue_view.update(self.queue.jobs)
        self.queue_view.set_reveal_child(False)
        self.queue_toggle.set_active(False)

    def _unique_path(self, path: str) -> str:
        if not os.path.exists(path):
            return path
        stem, extension = os.path.splitext(path)
        counter = 2
        while os.path.exists(f"{stem} ({counter}){extension}"):
            counter += 1
        return f"{stem} ({counter}){extension}"
