# ESP Flash Backup Tool



A PyQt6 desktop GUI for backing up and restoring flash images on Espressif
ESP8266 and ESP32 family chips. Built as a thin, responsive front-end on top
of the official [`esptool`](https://github.com/espressif/esptool) Python
package, plus a built-in mmap-backed hex viewer for inspecting the resulting
`.bin` files.

## Features

- **Auto-detect** the connected chip (ESP8266 / ESP32 / ESP32-S2 / -S3 /
  -C2 / -C3 / -C6 / -H2), MAC address and flash size — or override the chip
  manually if auto-detection misbehaves.
- **Backup** — read the entire flash (or any range) to a `.bin` file on disk.
  Set the size explicitly or leave it on *Auto-detect*: the app runs
  `flash_id` first, then chains the `read_flash`.
- **Restore** — write a `.bin` file back to the device, optionally erasing
  the chip first.
- **Live log + progress bar** — esptool's stdout is streamed line-by-line
  into the log, and percentages are parsed to drive the progress bar.
- **Cancel** any in-flight operation without leaving the app.
- **Hex viewer** (Ctrl+H, or the *View* button next to each file picker) —
  classic *offset · hex · ASCII* layout, virtualized via a
  `QAbstractTableModel` over `mmap`, so even a 16 MB dump opens instantly.
  Includes a Goto-offset field that accepts decimal, `0x` hex, etc.
- **Serial monitor** (Ctrl+M, or *Serial Monitor…* in the Connection group)
  — minicom-style live monitor with adjustable baud, send line + line-ending
  selector, optional local echo, timestamps and hex view, *Reset Device*
  button (pulses RTS), and Save Log. Multiple monitors can run on different
  ports simultaneously.
- **Resilient port detection** — pyserial enumeration is augmented with a
  glob fallback (`/dev/ttyUSB*`, `/dev/ttyACM*`, `/dev/ttyAMA*`,
  `/dev/serial/by-id/*` on Linux; `cu.usb*` / `cu.SLAB*` etc. on macOS), and
  the port field is editable so you can always type a path manually
  (`/dev/ttyUSB0`, `COM3`, `/dev/serial/by-id/usb-Silicon_Labs_…`).

## Supported boards

esptool detects the chip over the bootloader handshake, so any board built
around a supported SoC works without configuration. Verified targets include
the PlatformIO board profiles:

| PlatformIO board                     | Detected as           |
| ------------------------------------ | --------------------- |
| `espressif8266-nodemcuv2`            | `ESP8266EX`           |
| `espressif32-esp32dev`               | `ESP32-D0WD…`         |
| `espressif32-esp32-c3-devkitm-1`     | `ESP32-C3`            |

…plus any other ESP8266 / ESP32-S2 / -S3 / -C2 / -C6 / -H2 dev board.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Dependencies (`requirements.txt`):

- `PyQt6>=6.5`
- `esptool>=4.7`
- `pyserial>=3.5`

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

On some distros (Arch, Fedora) the group is `uucp` instead.

## Usage

1. Plug in the device and click **Refresh** if the port isn't already shown.
2. Choose a baud rate (460800 is a good default; drop to 115200 for finicky
   USB-serial cables).
3. Click **Detect Chip** — chip type, MAC and flash size populate.
4. **To back up:** pick an output `.bin` path → *Backup Flash*.
5. **To restore:** pick the `.bin` to write → *Restore Flash* (the *Erase
   entire flash first* checkbox is on by default, which is what you usually
   want when restoring a full dump).
6. **To inspect:** click *View* next to either file path, or open
   *Tools → Hex Viewer…* (Ctrl+H).
7. **To watch device output:** *Serial Monitor…* in the Connection group,
   or *Tools → Serial Monitor…* (Ctrl+M). Pick a baud (most ESP firmware
   logs at 115200; ESP8266 boot ROM messages are at 74880), click
   *Connect*, and use *Reset Device* to re-trigger boot output. Note:
   esptool and the monitor can't share the port — disconnect the monitor
   before running Detect / Backup / Restore (the *Serial Monitor…* button
   on the main window is disabled while an esptool op is in flight).

The status bar shows the result of the last operation; full esptool output
stays in the log pane.

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

The build pulls `--collect-all esptool` and `--collect-all serial` so
esptool's per-chip target submodules, stub-flasher data files and
pyserial's platform backends all end up in the bundle — without these,
chip detection silently breaks at runtime when the analyser misses the
dynamic imports. Cross-compilation is not supported by PyInstaller; build
on the OS you want to ship for.

## Project layout

```
esp_flasher/
├── esp_flasher.py            # single-file PyQt6 app
├── build.py                  # PyInstaller driver
├── requirements.txt          # runtime deps
├── requirements-build.txt    # build-only deps (pyinstaller)
└── README.md
```

Internals at a glance:

- `EspFlasher` — `QMainWindow` with the connection / backup / restore
  groups, progress bar and log pane. Drives esptool through `QProcess` with
  merged stdout/stderr; output is regex-scanned for chip info, flash size
  and percentage values.
- `HexModel` / `HexViewer` — `QAbstractTableModel` + `QDialog` rendering
  *offset · hex · ASCII* rows from a memory-mapped file. Closes the mmap on
  dialog close.
- `SerialReader` / `SerialMonitor` — `QThread` worker polling
  `serial.in_waiting` and emitting raw bytes, paired with a `QDialog`
  hosting the terminal-style display, send bar and reset/save controls.
- `list_serial_ports()` — pyserial enumeration plus a platform-specific
  glob fallback so freshly-attached USB UARTs always show up.

## Notes & caveats

- A full 4 MB read at 460800 baud takes about 40 seconds; a 16 MB chip is
  proportionally slower. Higher baud rates (921600, 1500000) work on most
  modern USB-serial chips but not all — back off if you see CRC errors.
- A flash dump captured from one chip is generally *not* portable to a
  different chip variant; restore to the same model you read from.
- The tool runs the `esptool` Python module from the same interpreter that
  launched the GUI (`sys.executable -m esptool`), so a venv keeps versions
  aligned automatically.

## License

MIT.

