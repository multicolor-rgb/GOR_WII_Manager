"""Command line frontend for the WBFS engine (useful for scripts and tests)."""

from __future__ import annotations

import argparse
import os
import sys
import time

from .core.disc import IsoSource, open_disc_source
from .core.convert import convert_file
from .core.folder_library import FolderLibrary, create_library
from .core.library import (
    LOADER_PROFILES,
    OVERWRITE_MODES,
    LibraryExporter,
    LibraryItem,
    LibraryOptions,
    LibraryPlanner,
)
from .core.settings import APP_VERSION, data_dir
from .core.titles import TitleDB
from .core.util import (
    DuplicateDiscError,
    WbfsError,
    human_size,
    parse_size,
)
from .core.wbfs import WbfsVolume

COMMANDS = (
    "list",
    "info",
    "format",
    "import",
    "extract",
    "remove",
    "verify",
    "repair",
    "device-list",
    "titles",
    "convert",
    "export",
)


def _progress_printer(label: str):
    state = {"last": -1.0, "start": time.monotonic()}

    def callback(done: int, total: int) -> None:
        if not total:
            return
        now = time.monotonic()
        ratio = done / total
        if ratio * 100 >= state["last"] + 2 or ratio >= 1.0:
            state["last"] = ratio * 100
            elapsed = max(now - state["start"], 1e-6)
            speed = done / elapsed
            print(
                f"\r{label}: {ratio * 100:5.1f}%  {human_size(done)} / "
                f"{human_size(total)}  {human_size(speed)}/s",
                end="",
                file=sys.stderr,
                flush=True,
            )
            if ratio >= 1.0:
                print(file=sys.stderr)

    return callback


def _open(path: str, writable: bool = True) -> WbfsVolume:
    return WbfsVolume.open(path, read_only=not writable)


def cmd_list(args) -> int:
    library = os.path.isdir(args.target)
    with_volume = FolderLibrary(args.target) if library else _open(args.target, writable=False)
    titles = TitleDB(data_dir(), args.lang)
    titles.load()
    games = with_volume.games()
    games.sort(key=lambda entry: entry.title.lower())
    print(f"{'slot':>4}  {'ID':6}  {'typ':9}  {'rozmiar':>10}  tytuł")
    for entry in games:
        title = titles.get(entry.disc_id) or entry.title
        geometry = getattr(with_volume, "geometry_for", lambda _e: None)(entry)
        if geometry is None and not library:
            geometry = getattr(with_volume, "geometry", None)
        stored = human_size(entry.stored_size(geometry))
        print(
            f"{entry.slot:>4}  {entry.disc_id:6}  {entry.kind:9}  {stored:>10}  {title}"
        )
    if library:
        print(
            f"\n{len(games)} gier w bibliotece, wolne "
            f"{human_size(with_volume.free_bytes)} z "
            f"{human_size(with_volume.capacity_bytes)}"
        )
    else:
        print(
            f"\n{len(games)} gier, wolne {human_size(with_volume.free_bytes)} "
            f"z {human_size(with_volume.capacity_bytes)} "
            f"(blok {human_size(with_volume.geometry.wbfs_sec_sz)})"
        )
    with_volume.close()
    return 0


def cmd_info(args) -> int:
    if os.path.isdir(args.target):
        library = FolderLibrary(args.target)
        print(f"Biblioteka:         {library.path}")
        print(f"Katalog Wii:        {library.wii_root}")
        print(f"Katalog GameCube:   {library.gc_root}")
        print(f"System plików:      {library.filesystem or '?'}")
        print(f"Gry:                {library.game_count}")
        print(f"Wolne:              {human_size(library.free_bytes)}")
        print(f"Pojemność:          {human_size(library.capacity_bytes)}")
        library.close()
        return 0
    volume = _open(args.target, writable=False)
    geometry = volume.geometry
    print(f"Ścieżka:            {volume.path}")
    print(f"Rozmiar:            {human_size(geometry.total_bytes)}")
    print(f"Sektor hosta:       {geometry.hd_sec_sz} B")
    print(f"Blok WBFS:          {human_size(geometry.wbfs_sec_sz)}")
    print(f"Bloków:             {geometry.n_wbfs_sec}")
    print(f"Maks. gier:         {geometry.max_disc}")
    print(f"Gry:                {volume.game_count}")
    print(f"Wolne:              {human_size(volume.free_bytes)}")
    print(f"Tabela gier offset: 0x{geometry.slot_offset:x}")
    print(f"Bitmapa offset:     0x{geometry.freeblks_lba * geometry.hd_sec_sz:x}")
    volume.close()
    return 0


def cmd_format(args) -> int:
    if args.device:
        volume = WbfsVolume.format_device(
            args.target,
            block_size=int(args.block_size) if args.block_size else None,
        )
    else:
        if not args.size:
            raise SystemExit("Podaj --size dla nowego pliku WBFS")
        volume = WbfsVolume.create(
            args.target,
            int(args.size),
            block_size=int(args.block_size) if args.block_size else None,
            split=args.split,
        )
    print(
        f"Utworzono WBFS: {args.target}\n"
        f"  rozmiar: {human_size(volume.geometry.total_bytes)}, "
        f"blok: {human_size(volume.geometry.wbfs_sec_sz)}, "
        f"maks. gier: {volume.geometry.max_disc}"
    )
    volume.close()
    return 0


def _import_one(target: WbfsVolume, source, args) -> list[str]:
    copied = []
    entries = [None]
    if isinstance(source, WbfsVolume):
        entries = source.games()
        labels = [(entry.disc_id, source.source(entry)) for entry in entries]
    else:
        labels = [(source.info.disc_id, source)]
    for disc_id, disc_source in labels:
        try:
            entry = target.add_disc(
                disc_source,
                mode=args.scrub,
                progress=_progress_printer(disc_id),
                replace=args.replace,
            )
        except DuplicateDiscError as error:
            print(f"Pominięto {error}", file=sys.stderr)
            continue
        copied.append(entry.disc_id)
        geometry = getattr(target, "geometry_for", lambda _e: None)(entry)
        if geometry is None:
            geometry = getattr(target, "geometry", None)
        print(
            f"Zaimportowano {entry.disc_id} ({entry.title}) "
            f"{human_size(entry.stored_size(geometry))}",
            file=sys.stderr,
        )
    return copied


def cmd_import(args) -> int:
    target = FolderLibrary(args.target) if os.path.isdir(args.target) else _open(args.target, writable=True)
    try:
        for path in args.images:
            if path.lower().endswith(".wbfs"):
                source = WbfsVolume.open(path, read_only=True)
                try:
                    _import_one(target, source, args)
                finally:
                    source.close()
            else:
                with open_disc_source(path) as source:
                    _import_one(target, source, args)
    finally:
        target.close()
    return 0


def cmd_extract(args) -> int:
    library = os.path.isdir(args.target)
    volume = FolderLibrary(args.target) if library else _open(args.target, writable=False)
    entry = volume.find_game(args.game_id)
    if entry is None:
        print(f"Nie znaleziono gry {args.game_id}", file=sys.stderr)
        volume.close()
        return 1
    destination = args.destination
    try:
        if args.wbfs:
            if library:
                volume.extract_wbfs(
                    entry,
                    destination,
                    split=args.split,
                    progress=_progress_printer(entry.disc_id),
                )
            else:
                volume.extract_wbfs(
                    entry,
                    destination,
                    split=args.split,
                    progress=_progress_printer(entry.disc_id),
                )
        else:
            if library:
                volume.extract_iso(
                    entry,
                    destination,
                    size=int(args.size) if args.size else None,
                    progress=_progress_printer(entry.disc_id),
                )
            else:
                volume.extract_iso(
                    entry,
                    destination,
                    progress=_progress_printer(entry.disc_id),
                    size=int(args.size) if args.size else None,
                )
    except WbfsError as error:
        print(f"Błąd: {error}", file=sys.stderr)
        volume.close()
        return 1
    print(f"Zapisano {destination}", file=sys.stderr)
    volume.close()
    return 0


def cmd_remove(args) -> int:
    library = os.path.isdir(args.target)
    volume = FolderLibrary(args.target) if library else _open(args.target, writable=True)
    entry = volume.find_game(args.game_id)
    if entry is None:
        print(f"Nie znaleziono gry {args.game_id}", file=sys.stderr)
        volume.close()
        return 1
    if not args.yes:
        answer = input(f"Usunąć {entry.title} [{entry.disc_id}]? [t/N] ")
        if answer.strip().lower() not in {"t", "tak", "y", "yes"}:
            volume.close()
            return 1
    if library:
        volume.remove_game(entry)
    else:
        volume.remove_game(entry.slot)
    volume.close()
    print(f"Usunięto {entry.disc_id}")
    return 0


def cmd_verify(args) -> int:
    volume = _open(args.target, writable=False)
    report = volume.verify(deep=args.deep)
    if args.json:
        import json

        print(
            json.dumps(
                {
                    "ok": report.ok,
                    "errors": report.errors,
                    "warnings": report.warnings,
                    "duplicate_blocks": report.duplicate_blocks,
                    "bitmap_mismatch": len(report.bitmap_mismatch),
                    "invalid_slots": report.invalid_slots,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for warning in report.warnings:
            print(f"OSTRZEŻENIE: {warning}")
        for error in report.errors:
            print(f"BŁĄD: {error}")
        print("Struktura WBFS jest poprawna." if report.ok else "Wykryto problemy.")
    volume.close()
    return 0 if report.ok else 1


def cmd_repair(args) -> int:
    volume = _open(args.target, writable=True)
    changes = volume.repair(clear_invalid=args.clear_invalid)
    volume.close()
    print(f"Naprawiono {changes} wpisów tablicy zajętości.")
    return 0


def cmd_convert(args) -> int:
    output = args.output
    target = args.to
    if target is None:
        lowered = output.lower()
        if lowered.endswith(".wbfs"):
            target = "wbfs"
        elif lowered.endswith(".iso"):
            target = "iso"
        else:
            target = "iso"
            output += ".iso"
    if target == "wbfs" and not output.lower().endswith(".wbfs"):
        output += ".wbfs"
    if target == "iso" and not output.lower().endswith(".iso"):
        output += ".iso"
    split = args.split
    if target == "wbfs" and not split:
        directory = os.path.dirname(output) or "."
        try:
            from .core.util import is_fat

            split = is_fat(directory)
        except Exception:
            split = False
    try:
        convert_file(
            args.input,
            output,
            mode=args.scrub,
            split=split,
            progress=_progress_printer(os.path.basename(args.input)),
        )
    except WbfsError as error:
        print(f"Błąd: {error}", file=sys.stderr)
        return 1
    print(f"Zapisano {output}", file=sys.stderr)
    return 0


def cmd_export(args) -> int:
    options = LibraryOptions(
        base_path=args.target,
        profile=args.profile,
        root_wii=args.root_wii,
        root_gc=args.root_gc,
        overwrite=args.overwrite,
        scrub=args.scrub,
    )
    titles = TitleDB(data_dir(), args.lang)
    titles.load()
    items = []
    volume = None
    try:
        if args.source:
            if os.path.isdir(args.source):
                library = FolderLibrary(args.source)
                entries = library.games()
                if args.ids:
                    wanted = {
                        item.strip().upper()
                        for item in args.ids.split(",")
                        if item.strip()
                    }
                    entries = [
                        entry for entry in entries if entry.disc_id.upper() in wanted
                    ]
                if not entries:
                    print("Brak gier do eksportu w bibliotece.", file=sys.stderr)
                    return 1
                for entry in entries:
                    items.append(
                        LibraryItem.from_folder_game(
                            library,
                            entry,
                            titles.get(entry.disc_id) or entry.title,
                        )
                    )
            else:
                volume = WbfsVolume.open(args.source, read_only=True)
                entries = volume.games()
                if args.ids:
                    wanted = {
                        item.strip().upper()
                        for item in args.ids.split(",")
                        if item.strip()
                    }
                    entries = [
                        entry for entry in entries if entry.disc_id.upper() in wanted
                    ]
                if not entries:
                    print("Brak gier do eksportu w wybranym napędzie.", file=sys.stderr)
                    return 1
                for entry in entries:
                    items.append(
                        LibraryItem.from_wbfs(
                            volume,
                            entry,
                            titles.get(entry.disc_id) or entry.title,
                        )
                    )
        for path in args.files:
            item = LibraryItem.from_file(path)
            item.title = titles.get(item.disc_id) or item.title
            items.append(item)
        if not items:
            print("Nie wybrano żadnych gier ani plików.", file=sys.stderr)
            return 1

        planner = LibraryPlanner(options)
        plan = planner.plan(items)
        total = sum(entry.estimate for entry in plan if entry.action == "write")
        print(
            f"Docelowo: {len(plan)} gier, około {human_size(total)}, "
            f"wolne {human_size(planner.target.free_bytes)}"
        )
        for entry in plan:
            prefix = "NADPISZ" if entry.existing else "EKSPORT"
            print(f"  {prefix}: {entry.destination}")
        if args.dry_run:
            return 0
        if options.overwrite == "ask" and any(entry.existing for entry in plan):
            answer = input("Nadpisać istniejące gry? [t/N] ")
            if answer.strip().lower() not in {"t", "tak", "y", "yes"}:
                for entry in plan:
                    if entry.existing:
                        entry.action = "skip"
        exporter = LibraryExporter(options)
        result = exporter.export_plan(
            plan, progress=_progress_printer("eksport")
        )
        print(
            f"Wyeksportowano {result.exported} (nadpisano {result.overwritten}), "
            f"pominięto {result.skipped}, błędy {result.failed}"
        )
        for error in result.errors:
            print(f"BŁĄD: {error}", file=sys.stderr)
        return 0 if result.failed == 0 else 1
    finally:
        if volume is not None:
            volume.close()


def cmd_device_list(args) -> int:
    from .core.device import format_candidates, wbfs_devices

    print("Napędy WBFS:")
    devices = wbfs_devices()
    if not devices:
        print("  (brak)")
    for device in devices:
        print(
            f"  {device.path}  {human_size(device.size)}  "
            f"{device.label or device.model or ''} {device.mountpoint}"
        )
    print("\nPartycje, które można sformatować:")
    candidates = format_candidates()
    if not candidates:
        print("  (brak)")
    for device in candidates:
        print(
            f"  {device.path}  {human_size(device.size)}  "
            f"{device.fstype or 'brak systemu'}  {device.model}"
        )
    return 0


def cmd_titles(args) -> int:
    database = TitleDB(data_dir(), args.lang)
    if args.refresh:
        count, info = database.refresh(args.url, progress=_progress_printer("tytuły"))
        print(info)
    else:
        count = database.load()
        print(f"Załadowano {count} tytułów z cache")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gor-wii-manager",
        description="Menedżer kopii gier Wii/GameCube (WBFS) dla Linuksa",
        epilog="Uruchom bez argumentów, aby otworzyć interfejs graficzny.",
    )
    parser.add_argument("--version", action="version", version=APP_VERSION)
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("list", help="lista gier na napędzie")
    p.add_argument("target")
    p.add_argument("--lang", default="pl")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("info", help="informacje o napędzie WBFS")
    p.add_argument("target")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("format", help="utwórz lub sformatuj WBFS")
    p.add_argument("target")
    p.add_argument("--size", type=parse_size, default=None)
    p.add_argument("--block-size", type=int, default=None)
    p.add_argument("--split", action="store_true", help="plik dzielony .wbfs/.wbf1...")
    p.add_argument("--device", action="store_true", help="formatuj urządzenie blokowe")
    p.set_defaults(func=cmd_format)

    p = sub.add_parser("import", help="dodaj obrazy ISO/GCM lub gry z innego WBFS")
    p.add_argument("target")
    p.add_argument("images", nargs="+")
    p.add_argument(
        "--scrub",
        choices=("all", "remove_update", "only_game"),
        default="remove_update",
    )
    p.add_argument("--replace", action="store_true")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("extract", help="wypakuj grę do ISO lub pojedynczego WBFS")
    p.add_argument("target")
    p.add_argument("game_id")
    p.add_argument("destination")
    p.add_argument("--wbfs", action="store_true")
    p.add_argument("--split", action="store_true")
    p.add_argument("--size", type=int, default=None)
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("remove", help="usuń grę z napędu")
    p.add_argument("target")
    p.add_argument("game_id")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("verify", help="sprawdź spójność WBFS")
    p.add_argument("target")
    p.add_argument("--deep", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("repair", help="napraw tablicę wolnych bloków")
    p.add_argument("target")
    p.add_argument("--clear-invalid", action="store_true")
    p.set_defaults(func=cmd_repair)

    p = sub.add_parser(
        "convert", help="konwertuj ISO/GCM/RVZ do ISO lub pojedynczego WBFS"
    )
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--to", choices=("iso", "wbfs"), default=None)
    p.add_argument("--split", action="store_true")
    p.add_argument(
        "--scrub",
        choices=("all", "remove_update", "only_game"),
        default="remove_update",
    )
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser(
        "export",
        help="eksportuj gry do biblioteki USB (GX/WiiFlow/CFG/Nintendont)",
    )
    p.add_argument("target", help="katalog bazowy biblioteki (np. /media/usb)")
    p.add_argument("source", nargs="?", help="plik/partycja WBFS źródłowa")
    p.add_argument(
        "--file",
        dest="files",
        action="append",
        default=[],
        metavar="PLIK",
        help="dodatkowy plik ISO/GCM/RVZ do eksportu (można powtarzać)",
    )
    p.add_argument("--ids", default=None, help="lista ID rozdzielona przecinkami")
    p.add_argument("--all", action="store_true", help="wszystkie gry z napędu")
    p.add_argument(
        "--profile",
        choices=tuple(LOADER_PROFILES),
        default="auto",
    )
    p.add_argument("--root-wii", default="wbfs")
    p.add_argument("--root-gc", default="games")
    p.add_argument(
        "--overwrite", choices=tuple(OVERWRITE_MODES), default="replace"
    )
    p.add_argument(
        "--scrub",
        choices=("all", "remove_update", "only_game"),
        default="remove_update",
    )
    p.add_argument("--lang", default="pl")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("device-list", help="pokaż napędy i partycje")
    p.set_defaults(func=cmd_device_list)

    p = sub.add_parser("titles", help="baza tytułów WiiTDB")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--lang", default="pl")
    p.add_argument("--url", default="https://www.gametdb.com/wiitdb.zip")
    p.set_defaults(func=cmd_titles)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except WbfsError as error:
        print(f"Błąd: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nPrzerwano.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
