"""Widget-level tests. Qt runs on the offscreen platform (see conftest)."""

import os
import sys

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import esp_flasher as ef  # noqa: E402


def partition_entry(label, ptype, subtype, offset, size, flags=0):
    return (b"\xaa\x50" + bytes([ptype, subtype])
            + offset.to_bytes(4, "little") + size.to_bytes(4, "little")
            + label.encode().ljust(16, b"\x00") + flags.to_bytes(4, "little"))


class TestChipInfoPanel:
    """The regression that started this: esptool 5 renamed the chip line."""

    def test_esptool5_output_populates_every_label(self, win, fake_process):
        win.process = fake_process(
            b"Connected to ESP32 on /dev/ttyUSB0:\n"
            b"Chip type:          ESP32-D0WD-V3 (revision v3.0)\n"
            b"Features:           WiFi, BT, Dual Core, 240MHz\n"
            b"Crystal frequency:  40MHz\n"
            b"MAC:                24:6f:28:1a:2b:3c\n"
            b"Detected flash size: 4MB\n"
        )
        win._on_stdout()
        assert win.chip_lbl.text() == "Chip: ESP32-D0WD-V3 (revision v3.0)"
        assert win.mac_lbl.text() == "MAC: 24:6f:28:1a:2b:3c"
        assert win.flash_lbl.text() == "Flash size: 4.00 MB"
        assert win.detected_flash_bytes == 4 * 1024 * 1024
        assert win.detected_chip == "ESP32-D0WD-V3 (revision v3.0)"

    def test_esptool4_output_still_works(self, win, fake_process):
        win.process = fake_process(
            b"Chip is ESP32-D0WD-V3 (revision v3.0)\n"
            b"MAC: 24:6f:28:1a:2b:3c\n"
            b"Detected flash size: 4MB\n"
        )
        win._on_stdout()
        assert win.chip_lbl.text() == "Chip: ESP32-D0WD-V3 (revision v3.0)"
        assert win.detected_mac == "24:6f:28:1a:2b:3c"

    def test_kb_flash_size_selects_the_matching_combo_entry(self, win, fake_process):
        win.process = fake_process(b"Detected flash size: 512KB\n")
        win._on_stdout()
        assert win.detected_flash_bytes == 512 * 1024
        assert win.backup_size_combo.currentText() == "512 KB"

    def test_mb_flash_size_selects_the_matching_combo_entry(self, win, fake_process):
        win.process = fake_process(b"Detected flash size: 16MB\n")
        win._on_stdout()
        assert win.backup_size_combo.currentText() == "16 MB"


class TestLogHygiene:
    def test_log_is_capped(self, win):
        assert win.log.maximumBlockCount() == ef.EspFlasher.LOG_MAX_BLOCKS

    def test_progress_lines_never_reach_the_log(self, win, fake_process):
        """esptool emits one progress line per 1024 bytes — thousands per dump."""
        lines = b"".join(
            f"Reading from 0x{i * 1024:08x} [=>]  {i / 40:.1f}% "
            f"{i * 1024}/4096000 bytes... \n".encode()
            for i in range(4000))
        win.process = fake_process(lines + b"Hash of data verified.\n")
        win._on_stdout()
        text = win.log.toPlainText()
        assert "Reading from" not in text
        assert "Hash of data verified." in text
        assert win.progress.value() == 100

    def test_ansi_codes_are_stripped_from_the_log(self, win, fake_process):
        win.process = fake_process(
            b"\x1b[1A\x1b[2K\x1b[KHash of data verified.\x1b[0m\n")
        win._on_stdout()
        assert win.log.toPlainText().strip() == "Hash of data verified."
        assert "\x1b" not in win.log.toPlainText()

    def test_progress_label_reports_throughput_and_eta(self, win):
        win._job_started = ef.time.monotonic() - 10.0
        win._job_total_bytes = 4 * 1024 * 1024
        win._update_progress({"progress": True, "percent": 50.0,
                              "done": 2 * 1024 * 1024, "total": 4 * 1024 * 1024})
        label = win.progress_lbl.text()
        assert "2.00 MB" in label and "/ 4.00 MB" in label
        assert "/s" in label and "ETA" in label
        assert win.progress.value() == 50

    def test_progress_without_byte_counts_still_moves_the_bar(self, win):
        win._job_total_bytes = 1024
        win._update_progress({"progress": True, "percent": 25.0})
        assert win.progress.value() == 25


class TestArgvConstruction:
    def test_hyphenated_commands_on_esptool5(self, win):
        win.port_combo.setEditText("/dev/ttyUSB0")
        argv = win._esptool_argv(ef.esptool_cmd("read_flash"), "0x0", "1024", "o.bin")
        assert argv[:2] == ["-m", "esptool"]
        assert "--port" in argv and "/dev/ttyUSB0" in argv
        assert "--baud" in argv
        expected = "read-flash" if ef.ESPTOOL_MAJOR >= 5 else "read_flash"
        assert expected in argv

    def test_forced_chip_is_passed_through(self, win):
        win.port_combo.setEditText("/dev/ttyUSB0")
        idx = [i for i, (_, flag) in enumerate(ef.CHIP_CHOICES) if flag == "esp32c3"][0]
        win.chip_override.setCurrentIndex(idx)
        argv = win._esptool_argv("flash-id")
        assert argv[argv.index("--chip") + 1] == "esp32c3"

    def test_missing_port_is_refused(self, win):
        win.port_combo.setEditText("")
        with pytest.raises(RuntimeError):
            win._esptool_argv("flash-id")

    def test_offline_commands_need_no_port(self, win):
        win.port_combo.setEditText("")
        argv = win._esptool_argv("image-info", "app.bin", needs_port=False)
        assert "--port" not in argv and "image-info" in argv

    def test_child_environment_disables_smart_terminal_output(self, win):
        env = ef.EspFlasher._child_environment()
        assert env.value("NO_COLOR") == "1"
        assert not env.contains("TERM")


class TestJobQueue:
    @pytest.fixture
    def queued(self, win, monkeypatch):
        monkeypatch.setattr(win, "_run_next", lambda: None)
        win.port_combo.setEditText("/dev/ttyUSB0")
        return win

    def test_restore_queues_erase_then_write_then_verify(self, queued, monkeypatch,
                                                         tmp_path):
        image = tmp_path / "fw.bin"
        image.write_bytes(b"\x00" * 4096)
        queued.restore_path.setText(str(image))
        queued.erase_chk.setChecked(True)
        queued.verify_chk.setChecked(True)
        monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                            lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
        queued.start_restore()
        assert [j.op for j in queued._queue] == ["erase", "restore", "verify"]

    def test_restore_without_erase_or_verify_is_one_job(self, queued, monkeypatch,
                                                       tmp_path):
        image = tmp_path / "fw.bin"
        image.write_bytes(b"\x00" * 16)
        queued.restore_path.setText(str(image))
        queued.erase_chk.setChecked(False)
        queued.verify_chk.setChecked(False)
        monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                            lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
        queued.start_restore()
        assert [j.op for j in queued._queue] == ["restore"]

    def test_declining_the_confirmation_queues_nothing(self, queued, monkeypatch,
                                                       tmp_path):
        image = tmp_path / "fw.bin"
        image.write_bytes(b"\x00" * 16)
        queued.restore_path.setText(str(image))
        monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                            lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No)
        queued.start_restore()
        assert not queued._queue

    def test_write_partition_erases_only_that_region(self, queued, tmp_path):
        image = tmp_path / "nvs.bin"
        image.write_bytes(b"\x00" * 1024)
        queued.write_partition(
            {"label": "nvs", "offset": 0x9000, "size": 0x6000}, str(image))
        ops = [j.op for j in queued._queue]
        assert ops == ["erase-region", "restore"]
        erase = queued._queue[0]
        assert hex(0x9000) in erase.args and str(0x6000) in erase.args
        assert ef.esptool_cmd("erase_region") == erase.args[0]

    def test_dump_partitions_makes_one_job_each_with_unique_names(self, queued,
                                                                  tmp_path):
        entries = [
            {"label": "nvs", "type": 1, "subtype": 2, "offset": 0x9000, "size": 0x6000},
            {"label": "nvs", "type": 1, "subtype": 2, "offset": 0x20000, "size": 0x6000},
            {"label": "", "type": 0, "subtype": 0, "offset": 0x10000, "size": 0x100000},
        ]
        queued.dump_partitions(entries, str(tmp_path))
        assert len(queued._queue) == 3
        outputs = [os.path.basename(j.args[-1]) for j in queued._queue]
        assert outputs == ["nvs.bin", "nvs_2.bin", "factory.bin"]
        assert len(set(outputs)) == 3

    def test_flash_images_builds_one_write_with_all_pairs(self, queued, tmp_path):
        images = []
        for name, addr in (("boot.bin", 0x1000), ("pt.bin", 0x8000),
                           ("app.bin", 0x10000)):
            path = tmp_path / name
            path.write_bytes(b"\x00" * 256)
            images.append((addr, str(path)))
        queued.flash_images(images, ["--flash-mode", "dio"], 768)
        job = queued._queue[0]
        assert job.args[0] == ef.esptool_cmd("write_flash")
        assert "--flash-mode" in job.args
        assert job.args.count("0x1000") == 1 and "0x10000" in job.args
        assert job.expect_bytes == 768

    def test_merge_needs_no_port_and_drops_erase_all(self, queued, tmp_path):
        image = tmp_path / "app.bin"
        image.write_bytes(b"\x00" * 16)
        queued.merge_images([(0x0, str(image))], str(tmp_path / "out.bin"),
                            "uf2", ["--erase-all", "--flash-size", "4MB"])
        job = queued._queue[0]
        assert job.needs_port is False
        assert "--erase-all" not in job.args
        assert "--format" in job.args and "uf2" in job.args

    def test_auto_size_backup_defers_the_read_until_detect_lands(self, queued,
                                                                 tmp_path):
        queued.backup_path.setText(str(tmp_path / "dump.bin"))
        queued.backup_size_combo.setCurrentIndex(0)  # Auto-detect
        queued.start_backup()
        assert [j.op for j in queued._queue] == ["detect"]
        detect = queued._queue[0]
        assert detect.args == [ef.esptool_cmd("flash_id")]

        queued._queue.clear()
        queued.detected_flash_bytes = 4 * 1024 * 1024
        detect.on_success()
        assert [j.op for j in queued._queue] == ["backup"]
        assert str(4 * 1024 * 1024) in queued._queue[0].args

    def test_backup_rejects_a_missing_output_folder(self, queued, monkeypatch):
        errors = []
        monkeypatch.setattr(queued, "_error", errors.append)
        queued.backup_path.setText("/nonexistent-dir-xyz/dump.bin")
        queued.start_backup()
        assert errors and "folder" in errors[0]
        assert not queued._queue

    def test_failed_job_drops_the_rest_of_the_queue(self, win, fake_process):
        win.current_job = ef.Job("erase", ["erase-flash"])
        win.process = fake_process()
        win._queue.extend([ef.Job("restore", ["write-flash"]),
                           ef.Job("verify", ["verify-flash"])])
        win._on_finished(2, None)
        assert not win._queue
        assert "skipped" in win.log.toPlainText()
        assert not win.cancel_btn.isEnabled()

    def test_successful_job_runs_its_callback(self, win, monkeypatch, fake_process):
        monkeypatch.setattr(win, "_run_next", lambda: None)
        seen = []
        win.current_job = ef.Job("backup", ["read-flash"],
                                 on_success=lambda: seen.append(True))
        win.process = fake_process()
        win._on_finished(0, None)
        assert seen == [True]

    def test_a_throwing_callback_does_not_break_the_queue(self, win, monkeypatch,
                                                          fake_process):
        monkeypatch.setattr(win, "_run_next", lambda: None)

        def boom():
            raise OSError("disk full")

        win.current_job = ef.Job("backup", ["read-flash"], on_success=boom)
        win.process = fake_process()
        win._on_finished(0, None)
        assert "post-processing failed" in win.log.toPlainText()


class TestCancelAndClose:
    def test_cancel_asks_politely_before_killing(self, win, fake_process):
        proc = fake_process()
        win.process = proc
        win._queue.append(ef.Job("verify", ["verify-flash"]))
        win.cancel()
        assert proc.terminated and not proc.killed
        assert not win._queue
        win._force_kill()
        assert proc.killed

    def test_cancelled_job_reports_cancelled_not_failed(self, win, fake_process):
        win.current_job = ef.Job("backup", ["read-flash"])
        win.process = fake_process()
        win.cancel()
        win._on_finished(1, None)
        text = win.log.toPlainText()
        assert "cancelled" in text and "FAILED" not in text

    def test_close_is_refused_while_running(self, win, monkeypatch, fake_process):
        win.process = fake_process()
        win.current_job = ef.Job("restore", ["write-flash"])
        prompts = []

        def fake_question(_parent, _title, text, *a, **k):
            prompts.append(text)
            return QtWidgets.QMessageBox.StandardButton.No

        monkeypatch.setattr(QtWidgets.QMessageBox, "question", fake_question)
        event = QtGui.QCloseEvent()
        win.closeEvent(event)
        assert not event.isAccepted()  # ignore() rejects the close
        assert prompts and "half-programmed" in prompts[0]

    def test_close_without_an_operation_just_saves(self, win, monkeypatch):
        called = []
        monkeypatch.setattr(win, "_save_settings", lambda: called.append(True))
        event = QtGui.QCloseEvent()
        win.closeEvent(event)
        assert called == [True]
        assert event.isAccepted()


class TestBusyGating:
    def test_running_disables_the_tools_menu_too(self, win):
        """The Ctrl+M shortcut used to steal the port mid-flash."""
        win.set_running(True)
        assert all(not act.isEnabled() for act in win.tool_actions)
        assert not win.monitor_btn.isEnabled()
        assert win.cancel_btn.isEnabled()
        win.set_running(False)
        assert all(act.isEnabled() for act in win.tool_actions)

    def test_open_dialogs_are_told_about_busy_state(self, win):
        dialog = ef.PartitionTableDialog(win)
        win._child_dialogs.append(dialog)
        dialog.load_from_bytes(partition_entry("nvs", 1, 2, 0x9000, 0x6000))
        assert dialog.dump_btn.isEnabled()
        win.set_running(True)
        assert not dialog.dump_btn.isEnabled()
        assert not dialog.read_btn.isEnabled()
        win.set_running(False)
        assert dialog.dump_btn.isEnabled()
        dialog.deleteLater()

    def test_busy_reflects_queue_and_process(self, win, fake_process):
        assert not win.busy()
        win._queue.append(ef.Job("verify", []))
        assert win.busy()
        win._queue.clear()
        win.process = fake_process()
        assert win.busy()
        win.process = None


class TestPostProcessBackup:
    def test_manifest_records_the_detected_chip(self, win, tmp_path):
        dump = tmp_path / "dump.bin"
        dump.write_bytes(b"\x01" * 2048)
        win.detected_chip = "ESP32-C3 (revision v0.4)"
        win.detected_mac = "aa:bb:cc:dd:ee:ff"
        win.detected_flash_bytes = 4 * 1024 * 1024
        win.manifest_chk.setChecked(True)
        win.trim_chk.setChecked(False)
        win._post_process_backup(str(dump), 0, full=True)
        meta = ef.read_manifest(str(dump))
        assert meta["chip_family"] == "ESP32-C3"
        assert meta["mac"] == "aa:bb:cc:dd:ee:ff"
        assert str(dump) in win._recent_backups

    def test_trim_shrinks_the_dump_and_records_the_original_size(self, win, tmp_path):
        dump = tmp_path / "dump.bin"
        dump.write_bytes(b"\x01" * 4096 + b"\xff" * (1 << 20))
        win.trim_chk.setChecked(True)
        win.manifest_chk.setChecked(True)
        win._post_process_backup(str(dump), 0, full=True)
        meta = ef.read_manifest(str(dump))
        assert dump.stat().st_size == 4096
        assert meta["size"] == 4096
        assert meta["original_size"] == 4096 + (1 << 20)
        assert "trimmed" in win.log.toPlainText()

    def test_partition_dumps_are_never_trimmed(self, win, tmp_path):
        dump = tmp_path / "nvs.bin"
        dump.write_bytes(b"\x01" * 16 + b"\xff" * (1 << 20))
        win.trim_chk.setChecked(True)
        win._post_process_backup(str(dump), 0x9000, full=False)
        assert dump.stat().st_size == 16 + (1 << 20)

    def test_manifest_can_be_switched_off(self, win, tmp_path):
        dump = tmp_path / "dump.bin"
        dump.write_bytes(b"\x01" * 16)
        win.manifest_chk.setChecked(False)
        win._post_process_backup(str(dump), 0, full=True)
        assert ef.read_manifest(str(dump)) is None


class TestRestoreDescription:
    def test_manifest_details_are_surfaced(self, win, tmp_path):
        image = tmp_path / "fw.bin"
        image.write_bytes(b"\x00" * 1024)
        ef.write_manifest(str(image), ef.build_manifest(
            str(image), chip="ESP32-C3 (revision v0.4)", mac="a1:b2:c3:d4:e5:f6"))
        win.restore_path.setText(str(image))
        text = win.restore_info.text()
        assert "ESP32-C3" in text and "a1:b2:c3:d4:e5:f6" in text

    def test_plain_file_shows_only_its_size(self, win, tmp_path):
        image = tmp_path / "fw.bin"
        image.write_bytes(b"\x00" * 2048)
        win.restore_path.setText(str(image))
        assert "2.00 KB" in win.restore_info.text()

    def test_mismatched_manifest_defaults_the_dialog_to_no(self, win, monkeypatch,
                                                           tmp_path):
        image = tmp_path / "fw.bin"
        image.write_bytes(b"\x00" * 64)
        ef.write_manifest(str(image), ef.build_manifest(str(image), chip="ESP8266EX"))
        win.restore_path.setText(str(image))
        win.detected_chip = "ESP32-C3 (revision v0.4)"
        win.port_combo.setEditText("/dev/ttyUSB0")
        captured = {}

        def fake_question(_parent, _title, text, _buttons, default):
            captured["text"] = text
            captured["default"] = default
            return QtWidgets.QMessageBox.StandardButton.No

        monkeypatch.setattr(QtWidgets.QMessageBox, "question", fake_question)
        win.start_restore()
        assert "ESP8266" in captured["text"]
        assert captured["default"] == QtWidgets.QMessageBox.StandardButton.No


class TestProfiles:
    def test_round_trip_through_the_widgets(self, win):
        win.port_combo.setEditText("/dev/ttyACM0")
        win.baud_combo.setCurrentText("921600")
        win.restore_addr.setText("0x10000")
        win.erase_chk.setChecked(False)
        saved = win.current_profile_dict()

        win.port_combo.setEditText("/dev/ttyUSB9")
        win.baud_combo.setCurrentText("115200")
        win.restore_addr.setText("0x0")
        win.erase_chk.setChecked(True)

        win.apply_profile_dict(saved)
        assert win.selected_port() == "/dev/ttyACM0"
        assert win.baud_combo.currentText() == "921600"
        assert win.restore_addr.text() == "0x10000"
        assert win.erase_chk.isChecked() is False

    def test_profiles_survive_a_settings_round_trip(self, win):
        win._profiles = {"nodemcu": win.current_profile_dict()}
        win._save_settings()
        reopened = ef.EspFlasher()
        try:
            assert "nodemcu" in reopened._profiles
            assert reopened.profile_combo.findText("nodemcu") > 0
        finally:
            reopened._port_timer.stop()
            reopened.deleteLater()

    def test_out_of_range_indices_are_clamped(self, win):
        win.apply_profile_dict({"chip_idx": 9999, "backup_size_idx": 9999})
        assert win.chip_override.currentIndex() == win.chip_override.count() - 1
        assert win.backup_size_combo.currentIndex() == \
            win.backup_size_combo.count() - 1


class TestRecentFiles:
    def test_recent_entries_are_deduped_and_capped(self, win):
        for i in range(12):
            win._remember_recent("backup", f"/tmp/dump{i}.bin")
        win._remember_recent("backup", "/tmp/dump0.bin")
        assert len(win._recent_backups) == 8
        assert win._recent_backups[0] == "/tmp/dump0.bin"
        assert len(set(win._recent_backups)) == 8

    def test_menu_action_fills_the_path_field(self, win):
        win._remember_recent("firmware", "/tmp/fw.bin")
        actions = win.recent_fw_menu.actions()
        assert [a.text() for a in actions] == ["/tmp/fw.bin"]
        actions[0].trigger()
        assert win.restore_path.text() == "/tmp/fw.bin"


class TestHotplug:
    def test_new_port_is_logged_and_selected(self, win, monkeypatch):
        win._known_ports = []
        win.port_combo.setEditText("")
        monkeypatch.setattr(ef, "list_serial_ports",
                            lambda: [("/dev/ttyUSB7", "CP2102")])
        win._poll_ports()
        assert "/dev/ttyUSB7 appeared" in win.log.toPlainText()
        assert win.selected_port() == "/dev/ttyUSB7"

    def test_removal_is_logged(self, win, monkeypatch):
        win._known_ports = [("/dev/ttyUSB7", "CP2102")]
        monkeypatch.setattr(ef, "list_serial_ports", lambda: [])
        win._poll_ports()
        assert "disappeared" in win.log.toPlainText()

    def test_polling_is_skipped_while_busy(self, win, monkeypatch, fake_process):
        win.process = fake_process()
        monkeypatch.setattr(ef, "list_serial_ports",
                            lambda: pytest.fail("must not enumerate while busy"))
        win._poll_ports()
        win.process = None

    def test_autodetect_on_plug_in_can_be_enabled(self, win, monkeypatch):
        win._known_ports = []
        win.autodetect_chk.setChecked(True)
        started = []
        monkeypatch.setattr(win, "start_detect", lambda: started.append(True))
        monkeypatch.setattr(ef, "list_serial_ports",
                            lambda: [("/dev/ttyUSB7", "CP2102")])
        win._poll_ports()
        assert started == [True]


class TestHexViewer:
    @pytest.fixture
    def image(self, tmp_path):
        path = tmp_path / "flash.bin"
        body = bytearray(b"\x00" * 0x8000)
        body += partition_entry("nvs", 1, 2, 0x9000, 0x6000)
        body += partition_entry("factory", 0, 0, 0x10000, 0x100000)
        body += b"\xff" * (0xC00 - 64)
        body += b"MAGIC-NEEDLE"
        body += b"\x00" * (0x20000 - len(body))
        path.write_bytes(bytes(body))
        return path

    def test_opens_and_reports_size(self, qapp, image):
        viewer = ef.HexViewer(path=str(image))
        try:
            assert viewer.model.size() == image.stat().st_size
            assert viewer.model.rowCount() == (viewer.model.size() + 15) // 16
            assert "bytes" in viewer.size_lbl.text()
        finally:
            viewer.close()

    def test_partition_combo_is_populated_from_the_image(self, qapp, image):
        viewer = ef.HexViewer(path=str(image))
        try:
            labels = [viewer.part_combo.itemText(i)
                      for i in range(viewer.part_combo.count())]
            assert labels[0] == "Partitions (2)"
            assert any("nvs" in text for text in labels)
            viewer.part_combo.setCurrentIndex(1)
            viewer._jump_partition(1)
            assert viewer.table.currentIndex().row() == 0x9000 // 16
        finally:
            viewer.close()

    def test_text_search_finds_and_highlights(self, qapp, image):
        viewer = ef.HexViewer(path=str(image))
        try:
            viewer.find_edit.setText("MAGIC-NEEDLE")
            viewer.find_mode.setCurrentText("Text")
            viewer.find_next()
            assert "hit at" in viewer.inspect_lbl.text()
            expected = bytes(image.read_bytes()).find(b"MAGIC-NEEDLE")
            assert viewer._last_hit == expected
            assert viewer.model._highlight == (expected, 12)
        finally:
            viewer.close()

    def test_hex_search(self, qapp, image):
        viewer = ef.HexViewer(path=str(image))
        try:
            viewer.find_edit.setText("aa 50")
            viewer.find_mode.setCurrentText("Hex")
            viewer.find_next()
            assert viewer._last_hit == 0x8000
        finally:
            viewer.close()

    def test_missing_needle_reports_cleanly(self, qapp, image):
        viewer = ef.HexViewer(path=str(image))
        try:
            viewer.find_edit.setText("ZZZ-NOT-PRESENT")
            viewer.find_mode.setCurrentText("Text")
            viewer.find_next()
            assert "not found" in viewer.inspect_lbl.text()
        finally:
            viewer.close()

    def test_diff_mode_finds_the_first_changed_byte(self, qapp, tmp_path):
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"\x00" * 4096)
        changed = bytearray(b"\x00" * 4096)
        changed[2000] = 0x42
        b.write_bytes(bytes(changed))
        viewer = ef.HexViewer(path=str(a))
        try:
            viewer.model.open_other(str(b))
            assert viewer.model.has_other()
            assert viewer.model.find_next_diff(0) == 2000
            assert viewer.model.row_differs(2000 // 16)
            assert not viewer.model.row_differs(0)
            assert viewer.model.find_next_diff(2001) == -1
            assert viewer.model.find_next_diff(4095, backwards=True) == 2000
        finally:
            viewer.close()

    def test_diff_handles_different_lengths(self, qapp, tmp_path):
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"\x00" * 4096)
        b.write_bytes(b"\x00" * 2048)
        viewer = ef.HexViewer(path=str(a))
        try:
            viewer.model.open_other(str(b))
            assert viewer.model.find_next_diff(0) == -1  # tail vs zero-fill
        finally:
            viewer.close()

    def test_diff_across_the_chunk_boundary(self, qapp, tmp_path):
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        size = (1 << 16) * 3
        a.write_bytes(b"\x00" * size)
        changed = bytearray(b"\x00" * size)
        changed[(1 << 16) * 2 + 5] = 0x99
        b.write_bytes(bytes(changed))
        viewer = ef.HexViewer(path=str(a))
        try:
            viewer.model.open_other(str(b))
            assert viewer.model.find_next_diff(0) == (1 << 16) * 2 + 5
        finally:
            viewer.close()

    def test_stop_comparing_clears_the_second_image(self, qapp, tmp_path):
        a = tmp_path / "a.bin"
        a.write_bytes(b"\x00" * 64)
        viewer = ef.HexViewer(path=str(a))
        try:
            viewer.model.open_other(str(a))
            viewer._pick_compare()  # toggles off when a compare is active
            assert not viewer.model.has_other()
        finally:
            viewer.close()

    def test_inspector_decodes_the_selected_row(self, qapp, tmp_path):
        path = tmp_path / "x.bin"
        path.write_bytes(bytes([0x78, 0x56, 0x34, 0x12] + [0] * 60))
        viewer = ef.HexViewer(path=str(path))
        try:
            viewer.table.selectRow(0)
            text = viewer.inspect_lbl.text()
            assert "u8 120" in text
            assert "u32le 0x12345678" in text
        finally:
            viewer.close()

    def test_row_rendering(self, qapp, tmp_path):
        path = tmp_path / "x.bin"
        path.write_bytes(b"AB" + bytes([0x00, 0xFF]))
        viewer = ef.HexViewer(path=str(path))
        try:
            model = viewer.model
            assert model.data(model.index(0, 0)) == "00000000"
            assert model.data(model.index(0, 1)).startswith("41 42 00 FF")
            assert model.data(model.index(0, 2)) == "AB.."
        finally:
            viewer.close()

    def test_empty_file_is_survivable(self, qapp, tmp_path):
        path = tmp_path / "empty.bin"
        path.write_bytes(b"")
        viewer = ef.HexViewer(path=str(path))
        try:
            assert viewer.model.rowCount() == 0
            viewer.find_edit.setText("anything")
            viewer.find_next()
        finally:
            viewer.close()


class TestSerialMonitor:
    @pytest.fixture
    def monitor(self, qapp):
        mon = ef.SerialMonitor()
        yield mon
        mon.close()

    def test_lines_are_buffered_and_rendered(self, monitor):
        monitor._on_bytes(b"I (300) app: hello\nI (301) app: world\n")
        assert [text for text, _ts in monitor._lines] == \
            ["I (300) app: hello", "I (301) app: world"]
        assert "hello" in monitor.display.toPlainText()

    def test_partial_line_waits_for_the_idle_flush(self, monitor):
        monitor._on_bytes(b"esp32> ")
        assert monitor.display.toPlainText() == ""
        monitor._flush_partial()
        assert monitor.display.toPlainText() == "esp32> "

    def test_a_completed_line_is_not_rendered_twice(self, monitor):
        monitor._on_bytes(b"parti")
        monitor._flush_partial()
        monitor._on_bytes(b"al line\n")
        assert monitor.display.toPlainText() == "partial line\n"

    def test_filter_hides_non_matching_lines(self, monitor):
        monitor._on_bytes(b"I (1) wifi: up\nI (2) mqtt: connected\n")
        monitor.filter_edit.setText("wifi")
        text = monitor.display.toPlainText()
        assert "wifi: up" in text and "mqtt" not in text
        monitor.filter_edit.setText("")
        assert "mqtt" in monitor.display.toPlainText()

    def test_invalid_filter_regex_is_ignored(self, monitor):
        monitor._on_bytes(b"line one\n")
        monitor.filter_edit.setText("[unclosed")
        assert "line one" in monitor.display.toPlainText()

    def test_ansi_colour_is_converted_not_printed(self, monitor):
        monitor.ansi_chk.setChecked(True)
        monitor._on_bytes(b"\x1b[0;31mE (5) boom\x1b[0m\n")
        text = monitor.display.toPlainText()
        assert text.strip() == "E (5) boom"
        assert "\x1b" not in text

    def test_ansi_can_be_disabled(self, monitor):
        monitor.ansi_chk.setChecked(False)
        monitor._on_bytes(b"\x1b[0;31mred\x1b[0m\n")
        assert monitor.display.toPlainText().strip() == "red"

    def test_timestamps_prefix_each_line(self, monitor):
        monitor.timestamps_chk.setChecked(True)
        monitor._on_bytes(b"tick\n")
        assert monitor.display.toPlainText().startswith("[")
        assert "tick" in monitor.display.toPlainText()

    def test_hex_mode_shows_raw_bytes(self, monitor):
        monitor.hex_chk.setChecked(True)
        monitor._on_bytes(b"\x00\xff")
        assert "00 FF" in monitor.display.toPlainText()

    def test_scrollback_is_bounded(self, monitor):
        for i in range(ef.SerialMonitor.SCROLLBACK + 500):
            monitor._commit_line(f"line {i}")
        assert len(monitor._lines) == ef.SerialMonitor.SCROLLBACK

    def test_backtrace_without_an_elf_says_so(self, monitor):
        monitor.decode_chk.setChecked(True)
        monitor._on_bytes(b"Backtrace: 0x400d1234:0x3ffb0000\n")
        assert "pick an ELF" in monitor.display.toPlainText()

    def test_backtrace_decode_uses_addr2line(self, monitor, monkeypatch, tmp_path):
        elf = tmp_path / "app.elf"
        elf.write_bytes(b"\x7fELF")
        monitor._elf_path = str(elf)
        monitor.decode_chk.setChecked(True)
        monkeypatch.setattr(ef, "decode_addresses",
                            lambda *a, **k: ["app_main at main.c:42"])
        monitor._on_bytes(b"Backtrace: 0x400d1234:0x3ffb0000\n")
        assert "app_main at main.c:42" in monitor.display.toPlainText()

    def test_ordinary_lines_never_invoke_the_decoder(self, monitor, monkeypatch):
        monitor.decode_chk.setChecked(True)
        monitor._elf_path = "/nope.elf"
        monkeypatch.setattr(ef, "decode_addresses",
                            lambda *a, **k: pytest.fail("should not decode"))
        monitor._on_bytes(b"I (300) app: nothing to see\n")

    def test_save_log_writes_the_whole_scrollback(self, monitor, tmp_path,
                                                  monkeypatch):
        monitor._on_bytes(b"\x1b[31mline one\x1b[0m\nline two\n")
        monitor.filter_edit.setText("two")
        out = tmp_path / "monitor.log"
        monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                            lambda *a, **k: (str(out), ""))
        monitor._save_log()
        content = out.read_text()
        assert "line one" in content and "line two" in content
        assert "\x1b" not in content

    def test_clear_resets_everything(self, monitor):
        monitor._on_bytes(b"noise\n")
        monitor._clear()
        assert not monitor._lines
        assert monitor.display.toPlainText() == ""

    def test_reader_error_schedules_a_reconnect(self, monitor):
        monitor.reconnect_chk.setChecked(True)
        monitor._on_reader_error("device disconnected")
        assert monitor._reconnect_timer.isActive()
        assert "retrying" in monitor.status.text()
        monitor._reconnect_timer.stop()

    def test_reader_error_without_reconnect_stays_down(self, monitor):
        monitor.reconnect_chk.setChecked(False)
        monitor._on_reader_error("device disconnected")
        assert not monitor._reconnect_timer.isActive()

    def test_suspend_is_a_noop_when_closed(self, monitor):
        assert monitor.suspend_for_flash() is False
        monitor.resume_after_flash()  # must not raise

    def test_controls_are_disabled_while_disconnected(self, monitor):
        assert not monitor.send_btn.isEnabled()
        assert not monitor.boot_btn.isEnabled()
        assert monitor.port_combo.isEnabled()


class TestMultiFlashDialog:
    def test_load_flash_args_populates_rows_and_options(self, win, tmp_path,
                                                        monkeypatch):
        build = tmp_path / "build"
        build.mkdir()
        for name in ("bootloader.bin", "partition-table.bin", "app.bin"):
            (build / name).write_bytes(b"\x00" * 512)
        args_file = build / "flash_args"
        args_file.write_text(
            "--flash_mode dio --flash_freq 40m --flash_size 2MB\n"
            "0x1000 bootloader.bin\n0x8000 partition-table.bin\n0x10000 app.bin\n")
        dialog = ef.MultiFlashDialog(win)
        try:
            monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                                lambda *a, **k: (str(args_file), ""))
            dialog._load_flash_args()
            assert dialog.table.rowCount() == 3
            assert dialog.mode_combo.currentText() == "dio"
            assert dialog.freq_combo.currentText() == "40m"
            assert dialog.size_combo.currentText() == "2MB"
            images = dialog.images()
            assert [a for a, _ in images] == [0x1000, 0x8000, 0x10000]
        finally:
            dialog.close()

    def test_missing_file_is_reported(self, win, tmp_path, monkeypatch):
        dialog = ef.MultiFlashDialog(win)
        try:
            dialog._append_row(0x0, str(tmp_path / "gone.bin"))
            monkeypatch.setattr(QtWidgets.QMessageBox, "critical",
                                lambda *a, **k: None)
            assert dialog.images() is None
        finally:
            dialog.close()

    def test_bad_address_is_reported(self, win, tmp_path, monkeypatch):
        image = tmp_path / "a.bin"
        image.write_bytes(b"\x00")
        dialog = ef.MultiFlashDialog(win)
        try:
            dialog._append_row(0x0, str(image))
            dialog.table.item(0, 0).setText("not-hex")
            monkeypatch.setattr(QtWidgets.QMessageBox, "critical",
                                lambda *a, **k: None)
            assert dialog.images() is None
        finally:
            dialog.close()

    def test_overlap_prompts_before_flashing(self, win, tmp_path, monkeypatch):
        a = tmp_path / "a.bin"
        a.write_bytes(b"\x00" * 0x2000)
        b = tmp_path / "b.bin"
        b.write_bytes(b"\x00" * 0x1000)
        dialog = ef.MultiFlashDialog(win)
        try:
            dialog._append_row(0x0, str(a))
            dialog._append_row(0x1000, str(b))
            prompts = []

            def fake_warning(_parent, _title, text, *rest):
                prompts.append(text)
                return QtWidgets.QMessageBox.StandardButton.No

            monkeypatch.setattr(QtWidgets.QMessageBox, "warning", fake_warning)
            assert dialog.images() is None
            assert prompts and "overlap" in prompts[0]
        finally:
            dialog.close()

    def test_flash_options_reflect_the_combos(self, win):
        dialog = ef.MultiFlashDialog(win)
        try:
            dialog.mode_combo.setCurrentText("qio")
            dialog.erase_all_chk.setChecked(True)
            opts = dialog.flash_options()
            # esptool 4 spells this --flash_mode, esptool 5 --flash-mode
            assert ef.esptool_flag("--flash-mode") in opts and "qio" in opts
            assert "--erase-all" in opts  # already hyphenated in both
            assert all(o.startswith("--") or not o.startswith("-") for o in opts)
        finally:
            dialog.close()


class TestPartitionDialog:
    def test_load_and_export_csv(self, win, tmp_path, monkeypatch):
        dialog = ef.PartitionTableDialog(win)
        try:
            dialog.load_from_bytes(
                partition_entry("nvs", 1, 2, 0x9000, 0x6000)
                + partition_entry("factory", 0, 0, 0x10000, 0x100000))
            assert dialog.table.rowCount() == 2
            assert "2 partitions" in dialog.status.text()
            out = tmp_path / "p.csv"
            monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                                lambda *a, **k: (str(out), ""))
            dialog._export_csv()
            lines = out.read_text().splitlines()
            assert lines[0] == "name,type,subtype,offset,size,flags"
            assert lines[1].startswith("nvs,data,nvs,0x9000")
        finally:
            dialog.close()

    def test_oversized_file_is_refused(self, win, tmp_path, monkeypatch):
        dialog = ef.PartitionTableDialog(win)
        try:
            dialog.load_from_bytes(partition_entry("nvs", 1, 2, 0x9000, 0x1000))
            dialog.table.selectRow(0)
            big = tmp_path / "big.bin"
            big.write_bytes(b"\x00" * 0x2000)
            monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                                lambda *a, **k: (str(big), ""))
            errors = []
            monkeypatch.setattr(QtWidgets.QMessageBox, "critical",
                                lambda _p, _t, text: errors.append(text))
            dialog._write_selected()
            assert errors and "only holds" in errors[0]
            assert not win._queue
        finally:
            dialog.close()

    def test_actions_stay_disabled_without_a_table(self, win):
        dialog = ef.PartitionTableDialog(win)
        try:
            assert not dialog.dump_btn.isEnabled()
            assert not dialog.erase_btn.isEnabled()
        finally:
            dialog.close()


EFUSE_SUMMARY_SAMPLE = """\
=== eFuse summary ===
SPI_BOOT_CRYPT_CNT (BLOCK0)   Enables encryption and decryption = 1 R/W (0b001)
SECURE_BOOT_EN (BLOCK0)       Set this bit to enable secure boot = False R/W (0b0)
MAC (BLOCK1)                  Factory MAC address               = 60:55:f9:f7:2c:a2
"""


class TestEfuseDialog:
    def test_read_goes_through_the_job_queue(self, win, monkeypatch):
        """A silent chip costs esptool several connect attempts; the UI must
        stay responsive and cancellable through that."""
        monkeypatch.setattr(win, "_run_next", lambda: None)
        win.port_combo.setEditText("/dev/ttyUSB0")
        dialog = ef.EfuseDialog(win)
        try:
            dialog.reload()
            assert [j.op for j in win._queue] == ["efuse"]
            job = win._queue[0]
            assert job.module == "espefuse" and job.capture is True
            argv = win._job_argv(job)
            assert argv[:2] == ["-m", "espefuse"]
            assert "--do-not-confirm" in argv and "summary" in argv

            win._job_output = EFUSE_SUMMARY_SAMPLE.splitlines()
            job.on_success()
            assert "FLASH ENCRYPTION IS ENABLED" in dialog.banner.text()
            assert "SPI_BOOT_CRYPT_CNT" in dialog.output.toPlainText()
        finally:
            dialog.close()

    def test_captured_output_reaches_the_dialog_and_the_log(self, win,
                                                            fake_process):
        win.current_job = ef.Job("efuse", ["summary"], module="espefuse",
                                 capture=True)
        win._job_output = []
        win.process = fake_process(b"SPI_BOOT_CRYPT_CNT (BLOCK0) x = 1 R/W (0b001)\n")
        win._on_stdout()
        assert win._job_output and "SPI_BOOT_CRYPT_CNT" in win._job_output[0]
        assert "SPI_BOOT_CRYPT_CNT" in win.log.toPlainText()

    def test_missing_espefuse_is_reported_without_queueing(self, win, monkeypatch):
        monkeypatch.setattr(ef, "espefuse", None)
        dialog = ef.EfuseDialog(win)
        try:
            dialog.reload()
            assert not win._queue
            assert "could not be imported" in dialog.output.toPlainText()
        finally:
            dialog.close()

    def test_encrypted_chip_raises_a_banner(self, win):
        dialog = ef.EfuseDialog(win)
        try:
            dialog.show_security({"flash_encryption": True, "secure_boot": False,
                                  "fields": {"FLASH_CRYPT_CNT": "1"}})
            assert not dialog.banner.isHidden()
            assert "FLASH ENCRYPTION IS ENABLED" in dialog.banner.text()
            assert win.security_info["flash_encryption"] is True
        finally:
            dialog.close()

    def test_plain_chip_reports_all_clear(self, win):
        dialog = ef.EfuseDialog(win)
        try:
            dialog.show_security({"flash_encryption": False, "secure_boot": False,
                                  "fields": {"FLASH_CRYPT_CNT": "0"}})
            assert "restore normally" in dialog.banner.text()
        finally:
            dialog.close()

    def test_no_data_means_no_banner(self, win):
        dialog = ef.EfuseDialog(win)
        try:
            dialog.show_security({"flash_encryption": None, "secure_boot": None,
                                  "fields": {}})
            assert dialog.banner.isHidden()
        finally:
            dialog.close()

    def test_encrypted_chip_warns_on_the_restore_dialog(self, win, monkeypatch,
                                                        tmp_path):
        image = tmp_path / "fw.bin"
        image.write_bytes(b"\x00" * 64)
        win.restore_path.setText(str(image))
        win.port_combo.setEditText("/dev/ttyUSB0")
        win.security_info = {"flash_encryption": True}
        captured = {}

        def fake_question(_parent, _title, text, *rest):
            captured["text"] = text
            return QtWidgets.QMessageBox.StandardButton.No

        monkeypatch.setattr(QtWidgets.QMessageBox, "question", fake_question)
        win.start_restore()
        assert "Flash encryption is enabled" in captured["text"]


class TestImageInfoDialog:
    def test_summary_is_extracted_from_the_output(self, win, monkeypatch, tmp_path):
        image = tmp_path / "app.bin"
        image.write_bytes(b"\x00" * 64)
        monkeypatch.setattr(win, "run_offline", lambda *a, **k: (
            "Image size: 176160 bytes\nProject name: hello_world\n"
            "App version: 1.0.0\nESP-IDF: v5.2\n"))
        dialog = ef.ImageInfoDialog(win, str(image))
        try:
            assert "hello_world" in dialog.summary.text()
            assert "v5.2" in dialog.summary.text()
        finally:
            dialog.close()

    def test_raw_dump_gets_an_explanatory_summary(self, win, monkeypatch, tmp_path):
        image = tmp_path / "dump.bin"
        image.write_bytes(b"\xff" * 64)
        monkeypatch.setattr(win, "run_offline",
                            lambda *a, **k: "Image parsing failed")
        dialog = ef.ImageInfoDialog(win, str(image))
        try:
            assert "raw flash dump" in dialog.summary.text()
        finally:
            dialog.close()


def test_gui_starts_and_reports_the_esptool_version(win):
    assert ef.APP_NAME in win.windowTitle()
    assert "esptool" in win.statusBar().currentMessage()
    assert win.chip_lbl.text() == "Chip: —"


class TestMonitorTimestampStability:
    """Filtering must not restamp old lines with the current time."""

    def test_timestamp_survives_a_filter_change(self, qapp):
        mon = ef.SerialMonitor()
        try:
            mon.timestamps_chk.setChecked(True)
            mon._on_bytes(b"first\n")
            stamp = mon._lines[0][1]
            mon._on_bytes(b"second\n")
            mon.filter_edit.setText("first")
            rendered = mon.display.toPlainText()
            assert f"[{stamp}] first" in rendered
            assert "second" not in rendered
        finally:
            mon.close()

    def test_enabling_timestamps_uses_real_arrival_times(self, qapp):
        mon = ef.SerialMonitor()
        try:
            mon._on_bytes(b"early\n")
            stamp = mon._lines[0][1]
            mon.timestamps_chk.setChecked(True)  # triggers a re-render
            assert f"[{stamp}] early" in mon.display.toPlainText()
        finally:
            mon.close()


def test_failed_queue_start_hands_the_port_back(win, monkeypatch):
    """No port means the job never runs — monitors must not stay suspended."""
    resumed = []
    monkeypatch.setattr(win, "_resume_monitors", lambda: resumed.append(True))
    monkeypatch.setattr(win, "_error", lambda _msg: None)
    win.port_combo.setEditText("")
    win.enqueue([ef.Job("verify", ["verify-flash"])])
    assert resumed == [True]
    assert not win._queue


class TestProcessLifecycle:
    def test_stale_kill_timer_spares_a_later_process(self, win, fake_process):
        """Cancel arms a 3 s kill; by then a new operation may have started."""
        first = fake_process()
        win.process = first
        win.cancel()
        second = fake_process()
        win.process = second          # user starts something else meanwhile
        win._force_kill(first)        # the timer fires for the OLD process
        assert first.killed
        assert not second.killed

    def test_force_kill_without_a_target_is_harmless(self, win):
        win.process = None
        win._force_kill()

    def test_failed_start_unwinds_instead_of_hanging(self, win, fake_process):
        win.process = fake_process()
        win.current_job = ef.Job("detect", ["flash-id"])
        win._queue.append(ef.Job("backup", ["read-flash"]))
        win.set_running(True)
        win._on_error(QtCore.QProcess.ProcessError.FailedToStart)
        assert win.process is None
        assert not win._queue
        assert not win.busy()
        assert win.detect_btn.isEnabled()   # controls came back

    def test_other_process_errors_do_not_unwind(self, win, fake_process):
        """A read error mid-run still ends via finished(); don't double-unwind."""
        win.process = fake_process()
        win.current_job = ef.Job("backup", ["read-flash"])
        win._on_error(QtCore.QProcess.ProcessError.ReadError)
        assert win.process is not None
        assert "process error" in win.log.toPlainText()


def test_finished_process_is_scheduled_for_deletion(win, fake_process):
    """One QProcess per job; they must not pile up over a long session."""
    proc = fake_process()
    win.process = proc
    win.current_job = ef.Job("detect", ["flash-id"])
    win._on_finished(0, None)
    assert proc.deleted
