"""Unit tests for esp_flasher's pure helpers (no device, no Qt widgets)."""

import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import esp_flasher as ef  # noqa: E402


def test_esptool_version_is_numeric():
    version = ef.esptool_version()
    assert version and all(isinstance(p, int) for p in version)


@pytest.mark.parametrize("name", ["read_flash", "write_flash", "erase_flash",
                                  "flash_id", "verify_flash", "erase_region",
                                  "image_info", "merge_bin"])
def test_esptool_cmd_matches_installed_dialect(name):
    got = ef.esptool_cmd(name)
    if ef.ESPTOOL_MAJOR >= 5:
        assert got == name.replace("_", "-")
    else:
        assert got == name


def test_strip_ansi_removes_cursor_and_colour_codes():
    raw = ("\x1b[1A\x1b[2K\x1b[KReading from 0x00000400 "
           "\x1b[1;31m[====>]\x1b[0m  50.0%")
    assert ef.strip_ansi(raw) == "Reading from 0x00000400 [====>]  50.0%"


class TestOutputScanner:
    """The scanner has to speak both esptool 4.x and 5.x."""

    def setup_method(self):
        self.scanner = ef.OutputScanner()

    def test_esptool5_chip_type(self):
        found = self.scanner.feed("Chip type:          ESP32-D0WD-V3 (revision v3.0)")
        assert found["chip"] == "ESP32-D0WD-V3 (revision v3.0)"

    def test_esptool4_chip_is(self):
        found = self.scanner.feed("Chip is ESP32-D0WD-V3 (revision v3.0)")
        assert found["chip"] == "ESP32-D0WD-V3 (revision v3.0)"

    def test_mac_padded(self):
        found = self.scanner.feed("MAC:                24:6f:28:1a:2b:3c")
        assert found["mac"] == "24:6f:28:1a:2b:3c"

    def test_base_mac_does_not_clobber_mac(self):
        """C6/H2 print MAC, then BASE MAC and MAC_EXT — only MAC counts."""
        assert "mac" not in self.scanner.feed("BASE MAC:           60:55:f9:f7:2c:a2")
        assert "mac" not in self.scanner.feed("MAC_EXT:            ff:fe")

    def test_eui64_mac(self):
        found = self.scanner.feed("MAC:                60:55:f9:ff:fe:f7:2c:a2")
        assert found["mac"] == "60:55:f9:ff:fe:f7:2c:a2"

    @pytest.mark.parametrize("line,expected", [
        ("Detected flash size: 4MB", 4 * 1024 * 1024),
        ("Detected flash size: 16MB", 16 * 1024 * 1024),
        ("Auto-detected flash size: 4MB", 4 * 1024 * 1024),
        ("Detected flash size: 512KB", 512 * 1024),
        ("Detected flash size: 256KB", 256 * 1024),
        ("flash size: 4 MB", 4 * 1024 * 1024),
    ])
    def test_flash_sizes_including_kb(self, line, expected):
        assert self.scanner.feed(line)["flash_bytes"] == expected

    def test_unknown_flash_size_is_not_a_number(self):
        assert "flash_bytes" not in self.scanner.feed("Detected flash size: Unknown")

    def test_progress_line_is_flagged_and_carries_bytes(self):
        line = ("Reading from 0x00000400 [==============>               ]  "
                "50.0% 524288/1048576 bytes... ")
        found = self.scanner.feed(line)
        assert found["progress"] is True
        assert found["percent"] == 50.0
        assert (found["done"], found["total"]) == (524288, 1048576)

    def test_esptool4_progress_line(self):
        found = self.scanner.feed("Writing at 0x00008000... (12 %)")
        assert found["progress"] is True
        assert found["percent"] == 12.0

    def test_progress_line_never_yields_chip_info(self):
        found = self.scanner.feed("Writing at 0x00001000 [=>] 3.0% 1024/32768 bytes...")
        assert set(found) <= {"progress", "percent", "done", "total"}

    def test_crystal_and_features(self):
        assert self.scanner.feed("Crystal frequency:  40MHz")["crystal_mhz"] == 40
        assert self.scanner.feed("Crystal is 26MHz")["crystal_mhz"] == 26
        assert "WiFi" in self.scanner.feed("Features:           WiFi, BT")["features"]

    def test_ansi_wrapped_line_still_parses(self):
        found = self.scanner.feed("\x1b[K\x1b[1;36mChip type:          ESP32-C3\x1b[0m")
        assert found["chip"] == "ESP32-C3"


@pytest.mark.parametrize("desc,family", [
    ("ESP32-D0WD-V3 (revision v3.0)", "ESP32"),
    ("ESP32-C3 (QFN32) (revision v0.4)", "ESP32-C3"),
    ("ESP32-S3 (QFN56) (revision v0.2)", "ESP32-S3"),
    ("ESP8266EX", "ESP8266"),
    ("ESP32-P4 (revision v0.1)", "ESP32-P4"),
    ("ESP32-S2 in Secure Download Mode", "ESP32-S2"),
    ("", ""),
])
def test_chip_family(desc, family):
    assert ef.chip_family(desc) == family


def test_chip_choices_covers_installed_targets():
    labels = dict(ef.chip_choices())
    assert labels["Auto"] is None
    flags = set(labels.values())
    assert {"esp8266", "esp32", "esp32c3", "esp32s3"} <= flags
    if ef._ESPTOOL_CHIP_LIST:
        assert flags - {None} == set(ef._ESPTOOL_CHIP_LIST) - {"auto"}


def test_flash_size_map_reaches_128mb():
    assert ef.FLASH_SIZE_MAP["128 MB"] == 0x8000000
    assert ef.FLASH_SIZE_MAP["Auto-detect"] is None


def test_human_bytes_and_duration():
    assert ef.human_bytes(512) == "512 B"
    assert ef.human_bytes(4 * 1024 * 1024) == "4.00 MB"
    assert ef.human_duration(75) == "1:15"
    assert ef.human_duration(3725) == "1:02:05"


class TestPartitionTable:
    @staticmethod
    def entry(label, ptype, subtype, offset, size, flags=0):
        return (b"\xaa\x50" + bytes([ptype, subtype])
                + offset.to_bytes(4, "little") + size.to_bytes(4, "little")
                + label.encode().ljust(16, b"\x00")
                + flags.to_bytes(4, "little"))

    def test_parses_typical_table(self):
        data = (self.entry("nvs", 1, 2, 0x9000, 0x6000)
                + self.entry("phy_init", 1, 1, 0xF000, 0x1000)
                + self.entry("factory", 0, 0, 0x10000, 0x100000)
                + b"\xff" * 32)
        entries = ef.parse_partition_table(data)
        assert [e["label"] for e in entries] == ["nvs", "phy_init", "factory"]
        assert entries[2]["offset"] == 0x10000
        assert ef._subtype_name(1, 2) == "nvs"
        assert ef._type_name(0) == "app"

    def test_skips_md5_entry_and_stops_at_blank(self):
        data = (self.entry("nvs", 1, 2, 0x9000, 0x6000)
                + b"\xeb\xeb" + b"\x00" * 30
                + self.entry("app", 0, 0x10, 0x10000, 0x100000)
                + b"\xff" * 32
                + self.entry("never", 0, 0, 0x200000, 0x1000))
        entries = ef.parse_partition_table(data)
        assert [e["label"] for e in entries] == ["nvs", "app"]
        assert ef._subtype_name(0, 0x10) == "ota_0"

    def test_empty_and_blank_input(self):
        assert ef.parse_partition_table(b"") == []
        assert ef.parse_partition_table(b"\xff" * 0xC00) == []


class TestFlashArgs:
    SAMPLE = (
        "--flash_mode dio --flash_freq 40m --flash_size 2MB\n"
        "0x1000 bootloader/bootloader.bin\n"
        "0x8000 partition_table/partition-table.bin\n"
        "0x10000 hello_world.bin\n"
    )

    def test_parses_options_and_images(self):
        images, opts = ef.parse_flash_args(self.SAMPLE, "/build")
        assert opts == {"flash-mode": "dio", "flash-freq": "40m",
                        "flash-size": "2MB"}
        assert images[0] == (0x1000, os.path.normpath("/build/bootloader/bootloader.bin"))
        assert [a for a, _ in images] == [0x1000, 0x8000, 0x10000]

    def test_absolute_paths_are_untouched(self):
        images, _ = ef.parse_flash_args("0x0 /tmp/app.bin\n", "/build")
        assert images == [(0x0, "/tmp/app.bin")]

    def test_ignores_comments_and_junk(self):
        images, opts = ef.parse_flash_args(
            "# comment\n\nnotanaddress file.bin\n0x0 app.bin\n--erase-all\n")
        assert images == [(0x0, "app.bin")]
        assert opts == {"erase-all": "true"}


def test_find_overlaps():
    assert ef.find_overlaps([(0x0, 0x1000, "a.bin"), (0x1000, 0x1000, "b.bin")]) == []
    problems = ef.find_overlaps([(0x0, 0x2000, "a.bin"), (0x1000, 0x1000, "b.bin")])
    assert len(problems) == 1 and "overlaps" in problems[0]


class TestManifest:
    def test_round_trip(self, tmp_path):
        binary = tmp_path / "dump.bin"
        binary.write_bytes(b"\x01\x02\x03\x04")
        meta = ef.build_manifest(str(binary), chip="ESP32-C3 (revision v0.4)",
                                 mac="aa:bb:cc:dd:ee:ff",
                                 flash_bytes=4 * 1024 * 1024, address=0)
        assert meta["chip_family"] == "ESP32-C3"
        assert meta["size"] == 4
        assert len(meta["sha256"]) == 64
        path = ef.write_manifest(str(binary), meta)
        assert path == str(binary) + ".json"
        assert ef.read_manifest(str(binary)) == meta

    def test_missing_and_corrupt_manifest(self, tmp_path):
        binary = tmp_path / "dump.bin"
        binary.write_bytes(b"x")
        assert ef.read_manifest(str(binary)) is None
        (tmp_path / "dump.bin.json").write_text("{not json")
        assert ef.read_manifest(str(binary)) is None
        (tmp_path / "dump.bin.json").write_text("[1, 2]")
        assert ef.read_manifest(str(binary)) is None

    def test_manifest_is_valid_json(self, tmp_path):
        binary = tmp_path / "d.bin"
        binary.write_bytes(b"abc")
        ef.write_manifest(str(binary), ef.build_manifest(str(binary)))
        json.loads((tmp_path / "d.bin.json").read_text())


class TestRestoreGuard:
    def test_blocks_a_different_family(self):
        meta = {"chip": "ESP32-C3 (revision v0.4)", "chip_family": "ESP32-C3"}
        problem = ef.restore_mismatch(meta, "ESP32-D0WD-V3 (revision v3.0)")
        assert problem and "ESP32-C3" in problem and "brick" in problem

    def test_allows_same_family_different_revision(self):
        meta = {"chip": "ESP32-D0WD-V3 (revision v3.0)", "chip_family": "ESP32"}
        assert ef.restore_mismatch(meta, "ESP32-D0WDR2-V3 (revision v3.1)") is None

    def test_derives_family_from_chip_when_absent(self):
        assert ef.restore_mismatch({"chip": "ESP8266EX"}, "ESP8266EX") is None
        assert ef.restore_mismatch({"chip": "ESP8266EX"}, "ESP32-C3") is not None

    def test_silent_without_data(self):
        assert ef.restore_mismatch(None, "ESP32") is None
        assert ef.restore_mismatch({"chip": "ESP32"}, None) is None
        assert ef.restore_mismatch({}, "ESP32") is None


class TestTrim:
    def test_measures_and_trims_the_blank_tail(self, tmp_path):
        path = tmp_path / "dump.bin"
        path.write_bytes(b"\x01" * 5000 + b"\xff" * (1 << 20))
        assert ef.trailing_blank_length(str(path)) == 1 << 20
        new_size, old_size = ef.trim_trailing_blank(str(path))
        assert old_size == 5000 + (1 << 20)
        assert new_size == 8192  # 5000 rounded up to the 4 KB alignment
        assert path.stat().st_size == 8192

    def test_leaves_small_tails_alone(self, tmp_path):
        path = tmp_path / "dump.bin"
        path.write_bytes(b"\x01" * 1000 + b"\xff" * 4096)
        assert ef.trim_trailing_blank(str(path)) is None
        assert path.stat().st_size == 1000 + 4096

    def test_no_tail_at_all(self, tmp_path):
        path = tmp_path / "dump.bin"
        path.write_bytes(b"\x01" * 100)
        assert ef.trailing_blank_length(str(path)) == 0
        assert ef.trim_trailing_blank(str(path)) is None

    def test_fully_erased_dump_keeps_one_block(self, tmp_path):
        path = tmp_path / "dump.bin"
        path.write_bytes(b"\xff" * (1 << 20))
        new_size, _ = ef.trim_trailing_blank(str(path))
        assert new_size == 4096

    def test_blank_run_spanning_read_chunks(self, tmp_path):
        path = tmp_path / "dump.bin"
        path.write_bytes(b"\x01" + b"\xff" * (3 << 20))
        assert ef.trailing_blank_length(str(path), chunk=1024) == 3 << 20

    def test_empty_file(self, tmp_path):
        path = tmp_path / "e.bin"
        path.write_bytes(b"")
        assert ef.trailing_blank_length(str(path)) == 0


class TestEfuseParsing:
    ESP32_PLAIN = """
FLASH_CRYPT_CNT (BLOCK0)      Flash encryption mode counter     = 0 R/W (0b0000000)
ABS_DONE_0 (BLOCK0)           Secure boot V1 is enabled         = False R/W (0b0)
MAC (BLOCK0)                  MAC address                       = 24:6f:28:1a:2b:3c
"""
    ESP32_ENCRYPTED = """
FLASH_CRYPT_CNT (BLOCK0)      Flash encryption mode counter     = 1 R/W (0b0000001)
ABS_DONE_0 (BLOCK0)           Secure boot V1 is enabled         = True R/W (0b1)
"""
    C3_ENCRYPTED = """
SPI_BOOT_CRYPT_CNT (BLOCK0)   Enables encryption and decryption = 1 R/W (0b001)
SECURE_BOOT_EN (BLOCK0)       Set this bit to enable secure boot = True R/W (0b1)
"""

    def test_plain_chip(self):
        info = ef.parse_efuse_security(self.ESP32_PLAIN)
        assert info["flash_encryption"] is False
        assert info["secure_boot"] is False
        assert info["fields"]["FLASH_CRYPT_CNT"] == "0"

    def test_encrypted_esp32(self):
        info = ef.parse_efuse_security(self.ESP32_ENCRYPTED)
        assert info["flash_encryption"] is True
        assert info["secure_boot"] is True

    def test_encrypted_riscv(self):
        info = ef.parse_efuse_security(self.C3_ENCRYPTED)
        assert info["flash_encryption"] is True
        assert info["secure_boot"] is True

    def test_even_bit_count_means_disabled_again(self):
        """Two burns of FLASH_CRYPT_CNT turn encryption back off."""
        info = ef.parse_efuse_security(
            "FLASH_CRYPT_CNT (BLOCK0)  counter = 3 R/W (0b0000011)\n")
        assert info["flash_encryption"] is False

    def test_empty_output(self):
        info = ef.parse_efuse_security("")
        assert info["flash_encryption"] is None
        assert info["fields"] == {}


class TestBacktrace:
    def test_xtensa_backtrace(self):
        pcs = ef.parse_backtrace(
            "Backtrace: 0x400d1234:0x3ffb0000 0x400d5678:0x3ffb0010 |<-CORRUPTED")
        assert pcs == [0x400d1234, 0x400d5678]

    def test_no_space_after_label(self):
        assert ef.parse_backtrace("Backtrace:0x400d1234:0x3ffb0000") == [0x400d1234]

    def test_riscv_register_dump(self):
        assert ef.parse_backtrace("MEPC    : 0x42009876  RA : 0x4200aaaa") == [0x42009876]

    def test_ordinary_lines_are_ignored(self):
        assert ef.parse_backtrace("I (300) app: hello") == []
        assert ef.parse_backtrace("") == []

    def test_ansi_coloured_backtrace(self):
        assert ef.parse_backtrace(
            "\x1b[0;31mBacktrace: 0x400d1234:0x3ffb0000\x1b[0m") == [0x400d1234]


def test_find_addr2line_rejects_missing_tool():
    assert ef.find_addr2line("definitely-not-a-real-addr2line-xyz") is None


def test_decode_addresses_is_safe_without_an_elf(tmp_path):
    assert ef.decode_addresses(str(tmp_path / "nope.elf"), [0x400d1234]) == []
    assert ef.decode_addresses(__file__, []) == []


class TestAnsiSegments:
    def test_plain_text_is_one_segment(self):
        segments, state = ef.parse_ansi_segments("hello")
        assert segments == [("hello", {})]
        assert state == {}

    def test_colour_applies_to_following_text(self):
        segments, _ = ef.parse_ansi_segments("a\x1b[0;31mred\x1b[0mb")
        assert segments[0] == ("a", {})
        assert segments[1][0] == "red"
        assert segments[1][1]["color"] == ef.ANSI_COLORS[31]
        assert segments[2] == ("b", {})

    def test_state_carries_across_calls(self):
        _, state = ef.parse_ansi_segments("\x1b[1;33mwarn")
        assert state == {"bold": True, "color": ef.ANSI_COLORS[33]}
        segments, _ = ef.parse_ansi_segments("more", state)
        assert segments == [("more", {"bold": True, "color": ef.ANSI_COLORS[33]})]

    def test_reset_clears_bold_and_colour(self):
        _, state = ef.parse_ansi_segments("\x1b[1;31mx\x1b[0m")
        assert state == {}

    def test_default_foreground_code(self):
        _, state = ef.parse_ansi_segments("\x1b[31ma\x1b[39m")
        assert "color" not in state


class TestSearch:
    @pytest.mark.parametrize("text,mode,expected", [
        ("de ad be ef", "auto", b"\xde\xad\xbe\xef"),
        ("0xdeadbeef", "auto", b"\xde\xad\xbe\xef"),
        ("DE:AD", "hex", b"\xde\xad"),
        ("hello", "auto", b"hello"),
        ("hello", "text", b"hello"),
        ("dead", "text", b"dead"),
        ("", "auto", None),
        ("xyz", "hex", None),
        ("abc", "hex", None),
    ])
    def test_parse_needle(self, text, mode, expected):
        assert ef.parse_search_needle(text, mode) == expected

    def test_search_forward_and_wrap(self):
        data = b"AAABBBAAA"
        assert ef.search_bytes(data, b"BBB", 0) == 3
        assert ef.search_bytes(data, b"AAA", 1) == 6
        assert ef.search_bytes(data, b"AAA", 7) == 0  # wraps around
        assert ef.search_bytes(data, b"ZZZ", 0) == -1

    def test_search_backwards(self):
        data = b"AAABBBAAA"
        assert ef.search_bytes(data, b"AAA", 9, backwards=True) == 6
        assert ef.search_bytes(data, b"AAA", 6, backwards=True) == 0
        assert ef.search_bytes(data, b"AAA", 0, backwards=True) == 6  # wraps

    def test_degenerate_input(self):
        assert ef.search_bytes(b"", b"A", 0) == -1
        assert ef.search_bytes(b"A", b"", 0) == -1


def test_parse_image_info():
    text = """esptool v5.2.0
Image size: 176160 bytes
Segments: 5
Validation hash: 8f1c… (valid)
Project name: hello_world
App version: 1.0.0
Compile time: Jan  1 2026 10:00:00
ESP-IDF: v5.2
"""
    fields = ef.parse_image_info(text)
    assert fields["Project name"] == "hello_world"
    assert fields["ESP-IDF"] == "v5.2"
    assert fields["Image size"] == "176160 bytes"
    assert ef.parse_image_info("nothing useful here") == {}


@pytest.mark.parametrize("text,expected", [
    ("4MB", 4 * 1024 * 1024),
    ("512KB", 512 * 1024),
    ("0x400000", 0x400000),
    ("4194304", 4194304),
    ("auto", None),
    ("detect", None),
    ("", None),
])
def test_parse_size(text, expected):
    assert ef.parse_size(text) == expected


def test_parse_size_rejects_nonsense():
    with pytest.raises(ValueError):
        ef.parse_size("banana")


def test_sanitize_profile_coerces_and_drops_unknowns():
    clean = ef.sanitize_profile({
        "port": "/dev/ttyUSB0", "baud": 460800, "chip_idx": "3",
        "erase_first": "true", "verify_after": False,
        "nonsense": "drop me", "backup_size_idx": "not a number",
    })
    assert clean == {"port": "/dev/ttyUSB0", "baud": "460800", "chip_idx": 3,
                     "erase_first": True, "verify_after": False}


def test_profile_round_trips_through_json():
    original = {"port": "/dev/ttyACM0", "baud": "921600", "chip_idx": 2,
                "backup_addr": "0x0", "backup_size_idx": 5,
                "restore_addr": "0x10000", "erase_first": False,
                "verify_after": True, "trim_backup": True}
    assert ef.sanitize_profile(json.loads(json.dumps(original))) == original


def test_worker_argv_uses_module_when_not_frozen():
    assert ef.worker_argv("esptool") == ["-m", "esptool"]


def test_serial_permission_hint_ignores_missing_ports():
    assert ef.serial_permission_hint("/dev/definitely-not-here") is None
    assert ef.serial_permission_hint("") is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_serial_permission_hint_quiet_on_accessible_file(tmp_path):
    path = tmp_path / "ttyFake"
    path.write_bytes(b"")
    assert ef.serial_permission_hint(str(path)) is None


def test_list_serial_ports_shape():
    for entry in ef.list_serial_ports():
        assert isinstance(entry, tuple) and len(entry) == 2
        assert all(isinstance(x, str) for x in entry)


class TestEsptoolFlagAliases:
    """esptool 4 kept --flash_* underscored but hyphenated everything else."""

    def test_v5_leaves_flags_alone(self, monkeypatch):
        monkeypatch.setattr(ef, "ESPTOOL_MAJOR", 5)
        for flag in ("--flash-mode", "--flash-freq", "--flash-size",
                     "--erase-all", "--output", "--format", "--no-progress"):
            assert ef.esptool_flag(flag) == flag

    def test_v4_translates_only_the_flash_options(self, monkeypatch):
        monkeypatch.setattr(ef, "ESPTOOL_MAJOR", 4)
        assert ef.esptool_flag("--flash-mode") == "--flash_mode"
        assert ef.esptool_flag("--flash-freq") == "--flash_freq"
        assert ef.esptool_flag("--flash-size") == "--flash_size"
        for unchanged in ("--erase-all", "--output", "--format", "--no-progress",
                          "--chip", "--port", "--baud"):
            assert ef.esptool_flag(unchanged) == unchanged

    def test_no_flag_ever_loses_its_double_dash(self, monkeypatch):
        for major in (4, 5):
            monkeypatch.setattr(ef, "ESPTOOL_MAJOR", major)
            for flag in _FLAGS_WE_EMIT:
                assert ef.esptool_flag(flag).startswith("--")


_FLAGS_WE_EMIT = ("--flash-mode", "--flash-freq", "--flash-size", "--erase-all",
                  "--output", "--format", "--chip", "--port", "--baud")


class TestVersionConsistency:
    """The app version, the packaging version and the changelog must agree.

    They drifted before: the Makefile shipped `VERSION ?= 0.1.0` while the
    module said 0.2.0, so a built .deb was named after a version that did not
    exist anywhere else.
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_version_looks_like_a_release(self):
        assert re.fullmatch(r"\d+\.\d+\.\d+", ef.APP_VERSION), ef.APP_VERSION

    def test_makefile_default_matches_the_module(self):
        makefile = open(os.path.join(self.ROOT, "Makefile"), encoding="utf-8").read()
        m = re.search(r"^VERSION\s*\?=\s*(\S+)", makefile, re.M)
        assert m, "Makefile has no VERSION default"
        assert m.group(1) == ef.APP_VERSION

    def test_changelog_leads_with_this_version(self):
        path = os.path.join(self.ROOT, "CHANGELOG.md")
        headings = re.findall(r"^##\s+(\d+\.\d+\.\d+)", open(path, encoding="utf-8").read(), re.M)
        assert headings, "CHANGELOG has no version headings"
        assert headings[0] == ef.APP_VERSION

    def test_changelog_versions_descend(self):
        path = os.path.join(self.ROOT, "CHANGELOG.md")
        headings = re.findall(r"^##\s+(\d+\.\d+\.\d+)", open(path, encoding="utf-8").read(), re.M)
        keys = [tuple(int(p) for p in h.split(".")) for h in headings]
        assert keys == sorted(keys, reverse=True), headings

    def test_readme_deb_examples_use_this_version(self):
        readme = open(os.path.join(self.ROOT, "README.md"), encoding="utf-8").read()
        for stale in re.findall(r"esp-flasher_(\d+\.\d+\.\d+)_\w+\.deb", readme):
            assert stale == ef.APP_VERSION, f"README names .deb {stale}"

    def test_documented_test_count_is_current(self, request):
        """README and CHANGELOG both quote a test count; keep it honest.

        This has gone stale twice already. Skipped on partial runs, where the
        collected count is not the whole suite.
        """
        option = request.config.option
        partial = (getattr(option, "keyword", "") or getattr(option, "markexpr", "")
                   or getattr(option, "file_or_dir", []) not in ([], ["tests"]))
        if partial:
            pytest.skip("test count is only meaningful for a full run")

        total = len(request.session.items)
        for name, pattern in (("README.md", r"^(\d+) tests:"),
                              ("CHANGELOG.md", r"Test suite \((\d+) tests")):
            text = open(os.path.join(self.ROOT, name), encoding="utf-8").read()
            m = re.search(pattern, text, re.M)
            assert m, f"{name} no longer quotes a test count"
            assert int(m.group(1)) == total, (
                f"{name} says {m.group(1)} tests, suite collects {total}")


class TestBuildInterpreter:
    """`make build-*` has to run build.py with the project venv.

    The distro python is externally managed (PEP 668), so it has neither
    PyInstaller nor PyQt6 and cannot be given them; a bare `python3` died on
    build.py's import guard. The Makefile is exercised with `-n`, which
    expands the recipes without running them, so these stay host-independent.
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _make(self, cwd, *targets):
        make = shutil.which("make")
        if make is None:
            pytest.skip("make is not installed")
        shutil.copy(os.path.join(self.ROOT, "Makefile"), os.path.join(cwd, "Makefile"))
        proc = subprocess.run([make, "-n", *targets], cwd=cwd,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    def _fake_venv(self, root):
        python = os.path.join(root, ".venv", "bin", "python")
        os.makedirs(os.path.dirname(python))
        with open(python, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(python, 0o755)

    def test_build_uses_the_venv_when_there_is_one(self, tmp_path):
        self._fake_venv(str(tmp_path))
        out = self._make(str(tmp_path), "build-linux-amd64")
        assert ".venv/bin/python build.py" in out

    def test_build_falls_back_to_python3_without_a_venv(self, tmp_path):
        out = self._make(str(tmp_path), "build-linux-amd64")
        assert "python3 build.py" in out

    def test_deps_creates_the_venv_before_installing(self, tmp_path):
        out = self._make(str(tmp_path), "deps")
        assert "-m venv .venv" in out
        assert out.index("-m venv") < out.index("pip install"), out

    def test_py_override_wins(self, tmp_path):
        self._fake_venv(str(tmp_path))
        out = self._make(str(tmp_path), "PY=/usr/bin/python3", "build-linux-amd64")
        assert "/usr/bin/python3 build.py" in out
