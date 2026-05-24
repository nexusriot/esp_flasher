#!/usr/bin/env bash
# Wrap a PyInstaller onefile binary into a Debian package.
#
#   build-deb.sh <binary-path> <deb-arch> <version> <output-dir>
#
# deb-arch is the dpkg architecture (amd64, arm64). The binary is a
# self-contained PyInstaller bundle, so runtime deps are minimal.
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

install -D -m 0644 /dev/stdin "$STAGE/usr/share/applications/esp-flasher.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=ESP Flash Backup Tool
Comment=Back up and restore flash images on ESP8266 / ESP32 chips
Exec=esp_flasher
Terminal=false
Categories=Development;Electronics;Utility;
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
Depends: libc6
Description: ESP Flash Backup Tool
 A PyQt6 desktop GUI for backing up and restoring flash images on
 Espressif ESP8266 and ESP32 family chips, built on top of esptool.
 Ships as a self-contained bundle (Qt and Python included).
CONTROL

mkdir -p "$OUTDIR"
OUT="$OUTDIR/${PKG}_${VERSION}_${DEB_ARCH}.deb"
dpkg-deb --build --root-owner-group "$STAGE" "$OUT"
echo "built: $OUT"
