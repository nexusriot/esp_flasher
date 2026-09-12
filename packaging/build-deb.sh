#!/usr/bin/env bash
# Wrap a PyInstaller onefile binary into a Debian package.
#
#   build-deb.sh <binary-path> <deb-arch> <version> <output-dir>
#
# deb-arch is the dpkg architecture (amd64, arm64). The binary bundles Python,
# Qt and esptool, but Qt still dlopens the host's X11/xcb/GL libraries — those
# are the Depends below. Without them the package installs and then fails to
# start with "could not load the Qt platform plugin xcb".
set -euo pipefail

BINARY=${1:?usage: build-deb.sh <binary> <deb-arch> <version> <outdir>}
DEB_ARCH=${2:?missing deb-arch}
VERSION=${3:?missing version}
OUTDIR=${4:?missing outdir}

PKG=esp-flasher
MAINTAINER="Vlad Ananyev <vlananyev@gmail.com>"

if [ ! -f "$BINARY" ]; then
    echo "error: binary not found: $BINARY" >&2
    exit 1
fi

ROOT=$(cd "$(dirname "$0")/.." && pwd)
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
chmod 0755 "$STAGE"

install -D -m 0755 "$BINARY"          "$STAGE/usr/bin/esp_flasher"
[ -f "$ROOT/README.md" ] && \
    install -D -m 0644 "$ROOT/README.md" "$STAGE/usr/share/doc/$PKG/README.md"
[ -f "$ROOT/CHANGELOG.md" ] && \
    install -D -m 0644 "$ROOT/CHANGELOG.md" "$STAGE/usr/share/doc/$PKG/CHANGELOG.md"

if [ -f "$ROOT/packaging/esp-flasher.png" ]; then
    install -D -m 0644 "$ROOT/packaging/esp-flasher.png" \
        "$STAGE/usr/share/icons/hicolor/256x256/apps/esp-flasher.png"
fi
if [ -f "$ROOT/packaging/esp-flasher.svg" ]; then
    install -D -m 0644 "$ROOT/packaging/esp-flasher.svg" \
        "$STAGE/usr/share/icons/hicolor/scalable/apps/esp-flasher.svg"
fi

install -D -m 0644 /dev/stdin "$STAGE/usr/share/applications/esp-flasher.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=ESP Flash Backup Tool
GenericName=ESP Flash Tool
Comment=Back up and restore flash images on ESP8266 / ESP32 chips
Exec=esp_flasher
Icon=esp-flasher
Terminal=false
StartupWMClass=esp_flasher
Categories=Development;Electronics;
Keywords=esp32;esp8266;esptool;flash;firmware;serial;embedded;
DESKTOP

INSTALLED_KB=$(du -sk "$STAGE" | cut -f1)

install -D -m 0644 /dev/stdin "$STAGE/DEBIAN/control" <<CONTROL
Package: $PKG
Version: $VERSION
Section: electronics
Priority: optional
Architecture: $DEB_ARCH
Maintainer: $MAINTAINER
Installed-Size: $INSTALLED_KB
Depends: libc6,
 libglib2.0-0t64 | libglib2.0-0,
 libgl1 | libgl1-mesa-glx,
 libegl1,
 libfontconfig1,
 libfreetype6,
 libdbus-1-3,
 libx11-6,
 libxkbcommon0,
 libxkbcommon-x11-0,
 libxcb1,
 libxcb-cursor0,
 libxcb-icccm4,
 libxcb-image0,
 libxcb-keysyms1,
 libxcb-randr0,
 libxcb-render-util0,
 libxcb-shape0,
 libxcb-shm0,
 libxcb-sync1,
 libxcb-xfixes0,
 libxcb-xinerama0,
 libxcb-xkb1
Recommends: binutils
Suggests: udev
Description: ESP Flash Backup Tool
 A PyQt6 desktop GUI for backing up and restoring flash images on
 Espressif ESP8266 and ESP32 family chips, built on top of esptool.
 .
 Reads and writes whole-chip images or individual partitions, flashes
 ESP-IDF image sets, inspects dumps in a hex viewer with search and
 diff, and includes a serial monitor that decodes panic backtraces.
 .
 Ships as a self-contained bundle (Python, Qt and esptool included).
 Serial access needs membership of the port's group, usually dialout:
 run "sudo usermod -aG dialout \$USER" and log in again.
CONTROL

mkdir -p "$OUTDIR"
OUT="$OUTDIR/${PKG}_${VERSION}_${DEB_ARCH}.deb"
dpkg-deb --build --root-owner-group "$STAGE" "$OUT"
echo "built: $OUT"
