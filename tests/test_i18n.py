"""Tests for the English/Polish interface translation layer."""

from __future__ import annotations

import ast
import os
import re
import unittest

import gor_wii_manager
from gor_wii_manager.core import i18n
from gor_wii_manager.core.settings import DEFAULTS
from gor_wii_manager.core.util import DuplicateDiscError, region_name

POLISH = re.compile(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")


class I18nTests(unittest.TestCase):
    def tearDown(self) -> None:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)

    def test_default_language_is_english(self) -> None:
        self.assertEqual(i18n.DEFAULT_LANGUAGE, "en")
        self.assertEqual(DEFAULTS["ui_language"], "en")

    def test_english_translations(self) -> None:
        i18n.set_language("en")
        self.assertEqual(i18n._("Brak gier"), "No games")
        self.assertEqual(i18n._("Ustawienia"), "Preferences")
        self.assertEqual(region_name("J"), "Japan")
        self.assertEqual(region_name("E"), "USA")
        error = DuplicateDiscError("SMNE01", 0, "New Super Mario Bros. Wii")
        self.assertIn("already exists", str(error))

    def test_polish_returns_source_strings(self) -> None:
        i18n.set_language("pl")
        self.assertEqual(i18n._("Brak gier"), "Brak gier")
        self.assertEqual(i18n._("Ustawienia"), "Ustawienia")
        self.assertEqual(region_name("J"), "Japonia")

    def test_every_translated_polish_literal_has_an_english_entry(self) -> None:
        ui = os.path.join(os.path.dirname(gor_wii_manager.__file__), "ui")
        missing: list[str] = []
        for name in ("window.py", "dialogs.py", "games.py", "queue.py", "app.py"):
            path = os.path.join(ui, name)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not isinstance(node.func, ast.Name) or node.func.id != "_":
                    continue
                if not node.args or not isinstance(node.args[0], ast.Constant):
                    continue
                value = node.args[0].value
                if isinstance(value, str) and POLISH.search(value):
                    if value not in i18n._EN:
                        missing.append(f"{name}: {value!r}")
        self.assertEqual(missing, [], "brak tłumaczeń: " + "; ".join(missing))


if __name__ == "__main__":
    unittest.main()
