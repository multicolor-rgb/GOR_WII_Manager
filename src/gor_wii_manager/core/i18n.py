"""Lightweight translation helper for the GTK interface.

Polish strings are used as message IDs in the source code.  English is the
default interface language; when Polish is selected the source string is
returned unchanged.  Unknown strings fall back to their source form so the
interface never shows empty labels.
"""

from __future__ import annotations

LANGUAGES = {"en": "English", "pl": "Polski"}
DEFAULT_LANGUAGE = "en"

_current = DEFAULT_LANGUAGE

# Polish message ID -> English translation.
_EN: dict[str, str] = {
    # --- main window / sidebar -------------------------------------------
    "Wybierz napęd": "Select a drive",
    "Dodaj gry z plików (Ctrl+N)": "Add games from files (Ctrl+N)",
    "Odśwież (F5)": "Refresh (F5)",
    "Menu": "Menu",
    "Otwórz plik .wbfs…": "Open .wbfs file…",
    "Formatuj napęd…": "Format drive…",
    "Otwórz bibliotekę USB…": "Open USB library…",
    "Nowa biblioteka USB…": "New USB library…",
    "Szukaj gry…": "Search games…",
    "Wypakuj zaznaczone (Ctrl+E)": "Extract selected (Ctrl+E)",
    "Kopiuj zaznaczone na inny napęd": "Copy selected to another drive",
    "Usuń zaznaczone (Delete)": "Delete selected (Delete)",
    "Zarządzaj kopiami gier Wii i GameCube na partycjach i plikach WBFS.":
        "Manage Wii and GameCube game backups on WBFS partitions and files.",
    "Otwórz napęd WBFS…": "Open WBFS drive…",
    "Utwórz nowy plik WBFS…": "Create a new WBFS file…",
    "Wykryj podłączone napędy": "Detect connected drives",
    "Wczytywanie gier…": "Loading games…",
    "Brak gier": "No games",
    "Ten napęd nie zawiera jeszcze żadnych gier.":
        "This drive does not contain any games yet.",
    "Dodaj gry…": "Add games…",
    "Tytuł": "Title",
    "Rozmiar": "Size",
    "Płyta": "Disc",
    "Przeniesiono dane z wbfs-manager: ":
        "Migrated data from wbfs-manager: ",
    "Otwarte napędy": "Open drives",
    "Wykryte napędy WBFS": "Detected WBFS drives",
    "Partycje do sprawdzenia (WBFS?)": "Partitions to check (WBFS?)",
    "Biblioteki USB": "USB libraries",
    "Ostatnio otwierane": "Recently opened",
    "Pliki WBFS na dyskach": "WBFS files on disks",
    "Brak napędów. Podłącz dysk lub otwórz plik .wbfs.":
        "No drives. Connect a disk or open a .wbfs file.",
    "WBFS na dysku USB: podłącz napęd i naciśnij F5.":
        "WBFS on USB: connect a drive and press F5.",
    " · tylko odczyt": " · read-only",
    "brak systemu plików": "no filesystem",
    "brak systemu": "no filesystem",
    "To nie jest biblioteka USB.": "This is not a USB library.",
    "W wybranym folderze nie ma katalogu wbfs/ ani games/.":
        "The selected folder has no wbfs/ or games/ directory.",
    "Brak katalogów wbfs/ i games/.": "No wbfs/ or games/ directories.",
    "Napęd otwarto tylko do odczytu (brak uprawnień do zapisu).":
        "Drive opened read-only (no write permission).",
    "Nie można wczytać listy gier.": "Cannot load the game list.",
    "Wczytywanie…": "Loading…",
    "Najpierw otwórz napęd WBFS.": "Open a WBFS drive first.",
    "Ten napęd jest otwarty tylko do odczytu.":
        "This drive is opened read-only.",
    "Trwa operacja na tym napędzie — poczekaj na jej zakończenie.":
        "An operation is running on this drive — wait for it to finish.",
    "Dodaj obrazy gier": "Add game images",
    "Obrazy Wii/GameCube": "Wii/GameCube images",
    "Wszystkie pliki": "All files",
    "Dodaj gry z folderu": "Add games from a folder",
    "W tym folderze nie znaleziono obrazów gier.":
        "No game images found in this folder.",
    "Wypakuj grę do ISO": "Extract game to ISO",
    "Wypakuj gry do folderu": "Extract games to a folder",
    "Nie można zapisać ISO większego niż 4 GiB na partycji FAT32.":
        "Cannot save an ISO larger than 4 GiB on a FAT32 partition.",
    "Wybierz inny folder albo użyj opcji „Wypakuj jako WBFS”, "
    "która tworzy dzielony plik .wbfs/.wbf1 zgodny z FAT32.":
        "Choose another folder or use “Extract as WBFS”, which creates a "
        "split .wbfs/.wbf1 file compatible with FAT32.",
    "ISO nie zmieści się na partycji FAT32.":
        "The ISO will not fit on a FAT32 partition.",
    "Wypakowywanie 1 gry": "Extracting 1 game",
    "Zapisz jako plik WBFS": "Save as WBFS file",
    "Obraz WBFS": "WBFS image",
    "Zapisz gry jako pliki WBFS": "Save games as WBFS files",
    "Zapisywanie WBFS": "Saving WBFS",
    "Usunąć gry?": "Delete games?",
    "Usuń": "Delete",
    "Ten napęd jest teraz otwarty.": "This drive is currently open.",
    "Zamknij go (Ctrl+Shift+W) przed formatowaniem.":
        "Close it (Ctrl+Shift+W) before formatting.",
    "Plik już istnieje": "File already exists",
    "Nadpisz": "Overwrite",
    "Napęd jest poprawny": "Drive is valid",
    "Struktura WBFS i tablica wolnych bloków są spójne.":
        "The WBFS structure and free-block table are consistent.",
    "Zamknij": "Close",
    "Wykryto problemy": "Problems detected",
    "Napraw": "Repair",
    "Weryfikacja jest dostępna dla napędów WBFS, nie dla bibliotek.":
        "Verification is available for WBFS drives, not libraries.",
    "Naprawa tablicy bloków dotyczy tylko napędów WBFS.":
        "Block table repair applies only to WBFS drives.",
    "Naprawić tablicę bloków?": "Repair the block table?",
    "Tablica wolnych bloków zostanie odtworzona na podstawie wpisów gier.":
        "The free-block table will be rebuilt from the game entries.",
    "Pobieranie bazy tytułów": "Downloading title database",
    "Wybierz obrazy do konwersji": "Select images to convert",
    "Obrazy RCZ/RVZ/ISO": "RCZ/RVZ/ISO images",
    "Zapisz obraz": "Save image",
    "Obraz ISO": "ISO image",
    "Wybierz folder docelowy": "Choose destination folder",
    "Brak poprawnych gier do eksportu.": "No valid games to export.",
    "Ten napęd nie zawiera gier do eksportu.":
        "This drive contains no games to export.",
    "Eksportuj pliki do biblioteki USB": "Export files to USB library",
    "Obrazy ISO/GCM/RVZ": "ISO/GCM/RVZ images",
    "Nie udało się odczytać części plików.":
        "Some files could not be read.",
    "Nie można przygotować eksportu.": "Cannot prepare the export.",
    "Nadpisać istniejące gry?": "Overwrite existing games?",
    "Znaleziono {count} napędów WBFS.": "Found {count} WBFS drives.",
    "Nie znaleziono napędów WBFS.": "No WBFS drives found.",
    "Nie udało się wykonać: {title}": "Failed: {title}",
    "Naprawiono {count} wpisów tablicy.":
        "Repaired {count} block table entries.",
    "Utworzono nowy napęd WBFS.": "Created a new WBFS drive.",
    "Sformatowano partycję WBFS.": "Formatted the WBFS partition.",
    "Zapisano {count} plik(ów).": "Saved {count} file(s).",
    "Nie udało się wyeksportować {count} gier.":
        "Failed to export {count} games.",
    "Eksport: {exported} gier (nadpisano {overwritten}, pominięto {skipped}).":
        "Export: {exported} games (overwritten {overwritten}, skipped {skipped}).",
    "Baza tytułów została odświeżona.": "Title database refreshed.",
    "Część operacji zakończyła się błędem.": "Some operations failed.",
    "Pominięto {count} istniejących gier.":
        "Skipped {count} existing games.",
    "Skopiowano gry do drugiego napędu.":
        "Games copied to the other drive.",
    "Usunięto {count} gier.": "Deleted {count} games.",
    "Zapisano obraz gry.": "Game image saved.",
    "Operacja anulowana.": "Operation cancelled.",
    "operacja anulowana": "operation cancelled",
    "Nie można utworzyć biblioteki: {error}":
        "Cannot create the library: {error}",
    "Folder biblioteki nie istnieje: {base}":
        "Library folder does not exist: {base}",
    "Nie można odczytać rozmiaru urządzenia":
        "Cannot read the device size",
    "nie można odczytać rozmiaru urządzenia":
        "cannot read the device size",
    "Anulowano autoryzację administratora lub brak pkexec.":
        "Administrator authorization was cancelled or pkexec is missing.",
    "pkexec zwrócił kod {code}": "pkexec returned code {code}",

    # --- menu ------------------------------------------------------------
    "Dodaj gry z folderu…": "Add games from a folder…",
    "Wypakuj jako WBFS…": "Extract as WBFS…",
    "Zamknij napęd": "Close drive",
    "Konwertuj RVZ/ISO do ISO…": "Convert RVZ/ISO to ISO…",
    "Konwertuj RVZ/ISO do pliku WBFS…": "Convert RVZ/ISO to a WBFS file…",
    "Otwórz bibliotekę USB…": "Open USB library…",
    "Nowa biblioteka USB…": "New USB library…",
    "Eksportuj zaznaczone do biblioteki USB…":
        "Export selected to USB library…",
    "Eksportuj pliki ISO/GCM/RVZ…": "Export ISO/GCM/RVZ files…",
    "Synchronizuj bibliotekę USB…": "Synchronize USB library…",
    "Sprawdź spójność napędu": "Check drive consistency",
    "Napraw tablicę bloków": "Repair block table",
    "Formatuj nowy napęd…": "Format a new drive…",
    "Odśwież bazę tytułów": "Refresh title database",
    "Skróty klawiszowe": "Keyboard shortcuts",
    "Ustawienia": "Preferences",
    "O programie": "About",

    # --- dialogs ---------------------------------------------------------
    "Wystąpił problem": "A problem occurred",
    "Wykonaj": "Run",
    "Anuluj": "Cancel",
    "Formatowanie WBFS": "WBFS formatting",
    "Formatuj": "Format",
    "Miejsce docelowe": "Destination",
    "Nowy plik .wbfs": "New .wbfs file",
    "Partycja urządzenia": "Device partition",
    "Plik WBFS": "WBFS file",
    "Ścieżka pliku": "File path",
    "Wybierz plik…": "Choose file…",
    "Rozmiar pliku (GiB)": "File size (GiB)",
    "Urządzenie": "Device",
    "Odśwież listę urządzeń": "Refresh device list",
    "Odśwież": "Refresh",
    "Opcje": "Options",
    "Rozmiar bloku": "Block size",
    "Formatowanie usuwa wszystkie dane z wybranego miejsca. "
    "Nie można tego cofnąć.":
        "Formatting erases all data in the selected location. "
        "This cannot be undone.",
    "Rozumiem, że wszystkie dane zostaną usunięte":
        "I understand that all data will be deleted",
    "(brak wolnych partycji — podłącz dysk USB)":
        "(no free partitions — connect a USB drive)",
    "Nowy obraz WBFS": "New WBFS image",
    "Podaj ścieżkę pliku WBFS.": "Enter a WBFS file path.",
    "Brak urządzeń do sformatowania.": "No devices to format.",
    "Obrazy WBFS": "WBFS images",
    "Brak otwartego napędu docelowego.": "No destination drive is open.",
    "Otwórz najpierw drugi plik .wbfs lub podłącz drugi dysk WBFS, "
    "a następnie powtórz kopiowanie.":
        "First open another .wbfs file or connect a second WBFS drive, "
        "then repeat the copy.",
    "Kopiuj gry": "Copy games",
    "Wybierz napęd docelowy. Istniejące gry o tych samych ID zostaną pominięte.":
        "Choose the destination drive. Existing games with the same IDs "
        "will be skipped.",
    "Kopiuj": "Copy",
    "Gra": "Game",
    "Numer płyty": "Disc number",
    "Rozmiar w napędzie": "Size on drive",
    "Bloki WBFS": "WBFS blocks",
    "Szacowany ISO": "Estimated ISO",
    "Nazwa w napędzie": "Name on drive",
    "Zmień": "Rename",
    "Partycje": "Partitions",
    "Nie można odczytać partycji": "Cannot read partitions",
    "Ogólne": "General",
    "Import gier": "Game import",
    "Czyszczenie obrazów": "Image scrubbing",
    "Które partycje zachować podczas kopiowania na WBFS":
        "Which partitions to keep when copying to WBFS",
    "Pomiń partycję aktualizacji (zalecane)":
        "Skip update partition (recommended)",
    "Tylko partycja gry": "Game partition only",
    "Zachowaj wszystkie partie": "Keep all partitions",
    "Zachowaj wszystkie partycje": "Keep all partitions",
    "Zastępuj istniejące gry": "Replace existing games",
    "Nadpisuj grę o tym samym ID zamiast ją pomijać":
        "Overwrite a game with the same ID instead of skipping it",
    "Weryfikuj po imporcie": "Verify after import",
    "Sprawdzaj spójność WBFS po każdej operacji zapisu":
        "Check WBFS consistency after every write",
    "Wygląd": "Appearance",
    "Pobieraj okładki": "Download covers",
    "Miniatury z GameTDB (wymaga internetu, cache lokalny)":
        "Thumbnails from GameTDB (requires internet, local cache)",
    "Baza tytułów": "Title database",
    "Tytuły gier z WiiTDB (GameTDB) są używane zamiast nazw z nagłówka płyty.":
        "Game titles from WiiTDB (GameTDB) are used instead of names from "
        "the disc header.",
    "Język bazy": "Database language",
    "Język interfejsu": "Interface language",
    "Pobieraj bazę automatycznie": "Download database automatically",
    "Przy starcie, jeśli cache jest pusty":
        "At startup if the cache is empty",
    "Adres bazy (titles.txt / zip)": "Database URL (titles.txt / zip)",
    "Odśwież bazę tytułów": "Refresh title database",
    "Pobierz najnowsze nazwy gier z sieci":
        "Download the latest game names from the network",
    "Pobierz": "Download",
    "W pamięci": "In memory",
    "Dane": "Data",
    "Cache okładek": "Cover cache",
    "Skróty": "Shortcuts",
    "Otwórz napęd": "Open drive",
    "Dodaj gry z plików": "Add games from files",
    "Wypakuj zaznaczone": "Extract selected",
    "Usuń zaznaczone": "Delete selected",
    "Zamknij okno": "Close window",
    "Eksport do biblioteki USB": "Export to USB library",
    "Dysk docelowy": "Destination drive",
    "Wybierz zamontowany dysk USB albo dowolny folder bazowy.":
        "Choose a mounted USB drive or any base folder.",
    "(brak wykrytych dysków — wybierz folder)":
        "(no drives detected — choose a folder)",
    "Wykryty dysk": "Detected drive",
    "Folder bazowy": "Base folder",
    "Wybierz folder…": "Choose folder…",
    "Układ biblioteki": "Library layout",
    "Profil loadera": "Loader profile",
    "Folder Wii": "Wii folder",
    "Folder GameCube (Nintendont)": "GameCube folder (Nintendont)",
    "Konflikty": "Conflicts",
    "Istniejące gry": "Existing games",
    "Podsumowanie": "Summary",
    "Folder bazowy biblioteki USB": "USB library base folder",
    "Folder zostanie utworzony. System plików: {filesystem} · wolne {free}":
        "The folder will be created. Filesystem: {filesystem} · free {free}",
    "System plików: {filesystem} · wolne {free}":
        "Filesystem: {filesystem} · free {free}",
    "USB Loader GX najpewniej nie odczyta tego systemu plików; "
    "zalecany jest FAT32 lub NTFS.":
        "USB Loader GX most likely cannot read this filesystem; "
        "FAT32 or NTFS is recommended.",
    "FAT32: duże gry zostaną automatycznie podzielone na .wbf1…":
        "FAT32: large games will be split into .wbf1… automatically",
    "Nazwy folderów muszą być względne (bez /, ..).":
        "Folder names must be relative (no /, ..).",
    "Nie można utworzyć folderu docelowego: {error}":
        "Cannot create the destination folder: {error}",

    # --- queue -----------------------------------------------------------
    "Oczekuje": "Queued",
    "W toku": "Running",
    "Gotowe": "Done",
    "Błąd": "Error",
    "Anulowano": "Cancelled",
    "Kolejka zadań": "Task queue",
    "Wyczyść": "Clear",
    "Ukryj kolejkę": "Hide queue",
    "1 zadanie w toku": "1 task running",
    "{count} w toku": "{count} running",
    "{time} pozostało": "{time} left",
    "Błąd: {error}": "Error: {error}",

    # --- list cells ------------------------------------------------------
    "Płyta {number}": "Disc {number}",
    "Japonia": "Japan",
    "Europa": "Europe",
    "Niemcy": "Germany",
    "Francja": "France",
    "Włochy": "Italy",
    "Hiszpania": "Spain",
    "Rosja": "Russia",
    "Tajwan": "Taiwan",
    "Chiny": "China",

    # --- templates -------------------------------------------------------
    "{count} gier · wolne {free}": "{count} games · free {free}",
    "wolne {free}": "free {free}",
    "{path} · {size} · brak systemu plików":
        "{path} · {size} · no filesystem",
    "{path} · {filesystem} · wolne {free}":
        "{path} · {filesystem} · free {free}",
    "{count} gier · wolne {free} z {total}":
        "{count} games · free {free} of {total}",
    "Import: {name}": "Import: {name}",
    "Import {count} plików": "Import {count} files",
    "Wypakowywanie {count} gier": "Extracting {count} games",
    "Zapisywanie {count} plików WBFS": "Saving {count} WBFS files",
    "Kopiowanie {count} gier": "Copying {count} games",
    "Zostanie usuniętych {count} gier: {names}.":
        "{count} games will be deleted: {names}.",
    " i {count} więcej": " and {count} more",
    "Usuwanie {count} gier": "Deleting {count} games",
    "Nadpisać {path}? Wszystkie dane w tym pliku zostaną utracone.":
        "Overwrite {path}? All data in this file will be lost.",
    "Formatowanie {name}": "Formatting {name}",
    "Sprawdzanie {label}": "Checking {label}",
    "Naprawa {label}": "Repairing {label}",
    "Konwersja do {format}: {count} plik(ów)":
        "Converting to {format}: {count} file(s)",
    "Na dysku docelowym istnieje już {conflicts} z {total} gier. "
    "Zostaną one zastąpione nowymi plikami.":
        "{conflicts} of {total} games already exist on the destination "
        "drive. They will be replaced with new files.",
    "Eksport do biblioteki: {count} gier":
        "Export to library: {count} games",
    "{count} tytułów": "{count} titles",
    "{count} gier": "{count} games",
    "{label} — wolne {free}": "{label} — free {free}",
    "Właściwości — {title}": "Properties — {title}",
    "{filename} ma {size}. Użyj opcji „Wypakuj jako WBFS”, "
    "która tworzy dzielony plik .wbfs/.wbf1.":
        "{filename} is {size}. Use “Extract as WBFS”, which creates a "
        "split .wbfs/.wbf1 file.",
    "Wii: {base}/{wii} · GameCube: {base}/{gc}":
        "Wii: {base}/{wii} · GameCube: {base}/{gc}",

    # --- about / application --------------------------------------------
    "Projekt GOR Wii Manager": "GOR Wii Manager Project",
    "Menedżer kopii gier Wii i GameCube dla Linuksa. "
    "Tworzy i zarządza partycjami oraz plikami WBFS, "
    "konwertuje obrazy ISO i okładki z GameTDB.":
        "A Wii and GameCube game backup manager for Linux. "
        "Creates and manages WBFS partitions and files, "
        "converts ISO images and downloads covers from GameTDB.",
    "Podziękowania": "Credits",
    "libwbfs (Kwiirk, oggzee) — format WBFS":
        "libwbfs (Kwiirk, oggzee) — WBFS format",
    "Wiimm (wit/wwt) — dokumentacja formatu":
        "Wiimm (wit/wwt) — format documentation",
    "GameTDB / WiiTDB — tytuły i okładki":
        "GameTDB / WiiTDB — titles and covers",
    "Menedżer kopii gier Wii/GameCube (WBFS) dla Linuksa":
        "Wii/GameCube (WBFS) game backup manager for Linux",
    "pliki .wbfs do otwarcia": ".wbfs files to open",
    "Zrzut ekranu zapisany: {path}": "Screenshot saved: {path}",
    "Nie udało się zapisać zrzutu ekranu: {error}":
        "Could not save the screenshot: {error}",

    # --- core messages commonly shown in the UI -------------------------
    "Gra {disc_id} ({title}) już istnieje w tym napędzie":
        "Game {disc_id} ({title}) already exists on this drive",
    "urządzenie jest za małe na partycję WBFS":
        "device is too small for a WBFS partition",
    "napęd jest otwarty tylko do odczytu": "drive is opened read-only",
    "brak wolnego miejsca na tym napędzie WBFS":
        "no free space on this WBFS drive",
    "gra jest za duża dla tego WBFS": "game is too large for this WBFS",
    "gra nie zawiera żadnych bloków danych": "game contains no data blocks",
    "eksport nie powiódł się": "export failed",
    "nie można odczytać miejsca docelowego: {path}":
        "cannot read the destination: {path}",
    "nie wybrano żadnych gier": "no games selected",
    "za mało miejsca na dysku docelowym: potrzeba około ":
        "not enough space on the destination drive: need about ",
    "Płasko: wbfs/<ID>.wbfs": "Flat: wbfs/<ID>.wbfs",
    "Nadpisuj istniejące": "Overwrite existing",
    "Pomiń istniejące": "Skip existing",
    "ustawienia": "settings",
    "baza tytułów": "title database",
    "okładki ({count})": "covers ({count})",
    "Automatycznie (Wii → WBFS, GC → Nintendont ISO)":
        "Automatic (Wii → WBFS, GC → Nintendont ISO)",
    "USB Loader GX / WiiFlow / CFG (foldery wbfs/)":
        "USB Loader GX / WiiFlow / CFG (wbfs/ folders)",
    "Nintendont (GameCube ISO w games/)":
        "Nintendont (GameCube ISO in games/)",
    "Automatyczny": "Automatic",
    "Interfejs": "Interface",
    "Otwórz bibliotekę USB": "Open USB library",
    "Nowa biblioteka USB": "New USB library",
    "Otwórz napęd WBFS": "Open WBFS drive",
}


def set_language(code: str) -> None:
    """Select the interface language (``en`` or ``pl``)."""
    global _current
    _current = code if code in LANGUAGES else DEFAULT_LANGUAGE


def get_language() -> str:
    return _current


def _(text: str) -> str:
    """Translate a Polish message ID to the current interface language."""
    if _current == "pl":
        return text
    return _EN.get(text, text)


def language_label(code: str) -> str:
    return LANGUAGES.get(code, code)
