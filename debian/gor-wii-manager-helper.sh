#!/bin/sh
# Wywoływane przez pkexec; pakiet musi być zainstalowany w dist-packages.
exec python3 -m gor_wii_manager.helper "$@"
