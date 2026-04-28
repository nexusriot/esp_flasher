#!/usr/bin/env python3
"""Build a standalone executable of esp_flasher with PyInstaller.

Cross-platform driver. Produces dist/esp_flasher-<os>-<arch>[.exe] in
single-file mode by default, or a directory bundle with --onedir.

    python build.py                 # release one-file build
    python build.py --onedir        # one-folder build (faster startup)
    python build.py --debug         # keep console attached for tracebacks
    python build.py --clean         # nuke build/ dist/ *.spec first
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "esp_flasher.py"
DIST = ROOT / "dist"
BUILD = ROOT / "build"


def output_name() -> str:
    sysname = platform.system().lower()
    if sysname == "darwin":
        sysname = "macos"
    arch = platform.machine().lower() or "unknown"
    # Normalise Windows' "AMD64" → "amd64", keep linux x86_64 / aarch64 as-is.
    return f"esp_flasher-{sysname}-{arch}"


def run(argv: list[str]) -> None:
    print("$", " ".join(argv), flush=True)
    subprocess.check_call(argv, cwd=ROOT)


def find_artifact(name: str, onedir: bool) -> Path | None:
    is_win = platform.system() == "Windows"
    is_mac = platform.system() == "Darwin"

    candidates: list[Path] = []
    if onedir:
        candidates.append(DIST / name)
    else:
        if is_win:
            candidates.append(DIST / f"{name}.exe")
        if is_mac:
            candidates.append(DIST / f"{name}.app")
        candidates.append(DIST / name)
    for c in candidates:
        if c.exists():
            return c
    return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--onedir", action="store_true",
                   help="Bundle as a directory (faster startup, more files).")
    p.add_argument("--debug", action="store_true",
                   help="Keep the console attached so tracebacks are visible.")
    p.add_argument("--clean", action="store_true",
                   help="Remove build/, dist/ and *.spec before building.")
    p.add_argument("--name", default=None,
                   help="Override the output binary name.")
    args = p.parse_args()

    if not ENTRY.exists():
        print(f"error: entry not found: {ENTRY}", file=sys.stderr)
        return 1

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "error: PyInstaller is not installed. Run:\n"
            "  pip install -r requirements.txt -r requirements-build.txt",
            file=sys.stderr,
        )
        return 1

    name = args.name or output_name()

    if args.clean:
        for d in (BUILD, DIST):
            if d.exists():
                print(f"removing {d}")
                shutil.rmtree(d)
        for spec in ROOT.glob("*.spec"):
            print(f"removing {spec.name}")
            spec.unlink()

    argv = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", name,
        # esptool ships chip-specific submodules and stub-flasher data files
        # that PyInstaller's static analysis misses; pyserial has per-platform
        # backends. --collect-all pulls submodules + data + binaries.
        "--collect-all", "esptool",
        "--collect-all", "serial",
        "--onedir" if args.onedir else "--onefile",
    ]
    if not args.debug:
        # macOS: produces a .app bundle. Windows: hides console window.
        # Linux: no-op (no console attached for GUI launches anyway).
        argv.append("--windowed")
    argv.append(str(ENTRY))

    try:
        run(argv)
    except subprocess.CalledProcessError as e:
        print(f"\nbuild failed (exit {e.returncode})", file=sys.stderr)
        return e.returncode

    art = find_artifact(name, onedir=args.onedir)
    if art is None:
        print(f"\nbuild finished but no artifact found in {DIST}/", file=sys.stderr)
        return 2

    if art.is_file():
        size_mb = art.stat().st_size / (1024 * 1024)
        print(f"\nbuilt: {art}  ({size_mb:.1f} MB)")
    else:
        print(f"\nbuilt: {art}/  (bundle)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
