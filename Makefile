PYTHON ?= python3
PKG := gor-wii-manager

.PHONY: run test ui-test deb clean install-deps

run:
	PYTHONPATH=src $(PYTHON) -m gor_wii_manager

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -q

ui-test:
	PYTHONPATH=src $(PYTHON) tests/ui_smoke.py

deb:
	dpkg-buildpackage -us -uc -b
	mkdir -p dist
	mv ../$(PKG)_*_all.deb dist/ 2>/dev/null || true
	@ls -l dist/*.deb 2>/dev/null || true

install-deps:
	sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 pkexec udisks2

clean:
	rm -rf build dist *.egg-info src/*.egg-info
	find . -name __pycache__ -type d -exec rm -rf {} +
	rm -rf debian/.debhelper debian/debhelper-build-stamp debian/files \
		debian/$(PKG) debian/*.substvars
