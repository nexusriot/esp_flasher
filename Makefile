# esp_flasher — build & packaging
#
# PyInstaller cannot cross-compile: each target must run on its native
# OS/arch. The build-* targets check the host and refuse on a mismatch.
#
#   make build-linux-amd64        # on an x86_64 Linux box
#   make build-linux-uconsole     # on the uConsole (aarch64 Linux)
#   make build-windows            # on Windows (MSYS2 / Git Bash)
#   make deb-linux-amd64          # .deb for amd64
#   make deb-linux-uconsole       # .deb for arm64
#   make deps                     # install build dependencies
#   make clean

APP        := esp_flasher
VERSION    ?= 0.3.0
PY         ?= python3
HOST_OS    := $(shell uname -s)
HOST_ARCH  := $(shell uname -m)
DIST       := dist
PACKAGING  := packaging

.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo "esp_flasher build targets (VERSION=$(VERSION)):"
	@echo "  make deps                  install PyInstaller + runtime deps"
	@echo "  make deps-dev              install the test dependencies"
	@echo "  make icon                  regenerate packaging/esp-flasher.png from the SVG"
	@echo "  make test                  run the test suite (headless Qt)"
	@echo "  make build-linux-amd64     binary -> $(DIST)/$(APP)-linux-amd64      (needs x86_64 Linux)"
	@echo "  make build-linux-uconsole  binary -> $(DIST)/$(APP)-linux-uconsole   (needs aarch64 Linux)"
	@echo "  make build-windows         binary -> $(DIST)/$(APP)-windows-amd64.exe (needs Windows)"
	@echo "  make deb-linux-amd64       package -> $(DIST)/$(APP)_$(VERSION)_amd64.deb"
	@echo "  make deb-linux-uconsole    package -> $(DIST)/$(APP)_$(VERSION)_arm64.deb"
	@echo "  make all-linux             build + deb for the current Linux arch"
	@echo "  make clean"

.PHONY: deps
deps:
	$(PY) -m pip install -r requirements.txt -r requirements-build.txt

.PHONY: deps-dev
deps-dev:
	$(PY) -m pip install -r requirements.txt -r requirements-dev.txt

# The PNG is a tracked asset (build.py and build-deb.sh consume it and neither
# builds it), so this only needs running when the SVG changes.
.PHONY: icon
icon:
	QT_QPA_PLATFORM=offscreen $(PY) -c "\
from PyQt6 import QtGui, QtCore, QtWidgets, QtSvg; \
app = QtWidgets.QApplication([]); \
r = QtSvg.QSvgRenderer('$(PACKAGING)/esp-flasher.svg'); \
img = QtGui.QImage(256, 256, QtGui.QImage.Format.Format_ARGB32); \
img.fill(QtCore.Qt.GlobalColor.transparent); \
pn = QtGui.QPainter(img); r.render(pn); pn.end(); \
img.save('$(PACKAGING)/esp-flasher.png') or exit('save failed')"
	@echo "wrote $(PACKAGING)/esp-flasher.png"

# Qt needs a platform plugin; offscreen keeps the suite runnable over ssh.
.PHONY: test
test:
	QT_QPA_PLATFORM=offscreen NO_COLOR=1 $(PY) -m pytest

.PHONY: build-linux-amd64
build-linux-amd64:
	@[ "$(HOST_OS)" = "Linux" ]   || { echo "error: build-linux-amd64 must run on Linux (host: $(HOST_OS))"; exit 1; }
	@[ "$(HOST_ARCH)" = "x86_64" ] || { echo "error: build-linux-amd64 must run on x86_64 (host: $(HOST_ARCH)); PyInstaller cannot cross-compile"; exit 1; }
	$(PY) build.py --name $(APP)-linux-amd64

.PHONY: build-linux-uconsole
build-linux-uconsole:
	@[ "$(HOST_OS)" = "Linux" ] || { echo "error: build-linux-uconsole must run on Linux (host: $(HOST_OS))"; exit 1; }
	@case "$(HOST_ARCH)" in aarch64|arm64) : ;; *) echo "error: build-linux-uconsole must run on the uConsole (aarch64 Linux); host is $(HOST_ARCH)"; exit 1 ;; esac
	$(PY) build.py --name $(APP)-linux-uconsole

.PHONY: build-windows
build-windows:
	@case "$(HOST_OS)" in MINGW*|MSYS*|CYGWIN*|Windows*) : ;; *) echo "error: build-windows must run on Windows (host: $(HOST_OS))"; exit 1 ;; esac
	$(PY) build.py --name $(APP)-windows-amd64

.PHONY: deb-linux-amd64
deb-linux-amd64: build-linux-amd64
	$(PACKAGING)/build-deb.sh "$(DIST)/$(APP)-linux-amd64" amd64 "$(VERSION)" "$(DIST)"

.PHONY: deb-linux-uconsole
deb-linux-uconsole: build-linux-uconsole
	$(PACKAGING)/build-deb.sh "$(DIST)/$(APP)-linux-uconsole" arm64 "$(VERSION)" "$(DIST)"

.PHONY: all-linux
all-linux:
	@case "$(HOST_ARCH)" in \
	  x86_64)        $(MAKE) deb-linux-amd64 ;; \
	  aarch64|arm64) $(MAKE) deb-linux-uconsole ;; \
	  *) echo "error: unsupported Linux arch $(HOST_ARCH)"; exit 1 ;; \
	esac

.PHONY: clean
clean:
	rm -rf build $(DIST) $(APP)-*.spec __pycache__ .pytest_cache \
	       tests/__pycache__
