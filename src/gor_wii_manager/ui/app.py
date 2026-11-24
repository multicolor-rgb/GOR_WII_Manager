"""GTK application entry point and resources."""

from __future__ import annotations

import argparse
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ..core.i18n import _, set_language  # noqa: E402
from ..core.settings import (  # noqa: E402
    APP_ID,
    APP_NAME,
    APP_VERSION,
    Settings,
    data_dir,
)
from ..core.titles import TitleDB  # noqa: E402
from .window import MainWindow  # noqa: E402

ASSETS = os.path.join(os.path.dirname(__file__), "assets")


class GorWiiApplication(Adw.Application):
    __gtype_name__ = "GorWiiManagerApplication"

    def __init__(self, options: argparse.Namespace):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.options = options
        self.settings = Settings()
        set_language(self.settings.get("ui_language", "en"))
        self.titles = TitleDB(data_dir(), self.settings.get("language", "en"))
        self.titles.load()
        self.window: MainWindow | None = None
        self._titles_requested = False

    # ------------------------------------------------------------------
    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        GLib.set_prgname("gor-wii-manager")
        self._load_css()
        self._build_app_actions()

    def _load_css(self) -> None:
        path = os.path.join(ASSETS, "style.css")
        if not os.path.exists(path):
            return
        provider = Gtk.CssProvider()
        try:
            provider.load_from_path(path)
        except Exception:
            return
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _build_app_actions(self) -> None:
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._show_about)
        self.add_action(about_action)

    def _show_about(self, *_args) -> None:
        dialog = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=APP_VERSION,
            developer_name=_("Projekt GOR Wii Manager"),
            comments=_(
                "Menedżer kopii gier Wii i GameCube dla Linuksa. "
                "Tworzy i zarządza partycjami oraz plikami WBFS, "
                "konwertuje obrazy ISO i okładki z GameTDB."
            ),
            website="https://github.com/multicolor-rgb/GOR_WII_Manager",
            license_type=Gtk.License.GPL_2_0,
            developers=["GOR Wii Manager"],
            copyright="© 2026 GOR Wii Manager",
        )
        dialog.add_credit_section(
            _("Podziękowania"),
            [
                _("libwbfs (Kwiirk, oggzee) — format WBFS"),
                _("Wiimm (wit/wwt) — dokumentacja formatu"),
                _("GameTDB / WiiTDB — tytuły i okładki"),
            ],
        )
        dialog.present(self.window)

    def reload_window(self) -> None:
        """Recreate the main window so a new interface language is applied."""
        paths: list[str] = []
        old = self.window
        if old is not None:
            paths = [state.path for state in old.volumes.values()]
            try:
                old.queue.shutdown(wait=False)
                old.art.shutdown()
            except Exception:
                pass
            self.window = None
            old.destroy()
        self.window = MainWindow(self)
        for path in paths:
            GLib.idle_add(self.window.open_path, path)
        self.window.present()

    # ------------------------------------------------------------------
    def do_activate(self) -> None:
        if self.window is None:
            self.window = MainWindow(self)
            window = self.window
            if self.options.open:
                for path in self.options.open:
                    GLib.idle_add(window.open_path, path)
            elif getattr(self.options, "files", None):
                for path in self.options.files:
                    GLib.idle_add(window.open_path, path)
            if (
                len(self.titles) == 0
                and self.settings.get("auto_fetch_titles", True)
                and not self._titles_requested
            ):
                self._titles_requested = True
                GLib.timeout_add(1200, self._auto_fetch_titles)
        self.window.present()
        if self.options.smoke_test:
            GLib.timeout_add(4200, self._finish_smoke_test)

    def _auto_fetch_titles(self) -> bool:
        if self.window is not None:
            self.window._start_titles_refresh(silent=True)
        return False

    def _finish_smoke_test(self) -> bool:
        target = self.options.screenshot
        if target and self.window is not None:
            try:
                _save_screenshot(self.window, target)
                print(_("Zrzut ekranu zapisany: {path}").format(path=target))
            except Exception as error:
                print(
                    _("Nie udało się zapisać zrzutu ekranu: {error}").format(
                        error=error
                    ),
                    file=sys.stderr,
                )
        self.quit()
        return False


def _save_screenshot(window: Gtk.Window, path: str) -> None:
    import cairo

    width = max(window.get_width(), 1)
    height = max(window.get_height(), 1)
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot.new()
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None:
        raise RuntimeError("brak węzła renderowania")
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    context = cairo.Context(surface)
    node.draw(context)
    surface.write_to_png(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gor-wii-manager",
        description=_("Menedżer kopii gier Wii/GameCube (WBFS) dla Linuksa"),
    )
    parser.add_argument("files", nargs="*", help=_("pliki .wbfs do otwarcia"))
    parser.add_argument("--open", action="append", default=[], metavar="FILE")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--screenshot", default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    application = GorWiiApplication(args)
    return application.run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
