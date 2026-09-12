"""ESP Flash Backup Tool — PyQt6 GUI and CLI around esptool.

Reads, writes and inspects flash images on ESP8266 and ESP32 family chips
(ESP32, ESP32-C3, -S2, -S3, -P4, …), whole-chip or per-partition. Chip and
flash size are auto-detected. Every esptool invocation runs in a QProcess
through a sequential job queue (`Job`, `EspFlasher._queue`), so the UI stays
responsive and any step is cancellable; the eFuse panel drives `espefuse`
the same way.

Launched with no arguments this opens the GUI; with a subcommand it is a
headless CLI (`cli_main()`) sharing the manifest, trimming and chip-mismatch
guard logic with the GUI. Under PyInstaller it re-invokes itself with the
`--esptool-worker` / `--espefuse-worker` sentinels, because `sys.executable`
is then the bundle rather than a Python interpreter.

Works against both esptool 4.x (underscore commands, "Chip is …") and
esptool 5.x (hyphenated commands, "Chip type: …"); the differences live in
`esptool_cmd()`, `esptool_flag()` and `OutputScanner`.

Requires Python 3.10+.
"""

import argparse
import collections
import glob
import hashlib
import json
import mmap
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import esptool
import serial
import serial.tools.list_ports
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import QProcess

try:
    import espefuse
    ESPEFUSE_ERROR = ""
except Exception as exc:  # noqa: BLE001 - a broken optional dep must not
    espefuse = None       # take the whole app down, just the eFuse panel
    ESPEFUSE_ERROR = f"{type(exc).__name__}: {exc}"

try:
    from esptool.targets import CHIP_LIST as _ESPTOOL_CHIP_LIST
except ImportError:
    _ESPTOOL_CHIP_LIST = []

try:
    import grp
except ImportError:  # not POSIX
    grp = None


APP_VERSION = "0.3.0"
APP_NAME = "ESP Flash Backup Tool"


def esptool_version() -> tuple[int, ...]:
    """Numeric prefix of esptool.__version__, e.g. (5, 2, 0)."""
    parts: list[int] = []
    for chunk in re.split(r"[.\-+]", str(getattr(esptool, "__version__", "0"))):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            break
    return tuple(parts) or (0,)


ESPTOOL_VERSION = esptool_version()
ESPTOOL_MAJOR = ESPTOOL_VERSION[0]


def esptool_cmd(name: str) -> str:
    """Spell a subcommand the way the installed esptool wants it.

    esptool 5 renamed every command to kebab-case; the underscore forms still
    work but print a deprecation warning and are slated for removal in 6.
    """
    return name.replace("_", "-") if ESPTOOL_MAJOR >= 5 else name


_FLAG_ALIASES_V4 = {
    "--flash-mode": "--flash_mode",
    "--flash-freq": "--flash_freq",
    "--flash-size": "--flash_size",
}


def esptool_flag(flag: str) -> str:
    """Spell an option the way the installed esptool wants it.

    esptool 5 hyphenated every option name. 4.x had already hyphenated most
    of them (--erase-all, --output, --format, --no-progress) but kept the
    three --flash_* options underscored, so only those need translating.
    """
    if ESPTOOL_MAJOR >= 5:
        return flag
    return _FLAG_ALIASES_V4.get(flag, flag)


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    """Drop CSI escape sequences.

    esptool enables its "smart terminal" output whenever $TERM looks
    colour-capable — even when stdout is a pipe — so cursor-movement and
    colour codes otherwise land in the log pane as literal garbage.
    """
    return ANSI_RE.sub("", text)


def list_serial_ports() -> list[tuple[str, str]]:
    """Return [(device_path, description)] tuples.

    pyserial's enumeration occasionally misses freshly-attached USB UARTs
    (race with udev, missing permissions on sysfs). We merge in a glob
    fallback so /dev/ttyUSB0, /dev/ttyACM0 etc. still appear.
    """
    seen: dict[str, str] = {}
    try:
        for p in serial.tools.list_ports.comports():
            seen[p.device] = p.description or "n/a"
    except Exception:
        pass

    if sys.platform.startswith("linux"):
        patterns = ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyAMA*", "/dev/serial/by-id/*")
    elif sys.platform == "darwin":
        patterns = ("/dev/cu.usb*", "/dev/cu.SLAB*", "/dev/cu.wchusb*",
                    "/dev/tty.usb*", "/dev/tty.SLAB*")
    else:
        patterns = ()
    for pat in patterns:
        for path in sorted(glob.glob(pat)):
            seen.setdefault(path, "(detected)")

    return sorted(seen.items())


def serial_permission_hint(port: str) -> str | None:
    """Return a fix-it hint when `port` exists but isn't readable/writable."""
    if not port or not sys.platform.startswith("linux"):
        return None
    try:
        st = os.stat(port)
    except OSError:
        return None
    if os.access(port, os.R_OK | os.W_OK):
        return None
    group = str(st.st_gid)
    if grp is not None:
        try:
            group = grp.getgrgid(st.st_gid).gr_name
        except KeyError:
            pass
    return (f"No read/write permission on {port}. Add yourself to the "
            f"'{group}' group and re-login:\n    sudo usermod -aG {group} \"$USER\"")


FLASH_SIZE_MAP: dict[str, int | None] = {
    "Auto-detect": None,
    "256 KB":  0x40000,
    "512 KB":  0x80000,
    "1 MB":    0x100000,
    "2 MB":    0x200000,
    "4 MB":    0x400000,
    "8 MB":    0x800000,
    "16 MB":   0x1000000,
    "32 MB":   0x2000000,
    "64 MB":   0x4000000,
    "128 MB":  0x8000000,
}

BAUD_RATES = ["115200", "230400", "460800", "921600", "1500000", "2000000"]
DEFAULT_BAUD = "460800"

FLASH_MODES = ["keep", "qio", "qout", "dio", "dout"]
FLASH_FREQS = ["keep", "80m", "60m", "48m", "40m", "30m", "26m", "24m", "20m"]
FLASH_SIZE_FLAGS = ["keep", "detect", "256KB", "512KB", "1MB", "2MB", "4MB",
                    "8MB", "16MB", "32MB", "64MB", "128MB"]

_CHIP_LABELS = {
    "esp8266": "ESP8266", "esp32": "ESP32", "esp32s2": "ESP32-S2",
    "esp32s3": "ESP32-S3", "esp32c2": "ESP32-C2", "esp32c3": "ESP32-C3",
    "esp32c5": "ESP32-C5", "esp32c6": "ESP32-C6", "esp32c61": "ESP32-C61",
    "esp32e22": "ESP32-E22", "esp32h2": "ESP32-H2", "esp32h21": "ESP32-H21",
    "esp32h4": "ESP32-H4", "esp32p4": "ESP32-P4", "esp32s31": "ESP32-S31",
}

_FALLBACK_CHIPS = ["esp8266", "esp32", "esp32s2", "esp32s3", "esp32c2",
                   "esp32c3", "esp32c6", "esp32h2", "esp32p4"]


def chip_choices() -> list[tuple[str, str | None]]:
    """[(label, --chip flag)] for the installed esptool's supported targets."""
    flags = [c for c in (_ESPTOOL_CHIP_LIST or _FALLBACK_CHIPS) if c != "auto"]
    out: list[tuple[str, str | None]] = [("Auto", None)]
    for flag in flags:
        out.append((_CHIP_LABELS.get(flag, flag.upper()), flag))
    return out


CHIP_CHOICES = chip_choices()


def chip_family(description: str) -> str:
    """Normalise a chip description to a comparable family key.

    "ESP32-D0WD-V3 (revision v3.0)" -> "ESP32"
    "ESP32-C3 (QFN32) (revision v0.4)" -> "ESP32-C3"
    """
    text = strip_ansi(description or "").strip().upper()
    text = re.sub(r"\(.*?\)", " ", text)
    text = text.split(" IN SECURE")[0]
    token = text.split()[0] if text.split() else ""
    token = token.strip(",;")
    if token.startswith("ESP8266"):
        return "ESP8266"
    m = re.match(r"(ESP32)(?:-(C\d+|S\d+|H\d+|P\d+|E\d+))?", token)
    if not m:
        return token
    return f"{m.group(1)}-{m.group(2)}" if m.group(2) else "ESP32"


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} GB"


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


class OutputScanner:
    """Pure line scanner for esptool stdout (4.x and 5.x dialects).

    `feed()` returns a dict of whatever the line revealed; a line flagged
    ``progress`` is a progress-bar repaint and should stay out of the log —
    esptool emits one every 1024 bytes, so a 16 MB read is ~16k lines.
    """

    CHIP_RE = re.compile(r"^(?:Chip is|Chip type:)\s+(.+?)\s*$")
    MAC_RE = re.compile(r"^MAC:\s*((?:[0-9a-fA-F]{2}:){5,}[0-9a-fA-F]{2})\s*$")
    CRYSTAL_RE = re.compile(r"^(?:Crystal is|Crystal frequency:)\s*(\d+)\s*MHz",
                            re.IGNORECASE)
    FEATURES_RE = re.compile(r"^(?:Features:|Chip features:)\s*(.+?)\s*$")
    FLASH_RE = re.compile(r"flash size:\s*(\d+)\s*(MB|KB)\b", re.IGNORECASE)
    PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
    BYTES_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*bytes")

    def feed(self, line: str) -> dict:
        line = strip_ansi(line)
        stripped = line.strip()
        found: dict = {}

        pct = self.PCT_RE.search(stripped)
        if pct:
            found["progress"] = True
            try:
                found["percent"] = max(0.0, min(100.0, float(pct.group(1))))
            except ValueError:
                pass
            nums = self.BYTES_RE.search(stripped)
            if nums:
                found["done"] = int(nums.group(1))
                found["total"] = int(nums.group(2))
            return found

        m = self.CHIP_RE.match(stripped)
        if m:
            found["chip"] = m.group(1).strip()
        m = self.MAC_RE.match(stripped)
        if m:
            found["mac"] = m.group(1)
        m = self.CRYSTAL_RE.match(stripped)
        if m:
            found["crystal_mhz"] = int(m.group(1))
        m = self.FEATURES_RE.match(stripped)
        if m:
            found["features"] = m.group(1)
        m = self.FLASH_RE.search(stripped)
        if m:
            mult = 1024 * 1024 if m.group(2).upper() == "MB" else 1024
            found["flash_bytes"] = int(m.group(1)) * mult
        return found


PARTITION_TABLE_SIZE = 0xC00
DEFAULT_PT_OFFSET = 0x8000

_APP_SUBTYPES = {0x00: "factory", 0x20: "test"}
_DATA_SUBTYPES = {
    0x00: "ota", 0x01: "phy", 0x02: "nvs", 0x03: "coredump",
    0x04: "nvs_keys", 0x05: "efuse", 0x06: "undefined",
    0x80: "esphttpd", 0x81: "fat", 0x82: "spiffs", 0x83: "littlefs",
}


def _type_name(t: int) -> str:
    return {0x00: "app", 0x01: "data"}.get(t, f"0x{t:02x}")


def _subtype_name(t: int, s: int) -> str:
    if t == 0x00:
        if 0x10 <= s <= 0x1F:
            return f"ota_{s - 0x10}"
        return _APP_SUBTYPES.get(s, f"0x{s:02x}")
    if t == 0x01:
        return _DATA_SUBTYPES.get(s, f"0x{s:02x}")
    return f"0x{s:02x}"


def parse_partition_table(data: bytes) -> list[dict]:
    """Parse an ESP-IDF partition table (the 0xC00 region at 0x8000).

    Each entry is 32 bytes: magic(2) type(1) subtype(1) offset(4) size(4)
    label(16) flags(4). 0xAA50 = partition, 0xEBEB = MD5 checksum entry
    (skipped), anything else (0xFFFF / blank) ends the table.
    """
    entries: list[dict] = []
    for i in range(0, min(len(data), PARTITION_TABLE_SIZE), 32):
        chunk = data[i:i + 32]
        if len(chunk) < 32:
            break
        magic = chunk[0:2]
        if magic == b"\xeb\xeb":  # MD5 checksum entry
            continue
        if magic != b"\xaa\x50":  # 0xFFFF blank / end of table
            break
        ptype = chunk[2]
        psub = chunk[3]
        offset = int.from_bytes(chunk[4:8], "little")
        size = int.from_bytes(chunk[8:12], "little")
        label = chunk[12:28].split(b"\x00", 1)[0].decode("utf-8", "replace")
        flags = int.from_bytes(chunk[28:32], "little")
        entries.append({
            "label": label, "type": ptype, "subtype": psub,
            "offset": offset, "size": size, "flags": flags,
        })
    return entries


def parse_flash_args(text: str, base_dir: str = "") -> tuple[list[tuple[int, str]], dict]:
    """Parse an ESP-IDF `flash_args` file.

    Option lines (``--flash_mode dio --flash_freq 40m``) become a dict with
    kebab-cased keys; every other line is an ``<address> <path>`` pair with
    paths resolved against `base_dir`.
    """
    images: list[tuple[int, str]] = []
    opts: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.split()
        if tokens[0].startswith("--"):
            i = 0
            while i < len(tokens):
                if not tokens[i].startswith("--"):
                    i += 1
                    continue
                key = tokens[i][2:].replace("_", "-")
                if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                    opts[key] = tokens[i + 1]
                    i += 2
                else:
                    opts[key] = "true"
                    i += 1
            continue
        if len(tokens) < 2:
            continue
        try:
            addr = int(tokens[0], 0)
        except ValueError:
            continue
        path = " ".join(tokens[1:])
        if base_dir and not os.path.isabs(path):
            path = os.path.normpath(os.path.join(base_dir, path))
        images.append((addr, path))
    return images, opts


MANIFEST_SUFFIX = ".json"


def manifest_path(bin_path: str) -> str:
    return bin_path + MANIFEST_SUFFIX


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def build_manifest(bin_path: str, *, chip: str | None = None,
                   mac: str | None = None, flash_bytes: int | None = None,
                   address: int = 0, original_size: int | None = None,
                   created: str | None = None) -> dict:
    size = os.path.getsize(bin_path)
    return {
        "tool": "esp_flasher",
        "tool_version": APP_VERSION,
        "created": created or QtCore.QDateTime.currentDateTimeUtc().toString(
            QtCore.Qt.DateFormat.ISODate),
        "chip": chip,
        "chip_family": chip_family(chip) if chip else None,
        "mac": mac,
        "flash_size": flash_bytes,
        "address": address,
        "size": size,
        "original_size": original_size if original_size is not None else size,
        "sha256": sha256_file(bin_path),
    }


def write_manifest(bin_path: str, meta: dict) -> str:
    path = manifest_path(bin_path)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def read_manifest(bin_path: str) -> dict | None:
    path = manifest_path(bin_path)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def restore_mismatch(meta: dict | None, chip: str | None) -> str | None:
    """Describe a manifest/device mismatch, or None when the restore looks safe."""
    if not meta or not chip:
        return None
    recorded = meta.get("chip_family") or (
        chip_family(meta["chip"]) if meta.get("chip") else None)
    if not recorded:
        return None
    live = chip_family(chip)
    if recorded != live:
        return (f"This image was captured from {recorded} "
                f"({meta.get('chip') or 'unknown revision'}) but the connected "
                f"device is {live} ({chip}).\n\nFlash layouts and calibration "
                f"data are chip-specific; writing it is likely to brick the "
                f"device.")
    return None


def trailing_blank_length(path: str, blank: int = 0xFF, chunk: int = 1 << 20) -> int:
    """Bytes of uniform `blank` fill at the end of `path`."""
    size = os.path.getsize(path)
    if size == 0:
        return 0
    pattern = bytes([blank])
    run = 0
    with open(path, "rb") as fh:
        pos = size
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step)
            stripped = data.rstrip(pattern)
            run += len(data) - len(stripped)
            if stripped:
                break
    return run


def trim_trailing_blank(path: str, align: int = 4096,
                        min_gain: int = 64 * 1024) -> tuple[int, int] | None:
    """Truncate a uniform 0xFF tail off a flash dump.

    Returns (new_size, original_size), or None when there was nothing worth
    trimming. The kept length is rounded up to `align` and never reaches zero,
    so a fully-erased dump still keeps one aligned block.
    """
    size = os.path.getsize(path)
    blank = trailing_blank_length(path)
    if blank <= 0:
        return None
    keep = size - blank
    keep = ((keep + align - 1) // align) * align
    keep = max(align, min(keep, size))
    if size - keep < min_gain:
        return None
    with open(path, "r+b") as fh:
        fh.truncate(keep)
    return keep, size


_EFUSE_FIELD_RE = re.compile(
    r"^([A-Z0-9_]+)\s*\(BLOCK\d+\).*?=\s*(\S+)(?:.*?\((0b[01]+|0x[0-9a-fA-F]+)\))?")


def _crypt_cnt_active(value: str, binary: str | None) -> bool:
    """Flash encryption is live when an odd number of counter bits are set."""
    bits = binary or value
    try:
        number = int(bits, 0) if bits.startswith(("0b", "0x")) else int(bits)
    except ValueError:
        return False
    return bin(number).count("1") % 2 == 1


def parse_efuse_security(text: str) -> dict:
    """Pull the security-relevant eFuses out of `espefuse summary` output."""
    fields: dict[str, str] = {}
    flash_enc: bool | None = None
    secure_boot: bool | None = None
    for raw in text.splitlines():
        m = _EFUSE_FIELD_RE.match(strip_ansi(raw).strip())
        if not m:
            continue
        name, value, binary = m.group(1), m.group(2), m.group(3)
        fields[name] = value
        if name in ("FLASH_CRYPT_CNT", "SPI_BOOT_CRYPT_CNT"):
            active = _crypt_cnt_active(value, binary)
            flash_enc = active or bool(flash_enc)
        elif name in ("ABS_DONE_0", "ABS_DONE_1", "SECURE_BOOT_EN"):
            active = value.lower() in ("true", "1")
            secure_boot = active or bool(secure_boot)
    return {"flash_encryption": flash_enc, "secure_boot": secure_boot,
            "fields": fields}


_BACKTRACE_RE = re.compile(r"Backtrace ?:?\s*((?:0x[0-9a-fA-F]+:0x[0-9a-fA-F]+\s*)+)")
_PC_RE = re.compile(r"(?:MEPC|PC)\s*[:=]\s*(0x[0-9a-fA-F]+)")


def parse_backtrace(line: str) -> list[int]:
    """Extract program counters from an ESP-IDF panic backtrace line.

    Handles the Xtensa ``Backtrace: 0xpc:0xsp 0xpc:0xsp`` form and the
    RISC-V ``MEPC : 0x…`` register dump.
    """
    line = strip_ansi(line)
    m = _BACKTRACE_RE.search(line)
    if m:
        return [int(pair.split(":")[0], 16) for pair in m.group(1).split()]
    return [int(m.group(1), 16) for m in _PC_RE.finditer(line)]


ADDR2LINE_CANDIDATES = (
    "xtensa-esp32-elf-addr2line", "xtensa-esp32s2-elf-addr2line",
    "xtensa-esp32s3-elf-addr2line", "riscv32-esp-elf-addr2line",
    "xtensa-esp-elf-addr2line", "addr2line",
)


def find_addr2line(preferred: str = "") -> str | None:
    if preferred:
        return preferred if shutil.which(preferred) else None
    for name in ADDR2LINE_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def decode_addresses(elf: str, addresses: list[int], tool: str = "",
                     timeout: float = 10.0) -> list[str]:
    """Resolve PCs to ``function at file:line`` via addr2line."""
    exe = find_addr2line(tool)
    if not exe or not addresses or not os.path.isfile(elf):
        return []
    argv = [exe, "-pfiaC", "-e", elf] + [f"0x{a:x}" for a in addresses]
    try:
        out = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln.rstrip() for ln in out.stdout.splitlines() if ln.strip()]


_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")

ANSI_COLORS = {
    30: "#000000", 31: "#c0392b", 32: "#27ae60", 33: "#b8860b",
    34: "#2980b9", 35: "#8e44ad", 36: "#16a085", 37: "#bdc3c7",
    90: "#7f8c8d", 91: "#e74c3c", 92: "#2ecc71", 93: "#f1c40f",
    94: "#3498db", 95: "#9b59b6", 96: "#1abc9c", 97: "#ecf0f1",
}


def parse_ansi_segments(text: str, state: dict | None = None
                        ) -> tuple[list[tuple[str, dict]], dict]:
    """Split `text` into (chunk, style) pairs, honouring SGR colour codes.

    `state` carries the active style across calls so colours survive being
    split over serial reads. Returns (segments, new_state).
    """
    style = dict(state or {})
    segments: list[tuple[str, dict]] = []
    pos = 0
    for m in _SGR_RE.finditer(text):
        if m.start() > pos:
            segments.append((text[pos:m.start()], dict(style)))
        for code in (m.group(1) or "0").split(";"):
            try:
                value = int(code or "0")
            except ValueError:
                continue
            if value == 0:
                style.clear()
            elif value == 1:
                style["bold"] = True
            elif value == 22:
                style.pop("bold", None)
            elif value == 39:
                style.pop("color", None)
            elif value in ANSI_COLORS:
                style["color"] = ANSI_COLORS[value]
        pos = m.end()
    if pos < len(text):
        segments.append((text[pos:], dict(style)))
    return segments, style


def parse_search_needle(text: str, mode: str = "auto") -> bytes | None:
    """Turn a search box string into bytes.

    mode "hex" reads ``de ad be ef`` / ``0xdeadbeef``, "text" reads UTF-8,
    "auto" picks hex when the input looks like an even run of hex digits.
    """
    text = text.strip()
    if not text:
        return None
    if mode == "text":
        return text.encode("utf-8", "replace")
    compact = re.sub(r"(?:\s|0x|,|:)", "", text, flags=re.IGNORECASE)
    looks_hex = bool(compact) and len(compact) % 2 == 0 and \
        re.fullmatch(r"[0-9a-fA-F]+", compact) is not None
    if mode == "hex":
        if not looks_hex:
            return None
        return bytes.fromhex(compact)
    if looks_hex and (len(compact) > 1 or text.lower().startswith("0x")):
        return bytes.fromhex(compact)
    return text.encode("utf-8", "replace")


def search_bytes(data, needle: bytes, start: int = 0,
                 backwards: bool = False) -> int:
    """Find `needle` in a bytes-like/mmap, wrapping around. -1 when absent."""
    if not needle:
        return -1
    size = len(data)
    if size == 0:
        return -1
    start = max(0, min(start, size))
    if backwards:
        hit = data.rfind(needle, 0, start)
        if hit >= 0:
            return hit
        return data.rfind(needle)
    hit = data.find(needle, start)
    if hit >= 0:
        return hit
    return data.find(needle, 0, start)


PROFILE_FIELDS = {
    "port": str, "baud": str, "chip_idx": int, "backup_addr": str,
    "backup_size_idx": int, "restore_addr": str, "erase_first": bool,
    "verify_after": bool, "trim_backup": bool,
}


def sanitize_profile(data: dict) -> dict:
    """Keep only known profile fields, coerced to their declared types."""
    out: dict = {}
    for key, kind in PROFILE_FIELDS.items():
        if key not in data:
            continue
        value = data[key]
        try:
            if kind is bool:
                out[key] = value if isinstance(value, bool) else \
                    str(value).lower() in ("1", "true", "yes")
            else:
                out[key] = kind(value)
        except (TypeError, ValueError):
            continue
    return out


class HexModel(QtCore.QAbstractTableModel):
    HEADERS = ("Offset", "Hex", "ASCII")
    BYTES_PER_ROW = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fh = None
        self._mm: mmap.mmap | None = None
        self._size = 0
        self._other_fh = None
        self._other_mm: mmap.mmap | None = None
        self._other_size = 0
        self._other_path = ""
        self._highlight: tuple[int, int] | None = None

    def open(self, path: str):
        self.beginResetModel()
        self.close()
        self._fh = open(path, "rb")
        self._size = os.path.getsize(path)
        if self._size > 0:
            self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        self.endResetModel()

    def open_other(self, path: str):
        """Attach a second image for side-by-side difference highlighting."""
        self.beginResetModel()
        self._close_other()
        self._other_fh = open(path, "rb")
        self._other_size = os.path.getsize(path)
        if self._other_size > 0:
            self._other_mm = mmap.mmap(self._other_fh.fileno(), 0,
                                       access=mmap.ACCESS_READ)
        self._other_path = path
        self.endResetModel()

    def _close_other(self):
        if self._other_mm is not None:
            self._other_mm.close()
            self._other_mm = None
        if self._other_fh is not None:
            self._other_fh.close()
            self._other_fh = None
        self._other_size = 0
        self._other_path = ""

    def clear_other(self):
        self.beginResetModel()
        self._close_other()
        self.endResetModel()

    def close(self):
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self._size = 0
        self._close_other()
        self._highlight = None

    def size(self) -> int:
        return self._size

    def data_source(self):
        return self._mm

    def other_path(self) -> str:
        return self._other_path

    def has_other(self) -> bool:
        return self._other_mm is not None

    def set_highlight(self, offset: int, length: int):
        """Mark a byte range (a search hit) for background tinting."""
        self._highlight = (offset, length) if length > 0 else None
        if self._size:
            top = self.index(0, 0)
            bottom = self.index(self.rowCount() - 1, self.columnCount() - 1)
            self.dataChanged.emit(top, bottom,
                                  [QtCore.Qt.ItemDataRole.BackgroundRole])

    def read(self, offset: int, length: int) -> bytes:
        if self._mm is None or length <= 0:
            return b""
        offset = max(0, min(offset, self._size))
        return bytes(self._mm[offset:min(offset + length, self._size)])

    def row_differs(self, row: int) -> bool:
        if self._other_mm is None or self._mm is None:
            return False
        start = row * self.BYTES_PER_ROW
        end = start + self.BYTES_PER_ROW
        mine = self._mm[start:min(end, self._size)]
        theirs = self._other_mm[start:min(end, self._other_size)] \
            if start < self._other_size else b""
        return mine != theirs

    def find_next_diff(self, start_byte: int, backwards: bool = False,
                       chunk: int = 1 << 16) -> int:
        """Offset of the next differing byte vs the compare image, or -1."""
        if self._other_mm is None or self._mm is None:
            return -1
        limit = max(self._size, self._other_size)

        def byte_at(source, size, index):
            return source[index] if index < size else None

        step = -1 if backwards else 1
        pos = start_byte
        while 0 <= pos < limit:
            lo = pos if not backwards else max(0, pos - chunk + 1)
            hi = min(limit, pos + chunk) if not backwards else pos + 1
            mine = self._mm[lo:min(hi, self._size)] if lo < self._size else b""
            theirs = self._other_mm[lo:min(hi, self._other_size)] \
                if lo < self._other_size else b""
            span = hi - lo
            mine = mine.ljust(span, b"\x00") if len(mine) < span else mine
            theirs = theirs.ljust(span, b"\x00") if len(theirs) < span else theirs
            if mine != theirs:
                indices = range(pos - lo, span) if not backwards \
                    else range(pos - lo, -1, -1)
                for i in indices:
                    absolute = lo + i
                    a = byte_at(self._mm, self._size, absolute)
                    b = byte_at(self._other_mm, self._other_size, absolute)
                    if a != b:
                        return absolute
            pos = hi if not backwards else lo - 1
            if step > 0 and pos >= limit:
                break
        return -1

    def rowCount(self, parent=QtCore.QModelIndex()):
        if parent.isValid():
            return 0
        return (self._size + self.BYTES_PER_ROW - 1) // self.BYTES_PER_ROW

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 3

    def headerData(self, section, orientation, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if role == QtCore.Qt.ItemDataRole.DisplayRole and orientation == QtCore.Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self._mm is None:
            return None
        row, col = index.row(), index.column()
        offset = row * self.BYTES_PER_ROW
        end = min(offset + self.BYTES_PER_ROW, self._size)
        chunk = self._mm[offset:end]

        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return f"{offset:08X}"
            if col == 1:
                cells = [f"{b:02X}" for b in chunk]
                while len(cells) < self.BYTES_PER_ROW:
                    cells.append("  ")
                return " ".join(cells[:8]) + "  " + " ".join(cells[8:])
            if col == 2:
                return "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        elif role == QtCore.Qt.ItemDataRole.TextAlignmentRole and col == 0:
            return int(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        elif role == QtCore.Qt.ItemDataRole.BackgroundRole:
            if self._highlight is not None:
                hit_start, hit_len = self._highlight
                if offset < hit_start + hit_len and hit_start < end:
                    return QtGui.QColor(255, 235, 130)
            if self.row_differs(row):
                return QtGui.QColor(255, 205, 205)
        return None


class HexViewer(QtWidgets.QDialog):
    """offset · hex · ASCII viewer with search, image diff and partition jumps."""

    def __init__(self, parent=None, path: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Hex Viewer")
        self.resize(980, 660)

        self.model = HexModel(self)
        self._partitions: list[dict] = []
        self._last_hit = -1

        v = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        self.path_lbl = QtWidgets.QLabel("(no file)")
        self.path_lbl.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        bar.addWidget(self.path_lbl, 1)

        open_btn = QtWidgets.QPushButton("Open…")
        open_btn.clicked.connect(self._open_dialog)
        bar.addWidget(open_btn)

        bar.addWidget(QtWidgets.QLabel("Goto:"))
        self.goto_edit = QtWidgets.QLineEdit()
        self.goto_edit.setPlaceholderText("0x1000")
        self.goto_edit.setMaximumWidth(110)
        self.goto_edit.returnPressed.connect(self._goto)
        bar.addWidget(self.goto_edit)
        go_btn = QtWidgets.QPushButton("Go")
        go_btn.clicked.connect(self._goto)
        bar.addWidget(go_btn)

        self.part_combo = QtWidgets.QComboBox()
        self.part_combo.setMinimumWidth(170)
        self.part_combo.setToolTip(
            "Partitions found in this image's table at 0x8000")
        self.part_combo.activated.connect(self._jump_partition)
        bar.addWidget(self.part_combo)
        v.addLayout(bar)

        find = QtWidgets.QHBoxLayout()
        find.addWidget(QtWidgets.QLabel("Find:"))
        self.find_edit = QtWidgets.QLineEdit()
        self.find_edit.setPlaceholderText("de ad be ef   or   some text")
        self.find_edit.returnPressed.connect(self.find_next)
        find.addWidget(self.find_edit, 1)
        self.find_mode = QtWidgets.QComboBox()
        self.find_mode.addItems(["Auto", "Hex", "Text"])
        find.addWidget(self.find_mode)
        prev_btn = QtWidgets.QPushButton("◀ Prev")
        prev_btn.clicked.connect(self.find_prev)
        find.addWidget(prev_btn)
        next_btn = QtWidgets.QPushButton("Next ▶")
        next_btn.clicked.connect(self.find_next)
        find.addWidget(next_btn)

        find.addSpacing(12)
        self.compare_btn = QtWidgets.QPushButton("Compare with…")
        self.compare_btn.clicked.connect(self._pick_compare)
        find.addWidget(self.compare_btn)
        self.diff_prev_btn = QtWidgets.QPushButton("◀ Diff")
        self.diff_prev_btn.clicked.connect(lambda: self._goto_diff(backwards=True))
        find.addWidget(self.diff_prev_btn)
        self.diff_next_btn = QtWidgets.QPushButton("Diff ▶")
        self.diff_next_btn.clicked.connect(lambda: self._goto_diff(backwards=False))
        find.addWidget(self.diff_next_btn)
        v.addLayout(find)

        self.table = QtWidgets.QTableView()
        self.table.setModel(self.model)
        mono = QtGui.QFont("monospace")
        mono.setStyleHint(QtGui.QFont.StyleHint.TypeWriter)
        self.table.setFont(mono)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(18)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.selectionModel().currentRowChanged.connect(self._update_inspector)
        v.addWidget(self.table, 1)

        self.size_lbl = QtWidgets.QLabel("")
        v.addWidget(self.size_lbl)
        self.inspect_lbl = QtWidgets.QLabel("")
        self.inspect_lbl.setFont(mono)
        self.inspect_lbl.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.inspect_lbl)

        self._update_diff_buttons()
        if path:
            self.open_file(path)

    def _open_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open file", "", "Binary (*.bin);;All files (*)"
        )
        if path:
            self.open_file(path)

    def open_file(self, path: str):
        try:
            self.model.open(path)
        except OSError as e:
            QtWidgets.QMessageBox.critical(self, "Error", str(e))
            return
        self.path_lbl.setText(path)
        self.size_lbl.setText(f"{self.model.size():,} bytes "
                              f"({human_bytes(self.model.size())})")
        self.setWindowTitle(f"Hex Viewer — {os.path.basename(path)}")
        self._last_hit = -1
        self._load_partitions()
        self._update_diff_buttons()
        self._update_inspector()

    def _load_partitions(self):
        self.part_combo.clear()
        self._partitions = []
        table = self.model.read(DEFAULT_PT_OFFSET, PARTITION_TABLE_SIZE)
        entries = parse_partition_table(table) if table else []
        self.part_combo.addItem(
            f"Partitions ({len(entries)})" if entries else "Partitions (none)", -1)
        for e in entries:
            self.part_combo.addItem(
                f"{e['label'] or _subtype_name(e['type'], e['subtype'])} "
                f"@ 0x{e['offset']:x}", e["offset"])
        self._partitions = entries
        self.part_combo.setEnabled(bool(entries))

    def _jump_partition(self, index: int):
        offset = self.part_combo.itemData(index)
        if offset is None or offset < 0:
            return
        if not self.scroll_to_offset(int(offset)):
            self.inspect_lbl.setText(
                f"0x{int(offset):X} is past the end of this file — it is a "
                f"partial dump, not a whole flash image.")

    def scroll_to_offset(self, offset: int, select: bool = True) -> bool:
        row = offset // HexModel.BYTES_PER_ROW
        if not (0 <= row < self.model.rowCount()):
            return False
        idx = self.model.index(row, 0)
        self.table.scrollTo(idx, QtWidgets.QAbstractItemView.ScrollHint.PositionAtTop)
        if select:
            self.table.selectRow(row)
        return True

    def _goto(self):
        text = self.goto_edit.text().strip()
        if not text:
            return
        try:
            offset = int(text, 0)
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Hex Viewer", f"Invalid offset: {text!r}")
            return
        if not self.scroll_to_offset(offset):
            self.inspect_lbl.setText(f"0x{offset:X} is past the end of this file.")

    def _needle(self) -> bytes | None:
        mode = self.find_mode.currentText().lower()
        return parse_search_needle(self.find_edit.text(), mode)

    def find_next(self):
        self._find(backwards=False)

    def find_prev(self):
        self._find(backwards=True)

    def _find(self, backwards: bool):
        source = self.model.data_source()
        if source is None:
            return
        needle = self._needle()
        if not needle:
            self.inspect_lbl.setText("Search: nothing to look for "
                                     "(hex mode needs whole bytes).")
            return
        if backwards:
            start = self._last_hit if self._last_hit >= 0 else self.model.size()
        else:
            start = self._last_hit + 1 if self._last_hit >= 0 else 0
        hit = search_bytes(source, needle, start, backwards)
        if hit < 0:
            self.inspect_lbl.setText(f"Search: {needle.hex(' ')} not found.")
            return
        self._last_hit = hit
        self.model.set_highlight(hit, len(needle))
        self.scroll_to_offset(hit)
        self.inspect_lbl.setText(
            f"Search: hit at 0x{hit:X} ({hit:,}) — {len(needle)} byte(s)")

    def _pick_compare(self):
        if self.model.has_other():
            self.model.clear_other()
            self._update_diff_buttons()
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Compare with image", "", "Binary (*.bin);;All files (*)")
        if not path:
            return
        try:
            self.model.open_other(path)
        except OSError as e:
            QtWidgets.QMessageBox.critical(self, "Hex Viewer", str(e))
            return
        self._update_diff_buttons()
        self._goto_diff(backwards=False)

    def _update_diff_buttons(self):
        on = self.model.has_other()
        self.compare_btn.setText("Stop comparing" if on else "Compare with…")
        self.diff_prev_btn.setEnabled(on)
        self.diff_next_btn.setEnabled(on)
        if on:
            self.size_lbl.setText(
                f"{self.model.size():,} bytes  ·  comparing against "
                f"{os.path.basename(self.model.other_path())}")

    def _goto_diff(self, backwards: bool):
        if not self.model.has_other():
            return
        current = self.table.currentIndex().row() * HexModel.BYTES_PER_ROW
        start = max(0, current - 1) if backwards else current + HexModel.BYTES_PER_ROW
        hit = self.model.find_next_diff(start, backwards)
        if hit < 0:
            self.inspect_lbl.setText("Diff: no further differences.")
            return
        self.scroll_to_offset(hit)
        self.inspect_lbl.setText(f"Diff: first differing byte at 0x{hit:X}")

    def _update_inspector(self, *_args):
        row = self.table.currentIndex().row()
        if row < 0 or self.model.data_source() is None:
            self.inspect_lbl.setText("")
            return
        offset = row * HexModel.BYTES_PER_ROW
        chunk = self.model.read(offset, 8)
        bits = [f"offset 0x{offset:X} ({offset:,})"]
        if chunk:
            bits.append(f"u8 {chunk[0]}")
        if len(chunk) >= 2:
            bits.append(f"u16le {int.from_bytes(chunk[:2], 'little')}")
        if len(chunk) >= 4:
            bits.append(f"u32le 0x{int.from_bytes(chunk[:4], 'little'):08X}")
            bits.append(f"i32le {int.from_bytes(chunk[:4], 'little', signed=True)}")
        self.inspect_lbl.setText("   ".join(bits))

    def closeEvent(self, e):
        self.model.close()
        super().closeEvent(e)


class SerialReader(QtCore.QThread):
    """Background reader: pulls bytes off a pyserial Serial and signals them."""
    bytes_received = QtCore.pyqtSignal(bytes)
    error = QtCore.pyqtSignal(str)

    def __init__(self, ser: "serial.Serial", parent=None):
        super().__init__(parent)
        self._ser = ser
        self._stop = False

    def run(self):
        while not self._stop:
            try:
                n = self._ser.in_waiting
                if n:
                    data = self._ser.read(n)
                    if data:
                        self.bytes_received.emit(bytes(data))
                else:
                    self.msleep(15)
            except (serial.SerialException, OSError) as e:
                if not self._stop:
                    self.error.emit(str(e))
                return

    def stop(self):
        self._stop = True


class SerialMonitor(QtWidgets.QDialog):
    """minicom-style serial monitor with ANSI colour, filtering and panic decode."""

    BAUDS = ["9600", "19200", "38400", "57600", "74880", "115200",
             "230400", "460800", "921600", "1500000"]
    EOLS = [("None", b""), ("LF (\\n)", b"\n"),
            ("CR (\\r)", b"\r"), ("CRLF", b"\r\n")]
    SCROLLBACK = 5000
    PARTIAL_FLUSH_MS = 120
    RECONNECT_MS = 1500

    def __init__(self, parent=None, port: str | None = None, baud: str = "115200"):
        super().__init__(parent)
        self.setWindowTitle("Serial Monitor")
        self.resize(940, 660)

        self.ser: serial.Serial | None = None
        self.reader: SerialReader | None = None
        # (text, arrival timestamp) — the timestamp is captured once, so
        # re-rendering after a filter change cannot restamp old lines.
        self._lines: collections.deque[tuple[str, str]] = collections.deque(
            maxlen=self.SCROLLBACK)
        self._pending = ""
        self._shown = 0
        self._ansi_state: dict = {}
        self._suspended_port: tuple[str, int] | None = None
        self._elf_path = ""

        self._flush_timer = QtCore.QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(self.PARTIAL_FLUSH_MS)
        self._flush_timer.timeout.connect(self._flush_partial)

        self._reconnect_timer = QtCore.QTimer(self)
        self._reconnect_timer.setInterval(self.RECONNECT_MS)
        self._reconnect_timer.timeout.connect(self._try_reconnect)

        self._build_ui()
        self._populate_ports(prefer=port)
        self.baud_combo.setCurrentText(baud)
        self._update_ui_state()

    def _build_ui(self):
        v = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Port:"))
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(260)
        top.addWidget(self.port_combo, 1)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(lambda: self._populate_ports())
        top.addWidget(refresh)

        top.addWidget(QtWidgets.QLabel("Baud:"))
        self.baud_combo = QtWidgets.QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems(self.BAUDS)
        self.baud_combo.setCurrentText("115200")
        top.addWidget(self.baud_combo)

        self.connect_btn = QtWidgets.QPushButton("Connect")
        self.connect_btn.clicked.connect(self._toggle_connection)
        top.addWidget(self.connect_btn)
        v.addLayout(top)

        bar = QtWidgets.QHBoxLayout()
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(self._clear)
        bar.addWidget(clear)
        self.reset_btn = QtWidgets.QPushButton("Reset Device")
        self.reset_btn.setToolTip("Pulse RTS low/high to reset the chip")
        self.reset_btn.clicked.connect(self._reset_device)
        bar.addWidget(self.reset_btn)
        self.boot_btn = QtWidgets.QPushButton("Reset → Bootloader")
        self.boot_btn.setToolTip(
            "DTR/RTS sequence that reboots the chip into serial download mode")
        self.boot_btn.clicked.connect(self._reset_to_bootloader)
        bar.addWidget(self.boot_btn)
        save = QtWidgets.QPushButton("Save Log…")
        save.clicked.connect(self._save_log)
        bar.addWidget(save)
        bar.addStretch(1)
        self.echo_chk = QtWidgets.QCheckBox("Local echo")
        self.timestamps_chk = QtWidgets.QCheckBox("Timestamps")
        self.timestamps_chk.toggled.connect(self._rerender)
        self.autoscroll_chk = QtWidgets.QCheckBox("Auto-scroll")
        self.autoscroll_chk.setChecked(True)
        self.ansi_chk = QtWidgets.QCheckBox("ANSI colour")
        self.ansi_chk.setChecked(True)
        self.ansi_chk.toggled.connect(self._rerender)
        self.hex_chk = QtWidgets.QCheckBox("Hex")
        self.reconnect_chk = QtWidgets.QCheckBox("Auto-reconnect")
        self.reconnect_chk.setChecked(True)
        self.reconnect_chk.setToolTip(
            "Reopen the port automatically after an unplug or a flash")
        for w in (self.echo_chk, self.timestamps_chk, self.autoscroll_chk,
                  self.ansi_chk, self.hex_chk, self.reconnect_chk):
            bar.addWidget(w)
        v.addLayout(bar)

        filt = QtWidgets.QHBoxLayout()
        filt.addWidget(QtWidgets.QLabel("Filter:"))
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText("regex — only matching lines are shown")
        self.filter_edit.textChanged.connect(self._rerender)
        filt.addWidget(self.filter_edit, 1)
        filt.addWidget(QtWidgets.QLabel("Highlight:"))
        self.highlight_edit = QtWidgets.QLineEdit()
        self.highlight_edit.setPlaceholderText("regex — matching lines are tinted")
        self.highlight_edit.textChanged.connect(self._rerender)
        filt.addWidget(self.highlight_edit, 1)
        v.addLayout(filt)

        decode = QtWidgets.QHBoxLayout()
        self.decode_chk = QtWidgets.QCheckBox("Decode panic backtraces")
        self.decode_chk.setToolTip(
            "Resolve 'Backtrace: 0x…' PCs through addr2line against an ELF")
        decode.addWidget(self.decode_chk)
        self.elf_lbl = QtWidgets.QLabel("(no ELF)")
        self.elf_lbl.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        decode.addWidget(self.elf_lbl, 1)
        elf_btn = QtWidgets.QPushButton("ELF…")
        elf_btn.clicked.connect(self._pick_elf)
        decode.addWidget(elf_btn)
        decode.addWidget(QtWidgets.QLabel("addr2line:"))
        self.addr2line_edit = QtWidgets.QLineEdit()
        self.addr2line_edit.setPlaceholderText(find_addr2line() or "addr2line")
        self.addr2line_edit.setMaximumWidth(220)
        decode.addWidget(self.addr2line_edit)
        v.addLayout(decode)

        self.display = QtWidgets.QPlainTextEdit()
        self.display.setReadOnly(True)
        mono = QtGui.QFont("monospace")
        mono.setStyleHint(QtGui.QFont.StyleHint.TypeWriter)
        self.display.setFont(mono)
        self.display.setMaximumBlockCount(self.SCROLLBACK * 2)
        v.addWidget(self.display, 1)

        send = QtWidgets.QHBoxLayout()
        send.addWidget(QtWidgets.QLabel("Send:"))
        self.send_edit = QtWidgets.QLineEdit()
        self.send_edit.returnPressed.connect(self._send)
        send.addWidget(self.send_edit, 1)
        self.eol_combo = QtWidgets.QComboBox()
        for label, _ in self.EOLS:
            self.eol_combo.addItem(label)
        self.eol_combo.setCurrentIndex(1)  # LF default
        send.addWidget(self.eol_combo)
        self.send_btn = QtWidgets.QPushButton("Send")
        self.send_btn.clicked.connect(self._send)
        send.addWidget(self.send_btn)
        v.addLayout(send)

        self.status = QtWidgets.QLabel("Disconnected")
        v.addWidget(self.status)

    def _populate_ports(self, prefer: str | None = None):
        prev = self._current_port_text() if hasattr(self, "port_combo") else None
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        for path, desc in list_serial_ports():
            self.port_combo.addItem(f"{path}  ({desc})", path)
        self.port_combo.blockSignals(False)

        target = prefer or prev
        if target:
            for i in range(self.port_combo.count()):
                if self.port_combo.itemData(i) == target:
                    self.port_combo.setCurrentIndex(i)
                    return
            self.port_combo.setEditText(target)

    def _current_port_text(self) -> str:
        text = self.port_combo.currentText().strip()
        if "  (" in text:
            text = text.split("  (", 1)[0]
        return text

    def _update_ui_state(self):
        connected = self.ser is not None
        self.connect_btn.setText("Disconnect" if connected else "Connect")
        for w in (self.reset_btn, self.boot_btn, self.send_edit, self.send_btn):
            w.setEnabled(connected)
        self.port_combo.setEnabled(not connected)
        self.baud_combo.setEnabled(not connected)

    def _toggle_connection(self):
        if self.ser is None:
            self._reconnect_timer.stop()
            self._connect()
        else:
            self._reconnect_timer.stop()
            self._disconnect()

    def current_port(self) -> str:
        return self._current_port_text()

    def _connect(self, quiet: bool = False) -> bool:
        port = self._current_port_text()
        if not port:
            if not quiet:
                QtWidgets.QMessageBox.warning(self, "Serial Monitor", "Pick or type a port.")
            return False
        try:
            baud = int(self.baud_combo.currentText())
        except ValueError:
            if not quiet:
                QtWidgets.QMessageBox.warning(self, "Serial Monitor", "Invalid baud rate.")
            return False
        try:
            ser = serial.Serial(port, baud, timeout=0)
            ser.dtr = False
            ser.rts = True
        except (serial.SerialException, OSError) as e:
            if not quiet:
                hint = serial_permission_hint(port)
                QtWidgets.QMessageBox.critical(
                    self, "Serial Monitor",
                    f"Open failed:\n{e}" + (f"\n\n{hint}" if hint else ""))
            return False
        self.ser = ser
        self.reader = SerialReader(ser, self)
        self.reader.bytes_received.connect(self._on_bytes)
        self.reader.error.connect(self._on_reader_error)
        self.reader.start()
        self.status.setText(f"Connected: {port} @ {baud}")
        self._update_ui_state()
        return True

    def _disconnect(self):
        if self.reader is not None:
            self.reader.stop()
            self.reader.wait(2000)
            self.reader = None
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        self.status.setText("Disconnected")
        self._update_ui_state()

    def suspend_for_flash(self) -> bool:
        """Release the port so esptool can have it. Returns True if it was open."""
        if self.ser is None:
            return False
        try:
            baud = int(self.baud_combo.currentText())
        except ValueError:
            baud = 115200
        self._suspended_port = (self._current_port_text(), baud)
        self._reconnect_timer.stop()
        self._disconnect()
        self._commit_line("[monitor] released the port for a flash operation")
        self.status.setText("Suspended (flash in progress)")
        return True

    def resume_after_flash(self):
        """Reopen a port that suspend_for_flash() closed."""
        if self._suspended_port is None:
            return
        port, _baud = self._suspended_port
        self._suspended_port = None
        if self._current_port_text() != port:
            return
        if self._connect(quiet=True):
            self._commit_line("[monitor] reconnected")
        elif self.reconnect_chk.isChecked():
            self._reconnect_timer.start()

    def _try_reconnect(self):
        if self.ser is not None:
            self._reconnect_timer.stop()
            return
        if self._connect(quiet=True):
            self._reconnect_timer.stop()
            self._commit_line("[monitor] reconnected")

    def _on_bytes(self, data: bytes):
        if self.hex_chk.isChecked():
            self._render_text(" ".join(f"{b:02X}" for b in data) + " ", {})
            return
        self._pending += data.decode("utf-8", errors="replace").replace("\r", "")
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._commit_line(line, already_shown=self._shown)
            self._shown = 0
        if self._pending:
            self._flush_timer.start()

    def _flush_partial(self):
        """Show an unterminated tail (a prompt) once the stream goes quiet."""
        if self._filter_regex() is not None:
            return
        if len(self._pending) > self._shown:
            self._render_text(self._pending[self._shown:], self._ansi_state)
            self._shown = len(self._pending)

    def _filter_regex(self):
        text = self.filter_edit.text().strip()
        if not text:
            return None
        try:
            return re.compile(text)
        except re.error:
            return None

    def _highlight_regex(self):
        text = self.highlight_edit.text().strip()
        if not text:
            return None
        try:
            return re.compile(text)
        except re.error:
            return None

    def _commit_line(self, line: str, already_shown: int = 0):
        stamp = QtCore.QDateTime.currentDateTime().toString("hh:mm:ss.zzz")
        self._lines.append((line, stamp))
        if self._passes_filter(line):
            self._render_line(line, already_shown, stamp)
        if self.decode_chk.isChecked():
            self._maybe_decode(line)

    def _passes_filter(self, line: str) -> bool:
        rx = self._filter_regex()
        return rx is None or rx.search(strip_ansi(line)) is not None

    def _render_line(self, line: str, already_shown: int = 0, stamp: str = ""):
        plain = strip_ansi(line)
        prefix = ""
        if self.timestamps_chk.isChecked() and already_shown == 0 and stamp:
            prefix = f"[{stamp}] "
        hl = self._highlight_regex()
        base: dict = {}
        if hl is not None and hl.search(plain):
            base["background"] = "#fff3a3"
        visible = line[already_shown:] if already_shown else line
        if prefix:
            self._render_text(prefix, base)
        if self.ansi_chk.isChecked():
            segments, self._ansi_state = parse_ansi_segments(visible, self._ansi_state)
            for text, style in segments:
                merged = dict(base)
                merged.update(style)
                self._render_text(text, merged)
        else:
            self._render_text(strip_ansi(visible), base)
        self._render_text("\n", {})

    def _render_text(self, text: str, style: dict):
        if not text:
            return
        fmt = QtGui.QTextCharFormat()
        if style.get("color"):
            fmt.setForeground(QtGui.QColor(style["color"]))
        if style.get("background"):
            fmt.setBackground(QtGui.QColor(style["background"]))
        if style.get("bold"):
            fmt.setFontWeight(QtGui.QFont.Weight.Bold)
        cursor = self.display.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        cursor.insertText(text, fmt)
        if self.autoscroll_chk.isChecked():
            self.display.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _rerender(self):
        """Rebuild the display from scrollback after a filter/highlight change."""
        self.display.clear()
        self._ansi_state = {}
        for line, stamp in self._lines:
            if self._passes_filter(line):
                self._render_line(line, stamp=stamp)
        self._shown = 0 if self._filter_regex() is not None else len(self._pending)
        if self._filter_regex() is None and self._pending:
            self._render_text(self._pending, self._ansi_state)

    def _pick_elf(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Choose the firmware ELF", "",
            "ELF files (*.elf);;All files (*)")
        if not path:
            return
        self._elf_path = path
        self.elf_lbl.setText(path)
        self.decode_chk.setChecked(True)

    def _maybe_decode(self, line: str):
        pcs = parse_backtrace(line)
        if not pcs:
            return
        if not self._elf_path:
            self._render_text("[decode] pick an ELF to resolve this backtrace\n",
                              {"color": ANSI_COLORS[33]})
            return
        decoded = decode_addresses(self._elf_path, pcs,
                                  self.addr2line_edit.text().strip(), timeout=4.0)
        if not decoded:
            self._render_text("[decode] addr2line produced nothing "
                              "(wrong ELF or missing toolchain)\n",
                              {"color": ANSI_COLORS[33]})
            return
        for entry in decoded:
            self._render_text(f"    ↳ {entry}\n", {"color": ANSI_COLORS[36]})

    def _on_reader_error(self, msg: str):
        self._commit_line(f"[serial error] {msg}")
        self._disconnect()
        if self.reconnect_chk.isChecked():
            self.status.setText("Disconnected — retrying…")
            self._reconnect_timer.start()

    def _send(self):
        if not self.ser:
            return
        text = self.send_edit.text()
        eol = self.EOLS[self.eol_combo.currentIndex()][1]
        payload = text.encode("utf-8", errors="replace") + eol
        try:
            self.ser.write(payload)
        except (serial.SerialException, OSError) as e:
            self._commit_line(f"[write error] {e}")
            self._disconnect()
            return
        if self.echo_chk.isChecked():
            self._commit_line(f"> {text}")
        self.send_edit.clear()

    def _reset_device(self):
        if not self.ser:
            return
        try:
            self.ser.rts = False
            QtCore.QThread.msleep(50)
            self.ser.rts = True
        except (serial.SerialException, OSError) as e:
            self._commit_line(f"[reset error] {e}")

    def _reset_to_bootloader(self):
        """esptool's classic reset: hold IO0 low across the EN pulse."""
        if not self.ser:
            return
        try:
            self.ser.dtr = False
            self.ser.rts = True
            QtCore.QThread.msleep(100)
            self.ser.dtr = True
            self.ser.rts = False
            QtCore.QThread.msleep(50)
            self.ser.dtr = False
            self._commit_line("[monitor] pulsed reset into download mode")
        except (serial.SerialException, OSError) as e:
            self._commit_line(f"[reset error] {e}")

    def _clear(self):
        self.display.clear()
        self._lines.clear()
        self._pending = ""
        self._shown = 0
        self._ansi_state = {}

    def _save_log(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save monitor log", "monitor.log",
            "Log files (*.log *.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                for line, stamp in self._lines:
                    prefix = f"[{stamp}] " if self.timestamps_chk.isChecked() else ""
                    f.write(prefix + strip_ansi(line) + "\n")
                if self._pending:
                    f.write(strip_ansi(self._pending))
        except OSError as e:
            QtWidgets.QMessageBox.critical(self, "Save log", str(e))

    def closeEvent(self, e):
        self._reconnect_timer.stop()
        self._flush_timer.stop()
        self._disconnect()
        super().closeEvent(e)


class Job:
    """One esptool invocation in a sequential queue.

    `on_success` runs after a zero exit code and can enqueue nothing —
    the queue owns sequencing — but may touch the UI or post-process files.
    """

    def __init__(self, op: str, args: list[str], message: str = "",
                 on_success=None, expect_bytes: int | None = None,
                 needs_port: bool = True, include_baud: bool = True,
                 label: str = "", module: str = "esptool",
                 capture: bool = False):
        self.op = op
        self.args = args
        self.message = message
        self.on_success = on_success
        self.expect_bytes = expect_bytes
        self.needs_port = needs_port
        self.include_baud = include_baud
        self.label = label or op
        self.module = module
        # capture=True buffers the output for on_success to read back, for
        # tools whose result is their stdout (espefuse summary).
        self.capture = capture


IMAGE_INFO_LABELS = (
    "Image size", "Segments", "Checksum", "Validation hash", "Entry point",
    "Image version", "Minimal chip revision", "Chip ID", "Project name",
    "App version", "Compile time", "ESP-IDF", "Secure version",
)


def parse_image_info(text: str) -> dict:
    """Pick the interesting labelled fields out of `esptool image-info`."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = strip_ansi(raw).strip()
        for label in IMAGE_INFO_LABELS:
            prefix = label + ":"
            if line.startswith(prefix):
                out[label] = line[len(prefix):].strip()
                break
    return out


def find_overlaps(images: list[tuple[int, int, str]]) -> list[str]:
    """Describe overlapping (address, size, name) write regions."""
    problems: list[str] = []
    ordered = sorted(images, key=lambda t: t[0])
    for (a_addr, a_size, a_name), (b_addr, b_size, b_name) in zip(ordered, ordered[1:]):
        if a_addr + a_size > b_addr:
            problems.append(
                f"{os.path.basename(a_name)} @ 0x{a_addr:x} (+{a_size}) overlaps "
                f"{os.path.basename(b_name)} @ 0x{b_addr:x}")
    return problems


class PartitionTableDialog(QtWidgets.QDialog):
    COLUMNS = ("Label", "Type", "SubType", "Offset", "Size", "Flags")

    def __init__(self, parent: "EspFlasher"):
        super().__init__(parent)
        self._owner = parent
        self.setWindowTitle("Partition Table")
        self.resize(820, 480)
        self.entries: list[dict] = []

        v = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel("Table offset:"))
        self.offset_edit = QtWidgets.QLineEdit(hex(DEFAULT_PT_OFFSET))
        self.offset_edit.setMaximumWidth(100)
        bar.addWidget(self.offset_edit)
        self.read_btn = QtWidgets.QPushButton("Read from device")
        self.read_btn.clicked.connect(self._read_device)
        bar.addWidget(self.read_btn)
        self.open_btn = QtWidgets.QPushButton("Open file…")
        self.open_btn.clicked.connect(self._open_file)
        bar.addWidget(self.open_btn)
        self.export_btn = QtWidgets.QPushButton("Export CSV…")
        self.export_btn.clicked.connect(self._export_csv)
        bar.addWidget(self.export_btn)
        bar.addStretch(1)
        v.addLayout(bar)

        self.table = QtWidgets.QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self.table, 1)

        actions = QtWidgets.QHBoxLayout()
        self.dump_btn = QtWidgets.QPushButton("Dump selected…")
        self.dump_btn.clicked.connect(self._dump_selected)
        actions.addWidget(self.dump_btn)
        self.dump_all_btn = QtWidgets.QPushButton("Dump all to folder…")
        self.dump_all_btn.setToolTip(
            "Read every partition into its own .bin, one after another")
        self.dump_all_btn.clicked.connect(self._dump_all)
        actions.addWidget(self.dump_all_btn)
        self.write_btn = QtWidgets.QPushButton("Write selected…")
        self.write_btn.setToolTip(
            "Erase just this partition's region and write a file into it")
        self.write_btn.clicked.connect(self._write_selected)
        actions.addWidget(self.write_btn)
        self.erase_btn = QtWidgets.QPushButton("Erase selected")
        self.erase_btn.setToolTip(
            "Erase only this partition — the usual way to reset stored NVS config")
        self.erase_btn.clicked.connect(self._erase_selected)
        actions.addWidget(self.erase_btn)
        actions.addStretch(1)
        v.addLayout(actions)

        self.status = QtWidgets.QLabel("No table loaded.")
        v.addWidget(self.status)
        self._set_actions_enabled(False)

    def _pt_offset(self) -> int:
        try:
            return int(self.offset_edit.text().strip(), 0)
        except ValueError:
            return DEFAULT_PT_OFFSET

    def _set_actions_enabled(self, on: bool):
        for w in (self.dump_btn, self.dump_all_btn, self.write_btn, self.erase_btn):
            w.setEnabled(on and bool(self.entries))

    def set_busy(self, busy: bool):
        self.read_btn.setEnabled(not busy)
        self._set_actions_enabled(not busy)

    def _read_device(self):
        self._owner.read_partition_table(self, self._pt_offset())

    def _open_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open flash image or partition dump", "",
            "Binary (*.bin);;All files (*)")
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as e:
            QtWidgets.QMessageBox.critical(self, "Partition Table", str(e))
            return
        off = self._pt_offset()
        if len(data) > off + 0x20:  # looks like a full flash image
            data = data[off:off + PARTITION_TABLE_SIZE]
        self.load_from_bytes(data)

    def load_from_bytes(self, data: bytes):
        try:
            self.entries = parse_partition_table(data)
        except Exception as e:  # noqa: BLE001 - surface any parse failure
            QtWidgets.QMessageBox.critical(self, "Partition Table",
                                           f"Parse failed: {e}")
            return
        self.table.setRowCount(len(self.entries))
        for row, e in enumerate(self.entries):
            cells = (
                e["label"],
                _type_name(e["type"]),
                _subtype_name(e["type"], e["subtype"]),
                f"0x{e['offset']:06x}",
                f"0x{e['size']:x} ({e['size'] // 1024} KB)",
                f"0x{e['flags']:x}",
            )
            for col, text in enumerate(cells):
                self.table.setItem(row, col, QtWidgets.QTableWidgetItem(text))
        self.table.resizeColumnsToContents()
        if self.entries:
            total = sum(e["size"] for e in self.entries)
            self.status.setText(f"{len(self.entries)} partitions, "
                                f"{human_bytes(total)} mapped.")
            self.table.selectRow(0)
        else:
            self.status.setText("No valid partition entries found.")
        self._set_actions_enabled(not self._owner.busy())

    def _selected(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.entries):
            QtWidgets.QMessageBox.information(self, "Partition Table",
                                              "Select a partition first.")
            return None
        return self.entries[row]

    def _export_csv(self):
        if not self.entries:
            QtWidgets.QMessageBox.information(self, "Partition Table",
                                              "Load a table first.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export partition table", "partitions.csv",
            "CSV (*.csv);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("name,type,subtype,offset,size,flags\n")
                for e in self.entries:
                    fh.write(f"{e['label']},{_type_name(e['type'])},"
                             f"{_subtype_name(e['type'], e['subtype'])},"
                             f"0x{e['offset']:x},0x{e['size']:x},"
                             f"0x{e['flags']:x}\n")
        except OSError as e:
            QtWidgets.QMessageBox.critical(self, "Partition Table", str(e))
            return
        self.status.setText(f"Exported to {path}")

    def _dump_selected(self):
        e = self._selected()
        if not e:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Dump partition as",
            f"{e['label'] or 'partition'}.bin",
            "Binary (*.bin);;All files (*)")
        if path:
            self._owner.dump_region(e["offset"], e["size"], path)

    def _dump_all(self):
        if not self.entries:
            return
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Dump every partition into…")
        if folder:
            self._owner.dump_partitions(self.entries, folder)

    def _write_selected(self):
        e = self._selected()
        if not e:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, f"File to write into '{e['label']}'", "",
            "Binary (*.bin);;All files (*)")
        if not path:
            return
        size = os.path.getsize(path)
        if size > e["size"]:
            QtWidgets.QMessageBox.critical(
                self, "Partition Table",
                f"{os.path.basename(path)} is {human_bytes(size)} but "
                f"'{e['label']}' only holds {human_bytes(e['size'])}.")
            return
        if QtWidgets.QMessageBox.question(
            self, "Write partition",
            f"Erase 0x{e['offset']:x}…0x{e['offset'] + e['size']:x} "
            f"('{e['label']}') and write {os.path.basename(path)} "
            f"({human_bytes(size)}) into it?",
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._owner.write_partition(e, path)

    def _erase_selected(self):
        e = self._selected()
        if not e:
            return
        if QtWidgets.QMessageBox.question(
            self, "Erase partition",
            f"Erase '{e['label']}' "
            f"(0x{e['offset']:x}, {human_bytes(e['size'])})?\n\n"
            f"The data in this partition is gone for good.",
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._owner.erase_partition(e)


class MultiFlashDialog(QtWidgets.QDialog):
    """Flash several images in one write-flash, ESP-IDF `flash_args` style."""

    def __init__(self, parent: "EspFlasher"):
        super().__init__(parent)
        self._owner = parent
        self.setWindowTitle("Flash Multiple Images")
        self.resize(820, 460)

        v = QtWidgets.QVBoxLayout(self)
        v.addWidget(QtWidgets.QLabel(
            "Each row is written at its own address in a single write-flash — "
            "the usual bootloader + partition-table + app trio."))

        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("Address", "File", "Size"))
        self.table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        v.addWidget(self.table, 1)

        row = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton("Add image…")
        add.clicked.connect(self._add_image)
        row.addWidget(add)
        load = QtWidgets.QPushButton("Load flash_args…")
        load.setToolTip("Import an ESP-IDF build/flash_args file")
        load.clicked.connect(self._load_flash_args)
        row.addWidget(load)
        remove = QtWidgets.QPushButton("Remove")
        remove.clicked.connect(self._remove_row)
        row.addWidget(remove)
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(lambda: self.table.setRowCount(0))
        row.addWidget(clear)
        row.addStretch(1)
        v.addLayout(row)

        opts = QtWidgets.QGridLayout()
        opts.addWidget(QtWidgets.QLabel("Flash mode:"), 0, 0)
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems(FLASH_MODES)
        opts.addWidget(self.mode_combo, 0, 1)
        opts.addWidget(QtWidgets.QLabel("Flash freq:"), 0, 2)
        self.freq_combo = QtWidgets.QComboBox()
        self.freq_combo.addItems(FLASH_FREQS)
        opts.addWidget(self.freq_combo, 0, 3)
        opts.addWidget(QtWidgets.QLabel("Flash size:"), 0, 4)
        self.size_combo = QtWidgets.QComboBox()
        self.size_combo.addItems(FLASH_SIZE_FLAGS)
        opts.addWidget(self.size_combo, 0, 5)
        self.erase_all_chk = QtWidgets.QCheckBox("Erase all flash first")
        opts.addWidget(self.erase_all_chk, 1, 0, 1, 3)
        opts.addWidget(QtWidgets.QLabel("Merge format:"), 1, 4)
        self.merge_format = QtWidgets.QComboBox()
        self.merge_format.addItems(["raw", "uf2", "hex"])
        opts.addWidget(self.merge_format, 1, 5)
        v.addLayout(opts)

        bottom = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel("")
        bottom.addWidget(self.status, 1)
        self.merge_btn = QtWidgets.QPushButton("Export merged image…")
        self.merge_btn.setToolTip(
            "Combine the rows into one flashable file (merge-bin)")
        self.merge_btn.clicked.connect(self._export_merged)
        bottom.addWidget(self.merge_btn)
        self.flash_btn = QtWidgets.QPushButton("Flash All")
        self.flash_btn.clicked.connect(self._flash)
        bottom.addWidget(self.flash_btn)
        v.addLayout(bottom)

    def set_busy(self, busy: bool):
        self.flash_btn.setEnabled(not busy)
        self.merge_btn.setEnabled(not busy)

    def _append_row(self, addr: int, path: str):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(f"0x{addr:x}"))
        file_item = QtWidgets.QTableWidgetItem(path)
        file_item.setFlags(file_item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, 1, file_item)
        size = os.path.getsize(path) if os.path.isfile(path) else -1
        size_item = QtWidgets.QTableWidgetItem(
            human_bytes(size) if size >= 0 else "missing!")
        size_item.setFlags(size_item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, 2, size_item)

    def _add_image(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Add image(s)", "", "Binary (*.bin);;All files (*)")
        for path in paths:
            self._append_row(0x10000 if self.table.rowCount() else 0x0, path)

    def _remove_row(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _load_flash_args(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open flash_args", "", "flash_args (flash_args*);;All files (*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            QtWidgets.QMessageBox.critical(self, "flash_args", str(e))
            return
        images, opts = parse_flash_args(text, os.path.dirname(os.path.abspath(path)))
        if not images:
            QtWidgets.QMessageBox.warning(self, "flash_args",
                                          "No <address> <file> pairs found.")
            return
        self.table.setRowCount(0)
        for addr, img in images:
            self._append_row(addr, img)
        for key, combo in (("flash-mode", self.mode_combo),
                           ("flash-freq", self.freq_combo),
                           ("flash-size", self.size_combo)):
            value = opts.get(key)
            if value:
                idx = combo.findText(value, QtCore.Qt.MatchFlag.MatchFixedString)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
        missing = [p for _, p in images if not os.path.isfile(p)]
        self.status.setText(
            f"Loaded {len(images)} images from {os.path.basename(path)}"
            + (f" — {len(missing)} missing!" if missing else ""))

    def images(self) -> list[tuple[int, str]] | None:
        out: list[tuple[int, str]] = []
        for row in range(self.table.rowCount()):
            addr_item = self.table.item(row, 0)
            file_item = self.table.item(row, 1)
            if addr_item is None or file_item is None:
                continue
            try:
                addr = int(addr_item.text().strip(), 0)
            except ValueError:
                QtWidgets.QMessageBox.critical(
                    self, "Flash Multiple Images",
                    f"Row {row + 1}: '{addr_item.text()}' is not an address.")
                return None
            path = file_item.text()
            if not os.path.isfile(path):
                QtWidgets.QMessageBox.critical(
                    self, "Flash Multiple Images", f"Missing file:\n{path}")
                return None
            out.append((addr, path))
        if not out:
            QtWidgets.QMessageBox.information(self, "Flash Multiple Images",
                                              "Add at least one image.")
            return None
        overlaps = find_overlaps([(a, os.path.getsize(p), p) for a, p in out])
        if overlaps:
            if QtWidgets.QMessageBox.warning(
                self, "Overlapping images",
                "These images overlap:\n\n" + "\n".join(overlaps)
                + "\n\nFlash anyway?",
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            ) != QtWidgets.QMessageBox.StandardButton.Yes:
                return None
        return out

    def flash_options(self) -> list[str]:
        args: list[str] = []
        for flag, combo in (("--flash-mode", self.mode_combo),
                            ("--flash-freq", self.freq_combo),
                            ("--flash-size", self.size_combo)):
            value = combo.currentText()
            if value != "keep" or flag == "--flash-size":
                args += [esptool_flag(flag), value]
        if self.erase_all_chk.isChecked():
            args.append("--erase-all")
        return args

    def _flash(self):
        images = self.images()
        if images is None:
            return
        total = sum(os.path.getsize(p) for _, p in images)
        if QtWidgets.QMessageBox.question(
            self, "Flash All",
            f"Write {len(images)} image(s), {human_bytes(total)} total?\n\n"
            + "\n".join(f"0x{a:x}  {os.path.basename(p)}" for a, p in images),
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.Yes,
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._owner.flash_images(images, self.flash_options(), total)

    def _export_merged(self):
        images = self.images()
        if images is None:
            return
        fmt = self.merge_format.currentText()
        suffix = {"raw": ".bin", "uf2": ".uf2", "hex": ".hex"}[fmt]
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save merged image", f"merged{suffix}", "All files (*)")
        if not path:
            return
        self._owner.merge_images(images, path, fmt, self.flash_options())


class ImageInfoDialog(QtWidgets.QDialog):
    """`esptool image-info` for a local .bin — no device needed."""

    def __init__(self, parent: "EspFlasher", path: str = ""):
        super().__init__(parent)
        self._owner = parent
        self.setWindowTitle("Firmware Image Info")
        self.resize(760, 560)

        v = QtWidgets.QVBoxLayout(self)
        bar = QtWidgets.QHBoxLayout()
        self.path_lbl = QtWidgets.QLabel(path or "(no file)")
        self.path_lbl.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        bar.addWidget(self.path_lbl, 1)
        pick = QtWidgets.QPushButton("Open…")
        pick.clicked.connect(self._pick)
        bar.addWidget(pick)
        v.addLayout(bar)

        self.summary = QtWidgets.QLabel("")
        self.summary.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary.setWordWrap(True)
        f = self.summary.font()
        f.setBold(True)
        self.summary.setFont(f)
        v.addWidget(self.summary)

        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)
        mono = QtGui.QFont("monospace")
        mono.setStyleHint(QtGui.QFont.StyleHint.TypeWriter)
        self.output.setFont(mono)
        v.addWidget(self.output, 1)

        self.path = path
        if path:
            self.reload()

    def _pick(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Choose a firmware image", "", "Binary (*.bin);;All files (*)")
        if path:
            self.path = path
            self.path_lbl.setText(path)
            self.reload()

    def reload(self):
        if not self.path:
            return
        self.output.setPlainText("Running image-info…")
        text = self._owner.run_offline([esptool_cmd("image_info"), self.path])
        self.output.setPlainText(text)
        fields = parse_image_info(text)
        if fields:
            self.summary.setText("   ".join(
                f"{k}: {v}" for k, v in fields.items()
                if k in ("Project name", "App version", "ESP-IDF",
                         "Compile time", "Image size")))
        else:
            self.summary.setText(
                "No app descriptor found — this is probably a raw flash dump "
                "rather than a single application image.")


class EfuseDialog(QtWidgets.QDialog):
    """espefuse summary, with the security state called out up front."""

    def __init__(self, parent: "EspFlasher"):
        super().__init__(parent)
        self._owner = parent
        self.setWindowTitle("Chip Security / eFuses")
        self.resize(880, 620)

        v = QtWidgets.QVBoxLayout(self)
        bar = QtWidgets.QHBoxLayout()
        self.read_btn = QtWidgets.QPushButton("Read eFuses from device")
        self.read_btn.clicked.connect(self.reload)
        bar.addWidget(self.read_btn)
        bar.addStretch(1)
        v.addLayout(bar)

        self.banner = QtWidgets.QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        v.addWidget(self.banner)

        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)
        mono = QtGui.QFont("monospace")
        mono.setStyleHint(QtGui.QFont.StyleHint.TypeWriter)
        self.output.setFont(mono)
        v.addWidget(self.output, 1)

        self.status = QtWidgets.QLabel(
            "Click 'Read eFuses from device'. These reads are read-only; "
            "nothing here burns a fuse.")
        v.addWidget(self.status)

    def set_busy(self, busy: bool):
        self.read_btn.setEnabled(not busy)

    def reload(self):
        self.output.setPlainText("Reading eFuses…")
        self._owner.read_efuses(self)

    def apply_output(self, text: str):
        self.output.setPlainText(text or "(no output)")
        self.show_security(parse_efuse_security(text))

    def show_security(self, info: dict):
        self._owner.security_info = info
        notes: list[str] = []
        if info.get("flash_encryption"):
            notes.append(
                "FLASH ENCRYPTION IS ENABLED. A raw dump of this chip is "
                "ciphertext: it cannot be restored to a different device, and "
                "on this one only if the key eFuse is intact. Treat backups as "
                "recovery images for this exact chip only.")
        if info.get("secure_boot"):
            notes.append(
                "SECURE BOOT IS ENABLED. Unsigned images will be rejected by "
                "the bootloader, so restoring third-party firmware will not boot.")
        if notes:
            self.banner.setText("\n\n".join(notes))
            self.banner.setStyleSheet(
                "background:#7a1f1f; color:#ffffff; padding:8px; border-radius:4px;")
            self.banner.setVisible(True)
        elif info.get("fields"):
            self.banner.setText(
                "No flash encryption or secure boot detected — plain dumps "
                "restore normally.")
            self.banner.setStyleSheet(
                "background:#1f5d33; color:#ffffff; padding:8px; border-radius:4px;")
            self.banner.setVisible(True)
        else:
            self.banner.setVisible(False)


class EspFlasher(QtWidgets.QMainWindow):
    LOG_MAX_BLOCKS = 5000
    PORT_POLL_MS = 2000
    KILL_GRACE_MS = 3000

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(820, 800)

        self.process: QProcess | None = None
        self.current_job: Job | None = None
        self._queue: collections.deque[Job] = collections.deque()
        self._cancelled = False
        self._scanner = OutputScanner()

        self.detected_flash_bytes: int | None = None
        self.detected_chip: str | None = None
        self.detected_mac: str | None = None
        self.security_info: dict = {}

        self._job_started = 0.0
        self._job_done_bytes = 0
        self._job_total_bytes = 0
        self._known_ports: list[tuple[str, str]] = []
        self._job_output: list[str] = []
        self._child_dialogs: list = []
        self._monitors: list[SerialMonitor] = []
        self._profiles: dict[str, dict] = {}
        self._recent_backups: list[str] = []
        self._recent_firmware: list[str] = []
        self._pending_parttable: tuple[PartitionTableDialog, str] | None = None

        self._build_ui()
        self._build_menus()
        self.refresh_ports()
        self._restore_settings()

        self._port_timer = QtCore.QTimer(self)
        self._port_timer.setInterval(self.PORT_POLL_MS)
        self._port_timer.timeout.connect(self._poll_ports)
        self._port_timer.start()
        QtCore.QTimer.singleShot(0, self._check_port_permission)

    def _build_menus(self):
        file_menu = self.menuBar().addMenu("&File")
        self.recent_backup_menu = file_menu.addMenu("Recent &backups")
        self.recent_fw_menu = file_menu.addMenu("Recent &firmware")
        file_menu.addSeparator()
        quit_act = QtGui.QAction("&Quit", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        tools = self.menuBar().addMenu("&Tools")
        self.tool_actions: list[QtGui.QAction] = []

        def add_tool(title: str, shortcut: str, slot):
            act = QtGui.QAction(title, self)
            if shortcut:
                act.setShortcut(shortcut)
            act.triggered.connect(slot)
            tools.addAction(act)
            self.tool_actions.append(act)
            return act

        add_tool("Serial Monitor…", "Ctrl+M", self._open_monitor)
        add_tool("Hex Viewer…", "Ctrl+H", self._open_hex_viewer)
        add_tool("Partition Table…", "Ctrl+P", self._open_partitions)
        add_tool("Flash Multiple Images…", "Ctrl+F", self._open_multiflash)
        add_tool("Firmware Image Info…", "Ctrl+I", self._open_image_info)
        add_tool("Chip Security / eFuses…", "", self._open_efuse)
        tools.addSeparator()
        add_tool("Erase Flash…", "", self.start_erase)

        help_menu = self.menuBar().addMenu("&Help")
        about = QtGui.QAction("&About", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _recent_button(self, kind: str) -> QtWidgets.QToolButton:
        btn = QtWidgets.QToolButton()
        btn.setText("▾")
        btn.setToolTip("Recently used files")
        btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(btn)
        btn.setMenu(menu)
        if kind == "backup":
            self._recent_backup_btn_menu = menu
        else:
            self._recent_fw_btn_menu = menu
        return btn

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)

        conn = QtWidgets.QGroupBox("Connection")
        g = QtWidgets.QGridLayout(conn)

        g.addWidget(QtWidgets.QLabel("Profile:"), 0, 0)
        self.profile_combo = QtWidgets.QComboBox()
        self.profile_combo.setMinimumWidth(200)
        self.profile_combo.activated.connect(self._profile_selected)
        g.addWidget(self.profile_combo, 0, 1)
        prof_row = QtWidgets.QHBoxLayout()
        self.profile_save_btn = QtWidgets.QPushButton("Save profile…")
        self.profile_save_btn.setToolTip(
            "Remember this port, baud, chip, sizes and addresses under a name")
        self.profile_save_btn.clicked.connect(self._save_profile)
        prof_row.addWidget(self.profile_save_btn)
        self.profile_del_btn = QtWidgets.QPushButton("Delete")
        self.profile_del_btn.clicked.connect(self._delete_profile)
        prof_row.addWidget(self.profile_del_btn)
        g.addLayout(prof_row, 0, 2, 1, 2)

        g.addWidget(QtWidgets.QLabel("Port:"), 1, 0)
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(280)
        self.port_combo.lineEdit().setPlaceholderText("/dev/ttyUSB0 or COM3")
        g.addWidget(self.port_combo, 1, 1)
        self.refresh_btn = QtWidgets.QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        g.addWidget(self.refresh_btn, 1, 2)
        self.autodetect_chk = QtWidgets.QCheckBox("Detect on plug-in")
        self.autodetect_chk.setToolTip(
            "Run Detect Chip automatically when a new serial port appears")
        g.addWidget(self.autodetect_chk, 1, 3)

        g.addWidget(QtWidgets.QLabel("Baud:"), 2, 0)
        self.baud_combo = QtWidgets.QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems(BAUD_RATES)
        self.baud_combo.setCurrentText(DEFAULT_BAUD)
        g.addWidget(self.baud_combo, 2, 1)
        self.detect_btn = QtWidgets.QPushButton("Detect Chip")
        self.detect_btn.clicked.connect(self.start_detect)
        g.addWidget(self.detect_btn, 2, 2)
        self.monitor_btn = QtWidgets.QPushButton("Serial Monitor…")
        self.monitor_btn.clicked.connect(self._open_monitor)
        g.addWidget(self.monitor_btn, 2, 3)

        g.addWidget(QtWidgets.QLabel("Force chip:"), 3, 0)
        self.chip_override = QtWidgets.QComboBox()
        for label, _ in CHIP_CHOICES:
            self.chip_override.addItem(label)
        g.addWidget(self.chip_override, 3, 1)
        self.security_btn = QtWidgets.QPushButton("Chip Security…")
        self.security_btn.clicked.connect(self._open_efuse)
        g.addWidget(self.security_btn, 3, 2)

        self.chip_lbl = QtWidgets.QLabel("Chip: —")
        self.mac_lbl = QtWidgets.QLabel("MAC: —")
        self.flash_lbl = QtWidgets.QLabel("Flash size: —")
        for w, row in ((self.chip_lbl, 4), (self.mac_lbl, 5), (self.flash_lbl, 6)):
            f = w.font(); f.setBold(True); w.setFont(f)
            g.addWidget(w, row, 0, 1, 4)
        root.addWidget(conn)

        self.warning_lbl = QtWidgets.QLabel("")
        self.warning_lbl.setWordWrap(True)
        self.warning_lbl.setVisible(False)
        self.warning_lbl.setStyleSheet(
            "background:#6b4b12; color:#ffffff; padding:6px; border-radius:4px;")
        self.warning_lbl.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.warning_lbl)

        backup = QtWidgets.QGroupBox("Backup  (read flash → file)")
        b = QtWidgets.QGridLayout(backup)

        b.addWidget(QtWidgets.QLabel("Output file:"), 0, 0)
        self.backup_path = QtWidgets.QLineEdit()
        b.addWidget(self.backup_path, 0, 1)
        b.addWidget(self._recent_button("backup"), 0, 2)
        bb = QtWidgets.QPushButton("Browse…")
        bb.clicked.connect(self._pick_backup_file)
        b.addWidget(bb, 0, 3)
        bv = QtWidgets.QPushButton("View")
        bv.clicked.connect(self._view_backup)
        b.addWidget(bv, 0, 4)

        b.addWidget(QtWidgets.QLabel("Flash size:"), 1, 0)
        self.backup_size_combo = QtWidgets.QComboBox()
        self.backup_size_combo.addItems(FLASH_SIZE_MAP.keys())
        b.addWidget(self.backup_size_combo, 1, 1)
        self.trim_chk = QtWidgets.QCheckBox("Trim trailing erased space")
        self.trim_chk.setToolTip(
            "Drop the uniform 0xFF tail from full dumps; the manifest keeps the "
            "original size so a restore still fills the chip")
        b.addWidget(self.trim_chk, 1, 2, 1, 3)

        b.addWidget(QtWidgets.QLabel("Start address:"), 2, 0)
        self.backup_addr = QtWidgets.QLineEdit("0x0")
        b.addWidget(self.backup_addr, 2, 1)
        self.manifest_chk = QtWidgets.QCheckBox("Write .json manifest")
        self.manifest_chk.setChecked(True)
        self.manifest_chk.setToolTip(
            "Record chip, MAC, size and SHA-256 next to the dump so a restore "
            "can refuse the wrong device")
        b.addWidget(self.manifest_chk, 2, 2, 1, 3)

        self.backup_btn = QtWidgets.QPushButton("Backup Flash")
        self.backup_btn.clicked.connect(self.start_backup)
        b.addWidget(self.backup_btn, 3, 0, 1, 5)
        root.addWidget(backup)

        restore = QtWidgets.QGroupBox("Restore  (file → flash)")
        r = QtWidgets.QGridLayout(restore)

        r.addWidget(QtWidgets.QLabel("Firmware file:"), 0, 0)
        self.restore_path = QtWidgets.QLineEdit()
        self.restore_path.textChanged.connect(self._describe_restore_file)
        r.addWidget(self.restore_path, 0, 1)
        r.addWidget(self._recent_button("firmware"), 0, 2)
        rb = QtWidgets.QPushButton("Browse…")
        rb.clicked.connect(self._pick_restore_file)
        r.addWidget(rb, 0, 3)
        rv = QtWidgets.QPushButton("View")
        rv.clicked.connect(self._view_restore)
        r.addWidget(rv, 0, 4)

        r.addWidget(QtWidgets.QLabel("Address:"), 1, 0)
        self.restore_addr = QtWidgets.QLineEdit("0x0")
        r.addWidget(self.restore_addr, 1, 1)
        self.erase_chk = QtWidgets.QCheckBox("Erase entire flash first")
        self.erase_chk.setChecked(True)
        r.addWidget(self.erase_chk, 1, 2, 1, 2)
        self.verify_chk = QtWidgets.QCheckBox("Verify after write")
        self.verify_chk.setChecked(True)
        r.addWidget(self.verify_chk, 1, 4)

        self.restore_info = QtWidgets.QLabel("")
        self.restore_info.setWordWrap(True)
        r.addWidget(self.restore_info, 2, 0, 1, 5)

        self.restore_btn = QtWidgets.QPushButton("Restore Flash")
        self.restore_btn.clicked.connect(self.start_restore)
        r.addWidget(self.restore_btn, 3, 0, 1, 3)
        self.verify_btn = QtWidgets.QPushButton("Verify Only")
        self.verify_btn.setToolTip(
            "Compare the firmware file against flash without writing")
        self.verify_btn.clicked.connect(self.start_verify)
        r.addWidget(self.verify_btn, 3, 3, 1, 2)
        root.addWidget(restore)

        maint = QtWidgets.QGroupBox("Flash maintenance")
        m = QtWidgets.QHBoxLayout(maint)
        self.erase_btn = QtWidgets.QPushButton("Erase Flash")
        self.erase_btn.setToolTip("Erase the entire chip (destructive)")
        self.erase_btn.clicked.connect(self.start_erase)
        m.addWidget(self.erase_btn)
        self.part_btn = QtWidgets.QPushButton("Partition Table…")
        self.part_btn.clicked.connect(self._open_partitions)
        m.addWidget(self.part_btn)
        self.multi_btn = QtWidgets.QPushButton("Flash Multiple Images…")
        self.multi_btn.clicked.connect(self._open_multiflash)
        m.addWidget(self.multi_btn)
        self.info_btn = QtWidgets.QPushButton("Image Info…")
        self.info_btn.clicked.connect(self._open_image_info)
        m.addWidget(self.info_btn)
        m.addStretch(1)
        root.addWidget(maint)

        prow = QtWidgets.QHBoxLayout()
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        prow.addWidget(self.progress, 1)
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        prow.addWidget(self.cancel_btn)
        root.addLayout(prow)

        self.progress_lbl = QtWidgets.QLabel("")
        mono = QtGui.QFont("monospace")
        mono.setStyleHint(QtGui.QFont.StyleHint.TypeWriter)
        self.progress_lbl.setFont(mono)
        root.addWidget(self.progress_lbl)

        log_box = QtWidgets.QGroupBox("Log")
        lv = QtWidgets.QVBoxLayout(log_box)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(mono)
        self.log.setMaximumBlockCount(self.LOG_MAX_BLOCKS)
        lv.addWidget(self.log)
        root.addWidget(log_box, 1)

        self.statusBar().showMessage(
            f"Ready — esptool {'.'.join(str(p) for p in ESPTOOL_VERSION)}")

    def busy(self) -> bool:
        return self.process is not None or bool(self._queue)

    def _about(self):
        QtWidgets.QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> {APP_VERSION}<br><br>"
            f"esptool {'.'.join(str(p) for p in ESPTOOL_VERSION)}<br>"
            f"pyserial {serial.__version__}<br>"
            f"Qt {QtCore.QT_VERSION_STR}<br><br>"
            "Back up, restore and inspect flash on ESP8266 / ESP32 chips.")

    def refresh_ports(self):
        prev = self._port_text()
        ports = list_serial_ports()
        self._known_ports = ports
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        for path, desc in ports:
            self.port_combo.addItem(f"{path}  ({desc})", path)
        self.port_combo.blockSignals(False)

        if prev:
            for i in range(self.port_combo.count()):
                if self.port_combo.itemData(i) == prev:
                    self.port_combo.setCurrentIndex(i)
                    break
            else:
                self.port_combo.setEditText(prev)
        if not ports:
            self.append_log(
                "No serial ports detected. Plug in a device and click Refresh, "
                "or type the path (e.g. /dev/ttyUSB0) directly.\n"
            )

    def _poll_ports(self):
        """Watch for hotplug so the port list stays live without Refresh."""
        if self.busy():
            return
        ports = list_serial_ports()
        if ports == self._known_ports:
            return
        added = [p for p, _ in ports if p not in dict(self._known_ports)]
        removed = [p for p, _ in self._known_ports if p not in dict(ports)]
        self.refresh_ports()
        for path in removed:
            self.append_log(f"[ports] {path} disappeared\n")
        for path in added:
            self.append_log(f"[ports] {path} appeared\n")
        if added and not self._port_text():
            self.port_combo.setEditText(added[0])
        if added:
            self._check_port_permission()
            if self.autodetect_chk.isChecked():
                self.append_log("[ports] auto-detecting the new device…\n")
                self.start_detect()

    def _check_port_permission(self):
        hint = serial_permission_hint(self._port_text())
        if hint:
            self.warning_lbl.setText(hint)
            self.warning_lbl.setVisible(True)
        elif self.warning_lbl.isVisible() and "permission" in self.warning_lbl.text():
            self.warning_lbl.setVisible(False)

    def _port_text(self) -> str:
        text = self.port_combo.currentText().strip()
        if "  (" in text:
            text = text.split("  (", 1)[0]
        return text

    def selected_port(self) -> str | None:
        return self._port_text() or None

    def selected_chip_flag(self) -> str | None:
        return CHIP_CHOICES[self.chip_override.currentIndex()][1]

    def _remember_recent(self, kind: str, path: str):
        store = self._recent_backups if kind == "backup" else self._recent_firmware
        if path in store:
            store.remove(path)
        store.insert(0, path)
        del store[8:]
        self._rebuild_recent_menus()

    def _rebuild_recent_menus(self):
        for kind, menus, target in (
            ("backup", (self.recent_backup_menu, self._recent_backup_btn_menu),
             self.backup_path),
            ("firmware", (self.recent_fw_menu, self._recent_fw_btn_menu),
             self.restore_path),
        ):
            paths = self._recent_backups if kind == "backup" else self._recent_firmware
            for menu in menus:
                menu.clear()
                if not paths:
                    menu.addAction("(nothing yet)").setEnabled(False)
                    continue
                for path in paths:
                    act = menu.addAction(path)
                    act.triggered.connect(
                        lambda _checked=False, p=path, t=target: t.setText(p))

    def _pick_backup_file(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save backup as", self.backup_path.text() or "flash_backup.bin",
            "Binary (*.bin);;All files (*)"
        )
        if path:
            self.backup_path.setText(path)

    def _pick_restore_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Choose firmware file", "", "Binary (*.bin);;All files (*)"
        )
        if path:
            self.restore_path.setText(path)

    def _describe_restore_file(self):
        path = self.restore_path.text().strip()
        if not path or not os.path.isfile(path):
            self.restore_info.setText("")
            return
        size = os.path.getsize(path)
        bits = [f"{human_bytes(size)} ({size:,} bytes)"]
        meta = read_manifest(path)
        if meta:
            bits.append(f"captured from {meta.get('chip') or 'unknown chip'}")
            if meta.get("mac"):
                bits.append(f"MAC {meta['mac']}")
            if meta.get("created"):
                bits.append(str(meta["created"]))
            if meta.get("original_size") and meta["original_size"] != meta.get("size"):
                bits.append(f"trimmed from {human_bytes(meta['original_size'])}")
        self.restore_info.setText("  ·  ".join(bits))

    def _view_backup(self):
        self._open_hex_viewer(self.backup_path.text().strip() or None)

    def _view_restore(self):
        self._open_hex_viewer(self.restore_path.text().strip() or None)

    def _register_dialog(self, dialog):
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self._child_dialogs.append(dialog)
        if hasattr(dialog, "set_busy"):
            dialog.set_busy(self.busy())
        dialog.show()
        return dialog

    def _live_dialogs(self) -> list:
        alive = []
        for dialog in self._child_dialogs:
            try:
                dialog.isVisible()
            except RuntimeError:
                continue
            alive.append(dialog)
        self._child_dialogs = alive
        return alive

    def _live_monitors(self) -> list[SerialMonitor]:
        alive = []
        for mon in self._monitors:
            try:
                mon.isVisible()
            except RuntimeError:
                continue
            alive.append(mon)
        self._monitors = alive
        return alive

    def _open_hex_viewer(self, path: str | None = None):
        if isinstance(path, bool):  # QAction.triggered passes `checked`
            path = None
        if path and not os.path.isfile(path):
            QtWidgets.QMessageBox.warning(self, "Hex Viewer", f"File not found:\n{path}")
            path = None
        self._register_dialog(HexViewer(self, path=path))

    def _open_monitor(self):
        mon = SerialMonitor(self, port=self._port_text() or None, baud="115200")
        self._monitors.append(mon)
        self._register_dialog(mon)

    def _open_partitions(self):
        self._register_dialog(PartitionTableDialog(self))

    def _open_multiflash(self):
        self._register_dialog(MultiFlashDialog(self))

    def _open_image_info(self):
        path = self.restore_path.text().strip()
        self._register_dialog(
            ImageInfoDialog(self, path if os.path.isfile(path) else ""))

    def _open_efuse(self):
        self._register_dialog(EfuseDialog(self))

    @staticmethod
    def _worker_argv(module: str) -> list[str]:
        return worker_argv(module)

    def _job_argv(self, job: Job) -> list[str]:
        if job.module == "espefuse":
            argv = self._worker_argv("espefuse")
            chip = self.selected_chip_flag()
            if chip:
                argv += ["--chip", chip]
            port = self.selected_port()
            if not port:
                raise RuntimeError("No serial port selected.")
            argv += ["--port", port, "--do-not-confirm"]
            return argv + [str(a) for a in job.args]
        return self._esptool_argv(*job.args, include_baud=job.include_baud,
                                  needs_port=job.needs_port)

    def _esptool_argv(self, *args, include_baud=True, needs_port=True) -> list[str]:
        argv = self._worker_argv("esptool")
        chip = self.selected_chip_flag()
        if needs_port:
            port = self.selected_port()
            if not port:
                raise RuntimeError("No serial port selected.")
            if chip:
                argv += ["--chip", chip]
            argv += ["--port", port]
            if include_baud:
                argv += ["--baud", self.baud_combo.currentText()]
        elif chip:
            argv += ["--chip", chip]
        argv += [str(a) for a in args]
        return argv

    @staticmethod
    def _child_environment() -> QtCore.QProcessEnvironment:
        """Force esptool into dumb-terminal mode.

        Its logger turns on colours and cursor tricks whenever $TERM looks
        capable — even writing to a pipe — which otherwise dumps raw escape
        codes into the log pane.
        """
        env = QtCore.QProcessEnvironment.systemEnvironment()
        env.insert("NO_COLOR", "1")
        env.insert("PYTHONUNBUFFERED", "1")
        env.remove("TERM")
        return env

    def enqueue(self, jobs: list[Job]) -> bool:
        if self.busy():
            QtWidgets.QMessageBox.warning(self, "Busy",
                                          "An operation is already running.")
            return False
        self._queue.extend(jobs)
        self._suspend_monitors()
        self._run_next()
        return True

    def _suspend_monitors(self):
        for mon in self._live_monitors():
            try:
                mon.suspend_for_flash()
            except RuntimeError:
                pass

    def _resume_monitors(self):
        for mon in self._live_monitors():
            try:
                mon.resume_after_flash()
            except RuntimeError:
                pass

    def _run_next(self):
        if not self._queue:
            self.set_running(False)
            self._resume_monitors()
            return
        job = self._queue.popleft()
        try:
            argv = self._job_argv(job)
        except RuntimeError as e:
            self._queue.clear()
            self.set_running(False)
            self._resume_monitors()
            self._error(str(e))
            return

        self.current_job = job
        self._cancelled = False
        self._job_output = []
        self._job_started = time.monotonic()
        self._job_done_bytes = 0
        self._job_total_bytes = job.expect_bytes or 0
        self.progress.setValue(0)
        self.progress_lbl.setText("")
        self.set_running(True)
        if job.message:
            self.append_log(job.message if job.message.endswith("\n")
                            else job.message + "\n")
        self.append_log(f">>> {os.path.basename(sys.executable)} "
                        f"{' '.join(argv)}\n")

        proc = QProcess(self)
        proc.setProgram(sys.executable)
        proc.setArguments(argv)
        proc.setProcessEnvironment(self._child_environment())
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        self.process = proc
        proc.start()

    def cancel(self):
        if not self.process or self.process.state() == QProcess.ProcessState.NotRunning:
            return
        self._cancelled = True
        self._queue.clear()
        self.append_log("\n[cancel] asking esptool to stop…\n")
        target = self.process
        target.terminate()
        # Bind the timer to *this* process: by the time it fires the user may
        # already have started another operation, which must not be killed.
        QtCore.QTimer.singleShot(self.KILL_GRACE_MS,
                                 lambda: self._force_kill(target))

    def _force_kill(self, proc=None):
        target = proc if proc is not None else self.process
        if target is None:
            return
        try:
            if target.state() == QProcess.ProcessState.NotRunning:
                return
        except RuntimeError:
            return  # QProcess already destroyed
        self.append_log("[cancel] esptool ignored SIGTERM; killing it\n")
        target.kill()

    def _reset_detect_labels(self):
        self.detected_flash_bytes = None
        self.detected_chip = None
        self.detected_mac = None
        self.chip_lbl.setText("Chip: …")
        self.mac_lbl.setText("MAC: …")
        self.flash_lbl.setText("Flash size: …")

    def _detect_job(self, on_success=None) -> Job:
        return Job("detect", [esptool_cmd("flash_id")], include_baud=False,
                   message="[detect] reading chip and flash id…",
                   on_success=on_success)

    def start_detect(self):
        self._reset_detect_labels()
        self.enqueue([self._detect_job()])

    def _backup_jobs(self, out: str, size_bytes: int, addr: int) -> list[Job]:
        def finish():
            self._post_process_backup(out, addr, full=(addr == 0))

        return [Job("backup", [esptool_cmd("read_flash"), hex(addr),
                               str(size_bytes), out],
                    message=f"[backup] reading {human_bytes(size_bytes)} from "
                            f"{hex(addr)} → {out}",
                    expect_bytes=size_bytes, on_success=finish)]

    def start_backup(self):
        out = self.backup_path.text().strip()
        if not out:
            self._error("Pick an output file."); return
        addr = self._parse_addr(self.backup_addr.text(), default=0)
        if addr is None:
            self._error("Invalid start address."); return
        parent = os.path.dirname(os.path.abspath(out))
        if not os.path.isdir(parent):
            self._error(f"Output folder does not exist:\n{parent}"); return

        size_bytes = FLASH_SIZE_MAP[self.backup_size_combo.currentText()]
        if size_bytes is None:
            if self.detected_flash_bytes is not None:
                self.enqueue(self._backup_jobs(out, self.detected_flash_bytes, addr))
                return
            self.append_log("[backup] flash size = Auto, detecting first…\n")
            self._reset_detect_labels()
            # The read is appended by the detect job's callback, once the size
            # is actually known — the queue runs it as the next step.
            self.enqueue([self._detect_job(
                on_success=lambda: self._queue_auto_backup(out, addr))])
            return
        self.enqueue(self._backup_jobs(out, size_bytes, addr))

    def _queue_auto_backup(self, out: str, addr: int):
        if self.detected_flash_bytes is None:
            self._error("Flash size auto-detect failed; pick a size manually.")
            return
        self._queue.extend(self._backup_jobs(out, self.detected_flash_bytes, addr))

    def _post_process_backup(self, out: str, addr: int, full: bool):
        if not os.path.isfile(out):
            return
        original = os.path.getsize(out)
        if full and self.trim_chk.isChecked():
            trimmed = trim_trailing_blank(out)
            if trimmed:
                new_size, old_size = trimmed
                self.append_log(
                    f"[backup] trimmed {human_bytes(old_size - new_size)} of "
                    f"trailing 0xFF ({human_bytes(old_size)} → "
                    f"{human_bytes(new_size)})\n")
        if self.manifest_chk.isChecked():
            try:
                meta = build_manifest(
                    out, chip=self.detected_chip, mac=self.detected_mac,
                    flash_bytes=self.detected_flash_bytes, address=addr,
                    original_size=original)
                path = write_manifest(out, meta)
                self.append_log(f"[backup] manifest → {path}\n")
            except OSError as e:
                self.append_log(f"[backup] manifest failed: {e}\n")
        self._remember_recent("backup", out)

    def start_verify(self):
        path = self.restore_path.text().strip()
        if not path or not os.path.isfile(path):
            self._error("Pick an existing firmware file to verify against."); return
        addr = self._parse_addr(self.restore_addr.text(), default=0)
        if addr is None:
            self._error("Invalid address."); return
        self.enqueue([self._verify_job(addr, path)])

    def _verify_job(self, addr: int, path: str) -> Job:
        return Job("verify", [esptool_cmd("verify_flash"), hex(addr), path],
                   message=f"[verify] comparing {path} against flash at {hex(addr)}",
                   expect_bytes=os.path.getsize(path))

    def start_erase(self):
        if QtWidgets.QMessageBox.question(
            self, "Erase Flash",
            "Erase the ENTIRE flash chip?\n\nThis wipes all firmware and "
            "data and cannot be undone.",
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.enqueue([Job("erase", [esptool_cmd("erase_flash")],
                          message="[erase] erasing entire flash…")])

    def start_restore(self):
        path = self.restore_path.text().strip()
        if not path or not os.path.isfile(path):
            self._error("Pick an existing firmware file."); return
        addr = self._parse_addr(self.restore_addr.text(), default=0)
        if addr is None:
            self._error("Invalid address."); return

        size = os.path.getsize(path)
        meta = read_manifest(path)
        mismatch = restore_mismatch(meta, self.detected_chip)
        lines = [f"Write {os.path.basename(path)} ({human_bytes(size)}) "
                 f"to flash at {hex(addr)}?"]
        if self.erase_chk.isChecked():
            lines.append("The whole chip is erased first.")
        if mismatch:
            lines.append("\n⚠  " + mismatch)
        elif meta:
            lines.append(f"\nManifest: captured from {meta.get('chip')} "
                         f"on {meta.get('created')}.")
        elif self.detected_chip is None:
            lines.append("\nNo chip detected yet, so the image cannot be "
                         "checked against this device — run Detect Chip first "
                         "if you are unsure.")
        if self.security_info.get("flash_encryption"):
            lines.append("\n⚠  Flash encryption is enabled on this chip; a "
                         "plain image will not boot.")

        default = QtWidgets.QMessageBox.StandardButton.No if mismatch \
            else QtWidgets.QMessageBox.StandardButton.Yes
        if QtWidgets.QMessageBox.question(
            self, "Restore Flash", "\n".join(lines),
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No, default,
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        jobs: list[Job] = []
        if self.erase_chk.isChecked():
            jobs.append(Job("erase", [esptool_cmd("erase_flash")],
                            message="[restore] erasing flash first…"))
        jobs.append(Job("restore", [esptool_cmd("write_flash"), hex(addr), path],
                        message=f"[restore] writing {path} to {hex(addr)}",
                        expect_bytes=size,
                        on_success=lambda: self._remember_recent("firmware", path)))
        if self.verify_chk.isChecked():
            jobs.append(self._verify_job(addr, path))
        self.enqueue(jobs)

    def read_partition_table(self, dialog: "PartitionTableDialog", offset: int):
        fd, tmp = tempfile.mkstemp(suffix=".bin", prefix="esp_pt_")
        os.close(fd)

        def finish():
            self._finish_parttable(dialog, tmp, success=True)

        job = Job("parttable", [esptool_cmd("read_flash"), hex(offset),
                                str(PARTITION_TABLE_SIZE), tmp],
                  message=f"[parttable] reading table at {hex(offset)}",
                  expect_bytes=PARTITION_TABLE_SIZE, on_success=finish)
        self._pending_parttable = (dialog, tmp)
        if not self.enqueue([job]):
            self._pending_parttable = None
            self._unlink(tmp)

    def _finish_parttable(self, dialog, tmp: str, success: bool):
        self._pending_parttable = None
        try:
            data = b""
            if success:
                try:
                    with open(tmp, "rb") as fh:
                        data = fh.read()
                except OSError as e:
                    self.append_log(f"[parttable] {e}\n")
                    success = False
            try:
                if success:
                    dialog.load_from_bytes(data)
                else:
                    dialog.status.setText("Read from device failed (see log).")
            except RuntimeError:
                pass  # dialog was closed mid-read
        finally:
            self._unlink(tmp)

    @staticmethod
    def _unlink(path: str):
        try:
            os.unlink(path)
        except OSError:
            pass

    def dump_region(self, addr: int, size: int, out: str):
        self.enqueue([Job("backup", [esptool_cmd("read_flash"), hex(addr),
                                     str(size), out],
                          message=f"[dump] {human_bytes(size)} from {hex(addr)} "
                                  f"→ {out}",
                          expect_bytes=size,
                          on_success=lambda: self._remember_recent("backup", out))])

    def dump_partitions(self, entries: list[dict], folder: str):
        jobs: list[Job] = []
        used: set[str] = set()
        for e in entries:
            base = e["label"] or _subtype_name(e["type"], e["subtype"]) or "partition"
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", base)
            candidate = f"{name}.bin"
            n = 1
            while candidate in used:
                n += 1
                candidate = f"{name}_{n}.bin"
            used.add(candidate)
            out = os.path.join(folder, candidate)
            jobs.append(Job("backup", [esptool_cmd("read_flash"),
                                       hex(e["offset"]), str(e["size"]), out],
                            message=f"[dump] {base} ({human_bytes(e['size'])}) "
                                    f"→ {candidate}",
                            expect_bytes=e["size"], label=f"dump:{base}"))
        if jobs:
            self.append_log(f"[dump] {len(jobs)} partitions → {folder}\n")
            self.enqueue(jobs)

    def write_partition(self, entry: dict, path: str):
        self.enqueue([
            Job("erase-region", [esptool_cmd("erase_region"),
                                 hex(entry["offset"]), str(entry["size"])],
                message=f"[partition] erasing {entry['label']} at "
                        f"{hex(entry['offset'])}"),
            Job("restore", [esptool_cmd("write_flash"), hex(entry["offset"]), path],
                message=f"[partition] writing {os.path.basename(path)} into "
                        f"{entry['label']}",
                expect_bytes=os.path.getsize(path)),
        ])

    def erase_partition(self, entry: dict):
        self.enqueue([Job("erase-region", [esptool_cmd("erase_region"),
                                           hex(entry["offset"]), str(entry["size"])],
                          message=f"[partition] erasing {entry['label']} "
                                  f"({human_bytes(entry['size'])})")])

    def flash_images(self, images: list[tuple[int, str]], options: list[str],
                     total: int):
        args = [esptool_cmd("write_flash")] + options
        for addr, path in images:
            args += [hex(addr), path]
        self.enqueue([Job("flash-multi", args,
                          message=f"[flash] {len(images)} images, "
                                  f"{human_bytes(total)}",
                          expect_bytes=total)])

    def merge_images(self, images: list[tuple[int, str]], out: str, fmt: str,
                     options: list[str]):
        args = [esptool_cmd("merge_bin"), "--output", out, "--format", fmt]
        args += [o for o in options if o != "--erase-all"]
        for addr, path in images:
            args += [hex(addr), path]
        self.enqueue([Job("merge", args, needs_port=False,
                          message=f"[merge] {len(images)} images → {out}")])

    def run_offline(self, args: list[str], timeout: float = 60.0) -> str:
        """Run an esptool subcommand that needs no device, synchronously."""
        argv = [sys.executable] + self._worker_argv("esptool") + [str(a) for a in args]
        env = dict(os.environ, NO_COLOR="1", PYTHONUNBUFFERED="1")
        env.pop("TERM", None)
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            done = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout, check=False, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            return f"failed to run esptool: {e}"
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        return strip_ansi((done.stdout or "") + (done.stderr or ""))

    def read_efuses(self, dialog: "EfuseDialog"):
        """Queue an espefuse summary read.

        Goes through the job queue rather than a blocking subprocess: a chip
        that does not answer costs esptool several connect attempts, and the
        UI must stay responsive (and cancellable) through that.
        """
        if espefuse is None:
            dialog.apply_output(
                "espefuse could not be imported, so eFuses cannot be read.\n\n"
                + (ESPEFUSE_ERROR or "reason unknown"))
            return

        def finish():
            try:
                dialog.apply_output("\n".join(self._job_output))
            except RuntimeError:
                pass  # dialog closed mid-read

        self.enqueue([Job("efuse", ["summary"], module="espefuse", capture=True,
                          message="[efuse] reading eFuse summary…",
                          on_success=finish)])

    def _on_stdout(self):
        if not self.process:
            return
        data = bytes(self.process.readAllStandardOutput()).decode(errors="replace")
        for piece in re.split(r"[\r\n]+", data):
            if not piece.strip():
                continue
            found = self._scanner.feed(piece)
            if found.get("progress"):
                self._update_progress(found)
                continue
            clean = strip_ansi(piece)
            if self.current_job is not None and self.current_job.capture:
                self._job_output.append(clean)
            self.append_log(clean + "\n")
            self._apply_scan(found)

    def _apply_scan(self, found: dict):
        if "chip" in found:
            self.detected_chip = found["chip"]
            self.chip_lbl.setText(f"Chip: {found['chip']}")
        if "mac" in found:
            self.detected_mac = found["mac"]
            self.mac_lbl.setText(f"MAC: {found['mac']}")
        if "crystal_mhz" in found and self.chip_lbl.text() != "Chip: —":
            self.chip_lbl.setToolTip(f"Crystal {found['crystal_mhz']} MHz")
        if "features" in found:
            self.chip_lbl.setToolTip(f"Features: {found['features']}")
        if "flash_bytes" in found:
            total = found["flash_bytes"]
            self.detected_flash_bytes = total
            self.flash_lbl.setText(f"Flash size: {human_bytes(total)}")
            for label, value in FLASH_SIZE_MAP.items():
                if value == total:
                    idx = self.backup_size_combo.findText(label)
                    if idx >= 0:
                        self.backup_size_combo.setCurrentIndex(idx)
                    break

    def _update_progress(self, found: dict):
        if "percent" in found:
            self.progress.setValue(int(found["percent"]))
        done = found.get("done")
        total = found.get("total") or self._job_total_bytes
        if done is None and "percent" in found and self._job_total_bytes:
            done = int(self._job_total_bytes * found["percent"] / 100.0)
        if not done:
            return
        self._job_done_bytes = done
        elapsed = max(1e-3, time.monotonic() - self._job_started)
        rate = done / elapsed
        bits = [f"{human_bytes(done)}"]
        if total:
            bits.append(f"/ {human_bytes(total)}")
        bits.append(f"· {human_bytes(rate)}/s")
        if total and rate > 0 and total > done:
            bits.append(f"· ETA {human_duration((total - done) / rate)}")
        self.progress_lbl.setText(" ".join(bits))

    def _on_finished(self, exit_code: int, _exit_status):
        job = self.current_job
        finished = self.process
        self.process = None
        self.current_job = None
        if finished is not None:
            finished.deleteLater()  # one QProcess per job, don't accumulate
        op = job.label if job else "operation"

        if self._cancelled:
            self.append_log(f"[{op}] cancelled.\n")
            self.statusBar().showMessage(f"{op}: cancelled", 5000)
            self._abort_queue()
            return

        if exit_code != 0:
            self.append_log(f"[{op}] FAILED (exit {exit_code}).\n")
            self.statusBar().showMessage(f"{op}: failed", 5000)
            self._abort_queue()
            return

        elapsed = time.monotonic() - self._job_started
        self.append_log(f"[{op}] done in {human_duration(elapsed)}.\n")
        self.progress.setValue(100)
        self.statusBar().showMessage(f"{op}: success", 5000)

        if job is not None and job.on_success is not None:
            try:
                job.on_success()
            except Exception as e:  # noqa: BLE001 - never lose the queue to a callback
                self.append_log(f"[{op}] post-processing failed: {e}\n")
        self._run_next()

    def _abort_queue(self):
        dropped = len(self._queue)
        self._queue.clear()
        if dropped:
            self.append_log(f"[queue] {dropped} queued step(s) skipped.\n")
        if self._pending_parttable is not None:
            dialog, tmp = self._pending_parttable
            self._finish_parttable(dialog, tmp, success=False)
        self.set_running(False)
        self._resume_monitors()

    def _on_error(self, err):
        msg = self.process.errorString() if self.process else "process error"
        self.append_log(f"[process error] {msg}\n")
        if err == QProcess.ProcessError.FailedToStart:
            # QProcess never emits finished() for a process that never ran, so
            # unwind by hand or the UI stays disabled forever.
            self.process = None
            self.current_job = None
            self.statusBar().showMessage("failed to start esptool", 5000)
            self._abort_queue()

    def append_log(self, text: str):
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)
        self.log.insertPlainText(text)
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def set_running(self, running: bool):
        for w in (self.detect_btn, self.backup_btn, self.restore_btn,
                  self.verify_btn, self.erase_btn, self.part_btn,
                  self.multi_btn, self.info_btn, self.security_btn,
                  self.refresh_btn, self.port_combo, self.baud_combo,
                  self.chip_override, self.monitor_btn, self.profile_combo,
                  self.profile_save_btn, self.profile_del_btn):
            w.setEnabled(not running)
        for act in self.tool_actions:
            act.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        for dialog in self._live_dialogs():
            if hasattr(dialog, "set_busy"):
                try:
                    dialog.set_busy(running)
                except RuntimeError:
                    pass

    def _error(self, msg: str):
        QtWidgets.QMessageBox.critical(self, "Error", msg)

    @staticmethod
    def _parse_addr(text: str, default: int = 0) -> int | None:
        text = text.strip()
        if not text:
            return default
        try:
            return int(text, 0)
        except ValueError:
            return None

    def current_profile_dict(self) -> dict:
        return {
            "port": self.selected_port() or "",
            "baud": self.baud_combo.currentText(),
            "chip_idx": self.chip_override.currentIndex(),
            "backup_addr": self.backup_addr.text(),
            "backup_size_idx": self.backup_size_combo.currentIndex(),
            "restore_addr": self.restore_addr.text(),
            "erase_first": self.erase_chk.isChecked(),
            "verify_after": self.verify_chk.isChecked(),
            "trim_backup": self.trim_chk.isChecked(),
        }

    def apply_profile_dict(self, data: dict):
        data = sanitize_profile(data)
        if data.get("port"):
            self._select_port(data["port"])
        if data.get("baud"):
            self.baud_combo.setCurrentText(data["baud"])
        if "chip_idx" in data:
            self.chip_override.setCurrentIndex(
                max(0, min(data["chip_idx"], self.chip_override.count() - 1)))
        if "backup_size_idx" in data:
            self.backup_size_combo.setCurrentIndex(
                max(0, min(data["backup_size_idx"],
                           self.backup_size_combo.count() - 1)))
        for key, widget in (("backup_addr", self.backup_addr),
                            ("restore_addr", self.restore_addr)):
            if data.get(key):
                widget.setText(data[key])
        for key, chk in (("erase_first", self.erase_chk),
                         ("verify_after", self.verify_chk),
                         ("trim_backup", self.trim_chk)):
            if key in data:
                chk.setChecked(data[key])

    def _select_port(self, port: str):
        for i in range(self.port_combo.count()):
            if self.port_combo.itemData(i) == port:
                self.port_combo.setCurrentIndex(i)
                return
        self.port_combo.setEditText(port)

    def _rebuild_profile_combo(self, select: str = ""):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("(none)")
        for name in sorted(self._profiles):
            self.profile_combo.addItem(name)
        if select:
            idx = self.profile_combo.findText(select)
            if idx >= 0:
                self.profile_combo.setCurrentIndex(idx)
        self.profile_combo.blockSignals(False)

    def _profile_selected(self, index: int):
        name = self.profile_combo.itemText(index)
        if name in self._profiles:
            self.apply_profile_dict(self._profiles[name])
            self.append_log(f"[profile] loaded '{name}'\n")
            self._check_port_permission()

    def _save_profile(self):
        current = self.profile_combo.currentText()
        suggestion = current if current in self._profiles else ""
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Save profile", "Profile name:", text=suggestion)
        name = name.strip()
        if not ok or not name:
            return
        self._profiles[name] = self.current_profile_dict()
        self._rebuild_profile_combo(select=name)
        self.append_log(f"[profile] saved '{name}'\n")

    def _delete_profile(self):
        name = self.profile_combo.currentText()
        if name not in self._profiles:
            return
        del self._profiles[name]
        self._rebuild_profile_combo()
        self.append_log(f"[profile] deleted '{name}'\n")

    def _restore_settings(self):
        s = QtCore.QSettings()
        geo = s.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)

        try:
            raw = json.loads(s.value("profiles", "{}", type=str))
            if isinstance(raw, dict):
                self._profiles = {str(k): sanitize_profile(v)
                                  for k, v in raw.items() if isinstance(v, dict)}
        except ValueError:
            self._profiles = {}
        self._rebuild_profile_combo()

        for key, store in (("recent_backups", self._recent_backups),
                           ("recent_firmware", self._recent_firmware)):
            try:
                items = json.loads(s.value(key, "[]", type=str))
            except ValueError:
                items = []
            store[:] = [str(i) for i in items if isinstance(i, str)][:8]
        self._rebuild_recent_menus()

        port = s.value("port", "", type=str)
        if port:
            self._select_port(port)

        self.baud_combo.setCurrentText(s.value("baud", DEFAULT_BAUD, type=str))
        self.chip_override.setCurrentIndex(
            max(0, min(s.value("chip_idx", 0, type=int),
                       self.chip_override.count() - 1)))

        self.backup_path.setText(s.value("backup_path", "", type=str))
        self.backup_addr.setText(s.value("backup_addr", "0x0", type=str))
        self.backup_size_combo.setCurrentIndex(
            max(0, min(s.value("backup_size_idx", 0, type=int),
                       self.backup_size_combo.count() - 1)))

        self.restore_path.setText(s.value("restore_path", "", type=str))
        self.restore_addr.setText(s.value("restore_addr", "0x0", type=str))
        self.erase_chk.setChecked(s.value("erase_first", True, type=bool))
        self.verify_chk.setChecked(s.value("verify_after", True, type=bool))
        self.trim_chk.setChecked(s.value("trim_backup", False, type=bool))
        self.manifest_chk.setChecked(s.value("write_manifest", True, type=bool))
        self.autodetect_chk.setChecked(s.value("autodetect", False, type=bool))

    def _save_settings(self):
        s = QtCore.QSettings()
        s.setValue("geometry", self.saveGeometry())
        s.setValue("port", self.selected_port() or "")
        s.setValue("baud", self.baud_combo.currentText())
        s.setValue("chip_idx", self.chip_override.currentIndex())
        s.setValue("backup_path", self.backup_path.text())
        s.setValue("backup_addr", self.backup_addr.text())
        s.setValue("backup_size_idx", self.backup_size_combo.currentIndex())
        s.setValue("restore_path", self.restore_path.text())
        s.setValue("restore_addr", self.restore_addr.text())
        s.setValue("erase_first", self.erase_chk.isChecked())
        s.setValue("verify_after", self.verify_chk.isChecked())
        s.setValue("trim_backup", self.trim_chk.isChecked())
        s.setValue("write_manifest", self.manifest_chk.isChecked())
        s.setValue("autodetect", self.autodetect_chk.isChecked())
        s.setValue("profiles", json.dumps(self._profiles, sort_keys=True))
        s.setValue("recent_backups", json.dumps(self._recent_backups))
        s.setValue("recent_firmware", json.dumps(self._recent_firmware))

    def closeEvent(self, e):
        if self.busy():
            op = self.current_job.label if self.current_job else "an operation"
            destructive = self.current_job is not None and \
                self.current_job.op in ("restore", "erase", "erase-region",
                                        "flash-multi")
            warning = ("\n\nInterrupting a write leaves the chip half-programmed "
                       "and it will not boot until you flash it again."
                       if destructive else "")
            if QtWidgets.QMessageBox.question(
                self, "Operation in progress",
                f"'{op}' is still running. Stop it and quit?{warning}",
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            ) != QtWidgets.QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self._queue.clear()
            self._cancelled = True
            if self.process is not None:
                self.process.terminate()
                if not self.process.waitForFinished(self.KILL_GRACE_MS):
                    self.process.kill()
                    self.process.waitForFinished(1000)
        self._port_timer.stop()
        self._save_settings()
        super().closeEvent(e)


def worker_argv(module: str) -> list[str]:
    """How to re-invoke `module` — as a -m import, or via a frozen sentinel."""
    if getattr(sys, "frozen", False):
        return [f"--{module}-worker"]
    return ["-m", module]


_SIZE_SUFFIXES = {"KB": 1024, "K": 1024, "MB": 1024 ** 2, "M": 1024 ** 2,
                  "GB": 1024 ** 3, "B": 1}


def parse_size(text: str) -> int | None:
    """Parse a CLI size: '4MB', '512KB', '0x400000', '4194304', 'auto'."""
    text = (text or "").strip()
    if not text or text.lower() in ("auto", "detect"):
        return None
    upper = text.upper().replace(" ", "")
    for suffix, mult in sorted(_SIZE_SUFFIXES.items(), key=lambda kv: -len(kv[0])):
        if upper.endswith(suffix):
            head = upper[:-len(suffix)]
            if head:
                return int(head, 0) * mult
    return int(text, 0)


def cli_esptool(args: list[str], *, port: str = "", baud: str = "",
                chip: str = "", capture: bool = False) -> tuple[int, str]:
    """Run one esptool subcommand, inheriting or capturing stdio."""
    argv = [sys.executable] + worker_argv("esptool")
    if chip:
        argv += ["--chip", chip]
    if port:
        argv += ["--port", port]
    if baud:
        argv += ["--baud", str(baud)]
    argv += [str(a) for a in args]
    env = dict(os.environ, NO_COLOR="1", PYTHONUNBUFFERED="1")
    env.pop("TERM", None)
    print(f"+ esptool {' '.join(str(a) for a in args)}", file=sys.stderr, flush=True)
    if not capture:
        return subprocess.call(argv, env=env), ""
    done = subprocess.run(argv, capture_output=True, text=True, check=False, env=env)
    return done.returncode, strip_ansi((done.stdout or "") + (done.stderr or ""))


def cli_probe(port: str, baud: str = "", chip: str = "") -> dict:
    """flash_id the device and return {chip, mac, flash_bytes} where known."""
    code, out = cli_esptool([esptool_cmd("flash_id")], port=port, chip=chip,
                            capture=True)
    scanner = OutputScanner()
    info: dict = {"exit_code": code}
    for line in out.splitlines():
        found = scanner.feed(line)
        found.pop("progress", None)
        info.update(found)
    if code != 0:
        sys.stderr.write(out)
    return info


def cli_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="esp_flasher",
        description=f"{APP_NAME} {APP_VERSION} — headless flash backup/restore. "
                    "Run with no arguments to open the GUI.")
    p.add_argument("--version", action="version",
                   version=f"esp_flasher {APP_VERSION} "
                           f"(esptool {'.'.join(str(x) for x in ESPTOOL_VERSION)})")
    p.add_argument("--gui", action="store_true", help="Force the GUI.")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-p", "--port", required=True, help="Serial port device.")
    common.add_argument("-b", "--baud", default=DEFAULT_BAUD, help="Serial baud rate.")
    common.add_argument("-c", "--chip", default="",
                        help="Force a chip target instead of auto-detecting.")

    sub = p.add_subparsers(dest="command")

    d = sub.add_parser("detect", parents=[common], help="Identify the chip.")
    d.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    bk = sub.add_parser("backup", parents=[common], help="Read flash to a file.")
    bk.add_argument("-o", "--out", required=True, help="Output .bin path.")
    bk.add_argument("-s", "--size", default="auto",
                    help="Bytes to read: 4MB, 0x400000, or 'auto'.")
    bk.add_argument("-a", "--address", default="0x0", help="Start address.")
    bk.add_argument("--trim", action="store_true",
                    help="Drop the trailing 0xFF tail from the dump.")
    bk.add_argument("--no-manifest", action="store_true",
                    help="Skip the .json sidecar.")

    rs = sub.add_parser("restore", parents=[common], help="Write a file to flash.")
    rs.add_argument("-f", "--file", required=True, help="Image to write.")
    rs.add_argument("-a", "--address", default="0x0", help="Target address.")
    rs.add_argument("--erase", action="store_true", help="Erase the chip first.")
    rs.add_argument("--verify", action="store_true", help="Verify after writing.")
    rs.add_argument("--force", action="store_true",
                    help="Write even when the manifest names a different chip.")

    vf = sub.add_parser("verify", parents=[common], help="Compare a file to flash.")
    vf.add_argument("-f", "--file", required=True, help="Image to compare.")
    vf.add_argument("-a", "--address", default="0x0", help="Address in flash.")

    er = sub.add_parser("erase", parents=[common], help="Erase the whole chip.")
    er.add_argument("--yes", action="store_true", help="Required confirmation.")

    pt = sub.add_parser("partitions", parents=[common],
                        help="Read and print the partition table.")
    pt.add_argument("--offset", default=hex(DEFAULT_PT_OFFSET),
                    help="Partition table offset.")
    pt.add_argument("--dump-dir", default="",
                    help="Also dump every partition into this folder.")
    pt.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    ii = sub.add_parser("image-info", help="Describe a local firmware image.")
    ii.add_argument("-f", "--file", required=True, help="Image to inspect.")

    args = p.parse_args(argv)
    if args.gui or not args.command:
        return gui_main()

    handlers = {
        "detect": _cli_detect, "backup": _cli_backup, "restore": _cli_restore,
        "verify": _cli_verify, "erase": _cli_erase,
        "partitions": _cli_partitions, "image-info": _cli_image_info,
    }
    return handlers[args.command](args)


def _cli_detect(args) -> int:
    info = cli_probe(args.port, args.baud, args.chip)
    code = info.pop("exit_code", 1)
    if args.json:
        print(json.dumps(info, indent=2, sort_keys=True))
    else:
        print(f"Chip:       {info.get('chip', 'unknown')}")
        print(f"MAC:        {info.get('mac', 'unknown')}")
        flash = info.get("flash_bytes")
        print(f"Flash size: {human_bytes(flash) if flash else 'unknown'}")
    return code


def _cli_backup(args) -> int:
    address = int(args.address, 0)
    size = parse_size(args.size)
    chip = mac = None
    flash_bytes = None
    if size is None:
        info = cli_probe(args.port, args.baud, args.chip)
        if info.get("exit_code"):
            return info["exit_code"]
        chip, mac = info.get("chip"), info.get("mac")
        flash_bytes = info.get("flash_bytes")
        size = flash_bytes
        if not size:
            print("error: could not auto-detect the flash size; pass --size",
                  file=sys.stderr)
            return 2

    code, _ = cli_esptool([esptool_cmd("read_flash"), hex(address), str(size),
                           args.out], port=args.port, baud=args.baud, chip=args.chip)
    if code != 0:
        return code

    original = os.path.getsize(args.out)
    if args.trim and address == 0:
        trimmed = trim_trailing_blank(args.out)
        if trimmed:
            print(f"trimmed {human_bytes(trimmed[1] - trimmed[0])} of trailing 0xFF")
    if not args.no_manifest:
        meta = build_manifest(args.out, chip=chip, mac=mac,
                              flash_bytes=flash_bytes, address=address,
                              original_size=original)
        print(f"manifest -> {write_manifest(args.out, meta)}")
    return 0


def _cli_restore(args) -> int:
    if not os.path.isfile(args.file):
        print(f"error: no such file: {args.file}", file=sys.stderr)
        return 2
    address = int(args.address, 0)
    meta = read_manifest(args.file)
    if meta and not args.force:
        info = cli_probe(args.port, args.baud, args.chip)
        problem = restore_mismatch(meta, info.get("chip"))
        if problem:
            print("error: " + problem.replace("\n\n", " "), file=sys.stderr)
            print("Pass --force to write it anyway.", file=sys.stderr)
            return 3

    if args.erase:
        code, _ = cli_esptool([esptool_cmd("erase_flash")], port=args.port,
                              baud=args.baud, chip=args.chip)
        if code != 0:
            return code
    code, _ = cli_esptool([esptool_cmd("write_flash"), hex(address), args.file],
                          port=args.port, baud=args.baud, chip=args.chip)
    if code != 0 or not args.verify:
        return code
    code, _ = cli_esptool([esptool_cmd("verify_flash"), hex(address), args.file],
                          port=args.port, baud=args.baud, chip=args.chip)
    return code


def _cli_verify(args) -> int:
    code, _ = cli_esptool([esptool_cmd("verify_flash"), hex(int(args.address, 0)),
                           args.file],
                          port=args.port, baud=args.baud, chip=args.chip)
    return code


def _cli_erase(args) -> int:
    if not args.yes:
        print("error: refusing to erase without --yes", file=sys.stderr)
        return 2
    code, _ = cli_esptool([esptool_cmd("erase_flash")], port=args.port,
                          baud=args.baud, chip=args.chip)
    return code


def _cli_partitions(args) -> int:
    offset = int(args.offset, 0)
    fd, tmp = tempfile.mkstemp(suffix=".bin", prefix="esp_pt_")
    os.close(fd)
    try:
        code, _ = cli_esptool([esptool_cmd("read_flash"), hex(offset),
                               str(PARTITION_TABLE_SIZE), tmp],
                              port=args.port, baud=args.baud, chip=args.chip)
        if code != 0:
            return code
        with open(tmp, "rb") as fh:
            entries = parse_partition_table(fh.read())
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    if args.json:
        print(json.dumps([
            dict(e, type_name=_type_name(e["type"]),
                 subtype_name=_subtype_name(e["type"], e["subtype"]))
            for e in entries], indent=2))
    else:
        print(f"{'label':<18}{'type':<6}{'subtype':<10}{'offset':>10}{'size':>12}")
        for e in entries:
            print(f"{e['label']:<18}{_type_name(e['type']):<6}"
                  f"{_subtype_name(e['type'], e['subtype']):<10}"
                  f"{hex(e['offset']):>10}{human_bytes(e['size']):>12}")
    if not entries:
        print("no valid partition entries found", file=sys.stderr)
        return 1

    if args.dump_dir:
        os.makedirs(args.dump_dir, exist_ok=True)
        for e in entries:
            base = re.sub(r"[^A-Za-z0-9_.-]", "_",
                          e["label"] or _subtype_name(e["type"], e["subtype"]))
            out = os.path.join(args.dump_dir, f"{base}.bin")
            code, _ = cli_esptool([esptool_cmd("read_flash"), hex(e["offset"]),
                                   str(e["size"]), out],
                                  port=args.port, baud=args.baud, chip=args.chip)
            if code != 0:
                return code
    return 0


def _cli_image_info(args) -> int:
    code, out = cli_esptool([esptool_cmd("image_info"), args.file], capture=True)
    print(out)
    fields = parse_image_info(out)
    if fields:
        print("summary:")
        for key, value in fields.items():
            print(f"  {key}: {value}")
    return code


def gui_main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("EspFlasher")
    app.setApplicationName("EspFlasher")
    app.setApplicationVersion(APP_VERSION)
    app.setDesktopFileName("esp-flasher")
    win = EspFlasher()
    win.show()
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # When packaged with PyInstaller, sys.executable is the bundled EXE, not a
    # Python interpreter.  Jobs re-invoke the EXE with these sentinels so we
    # can dispatch into esptool/espefuse without opening a second GUI window.
    if argv[:1] == ["--esptool-worker"]:
        sys.argv = [sys.argv[0]] + argv[1:]
        esptool.main()
        return 0
    if argv[:1] == ["--espefuse-worker"]:
        sys.argv = [sys.argv[0]] + argv[1:]
        if espefuse is None:
            print(f"espefuse could not be imported: "
                  f"{ESPEFUSE_ERROR or 'reason unknown'}", file=sys.stderr)
            return 1
        espefuse.main()
        return 0

    if argv:
        return cli_main(argv)
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
