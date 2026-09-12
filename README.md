# ESP Flash Backup Tool

A PyQt6 desktop GUI for backing up, restoring and inspecting flash images on
Espressif ESP8266 and ESP32 family chips. Built as a thin, responsive
front-end on top of the official
[`esptool`](https://github.com/espressif/esptool) package, plus an
mmap-backed hex viewer, a serial monitor and a headless CLI.

Works against **esptool 4.x and 5.x**. esptool 5 renamed every subcommand
(`read_flash` → `read-flash`) and changed its console output (`Chip is …` →
`Chip type: …`), so the app detects the installed version and speaks the
matching dialect — see `esptool_cmd()`, `esptool_flag()` and `OutputScanner`.

## Features

### Chip and connection

- **Auto-detect** the connected chip, MAC address and flash size. The target
  list is taken from the installed esptool, so newer SoCs (ESP32-P4, -C5,
  -C61, -H21, …) appear without a code change; the chip can also be forced
  manually if auto-detection misbehaves.
- **Board profiles** — save port + baud + chip + flash size + addresses, plus
  the erase / verify / trim toggles, under a name and switch between boards in
  one click.
- **Hotplug detection** — the port list refreshes itself as devices come and
  go, with an optional *Detect on plug-in*. No more hitting *Refresh*.
- **Permission preflight** — if the selected port isn't readable, the app says
  which group owns it and prints the exact `usermod` command.
- **Chip security panel** (eFuses) — reads `espefuse summary` and calls out
  flash encryption and secure boot up front, because **a raw dump of an
  encrypted chip is ciphertext**: it cannot be restored to another device, and
  unsigned images won't boot under secure boot.

### Backup and restore

- **Backup** — read the whole flash (or any range) to a `.bin`. Set the size
  explicitly or leave it on *Auto-detect*, which chains `flash_id` first.
- **Backup manifests** — by default each dump gets a `.bin.json` sidecar
  recording chip, MAC, flash size, address, size and SHA-256 (togglable in the
  Backup group, `--no-manifest` on the CLI).
- **Restore guard** — restoring an image whose manifest names a *different*
  chip family is refused (with an explicit override), because flash layouts
  and calibration data are chip-specific. Every restore shows exactly what is
  about to happen before it writes.
- **Trailing-blank trimming** — most 16 MB dumps are mostly `0xFF`. Optionally
  truncate the erased tail; the manifest keeps the original size.
- **Verify** — compare a file against flash, either standalone or chained
  automatically after a write.
- **Erase** — whole chip, or a single partition's region.

### Multi-image flashing

- **Flash Multiple Images** (Ctrl+F) — a table of `<address> <file>` rows
  written in one `write-flash`: the usual bootloader + partition-table + app
  trio. Flash mode / frequency / size selectors and *erase all first*.
- **Import `flash_args`** — load an ESP-IDF `build/flash_args` file and the
  addresses, files and flash options fill themselves in.
- **Overlap detection** — overlapping write regions are reported before
  anything is written.
- **Export merged image** — combine the rows into a single flashable
  `raw` / `uf2` / `hex` file via `merge-bin`.

### Partition table (Ctrl+P)

- Read the table from the device or from a flash image, shown as
  label / type / subtype / offset / size / flags, with a CSV export.
- **Dump all partitions** into a folder, one `.bin` each, queued back to back.
- **Write a single partition** — erases just that region, with a size check
  against the partition.
- **Erase a single partition** — the usual way to reset stored NVS config
  without reflashing firmware.

### Hex viewer (Ctrl+H)

- Classic *offset · hex · ASCII*, virtualized via a `QAbstractTableModel` over
  `mmap`, so a 16 MB dump opens instantly.
- **Search** forward/backward for hex bytes (`de ad be ef`, `0xdeadbeef`) or
  text, with the hit highlighted.
- **Diff two images** — differing rows are tinted and *Diff ▶* jumps to the
  next changed byte. Ideal for before/after dumps.
- **Jump to a partition** — the table at `0x8000` is parsed out of the open
  image and offered as a dropdown.
- **Byte inspector** — offset plus `u8` / `u16le` / `u32le` / `i32le` for the
  selected row, and a Goto field accepting decimal or `0x` hex.

### Serial monitor (Ctrl+M)

- Live monitor with adjustable baud, send line + line-ending selector, local
  echo, timestamps and a hex view.
- **ANSI colour rendering** — ESP-IDF's coloured log levels show as colours
  instead of escape-code litter.
- **Filter and highlight** by regex — show only matching lines, or tint them.
- **Panic backtrace decoding** — `Backtrace: 0x… 0x…` (Xtensa) and `MEPC : 0x…`
  (RISC-V) are resolved through `addr2line` against an ELF you pick, printed
  inline as `function at file:line`.
- **Reset → Bootloader** — the DTR/RTS sequence that reboots into serial
  download mode, next to the plain *Reset Device*.
- **Auto-reconnect** after an unplug, and **flash hand-off**: the monitor
  releases the port when an esptool operation starts and reopens it when the
  operation finishes. esptool and the monitor can't share a port, and you no
  longer have to juggle that by hand.
- Timestamps record real arrival times, so filtering never restamps old lines.

### Operation handling

- **Sequential job queue** — multi-step flows (detect → read, erase → write →
  verify, dump 8 partitions) run as one queue; a failure drops the remaining
  steps and says so.
- **Live progress** with percentage, bytes, throughput and ETA. esptool emits
  a progress line every 1024 bytes — those drive the bar and never reach the
  log, which is also block-capped.
- **Clean log** — the child process runs with `NO_COLOR=1` and no `$TERM`, and
  output is ANSI-scrubbed on the way in.
- **Safe cancel** — esptool is asked to stop first (`SIGTERM` on POSIX) and
  only killed outright if it ignores that for three seconds.
- **Close guard** — quitting mid-operation asks first, and warns that
  interrupting a write leaves the chip unbootable.

## Supported boards

esptool detects the chip over the bootloader handshake, so any board built
around a supported SoC works without configuration. Verified targets include
the PlatformIO board profiles:

| PlatformIO board                     | Detected as           |
| ------------------------------------ | --------------------- |
| `espressif8266-nodemcuv2`            | `ESP8266EX`           |
| `espressif32-esp32dev`               | `ESP32-D0WD…`         |
| `espressif32-esp32-c3-devkitm-1`     | `ESP32-C3`            |

…plus any other ESP8266 / ESP32-S2 / -S3 / -C2 / -C6 / -H2 / -P4 dev board.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Needs **Python 3.10 or newer** (esptool 5 requires it, and so do this
app's `X | None` type annotations).

Dependencies (`requirements.txt`):

- `PyQt6>=6.5`
- `esptool>=4.7`
- `pyserial>=3.5`

`espefuse`, which backs the Chip Security panel, ships inside the `esptool`
distribution — there is nothing extra to install for it.

Backtrace decoding additionally wants an `addr2line` on `PATH` — either the
ESP-IDF toolchain's (`xtensa-esp32-elf-addr2line`, `riscv32-esp-elf-addr2line`)
or plain `binutils`.

## Run

```bash
python esp_flasher.py
```

### Linux serial permissions

If your user can't see `/dev/ttyUSB*` or gets `Permission denied`, add
yourself to the `dialout` group and re-login:

```bash
sudo usermod -aG dialout "$USER"
```

On some distros (Arch, Fedora) the group is `uucp` instead. The app detects
this and shows the right command for your system.

## Usage

1. Plug in the device and pick the port (it appears by itself).
2. Choose a baud rate (460800 is a good default; drop to 115200 for finicky
   USB-serial cables).
3. Click **Detect Chip** — chip type, MAC and flash size populate.
4. **To back up:** pick an output `.bin` path → *Backup Flash*.
5. **To restore:** pick the `.bin` to write → *Restore Flash* (the *Erase
   entire flash first* checkbox is on by default, which is what you usually
   want when restoring a full dump).
6. **To flash a build:** *Tools → Flash Multiple Images…* → *Load flash_args…*
   → *Flash All*.
7. **To inspect:** click *View* next to either file path, or open
   *Tools → Hex Viewer…* (Ctrl+H). *Tools → Firmware Image Info…* (Ctrl+I)
   shows the app name, version and IDF version of a single application image.
8. **To watch device output:** *Serial Monitor…* (Ctrl+M). Pick a baud (most
   ESP firmware logs at 115200; ESP8266 boot ROM messages are at 74880).

Save a **profile** once you have a board configured and switching between
boards becomes a one-click affair.

## Headless / scripted use

Every operation is also available without the GUI, which makes scheduled or
scripted backups straightforward. Running with no arguments opens the GUI;
`--gui` forces it even when other arguments are present.

```bash
python esp_flasher.py detect     -p /dev/ttyUSB0 --json
python esp_flasher.py backup     -p /dev/ttyUSB0 -o dump.bin --trim
python esp_flasher.py restore    -p /dev/ttyUSB0 -f dump.bin --erase --verify
python esp_flasher.py verify     -p /dev/ttyUSB0 -f dump.bin
python esp_flasher.py erase      -p /dev/ttyUSB0 --yes
python esp_flasher.py partitions -p /dev/ttyUSB0 --json --dump-dir ./parts
python esp_flasher.py image-info -f app.bin
```

`backup` defaults to `--size auto` (it runs `flash_id` first) and writes the
same `.json` manifest as the GUI; `restore` enforces the same chip-mismatch
guard and needs `--force` to override it. `erase` refuses to run without
`--yes`.

Exit codes: `0` success, `1` the operation ran but found nothing (an empty
partition table), `2` bad usage, and `3` a refused chip mismatch. Any other
non-zero status is esptool's own exit code, passed straight through.

A nightly backup is then just a cron line — with the `.deb` installed the
binary is on `PATH` as `esp_flasher`:

```bash
0 3 * * * esp_flasher backup -p /dev/ttyUSB0 -o /backups/esp-$(date +\%F).bin --trim
```

From a source checkout, use `python /path/to/esp_flasher.py backup …` instead.

## Tests

```bash
make deps-dev
make test
```

220 tests: the pure helpers (output scanning for both esptool dialects,
partition parsing, `flash_args`, manifests, trimming, eFuse and backtrace
parsing, ANSI segmentation, byte search) plus widget-level tests that drive
the real windows on Qt's `offscreen` platform. Qt settings are redirected to a
temporary directory, so the suite never touches your configuration.

A small group also pins the release version: `APP_VERSION` has to match the
`Makefile` default, the leading `CHANGELOG.md` heading and the `.deb` names in
this README. Those had already drifted apart once.

Both esptool generations matter here, and the suite is written to pass under
either: run it once per interpreter if you have both installed, since
`esptool_cmd()`, `esptool_flag()` and `OutputScanner` all branch on the
detected version.

CI (`.github/workflows/ci.yml`) runs the suite on Python 3.10 and 3.12,
smoke-tests the CLI, validates the generated `.desktop` file, and builds the
PyInstaller bundle and checks that its `--esptool-worker` sentinel works.

## Build a standalone binary

The repo ships a cross-platform PyInstaller driver (`build.py`) that
produces a single-file executable for the current OS / architecture.

```bash
pip install -r requirements.txt -r requirements-build.txt
python build.py                 # release one-file build
python build.py --onedir        # one-folder build (faster startup)
python build.py --debug         # keep console attached for tracebacks
python build.py --clean         # purge build/, dist/, *.spec first
```

Output goes to `dist/`:

| Platform | Artifact                                            |
| -------- | --------------------------------------------------- |
| Linux    | `dist/esp_flasher-linux-x86_64`                     |
| macOS    | `dist/esp_flasher-macos-arm64.app/` (bundle)        |
| Windows  | `dist\esp_flasher-windows-amd64.exe`                |

The build pulls `--collect-all` for `esptool`, `serial`, `espefuse`,
`bitstring` and `bitarray`. esptool's per-chip target submodules and
stub-flasher data files, pyserial's platform backends and bitstring's
dynamically-selected backend (`bitstring.bitstore_bitarray`, reached via
espefuse) are all invisible to PyInstaller's static analysis — without these
the bundle builds cleanly and then fails at runtime, chip detection or the
eFuse panel breaking while everything else works. Cross-compilation is not
supported by PyInstaller; build on the OS you want to ship for.

Note that `--windowed` detaches stdout on Windows and macOS, so use
`--debug` if you want a binary that can also serve as the CLI there.

### Packaging

```bash
make deb-linux-amd64      # -> dist/esp-flasher_0.3.0_amd64.deb
make deb-linux-uconsole   # -> dist/esp-flasher_0.3.0_arm64.deb
make all-linux            # build + deb for the current Linux arch
```

The `.deb` installs the binary, an icon (`hicolor` 256×256 PNG plus scalable
SVG), a validated `.desktop` entry and the README. Its `Depends` lists the
X11/xcb/GL libraries Qt dlopens at runtime — the bundle carries its own Qt,
but not the host libraries Qt itself needs.

## Project layout

```
esp_flasher/
├── esp_flasher.py            # single-file PyQt6 app + CLI
├── build.py                  # PyInstaller driver
├── Makefile                  # build / package / test targets
├── pytest.ini                # test config (testpaths, warning filters)
├── tests/
│   ├── conftest.py           # offscreen Qt + isolated QSettings + FakeProcess
│   ├── test_helpers.py       # pure functions, no Qt widgets
│   └── test_gui.py           # widget-level tests
├── packaging/
│   ├── build-deb.sh          # .deb builder
│   └── esp-flasher.svg/.png  # application icon
├── .github/workflows/ci.yml  # tests + CLI smoke + bundle build
├── .gitignore                # also ignores the dumps the tool writes
├── requirements.txt          # runtime deps
├── requirements-build.txt    # build-only deps (pyinstaller)
├── requirements-dev.txt      # test deps (pytest)
├── CHANGELOG.md              # what changed between versions
└── README.md
```

Internals at a glance:

- `esptool_cmd()` / `esptool_flag()` / `OutputScanner` — the esptool 4-vs-5
  compatibility layer: subcommand spelling, option spelling (only `--flash_*`
  differs) and console-output parsing.
- `Job` + `EspFlasher._queue` — a sequential operation queue. Each job is one
  esptool invocation with an optional success callback; a failure clears the
  rest.
- `EspFlasher` — `QMainWindow` with the connection / backup / restore /
  maintenance groups, progress bar and log pane. Drives esptool through
  `QProcess` with merged stdout/stderr and a deliberately dumb terminal
  environment.
- `HexModel` / `HexViewer` — `QAbstractTableModel` + `QDialog` over a
  memory-mapped file, with chunked search and difference scanning.
- `SerialReader` / `SerialMonitor` — `QThread` worker polling
  `serial.in_waiting`, paired with a `QDialog` that does line buffering, ANSI
  segmentation, regex filtering and backtrace decoding.
- `PartitionTableDialog` / `MultiFlashDialog` / `ImageInfoDialog` /
  `EfuseDialog` — the feature dialogs; each exposes `set_busy()` so the main
  window can gate them while an operation runs.
- `cli_main()` — argparse front-end sharing the manifest, trimming and guard
  logic with the GUI.
- `list_serial_ports()` — pyserial enumeration plus a platform-specific glob
  fallback so freshly-attached USB UARTs always show up.

## Notes & caveats

- A full 4 MB read at 460800 baud takes about 40 seconds; a 16 MB chip is
  proportionally slower. Higher baud rates (921600, 1500000) work on most
  modern USB-serial chips but not all — back off if you see CRC errors.
- A flash dump captured from one chip is generally *not* portable to a
  different chip variant; restore to the same model you read from. The
  manifest guard enforces this at the family level.
- Flash encryption changes the meaning of a backup entirely: what you read is
  ciphertext bound to that chip's key eFuse. Check the Chip Security panel
  before trusting a dump as a recovery image.
- Interrupting a write leaves the chip half-programmed. Cancel and window
  close both warn about this, but there is no undo — keep a known-good image.
- The tool runs the `esptool` Python module from the same interpreter that
  launched the GUI (`sys.executable -m esptool`), so a venv keeps versions
  aligned automatically. In a PyInstaller bundle it re-invokes itself with the
  `--esptool-worker` / `--espefuse-worker` sentinels instead.

## Changelog

See [CHANGELOG.md](CHANGELOG.md). If you are coming from 0.0.2 (the last
tagged release), the headline is that chip detection was silently broken
against esptool 5 and is fixed.

## License

MIT.
