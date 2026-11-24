"""Entry point: GUI by default, command line when a known command is given."""

from __future__ import annotations

import sys

from .cli import COMMANDS, main as cli_main


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in COMMANDS:
        return cli_main(args)
    if args and args[0] in {"--version", "-V"}:
        from .core.settings import APP_VERSION

        print(f"gor-wii-manager {APP_VERSION}")
        return 0
    if args and args[0] in {"--help", "-h"}:
        from .cli import build_parser

        build_parser().print_help()
        print("\nAby otworzyć GUI uruchom: gor-wii-manager")
        return 0
    from .ui.app import main as gui_main

    return gui_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
