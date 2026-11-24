"""Dialogs: format, copy target, properties, preferences and helpers."""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from ..core import device as device_module  # noqa: E402
from ..core.i18n import LANGUAGES, _  # noqa: E402
from ..core.util import human_size  # noqa: E402
from .games import ArtLoader  # noqa: E402

GIB = 1024 ** 3

BLOCK_SIZES = [
    ("Automatyczny", None),
    ("2 MiB", 2 * 1024 ** 2),
    ("4 MiB", 4 * 1024 ** 2),
    ("8 MiB", 8 * 1024 ** 2),
    ("16 MiB", 16 * 1024 ** 2),
    ("32 MiB", 32 * 1024 ** 2),
]

SCRUB_MODES = [
    ("remove_update", "Pomiń partycję aktualizacji (zalecane)"),
    ("only_game", "Tylko partycja gry"),
    ("all", "Zachowaj wszystkie partycje"),
]


def show_error(parent: Gtk.Window, message: str, detail: str = "") -> None:
    dialog = Adw.MessageDialog(
        transient_for=parent,
        modal=True,
        heading=_("Wystąpił problem"),
        body=_(message),
    )
    if detail:
        dialog.set_extra_child(
            Gtk.Label(label=_(detail), wrap=True, selectable=True, xalign=0.0)
        )
    dialog.add_response("ok", "OK")
    dialog.set_default_response("ok")
    dialog.present()


def confirm(
    parent: Gtk.Window,
    heading: str,
    body: str,
    action_label: str | None = None,
    destructive: bool = True,
    on_confirm=None,
) -> None:
    action_label = action_label or _("Wykonaj")
    dialog = Adw.MessageDialog(
        transient_for=parent,
        modal=True,
        heading=_(heading),
        body=_(body),
    )
    dialog.add_response("cancel", _("Anuluj"))
    dialog.add_response("ok", _(action_label))
    if destructive:
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
    else:
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def response(_dialog, response_id):
        if response_id == "ok" and on_confirm is not None:
            on_confirm()

    dialog.connect("response", response)
    dialog.present()


class FormatDialog(Adw.Window):
    def __init__(self, parent: Gtk.Window, on_submit):
        super().__init__(
            transient_for=parent, modal=True, title=_("Formatowanie WBFS")
        )
        self.on_submit = on_submit
        self.set_default_size(580, 640)
        self.block_sizes = BLOCK_SIZES
        self._candidates: list = []

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        cancel = Gtk.Button(label=_("Anuluj"))
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.format_button = Gtk.Button(label=_("Formatuj"))
        self.format_button.add_css_class("destructive-action")
        self.format_button.connect("clicked", self._submit)
        header.pack_end(self.format_button)
        view.add_top_bar(header)

        page = Adw.PreferencesPage()

        # --- source selection -----------------------------------------
        source_group = Adw.PreferencesGroup(title=_("Miejsce docelowe"))
        self.source_row = Adw.ComboRow(
            title=_("Formatuj"),
            model=Gtk.StringList.new(
                [_("Nowy plik .wbfs"), _("Partycja urządzenia")]
            ),
        )
        self.source_row.connect("notify::selected", self._source_changed)
        source_group.add(self.source_row)
        page.add(source_group)

        # --- file page ------------------------------------------------
        file_box = Adw.PreferencesGroup(title=_("Plik WBFS"))
        self.path_row = Adw.EntryRow(title=_("Ścieżka pliku"))
        browse = Gtk.Button(icon_name="document-open-symbolic")
        browse.add_css_class("flat")
        browse.set_tooltip_text(_("Wybierz plik…"))
        browse.connect("clicked", self._choose_file)
        self.path_row.add_suffix(browse)
        file_box.add(self.path_row)

        adjustment = Gtk.Adjustment(
            value=32.0, lower=0.1, upper=2048.0, step_increment=1.0, page_increment=8.0
        )
        self.size_row = Adw.SpinRow.new(adjustment, 1.0, 1)
        self.size_row.set_title(_("Rozmiar pliku (GiB)"))
        file_box.add(self.size_row)
        self.file_group = file_box
        page.add(file_box)

        # --- device page ---------------------------------------------
        device_box = Adw.PreferencesGroup(title=_("Partycja urządzenia"))
        self.device_row = Adw.ComboRow(title=_("Urządzenie"))
        device_box.add(self.device_row)
        refresh = Adw.ActionRow(title=_("Odśwież listę urządzeń"))
        refresh_button = Gtk.Button(label=_("Odśwież"))
        refresh_button.add_css_class("flat")
        refresh_button.connect("clicked", lambda *_: self._refresh_devices())
        refresh.add_suffix(refresh_button)
        device_box.add(refresh)
        self.device_group = device_box
        page.add(device_box)

        # --- options ---------------------------------------------------
        options = Adw.PreferencesGroup(title=_("Opcje"))
        self.block_row = Adw.ComboRow(
            title=_("Rozmiar bloku"),
            model=Gtk.StringList.new(
                [_(label) for label, _value in self.block_sizes]
            ),
        )
        options.add(self.block_row)
        page.add(options)

        warning = Adw.PreferencesGroup()
        self.warning_label = Gtk.Label(
            label=_(
                "Formatowanie usuwa wszystkie dane z wybranego miejsca. "
                "Nie można tego cofnąć."
            ),
            wrap=True,
            xalign=0.0,
        )
        self.warning_label.add_css_class("error")
        self.warning_label.set_margin_top(12)
        self.warning_label.set_margin_bottom(6)
        warning.add(self.warning_label)
        self.confirm_check = Gtk.CheckButton(
            label=_("Rozumiem, że wszystkie dane zostaną usunięte")
        )
        self.confirm_check.set_margin_start(4)
        self.confirm_check.connect("toggled", lambda *_: self._update_sensitivity())
        warning.add(self.confirm_check)
        page.add(warning)

        view.set_content(page)
        self.set_content(view)
        self._load_defaults()
        self._refresh_devices()
        self._source_changed()

    # ------------------------------------------------------------------
    def _load_defaults(self) -> None:
        last_dir = os.path.expanduser("~")
        self.path_row.set_text(os.path.join(last_dir, "gry.wbfs"))

    def _source_changed(self, *_args) -> None:
        selected = self.source_row.get_selected()
        self.file_group.set_visible(selected == 0)
        self.device_group.set_visible(selected == 1)
        self._update_sensitivity()

    def _update_sensitivity(self) -> None:
        device_mode = self.source_row.get_selected() == 1
        has_device = bool(self._candidates)
        self.format_button.set_sensitive(
            self.confirm_check.get_active() and (has_device if device_mode else True)
        )

    def _refresh_devices(self) -> None:
        try:
            candidates = device_module.format_candidates()
        except Exception:
            candidates = []
        self._candidates = candidates
        labels = []
        for candidate in candidates:
            description = (
                candidate.label
                or candidate.model
                or candidate.fstype
                or _("brak systemu")
            )
            labels.append(
                f"{candidate.path} · {human_size(candidate.size)} · {description}"
            )
        if not labels:
            labels = [_("(brak wolnych partycji — podłącz dysk USB)")]
        self.device_row.set_model(Gtk.StringList.new(labels))
        self.device_row.set_selected(0)
        self._update_sensitivity()

    def _choose_file(self) -> None:
        dialog = Gtk.FileDialog(title=_("Nowy obraz WBFS"))
        dialog.set_initial_name("gry.wbfs")
        filters = Gio_list_store_file_filters()
        dialog.set_filters(filters)
        dialog.set_default_filter(filters.get_item(0))
        dialog.save(self, None, self._file_chosen)

    def _file_chosen(self, dialog, result) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return
        path = file.get_path()
        if path and not path.lower().endswith(".wbfs"):
            path += ".wbfs"
        self.path_row.set_text(path or "")

    def _submit(self) -> None:
        if not self.confirm_check.get_active():
            return
        block_size = self.block_sizes[self.block_row.get_selected()][1]
        if self.source_row.get_selected() == 0:
            path = self.path_row.get_text().strip()
            if not path:
                show_error(self, "Podaj ścieżkę pliku WBFS.")
                return
            if not path.lower().endswith(".wbfs"):
                path += ".wbfs"
            size = int(self.size_row.get_value() * GIB)
            self.close()
            self.on_submit("file", path, size, block_size)
        else:
            if not self._candidates:
                show_error(self, "Brak urządzeń do sformatowania.")
                return
            candidate = self._candidates[self.device_row.get_selected()]
            self.close()
            self.on_submit("device", candidate.path, candidate.size, block_size)


def Gio_list_store_file_filters():
    filters = Gio.ListStore.new(Gtk.FileFilter)
    file_filter = Gtk.FileFilter()
    file_filter.set_name(_("Obrazy WBFS"))
    file_filter.add_pattern("*.wbfs")
    filters.append(file_filter)
    any_filter = Gtk.FileFilter()
    any_filter.set_name(_("Wszystkie pliki"))
    any_filter.add_pattern("*")
    filters.append(any_filter)
    return filters


def choose_copy_target(parent: Gtk.Window, volumes: list, on_choose) -> None:
    if not volumes:
        show_error(
            parent,
            "Brak otwartego napędu docelowego.",
            "Otwórz najpierw drugi plik .wbfs lub podłącz drugi dysk WBFS, "
            "a następnie powtórz kopiowanie.",
        )
        return
    dialog = Adw.MessageDialog(
        transient_for=parent,
        modal=True,
        heading=_("Kopiuj gry"),
        body=_(
            "Wybierz napęd docelowy. Istniejące gry o tych samych ID "
            "zostaną pominięte."
        ),
    )
    dropdown = Gtk.DropDown.new_from_strings(
        [
            _("{label} — wolne {free}").format(
                label=state.label,
                free=human_size(state.volume.free_bytes),
            )
            for state in volumes
        ]
    )
    dialog.set_extra_child(dropdown)
    dialog.add_response("cancel", _("Anuluj"))
    dialog.add_response("copy", _("Kopiuj"))
    dialog.set_response_appearance("copy", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("copy")

    def response(_dialog, response_id):
        if response_id == "copy":
            on_choose(volumes[dropdown.get_selected()])

    dialog.connect("response", response)
    dialog.present()


class PropertiesWindow(Adw.Window):
    def __init__(self, parent, state, item, art_loader: ArtLoader, on_renamed=None):
        super().__init__(
            transient_for=parent,
            modal=False,
            title=_("Właściwości — {title}").format(title=item.title),
        )
        self.state = state
        self.item = item
        self.on_renamed = on_renamed
        self.set_default_size(560, 720)
        entry = item.entry
        volume = state.volume
        geometry = item.geometry

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        view.add_top_bar(header)

        page = Adw.PreferencesPage()

        cover_group = Adw.PreferencesGroup()
        cover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        cover_box.set_margin_top(8)
        cover_box.set_margin_bottom(8)
        cover_box.set_halign(Gtk.Align.CENTER)
        self.cover = Gtk.Picture()
        self.cover.set_size_request(240, 330)
        self.cover.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.cover.add_css_class("game-cover")
        cover_box.append(self.cover)
        if art_loader is not None:
            art_loader.request(item, self._cover_loaded)
        cover_group.add(cover_box)
        page.add(cover_group)

        details = Adw.PreferencesGroup(title=_("Gra"))
        details.add(Adw.ActionRow(title=_("Tytuł"), subtitle=item.title))
        details.add(Adw.ActionRow(title="ID", subtitle=entry.disc_id))
        details.add(Adw.ActionRow(title="System", subtitle=entry.kind))
        details.add(Adw.ActionRow(title="Region", subtitle=item.region))
        if entry.disc_number:
            details.add(
                Adw.ActionRow(
                    title=_("Numer płyty"),
                    subtitle=str(entry.disc_number + 1),
                )
            )
        details.add(
            Adw.ActionRow(
                title=_("Rozmiar w napędzie"),
                subtitle=human_size(entry.stored_size(geometry)),
            )
        )
        if geometry is not None and entry.blocks:
            details.add(
                Adw.ActionRow(
                    title=_("Bloki WBFS"),
                    subtitle=f"{entry.blocks} × {human_size(geometry.wbfs_sec_sz)}",
                )
            )
        details.add(
            Adw.ActionRow(
                title=_("Szacowany ISO"),
                subtitle=human_size(entry.iso_size(geometry)),
            )
        )
        page.add(details)

        rename_group = Adw.PreferencesGroup(title=_("Nazwa w napędzie"))
        self.name_row = Adw.EntryRow(title=_("Tytuł"))
        self.name_row.set_text(entry.title)
        apply_button = Gtk.Button(label=_("Zmień"))
        apply_button.add_css_class("flat")
        apply_button.connect("clicked", self._rename)
        self.name_row.add_suffix(apply_button)
        rename_group.add(self.name_row)
        page.add(rename_group)

        if entry.is_wii:
            partitions_group = Adw.PreferencesGroup(title=_("Partycje"))
            source = None
            try:
                source = volume.source(entry)
                info = source.info
                for part in info.partitions:
                    partitions_group.add(
                        Adw.ActionRow(
                            title=f"{part.index}. {part.kind}",
                            subtitle=f"offset 0x{part.offset:x}, {human_size(part.size)}",
                        )
                    )
            except Exception as error:  # pragma: no cover - defensive
                partitions_group.add(
                    Adw.ActionRow(
                        title=_("Nie można odczytać partycji"),
                        subtitle=str(error),
                    )
                )
            finally:
                if source is not None:
                    try:
                        source.close()
                    except Exception:
                        pass
            page.add(partitions_group)

        view.set_content(page)
        self.set_content(view)

    def _cover_loaded(self, texture) -> None:
        if texture is not None:
            self.cover.set_paintable(texture)

    def _rename(self) -> None:
        title = self.name_row.get_text().strip()
        if not title:
            return
        try:
            if getattr(self.state.volume, "is_folder_library", False):
                self.state.volume.rename_game(self.item.entry, title)
            else:
                self.state.volume.rename_game(self.item.entry.slot, title)
            self.item.title = title
            if self.on_renamed is not None:
                self.on_renamed()
            self.set_title(
                _("Właściwości — {title}").format(title=self.item.title)
            )
        except Exception as error:
            show_error(self, str(error))


class PreferencesWindow(Adw.PreferencesWindow):
    def __init__(self, parent, settings, titles, on_refresh_titles, on_settings_changed=None):
        super().__init__(
            transient_for=parent, modal=False, title=_("Ustawienia")
        )
        self.settings = settings
        self.titles = titles
        self.on_refresh_titles = on_refresh_titles
        self.on_settings_changed = on_settings_changed
        self.set_default_size(620, 700)

        page = Adw.PreferencesPage(
            title=_("Ogólne"), icon_name="emblem-system-symbolic"
        )

        interface = Adw.PreferencesGroup(title=_("Interfejs"))
        self.ui_language_row = Adw.ComboRow(
            title=_("Język interfejsu"),
            model=Gtk.StringList.new(
                [LANGUAGES[code] for code in ("en", "pl")]
            ),
        )
        current_ui = settings.get("ui_language", "en")
        self.ui_language_row.set_selected(0 if current_ui == "en" else 1)
        self.ui_language_row.connect(
            "notify::selected", self._ui_language_changed
        )
        interface.add(self.ui_language_row)
        page.add(interface)

        behavior = Adw.PreferencesGroup(title=_("Import gier"))
        self.scrub_row = Adw.ComboRow(
            title=_("Czyszczenie obrazów"),
            subtitle=_("Które partycje zachować podczas kopiowania na WBFS"),
            model=Gtk.StringList.new(
                [_(label) for _mode, label in SCRUB_MODES]
            ),
        )
        modes = [mode for mode, _ in SCRUB_MODES]
        current = settings.get("scrub", "remove_update")
        self.scrub_row.set_selected(modes.index(current) if current in modes else 0)
        self.scrub_row.connect("notify::selected", self._scrub_changed)
        behavior.add(self.scrub_row)

        self.replace_row = self._switch_row(
            "Zastępuj istniejące gry",
            "Nadpisuj grę o tym samym ID zamiast ją pomijać",
            "replace_duplicates",
        )
        behavior.add(self.replace_row)
        self.verify_row = self._switch_row(
            "Weryfikuj po imporcie",
            "Sprawdzaj spójność WBFS po każdej operacji zapisu",
            "verify_after_import",
        )
        behavior.add(self.verify_row)
        page.add(behavior)

        appearance = Adw.PreferencesGroup(title=_("Wygląd"))
        self.covers_row = self._switch_row(
            "Pobieraj okładki",
            "Miniatury z GameTDB (wymaga internetu, cache lokalny)",
            "show_covers",
        )
        appearance.add(self.covers_row)
        page.add(appearance)

        titles_group = Adw.PreferencesGroup(
            title=_("Baza tytułów"),
            description=_(
                "Tytuły gier z WiiTDB (GameTDB) są używane zamiast nazw "
                "z nagłówka płyty."
            ),
        )
        self.language_row = Adw.ComboRow(
            title=_("Język bazy"),
            model=Gtk.StringList.new(
                ["pl", "en", "de", "fr", "es", "it", "nl"]
            ),
        )
        languages = ["pl", "en", "de", "fr", "es", "it", "nl"]
        language = settings.get("language", "en")
        self.language_row.set_selected(
            languages.index(language) if language in languages else 1
        )
        self.language_row.connect("notify::selected", self._language_changed)
        titles_group.add(self.language_row)

        self.auto_titles_row = self._switch_row(
            "Pobieraj bazę automatycznie",
            "Przy starcie, jeśli cache jest pusty",
            "auto_fetch_titles",
        )
        titles_group.add(self.auto_titles_row)

        self.url_row = Adw.EntryRow(title=_("Adres bazy (titles.txt / zip)"))
        self.url_row.set_text(settings.get("titles_url", ""))
        self.url_row.connect("changed", self._url_changed)
        titles_group.add(self.url_row)

        refresh_row = Adw.ActionRow(
            title=_("Odśwież bazę tytułów"),
            subtitle=_("Pobierz najnowsze nazwy gier z sieci"),
        )
        refresh_button = Gtk.Button(label=_("Pobierz"))
        refresh_button.add_css_class("flat")
        refresh_button.connect("clicked", lambda *_: self.on_refresh_titles())
        refresh_row.add_suffix(refresh_button)
        titles_group.add(refresh_row)

        self.titles_count_row = Adw.ActionRow(
            title=_("W pamięci"),
            subtitle=_("{count} tytułów").format(count=len(titles)),
        )
        titles_group.add(self.titles_count_row)
        page.add(titles_group)

        cache_group = Adw.PreferencesGroup(title=_("Dane"))
        cache_group.add(
            Adw.ActionRow(
                title=_("Cache okładek"), subtitle=str(self._cache_path())
            )
        )
        page.add(cache_group)
        self.add(page)

    def _cache_path(self) -> str:
        from ..core.settings import cache_dir

        return os.path.join(cache_dir(), "covers")

    def _switch_row(self, title: str, subtitle: str, key: str) -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=_(title), subtitle=_(subtitle))
        row.set_active(bool(self.settings.get(key)))
        row.connect("notify::active", lambda row, _p, k=key: self._toggle(k, row))
        return row

    def _toggle(self, key: str, row: Adw.SwitchRow) -> None:
        self.settings.set(key, row.get_active())
        if self.on_settings_changed is not None:
            self.on_settings_changed(key)

    def _scrub_changed(self, row, _param) -> None:
        mode = SCRUB_MODES[row.get_selected()][0]
        self.settings.set("scrub", mode)

    def _ui_language_changed(self, row, _param) -> None:
        code = "en" if row.get_selected() == 0 else "pl"
        self.settings.set("ui_language", code)
        # Keep the title database in the same language when possible.
        self.settings.set("language", code)
        self.titles.language = code
        self.titles.load()
        self.update_title_count()
        if self.on_settings_changed is not None:
            self.on_settings_changed("ui_language")

    def _language_changed(self, row, _param) -> None:
        languages = ["pl", "en", "de", "fr", "es", "it", "nl"]
        self.settings.set("language", languages[row.get_selected()])
        self.titles.language = self.settings.get("language", "en")
        self.titles.load()
        self.update_title_count()

    def _url_changed(self, row: Adw.EntryRow) -> None:
        self.settings.set("titles_url", row.get_text().strip())

    def update_title_count(self) -> None:
        self.titles_count_row.set_subtitle(
            _("{count} tytułów").format(count=len(self.titles))
        )


class ShortcutsWindow(Adw.Window):
    def __init__(self, parent):
        super().__init__(
            transient_for=parent, modal=True, title=_("Skróty klawiszowe")
        )
        self.set_default_size(480, 520)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title=_("Skróty"))
        shortcuts = [
            (_("Otwórz napęd"), "Ctrl+O"),
            (_("Dodaj gry z plików"), "Ctrl+N"),
            (_("Wypakuj zaznaczone"), "Ctrl+E"),
            (_("Usuń zaznaczone"), "Delete"),
            (_("Odśwież"), "F5"),
            (_("Ustawienia"), "Ctrl+,"),
            (_("Zamknij okno"), "Ctrl+W"),
        ]
        for title, accel in shortcuts:
            group.add(Adw.ActionRow(title=title, subtitle=accel))
        page.add(group)
        view.set_content(page)
        self.set_content(view)


class LibraryExportDialog(Adw.Window):
    """Choose a USB folder-library target and export options."""

    def __init__(self, parent, count: int, on_submit, source_label: str = ""):
        super().__init__(
            transient_for=parent,
            modal=True,
            title=_("Eksport do biblioteki USB"),
        )
        from ..core.library import (
            LOADER_PROFILES,
            OVERWRITE_MODES,
            LibraryTarget,
            detect_targets,
        )

        self._detect_targets = detect_targets
        self._target_class = LibraryTarget
        self.on_submit = on_submit
        self.count = count
        self.source_label = source_label
        self.targets = detect_targets()
        self.profile_keys = list(LOADER_PROFILES)
        self.overwrite_keys = list(OVERWRITE_MODES)
        self.set_default_size(640, 720)

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        cancel = Gtk.Button(label=_("Anuluj"))
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.start_button = Gtk.Button(label=_("Eksportuj"))
        self.start_button.add_css_class("suggested-action")
        self.start_button.connect("clicked", self._submit)
        header.pack_end(self.start_button)
        view.add_top_bar(header)

        page = Adw.PreferencesPage()

        target_group = Adw.PreferencesGroup(
            title=_("Dysk docelowy"),
            description=_(
                "Wybierz zamontowany dysk USB albo dowolny folder bazowy."
            ),
        )
        labels = [target.display_name for target in self.targets]
        if not labels:
            labels = [_("(brak wykrytych dysków — wybierz folder)")]
        self.target_row = Adw.ComboRow(
            title=_("Wykryty dysk"), model=Gtk.StringList.new(labels)
        )
        self.target_row.connect("notify::selected", self._target_changed)
        target_group.add(self.target_row)

        self.base_row = Adw.EntryRow(title=_("Folder bazowy"))
        browse = Gtk.Button(icon_name="folder-open-symbolic")
        browse.add_css_class("flat")
        browse.set_tooltip_text(_("Wybierz folder…"))
        browse.connect("clicked", self._choose_folder)
        self.base_row.add_suffix(browse)
        self.base_row.connect("notify::text", lambda *_: self._update_state())
        target_group.add(self.base_row)
        page.add(target_group)

        layout_group = Adw.PreferencesGroup(title=_("Układ biblioteki"))
        self.profile_row = Adw.ComboRow(
            title=_("Profil loadera"),
            model=Gtk.StringList.new(
                [_(LOADER_PROFILES[key]) for key in self.profile_keys]
            ),
        )
        layout_group.add(self.profile_row)
        self.root_wii_row = Adw.EntryRow(title=_("Folder Wii"))
        self.root_wii_row.set_text("wbfs")
        self.root_wii_row.connect("notify::text", lambda *_: self._update_state())
        layout_group.add(self.root_wii_row)
        self.root_gc_row = Adw.EntryRow(title=_("Folder GameCube (Nintendont)"))
        self.root_gc_row.set_text("games")
        self.root_gc_row.connect("notify::text", lambda *_: self._update_state())
        layout_group.add(self.root_gc_row)
        page.add(layout_group)

        conflict_group = Adw.PreferencesGroup(title=_("Konflikty"))
        self.overwrite_row = Adw.ComboRow(
            title=_("Istniejące gry"),
            model=Gtk.StringList.new(
                [_(OVERWRITE_MODES[key]) for key in self.overwrite_keys]
            ),
        )
        conflict_group.add(self.overwrite_row)
        page.add(conflict_group)

        info_group = Adw.PreferencesGroup(title=_("Podsumowanie"))
        summary = _("{count} gier").format(count=count)
        if source_label:
            summary += f" — {source_label}"
        self.summary_row = Adw.ActionRow(title=summary, subtitle="")
        info_group.add(self.summary_row)
        self.info_label = Gtk.Label(xalign=0.0, wrap=True)
        self.info_label.add_css_class("caption")
        self.info_label.add_css_class("dim-label")
        self.info_label.set_margin_top(6)
        info_group.add(self.info_label)
        self.warning_label = Gtk.Label(xalign=0.0, wrap=True)
        self.warning_label.add_css_class("caption")
        self.warning_label.set_margin_bottom(8)
        info_group.add(self.warning_label)
        page.add(info_group)

        view.set_content(page)
        self.set_content(view)
        if self.targets:
            self.base_row.set_text(self.targets[0].base_path)
        self._update_state()

    def _target_changed(self, row, _param) -> None:
        index = row.get_selected()
        if 0 <= index < len(self.targets):
            self.base_row.set_text(self.targets[index].base_path)
        self._update_state()

    def _choose_folder(self) -> None:
        dialog = Gtk.FileDialog(title=_("Folder bazowy biblioteki USB"))

        def chosen(dialog, result):
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error:
                return
            path = folder.get_path()
            if path:
                self.base_row.set_text(path)
                self._update_state()

        dialog.select_folder(self, None, chosen)

    def _update_state(self) -> None:
        import os

        base = self.base_row.get_text().strip()
        writable = False
        info = ""
        warning = ""
        if base:
            try:
                if not os.path.isdir(base):
                    parent = os.path.dirname(base) or "."
                    writable = os.path.isdir(parent) and os.access(parent, os.W_OK)
                    target = self._target_class.from_path(parent)
                    info = _(
                        "Folder zostanie utworzony. System plików: "
                        "{filesystem} · wolne {free}"
                    ).format(
                        filesystem=target.filesystem or "?",
                        free=human_size(target.free_bytes),
                    )
                else:
                    target = self._target_class.from_path(base)
                    writable = os.access(base, os.W_OK)
                    info = _(
                        "System plików: {filesystem} · wolne {free}"
                    ).format(
                        filesystem=target.filesystem or "?",
                        free=human_size(target.free_bytes),
                    )
                if target.filesystem in {"exfat", "ext4", "ext3", "btrfs", "xfs"}:
                    warning = _(
                        "USB Loader GX najpewniej nie odczyta tego systemu "
                        "plików; zalecany jest FAT32 lub NTFS."
                    )
                elif target.filesystem in {"vfat", "fat32", "msdos"}:
                    warning = _(
                        "FAT32: duże gry zostaną automatycznie podzielone "
                        "na .wbf1…"
                    )
            except Exception as error:
                info = str(error)
                writable = False
            root_wii = self.root_wii_row.get_text().strip()
            root_gc = self.root_gc_row.get_text().strip()
            if (
                not root_wii
                or not root_gc
                or root_wii.startswith(("/", "."))
                or root_gc.startswith(("/", "."))
                or "/" in root_wii
                or "\\" in root_wii
                or "/" in root_gc
                or "\\" in root_gc
                or ".." in root_wii
                or ".." in root_gc
            ):
                warning = _("Nazwy folderów muszą być względne (bez /, ..).")
                writable = False
            info = _(
                "Wii: {base}/{wii} · GameCube: {base}/{gc}"
            ).format(
                base=base,
                wii=root_wii or "wbfs",
                gc=root_gc or "games",
            ) + "\n" + info
        self.info_label.set_text(info)
        self.warning_label.set_text(warning)
        self.start_button.set_sensitive(bool(base) and writable)

    def _submit(self) -> None:
        import os

        base = self.base_row.get_text().strip()
        if not base:
            return
        try:
            os.makedirs(base, exist_ok=True)
        except OSError as error:
            show_error(
                self,
                _("Nie można utworzyć folderu docelowego: {error}").format(
                    error=error
                ),
            )
            return
        profile = self.profile_keys[self.profile_row.get_selected()]
        overwrite = self.overwrite_keys[self.overwrite_row.get_selected()]
        self.close()
        self.on_submit(
            base,
            profile,
            overwrite,
            self.root_wii_row.get_text().strip() or "wbfs",
            self.root_gc_row.get_text().strip() or "games",
        )
