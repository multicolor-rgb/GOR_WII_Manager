"""Static guard against unprefixed Gtk action names in the UI."""

from __future__ import annotations

import os
import re
import unittest

import gor_wii_manager


class UiActionTests(unittest.TestCase):
    def test_all_activate_action_names_are_prefixed(self) -> None:
        path = os.path.join(
            os.path.dirname(gor_wii_manager.__file__), "ui", "window.py"
        )
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        for match in re.finditer(r'activate_action\(\s*"([^"]+)"', source):
            name = match.group(1)
            self.assertTrue(
                name.startswith(("win.", "app.")),
                f"akcja bez prefiksu: {name}",
            )


if __name__ == "__main__":
    unittest.main()
