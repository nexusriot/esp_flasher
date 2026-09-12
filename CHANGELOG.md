# Changelog

## 0.3.0 — 2026-09-12

### Fixed

- **Chip detection was silently broken against esptool 5.** esptool 5 prints
  `Chip type:          ESP32-…` where 4.x printed `Chip is ESP32-…`, so the
  *Chip:* label never populated. Output parsing now understands both dialects
  (`OutputScanner`).
- **Flash sizes below 1 MB were ignored** — `Detected flash size: 512KB` did
  not match the MB-only pattern, so *Auto-detect* backups failed on 256 KB /
  512 KB ESP8266 parts.
- **Deprecated subcommand names.** esptool 5 accepts `read_flash` but warns and
  will drop it in 6; commands and options are now spelled per the installed
  version (`esptool_cmd()`, `esptool_flag()`).
- **Log pane growth.** esptool emits a progress line every 1024 bytes, so a
  16 MB read produced ~16k log lines into an uncapped `QPlainTextEdit`. Those
  lines now drive the progress bar only, and the log is block-capped.
- **Raw ANSI escapes in the log.** esptool enables colours and cursor tricks
  whenever `$TERM` looks capable — even when writing to a pipe. Child
  processes now run with `NO_COLOR=1` and no `$TERM`, and output is scrubbed.
- **Cancel used `SIGKILL` immediately**, which could interrupt a write at an
  arbitrary point. It now asks the process to stop and escalates only after a
  three-second grace period, bound to the process it was armed for.
- **Closing the window mid-operation killed esptool without asking.** It now
  confirms, and warns when a write is in flight.
- **The Tools menu was not gated during operations**, so Ctrl+M could open a
  serial monitor and steal the port mid-flash.
- **`make build-*` used the distro python**, which is externally managed
  (PEP 668) and so can never hold PyInstaller or PyQt6 — the build died on
  build.py's import guard, and the message it printed (`pip install -r …`)
  could not work either. The Makefile now uses `.venv` when it exists,
  `make deps` creates it, and build.py names the interpreter that is missing
  PyInstaller.
- A failed `QProcess` start left the UI disabled forever, because
  `finished()` is never emitted in that case.

### Added

- Headless CLI: `detect`, `backup`, `restore`, `verify`, `erase`,
  `partitions`, `image-info`, with `--json` output where it makes sense.
- Sequential job queue, replacing the ad-hoc chaining fields. Multi-step flows
  (detect → read, erase → write → verify, dump N partitions) run as one queue
  and abort cleanly.
- Progress throughput and ETA.
- Backup manifests (`.bin.json`: chip, MAC, flash size, address, size,
  SHA-256) and a restore guard that refuses an image captured from a different
  chip family unless overridden.
- Trailing-`0xFF` trimming for full dumps, with the original size preserved in
  the manifest.
- Multi-image flashing with ESP-IDF `flash_args` import, overlap detection and
  merged `raw` / `uf2` / `hex` export.
- Partition operations: dump all to a folder, write a single partition
  (erase-region + write, size-checked), erase a single partition, CSV export.
- Chip Security / eFuse panel (`espefuse summary`) that calls out flash
  encryption and secure boot, and warns before a restore that cannot work.
- Firmware image inspector (`esptool image-info`).
- Board profiles, recent-file menus, serial-port hotplug detection with an
  optional detect-on-plug-in, and a serial-permission preflight that names the
  owning group.
- Hex viewer: hex/text search, two-image diff with next-difference navigation,
  jump-to-partition, and a byte inspector.
- Serial monitor: ANSI colour rendering, regex filter and highlight, panic
  backtrace decoding via `addr2line`, *Reset → Bootloader*, auto-reconnect
  after an unplug, and automatic port hand-off around esptool operations.
- Test suite (220 tests, `pytest`) and CI. Qt runs on the `offscreen`
  platform and `QSettings` is redirected to a temporary directory. Five of
  them pin `APP_VERSION` against the `Makefile` default, the leading
  `CHANGELOG.md` heading and the README's `.deb` names, because those had
  already drifted apart.
- `CHANGELOG.md`, and a `make icon` target that regenerates
  `packaging/esp-flasher.png` from the SVG so the tracked icon is
  reproducible.
- Packaging: application icon (SVG + 256×256 PNG), `Icon=`/`Keywords=`/
  `StartupWMClass=` in the `.desktop` entry, and the X11/xcb/GL libraries Qt
  dlopens listed in the package `Depends` — previously only `libc6`, so the
  package could install and then fail to start.

### Changed

- Chip targets come from the installed esptool (`esptool.targets.CHIP_LIST`),
  so ESP32-P4/-C5/-C61/-H21 and later appear without a code change. Flash
  sizes now go up to 128 MB, and 2000000 was added to the baud list.
- Restores ask for confirmation and show what is about to happen.
- Serial monitor timestamps record real arrival times, so filtering or
  toggling timestamps no longer restamps old lines with the current time.
- PyInstaller bundles additionally collect `espefuse`, `bitstring` and
  `bitarray`; without them the eFuse panel raised `ModuleNotFoundError` in a
  frozen build while everything else worked.
- `.gitignore` now covers the output the tool itself writes into the working
  directory — flash dumps and their manifests, partition dumps, merged
  images, the serial log and the CSV export. A flash image is device-specific
  and can contain WiFi credentials, so it must never be committed.
- The README was rewritten for this release and then audited against the
  code, correcting a stale test count, a `.deb`-only cron example, an
  unstated Python floor (3.10+), incomplete exit codes and three claims that
  overstated what the code does.

### Notes

This release has been verified against esptool 4.7.0 and 5.2.0, in a source
checkout and as a PyInstaller bundle (both worker sentinels, the CLI and the
GUI). It has **not** been exercised against physical hardware.

## Unreleased between 0.0.2 and 0.3.0

The read-only partition table viewer, the `Makefile` and the `.deb` packaging
script landed in `da7b680` (2026-05-24) but were never tagged, so they reach a
release for the first time in 0.3.0.

## 0.0.2 — 2026-05-10

- Fixed the packaged executable: under PyInstaller `sys.executable` is the
  bundle rather than a Python interpreter, so chip operations launched a second
  copy of the GUI instead of esptool. Added the `--esptool-worker` sentinel the
  binary uses to re-invoke itself.

## 0.0.1 — 2026-04-28

First tagged version: chip detection, whole-flash backup and restore with
optional erase and verify, an mmap-backed hex viewer, a serial monitor, and the
PyInstaller build driver.
